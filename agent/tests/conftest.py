"""Test setup: fake environment and helpers to build fake Kubernetes objects.

No cluster, AI model API or Teams webhook is contacted by any test.
"""
import base64
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

# Environment must be set before the agent modules are imported.
for _key in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "AGENT_MODEL",
             "AGENT_EFFORT", "LLM_MAX_STEPS", "LLM_TOOL_OUTPUT_CHARS", "LLM_MAX_REQUEST_CHARS",
             "LLM_MAX_OUTPUT_TOKENS"):
    os.environ.pop(_key, None)  # tests start in rules-only mode, whatever the shell has set
os.environ["ALLOWED_NAMESPACES"] = "default,apps"
os.environ["WATCH_ENABLED"] = "false"
TEST_SECRET = base64.b64encode(b"test-outgoing-webhook-secret").decode()
os.environ["TEAMS_OUTGOING_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["TEAMS_ALERT_WEBHOOK_URLS"] = "https://example.invalid/alert"
os.environ["TEAMS_REPLY_WEBHOOK_URL"] = "https://example.invalid/reply"
os.environ["REPORT_DIR"] = os.path.join(tempfile.gettempdir(), "kubesupport-test-reports")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


def container(name="app", restarts=0, waiting=None, waiting_msg=None,
              last_reason=None, last_exit=None, finished_ago=None):
    state = NS(waiting=NS(reason=waiting, message=waiting_msg) if waiting else None,
               terminated=None, running=None)
    if last_reason is not None or last_exit is not None:
        finished = NOW - timedelta(seconds=finished_ago) if finished_ago is not None else None
        last = NS(terminated=NS(reason=last_reason, exit_code=last_exit, finished_at=finished))
    else:
        last = NS(terminated=None)
    return NS(name=name, restart_count=restarts, state=state, last_state=last, ready=waiting is None)


def make_pod(name, ns="default", phase="Running", owner=("ReplicaSet", "web-5d8f7"), pod_hash="5d8f7",
             created_ago=3600, containers=(), conditions=()):
    owners = [NS(kind=owner[0], name=owner[1])] if owner else []
    labels = {"pod-template-hash": pod_hash} if pod_hash else {}
    return NS(
        metadata=NS(name=name, namespace=ns, uid=name, owner_references=owners, labels=labels,
                    creation_timestamp=NOW - timedelta(seconds=created_ago)),
        status=NS(phase=phase, container_statuses=list(containers), init_container_statuses=[],
                  conditions=list(conditions)),
    )


def make_node(name, ready="True", reason=None, message=None):
    return NS(metadata=NS(name=name),
              status=NS(conditions=[NS(type="Ready", status=ready, reason=reason, message=message)]))


SAMPLE_RCA = {
    "title": "Application Crash and Restart Loop in web",
    "severity": "SEV2",
    "summary": "web keeps restarting",
    "impact": "degraded",
    "affected_services": ["web"],
    "timeline": [{"time": "now", "event": "crash"}],
    "root_cause": "exit code 1 on startup",
    "evidence": [{"source": "logs", "excerpt": "Traceback ..."}],
    "contributing_factors": [],
    "resolution": ["Fix the config", "Roll back"],
    "prevention": ["Add a smoke test"],
    "confidence": "high",
    "stakeholder_summary": "The web service is restarting.",
}
