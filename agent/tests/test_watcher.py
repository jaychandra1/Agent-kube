import agent
from conftest import SAMPLE_RCA, fake_llm
from watcher import Watcher

INC = {"key": "default/Deployment/web/CrashLoop", "namespace": "default", "workload": "Deployment/web",
       "reason": "CrashLoop", "pod": "web-1", "pods": ["web-1"], "detail": "exit 1"}
INC2 = {**INC, "key": "default/Deployment/api/CrashLoop", "workload": "Deployment/api", "pod": "api-1"}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def title(card):
    return card["body"][0]["items"][0]["text"]


def make_watcher(max_llm_per_hour=10):
    state = {"found": {}}
    sent, rca_calls = [], []
    clock = Clock()

    def rca(incident, use_llm):
        rca_calls.append((incident["key"], use_llm))
        return dict(SAMPLE_RCA, generated_by="test")

    w = Watcher(interval=60, cooldown_min=60, resolve_after_min=10, max_llm_per_hour=max_llm_per_hour,
                detect=lambda prev: (dict(state["found"]), {}), rca=rca, notify=sent.append, clock=clock)
    return w, state, sent, rca_calls, clock


def test_first_scan_sends_one_summary_not_alerts():
    w, state, sent, rca_calls, _ = make_watcher()
    state["found"] = {INC["key"]: INC}
    w.tick()
    assert len(sent) == 1 and "1 ongoing incident" in title(sent[0])
    assert rca_calls == []


def test_new_incident_gets_alert_with_rca():
    w, state, sent, rca_calls, clock = make_watcher()
    w.tick()  # startup, nothing open
    clock.t, state["found"] = 60, {INC["key"]: INC}
    w.tick()
    assert rca_calls == [(INC["key"], False)]  # no AI model in tests
    assert title(sent[-1]).startswith(SAMPLE_RCA["title"])


def test_reminder_only_after_cooldown():
    w, state, sent, _, clock = make_watcher()
    w.tick()
    clock.t, state["found"] = 60, {INC["key"]: INC}
    w.tick()
    clock.t = 120
    w.tick()
    assert len(sent) == 2  # startup + alert, no reminder yet
    clock.t = 60 + 3600
    w.tick()
    assert len(sent) == 3 and "Still failing" in sent[-1]["body"][0]["items"][1]["text"]


def test_resolved_only_after_grace_and_no_flapping():
    w, state, sent, _, clock = make_watcher()
    w.tick()
    clock.t, state["found"] = 60, {INC["key"]: INC}
    w.tick()
    clock.t, state["found"] = 120, {}
    w.tick()  # gone briefly: not resolved yet
    clock.t, state["found"] = 180, {INC["key"]: INC}
    w.tick()  # back again: still the same incident, no new alert
    assert len(sent) == 2
    clock.t, state["found"] = 300, {}
    w.tick()
    clock.t = 180 + 600
    w.tick()
    assert title(sent[-1]).startswith("Resolved:")
    assert w.current_incidents() is None or w.open == {}


def test_hourly_model_budget(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())  # pretend an AI model is configured
    w, state, _, rca_calls, clock = make_watcher(max_llm_per_hour=1)
    w.tick()
    clock.t, state["found"] = 60, {INC["key"]: INC, INC2["key"]: INC2}
    w.tick()
    assert sorted(use for _, use in rca_calls) == [False, True]
    clock.t, state["found"] = 60 + 3700, {}
    w.tick()
    clock.t, state["found"] = 60 + 3800, {"x": {**INC, "key": "x"}}
    w.tick()
    assert rca_calls[-1] == ("x", True)  # budget refilled after an hour
