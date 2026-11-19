"""Production safeguards: new incident types, partial API failures, retries, health checks,
bounded memory, secret hiding, input validation, deadlines and the closed internal API."""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import activity
import agent
import app as app_module
import main
import teams
import tools
import watcher as watcher_module
from conftest import NOW, SAMPLE_RCA, container, fake_llm, make_pod
from test_watcher import INC, INC2, make_watcher, title
from tools import find_incidents


# --- New incident types ---

def failed_create(name="kubesupport-769bb95cc4", kind="ReplicaSet", ns="kubesupport", minutes_ago=2,
                  reason="FailedCreate"):
    return NS(reason=reason, message='pods "x" is forbidden: violates PodSecurity "restricted:latest"',
              involved_object=NS(kind=kind, name=name, namespace=ns),
              last_timestamp=NOW - timedelta(minutes=minutes_ago), event_time=None, metadata=NS(creation_timestamp=None))


def test_pods_that_cannot_be_created_are_an_incident():
    incidents, _ = find_incidents({}, [], {}, NOW, events=[failed_create()])
    (inc,) = incidents.values()
    assert inc["key"] == "kubesupport/Deployment/kubesupport/CannotCreatePods"
    assert inc["pod"] is None and "violates PodSecurity" in inc["detail"]


def test_old_or_unrelated_events_are_ignored():
    events = [failed_create(minutes_ago=60), failed_create(kind="Pod", name="p"), failed_create(reason="Other")]
    assert find_incidents({}, [], {}, NOW, events=events)[0] == {}


def job(name, ns="batch", cron=None, failed=False, created_ago=600):
    owners = [NS(kind="CronJob", name=cron)] if cron else []
    conditions = [NS(type="Failed", status="True", reason="BackoffLimitExceeded",
                     message="Job has reached the specified backoff limit")] if failed else []
    return NS(metadata=NS(name=name, namespace=ns, owner_references=owners,
                          creation_timestamp=NOW - timedelta(seconds=created_ago)),
              status=NS(conditions=conditions))


def test_failed_job_is_an_incident():
    (inc,) = find_incidents({}, [], {}, NOW, jobs=[job("migrate", failed=True)])[0].values()
    assert inc["key"] == "batch/Job/migrate/JobFailed" and inc["job"] == "migrate"
    assert inc["detail"].startswith("BackoffLimitExceeded")


def test_cronjob_failure_ends_when_a_later_run_succeeds():
    failed_then_ok = [job("report-1", cron="report", failed=True, created_ago=7200), job("report-2", cron="report")]
    assert find_incidents({}, [], {}, NOW, jobs=failed_then_ok)[0] == {}
    ok_then_failed = [job("report-1", cron="report", created_ago=7200), job("report-2", cron="report", failed=True)]
    (inc,) = find_incidents({}, [], {}, NOW, jobs=ok_then_failed)[0].values()
    assert inc["workload"] == "CronJob/report" and inc["job"] == "report-2"


def evicted_pod(minutes_ago):
    p = make_pod("web-5d8f7-x", phase="Failed", containers=[container()])
    p.status.reason, p.status.message = "Evicted", "The node was low on resource: memory."
    p.status.conditions = [NS(type="DisruptionTarget", status="True",
                              last_transition_time=NOW - timedelta(minutes=minutes_ago))]
    p.status.start_time = NOW - timedelta(hours=5)
    return p


def test_recent_eviction_is_an_incident_old_one_is_not():
    (inc,) = find_incidents({"default": [evicted_pod(3)]}, [], {}, NOW)[0].values()
    assert inc["key"] == "default/Deployment/web/Evicted" and "low on resource" in inc["detail"]
    assert find_incidents({"default": [evicted_pod(90)]}, [], {}, NOW)[0] == {}


def test_detect_reports_what_it_could_not_read(monkeypatch):
    def broken(*a, **kw):
        raise TimeoutError("API timeout")
    monkeypatch.setattr(tools, "ALLOWED", {"apps"})
    monkeypatch.setattr(tools, "core", NS(list_namespaced_pod=broken, list_node=broken, list_namespaced_event=broken,
                                          list_event_for_all_namespaces=broken))
    monkeypatch.setattr(tools, "batch", NS(list_namespaced_job=lambda ns, **kw: NS(items=[]),
                                           list_job_for_all_namespaces=broken))
    _, _, unchecked = tools.detect_incidents({})
    assert unchecked == {"pods:apps", "nodes", "events"}


# --- Watcher: partial failures, retries, health, AI backlog ---

def test_unreadable_namespace_does_not_resolve_its_incidents():
    w, state, sent, _, clock = make_watcher()
    w._detect = lambda prev: (dict(state["found"]), {}, state.get("unchecked", set()))
    w.tick()
    clock.t, state["found"] = 60, {INC["key"]: INC}
    w.tick()
    clock.t, state["found"], state["unchecked"] = 60 * 30, {}, {"pods:default"}
    w.tick()  # 29 minutes later, but the namespace couldn't be read: still open
    assert INC["key"] in w.open and not any("Resolved" in title(c) for c in sent)
    state["unchecked"] = set()
    clock.t += 60 * 11
    w.tick()
    w.tick()
    assert INC["key"] not in w.open


def test_cards_teams_refused_are_retried_on_the_next_scan():
    w, state, sent, _, clock = make_watcher()
    attempts = []
    w._notify = lambda card: attempts.append(card) or [{"ok": len(attempts) > 2}]
    w.tick()                      # startup card: refused, kept
    assert len(w.outbox) == 1
    clock.t = 60
    w.tick()                      # retried: refused again
    clock.t = 120
    w.tick()                      # retried: accepted
    assert len(w.outbox) == 0 and len(attempts) == 3


def test_health_reports_a_stuck_scan():
    w, _, _, _, clock = make_watcher()
    assert w.healthy() == (True, "watcher not started")
    w._thread = NS(is_alive=lambda: True)
    w.run_started = 0
    clock.t = 100
    assert w.healthy()[0]
    clock.t = 400  # nothing finished for 5 intervals / 300 s
    ok, why = w.healthy()
    assert not ok and "no completed scan" in why


def test_ai_backlog_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    w, state, _, rca_calls, clock = make_watcher()
    w.tick()
    w.ai_backlog = watcher_module.MAX_AI_BACKLOG  # the worker is still busy with earlier incidents
    clock.t, state["found"] = 60, {INC["key"]: INC, INC2["key"]: INC2}
    w.tick()
    assert [use_llm for _, use_llm in rca_calls] == [False, False]


# --- Teams: bounded memory and a busy reply ---

def test_sessions_are_bounded(monkeypatch):
    monkeypatch.setattr(teams, "MAX_SESSIONS", 3)
    teams._sessions.clear()
    for i in range(5):
        teams.session_for(f"c{i}")
    assert list(teams._sessions) == ["c2", "c3", "c4"]
    teams._sessions["c2"]["used"] -= teams.SESSION_IDLE_SECONDS + 1
    teams.session_for("c5")
    assert "c2" not in teams._sessions


def test_too_many_waiting_questions_get_a_busy_reply(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(teams, "_slow_slots", threading.BoundedSemaphore(1))
    release = threading.Event()
    monkeypatch.setattr(teams, "answer_in_session", lambda conv, q: release.wait(5) and "done")
    monkeypatch.setattr(teams, "post_card", lambda *a: [{"ok": True}])
    body = {"type": "message", "text": "<at>kubesupport</at> why?", "conversation": {"id": "c"}}
    assert teams.handle_outgoing({**body, "id": "busy-1"})["text"].startswith("Looking into it")
    assert "busy" in teams.handle_outgoing({**body, "id": "busy-2"})["text"]
    release.set()


# --- Secrets and input validation ---

def test_environment_values_are_hidden():
    obj = {"spec": {"template": {"spec": {"containers": [{"name": "app", "env": [
        {"name": "DB_PASSWORD", "value": "hunter2"}, {"name": "MODE", "value": "prod"},
        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "t"}}}]}]}}}}
    env = tools._hide_env_values(obj)["spec"]["template"]["spec"]["containers"][0]["env"]
    assert [v.get("value") for v in env] == ["[REDACTED]", "[REDACTED]", None]
    assert env[2]["valueFrom"]["secretKeyRef"]["name"] == "s"


def test_credentials_in_urls_are_redacted():
    text = "psql postgresql://admin:s3cret@db:5432/app and https://user:pw@example.com/x"
    assert tools.redact(text) == ("psql postgresql://admin:[REDACTED]@db:5432/app and "
                                  "https://user:[REDACTED]@example.com/x")


@pytest.mark.parametrize("name", ["x/../../kube-system/configmaps/aws-auth", "../etc", "a b", "UPPER", ""])
def test_object_names_that_could_change_the_api_path_are_refused(name):
    with pytest.raises(ValueError, match="invalid"):
        tools._valid(name)


def test_namespace_must_be_a_valid_name():
    with pytest.raises(ValueError, match="invalid namespace"):
        tools._ns("default/../kube-system")


def test_exec_commands_are_redacted_in_activity():
    review = {"request": {"uid": "u", "operation": "CONNECT", "resource": {"resource": "pods"},
                          "subResource": "exec", "namespace": "db", "name": "pg-0",
                          "userInfo": {"username": "alice"},
                          "object": {"command": ["psql", "postgresql://admin:pw@localhost/app"]}}}
    assert activity.parse_admission(review)["command"] == "psql postgresql://admin:[REDACTED]@localhost/app"


def test_activity_queue_is_capped(monkeypatch):
    monkeypatch.setattr(activity, "MAX_PENDING", 3)
    feed = activity.ActivityFeed(notify=lambda card: None)
    for i in range(5):
        feed.record({"category": "change", "action": "created", "kind": "Service", "name": f"s{i}"})
    assert len(feed.pending) == 3 and feed.dropped_total == 2 and feed.events_total == 5


def test_activity_stop_sends_the_last_card():
    sent = []
    feed = activity.ActivityFeed(batch_seconds=3600, notify=sent.append)
    feed.start()
    feed.record({"category": "change", "action": "created", "kind": "Service", "name": "s", "namespace": "a",
                 "user": "alice"})
    feed.stop()
    assert len(sent) == 1


# --- Deadline, internal API, health endpoint, supervised servers ---

def test_slow_ai_investigation_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "DEADLINE_SECONDS", 0.05)

    def slow(messages, tools_, require_tool=None):
        time.sleep(0.1)
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t", name="list_pods",
                                                      input={"namespace": "default"})])
    monkeypatch.setattr(agent, "_create", slow)
    monkeypatch.setattr(agent, "call_tool", lambda *a: "<untrusted_data>[]")
    monkeypatch.setattr(agent, "generate_heuristic_rca", lambda **kw: dict(SAMPLE_RCA))
    rca = agent.investigate_and_rca("why?")
    assert rca["generated_by"] == "heuristic" and "LLM_DEADLINE_SECONDS" in rca["llm_error"]


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_internal_api_is_closed_without_a_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "")
    for method, path in [("get", "/alerts/status"), ("post", "/alerts/test"), ("get", "/rca/latest"),
                         ("get", "/activity")]:
        r = getattr(client, method)(path)
        assert r.status_code == 403 and "AGENT_API_KEY" in r.text, path
    monkeypatch.setattr(app_module, "API_KEY", "k")
    assert client.get("/rca/latest").status_code == 401


def test_healthz_fails_when_the_watcher_is_stuck_or_teams_server_died(client, monkeypatch):
    assert client.get("/healthz").status_code == 200
    monkeypatch.setattr(app_module.watcher.WATCHER, "healthy", lambda: (False, "no completed scan for 900 s"))
    r = client.get("/healthz")
    assert r.status_code == 503 and "no completed scan" in r.text
    monkeypatch.setattr(app_module.watcher.WATCHER, "healthy", lambda: (True, "ok"))
    monkeypatch.setitem(app_module.SERVER_FAILURES, "teams", "SystemExit: 1")
    assert client.get("/healthz").status_code == 503


def test_a_server_that_stops_is_recorded(monkeypatch):
    failures = {}

    class Boom:
        def __init__(self, config):
            pass

        def run(self):
            raise SystemExit(1)  # what uvicorn does when the port can't be bound
    monkeypatch.setattr(main.uvicorn, "Server", Boom)
    thread = main.supervised("teams", None, lambda name, why: failures.update({name: why}))
    thread.start()
    thread.join(5)
    assert failures == {"teams": "SystemExit: 1"}
