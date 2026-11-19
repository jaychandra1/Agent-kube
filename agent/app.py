"""HTTP API.

app (8000, internal only)
    /healthz /ready /chat /rca /rca/latest /alerts/status /alerts/test /activity
    /activity/status /security/restricted, plus /teams/webhook for local testing
teams_app (8080, the only port the Cloudflare tunnel reaches)
    POST /teams/webhook and nothing else, not even /docs. A mistake in the Cloudflare
    settings can't expose the internal API this way.

The activity webhook the API server calls is a separate HTTPS app on 8443 (activity.py).
main.py starts all three.
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
import boundaries
import cards
import report
import teams
import watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("kubesupport.app")

API_KEY = os.getenv("AGENT_API_KEY", "").strip()
WATCH_ENABLED = os.getenv("WATCH_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Servers that died, filled in by main.py. "teams" in here makes /healthz fail.
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
    if boundaries.ENABLED:
        logger.info("Boundaries on: questions about kubesupport itself are refused and reported to %s; protected "
                    "namespaces: %s", ", ".join(name for name, _ in teams.SECURITY_TARGETS) or "the log only",
                    ", ".join(sorted(boundaries.PROTECTED_NAMESPACES)) or "none")
    else:
        logger.warning("Boundaries are off (BOUNDARIES_ENABLED=false): people in Teams can ask about kubesupport itself")
    if activity.ENABLED:
        activity.FEED.start()
    yield
    watcher.WATCHER.stop()
    activity.FEED.stop()  # flushes the last batch


app = FastAPI(title="kubesupport", version="2.0.0", lifespan=lifespan)


def auth(key: str | None):
    """No AGENT_API_KEY configured means the internal API is off, otherwise the header must match."""
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
    """Liveness. 503 when the watcher stopped scanning or the Teams server died, better a
    restart than a pod that silently stopped alerting."""
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
    """Teams outgoing webhook. Teams waits 5 seconds for the reply."""
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
    """Last RCA this pod rendered, as HTML."""
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
            "security_targets": [name for name, _ in teams.SECURITY_TARGETS],
            "reply_webhook": teams.REPLY_TARGET[0] if teams.REPLY_TARGET else None,
            "outgoing_webhook": bool(teams.OUTGOING_SECRET),
        },
        "llm": {"configured": agent.llm is not None, **(agent.llm.describe() if agent.llm else {}),
                "status": agent.LLM_STATUS},
    }


@app.post("/alerts/test")
def alerts_test(x_api_key: str = Header(None)):
    """Send a test card to every alert webhook."""
    auth(x_api_key)
    if not teams.ALERT_TARGETS:
        raise HTTPException(status_code=400, detail="TEAMS_ALERT_WEBHOOK_URLS is not set")
    results = teams.post_alert(cards.test_card())
    return {"sent_to": len(results), "results": results}


@app.get("/activity")
def activity_recent(limit: int = 50, x_api_key: str = Header(None)):
    """Recent cluster activity."""
    auth(x_api_key)
    events = activity.FEED.latest(max(1, min(limit, 500)))
    return {"count": len(events), "events": [dict(e, text=activity.describe(e)) for e in events]}


@app.get("/security/restricted")
def security_restricted(limit: int = 50, x_api_key: str = Header(None)):
    """Refused questions about kubesupport itself, since the pod started."""
    auth(x_api_key)
    events = boundaries.recent(max(1, min(limit, boundaries.MAX_EVENTS)))
    return {"enabled": boundaries.ENABLED, "protected_namespaces": sorted(boundaries.PROTECTED_NAMESPACES),
            "count": len(events), "events": events}


@app.get("/activity/status")
def activity_status(x_api_key: str = Header(None)):
    """State of the activity webhook: enabled, cert present, last call."""
    auth(x_api_key)
    return activity.FEED.status()
