"""kubesupport entrypoint.

* Port 8000 (HTTP, internal only): the API and health checks (app.app).
* Port 8080 (HTTP): only POST /teams/webhook (app.teams_app). This is the one port the
  Cloudflare Tunnel forwards to; the NetworkPolicy lets only the tunnel pods reach it.
* Port 8443 (HTTPS): the activity webhook the Kubernetes API server calls (activity.py).
  It starts only when the certificate from deploy/webhook-cert.sh is mounted and
  ACTIVITY_ENABLED is true.
The extra servers run in background threads, so the main server keeps handling shutdown
signals. If one of them stops, that is recorded: a dead Teams server fails /healthz (the pod
is restarted); a dead activity webhook shows in /activity/status.
"""
import logging
import os
import threading


def setup_logging():
    """Logging comes first, so messages written while the modules load aren't lost.
    LOG_LEVEL: DEBUG, INFO (default), WARNING, ERROR. Health-check requests aren't logged."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)

    class _NoProbes(logging.Filter):
        def filter(self, record):
            return not any(path in record.getMessage() for path in ('"GET /healthz', '"GET /ready'))
    logging.getLogger("uvicorn.access").addFilter(_NoProbes())


setup_logging()

import uvicorn  # noqa: E402

import activity  # noqa: E402
import app as app_module  # noqa: E402
from app import app, teams_app  # noqa: E402

logger = logging.getLogger("kubesupport.main")

API_PORT, TEAMS_PORT, WEBHOOK_PORT = 8000, 8080, 8443


def build_configs():
    """(API config, Teams webhook config, activity webhook config or None)."""
    api = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_config=None)
    public = uvicorn.Config(teams_app, host="0.0.0.0", port=TEAMS_PORT, log_config=None, access_log=False)
    tls = activity.tls_files() if activity.ENABLED else None
    if not tls:
        reason = "ACTIVITY_ENABLED is false" if not activity.ENABLED else \
            f"no certificate in {activity.TLS_DIR} (run deploy/webhook-cert.sh)"
        logger.info("Activity webhook off: %s", reason)
        return api, public, None
    webhook = uvicorn.Config(activity.webhook_app, host="0.0.0.0", port=WEBHOOK_PORT, log_config=None,
                             access_log=False, ssl_certfile=tls[0], ssl_keyfile=tls[1])
    return api, public, webhook


def supervised(name: str, config, on_failure):
    """Run a server in this thread; record it when it stops (bad certificate, port in use, crash)."""
    def run():
        try:
            uvicorn.Server(config).run()
            on_failure(name, "stopped")
        except BaseException as e:  # SystemExit from a failed port bind included
            logger.error("The %s server stopped: %s: %s", name, type(e).__name__, e)
            on_failure(name, f"{type(e).__name__}: {e}")
    return threading.Thread(target=run, name=f"{name}-server", daemon=True)


def record_failure(name: str, why: str):
    app_module.SERVER_FAILURES[name] = why
    if name == "activity":
        activity.SERVER_ERROR = why


def main():
    api, public, webhook = build_configs()
    supervised("teams", public, record_failure).start()
    logger.info("Teams webhook listening on :%d (POST /teams/webhook only)", TEAMS_PORT)
    if webhook:
        supervised("activity", webhook, record_failure).start()
        logger.info("Activity webhook listening on :%d (HTTPS)", WEBHOOK_PORT)
    uvicorn.Server(api).run()


if __name__ == "__main__":
    main()
