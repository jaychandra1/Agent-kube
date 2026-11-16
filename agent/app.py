"""kubesupport HTTP API.

`app` (port 8000, internal only):  /healthz /ready /chat /rca /rca/latest /alerts/status /alerts/test
                                   /activity /activity/status, and /teams/webhook for local testing
`teams_app` (port 8080, the only one the Cloudflare Tunnel reaches): POST /teams/webhook and
                                   nothing else, not even /docs, so a mistake in the Cloudflare
                                   settings can never expose the internal API
The activity webhook (called by the Kubernetes API server) is a separate HTTPS app on port
8443, in activity.py. main.py starts all three.
"""
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import activity
import agent
import cards
import report
import teams
import watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("kubesupport.app")

API_KEY = os.getenv("AGENT_API_KEY", "").strip()
WATCH_ENABLED = os.getenv("WATCH_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Web servers that stopped (set by main.py). A dead Teams server fails /healthz so the pod restarts.
SERVER_FAILURES: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if WATCH_ENABLED:
        watcher.WATCHER.start()
        logger.info("Watcher started (every %ss, %d Teams alert target(s))",
                    watcher.WATCHER.interval, len(teams.ALERT_TARGETS))
    else:
        logger.info("Watcher disabled (WATCH_ENABLED=false)")
    if not teams.OUTGOING_SECRET:
        logger.info("TEAMS_OUTGOING_WEBHOOK_SECRET not set: /teams/webhook is disabled")
    if not API_KEY:
        logger.warning("AGENT_API_KEY is not set: the internal API (/rca, /chat, /alerts/*, /activity*) is off")
    if activity.ENABLED:
        activity.FEED.start()
    yield
    watcher.WATCHER.stop()
    activity.FEED.stop()  # sends what was collected before the pod stops


app = FastAPI(title="kubesupport", version="2.0.0", lifespan=lifespan)


def auth(key: str | None):
    """The internal API is closed unless AGENT_API_KEY is set, and then needs that key."""
    if not API_KEY:
        raise HTTPException(status_code=403, detail="The internal API is off: set AGENT_API_KEY in the Secret")
    if not key or not hmac.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key header")


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


class RcaIn(BaseModel):
    question: str = "Scan the watched namespaces, diagnose any failing pods and produce an RCA."
    namespace: str | None = None
    pod: str | None = None
    post_to_teams: bool = False


@app.get("/healthz")
def healthz():
    """Liveness: fails (503) when the watcher stopped scanning or the Teams server died, so
    Kubernetes restarts the pod instead of it silently not alerting."""
    problems = []
    ok, why = watcher.WATCHER.healthy()
    if not ok:
        problems.append(f"watcher: {why}")
    if "teams" in SERVER_FAILURES:
        problems.append(f"teams server: {SERVER_FAILURES['teams']}")
    if problems:
        return JSONResponse({"status": "failing", "service": "kubesupport", "problems": problems}, status_code=503)
    return {"status": "ok", "service": "kubesupport"}


@app.get("/ready")
def ready():
    return {"status": "ready", "service": "kubesupport"}


teams_app = FastAPI(title="kubesupport Teams webhook", docs_url=None, redoc_url=None, openapi_url=None)


@teams_app.post("/teams/webhook")
@app.post("/teams/webhook")
async def teams_webhook(request: Request, authorization: str = Header(None)):
    """Teams outgoing webhook. The reply must come back within 5 seconds."""
    if not teams.OUTGOING_SECRET:
        raise HTTPException(status_code=404, detail="Teams outgoing webhook is not configured")
    raw = await request.body()
    if not teams.verify_hmac(raw, authorization):
        logger.warning("Rejected /teams/webhook call with a missing or wrong HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")
    try:
        message = teams.parse_body(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    reply = await run_in_threadpool(teams.handle_outgoing, message)
    return JSONResponse(reply)


@app.post("/chat")
def chat(body: ChatIn, x_api_key: str = Header(None)):
    auth(x_api_key)
    sid = body.session_id or str(uuid.uuid4())
    return {"session_id": sid, "answer": teams.answer_in_session(f"api:{sid}", body.message)}


@app.post("/rca")
def rca(body: RcaIn, x_api_key: str = Header(None)):
    auth(x_api_key)
    target = None
    if body.namespace and body.pod:
        target = {"namespace": body.namespace, "pod": body.pod, "workload": None,
                  "reason": "requested via API", "detail": ""}
    logger.info("Executing RCA: %s", body.question)
    data = agent.investigate_and_rca(body.question, target=target)
    filename, _ = report.render_rca(data)
    teams_result = teams.post_alert(cards.rca_card(data)) if body.post_to_teams else None
    return {"report_file": filename, "teams": teams_result, "rca": data}


@app.get("/rca/latest", response_class=HTMLResponse)
def get_latest_report(x_api_key: str = Header(None)):
    """The last RCA rendered by this pod, as HTML. Internal only."""
    auth(x_api_key)
    if report.LATEST_HTML:
        return HTMLResponse(content=report.LATEST_HTML, status_code=200)
    return HTMLResponse(content="<p>No RCA has been generated since the agent started.</p>", status_code=404)


@app.get("/alerts/status")
def alerts_status(x_api_key: str = Header(None)):
    auth(x_api_key)
    return {
        "watcher": watcher.WATCHER.status(),
        "teams": {
            "alert_targets": [name for name, _ in teams.ALERT_TARGETS],
            "reply_webhook": teams.REPLY_TARGET[0] if teams.REPLY_TARGET else None,
            "outgoing_webhook": bool(teams.OUTGOING_SECRET),
        },
        "llm": {"configured": agent.llm is not None, **(agent.llm.describe() if agent.llm else {}),
                "status": agent.LLM_STATUS},
    }


@app.post("/alerts/test")
def alerts_test(x_api_key: str = Header(None)):
    """Send a sample card to every alert webhook, to check the Teams setup."""
    auth(x_api_key)
    if not teams.ALERT_TARGETS:
        raise HTTPException(status_code=400, detail="TEAMS_ALERT_WEBHOOK_URLS is not set")
    results = teams.post_alert(cards.test_card())
    return {"sent_to": len(results), "results": results}


@app.get("/activity")
def activity_recent(limit: int = 50, x_api_key: str = Header(None)):
    """Recent cluster activity (exec, created/deleted objects, security changes, updates)."""
    auth(x_api_key)
    events = activity.FEED.latest(max(1, min(limit, 500)))
    return {"count": len(events), "events": [dict(e, text=activity.describe(e)) for e in events]}


@app.get("/activity/status")
def activity_status(x_api_key: str = Header(None)):
    """Is the activity webhook on, and when did the API server last call it?"""
    auth(x_api_key)
    return activity.FEED.status()
