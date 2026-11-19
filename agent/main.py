"""Entrypoint. Starts up to three servers:

  8000  internal API and health checks (app.app)
  8080  POST /teams/webhook only (app.teams_app). The Cloudflare tunnel forwards here and
        the NetworkPolicy only lets the tunnel pods in.
  8443  activity webhook, HTTPS (activity.py). Only when the cert from
        deploy/webhook-cert.sh is mounted and ACTIVITY_ENABLED is true.

8080 and 8443 run in background threads so the main server keeps handling signals. If the
Teams one dies /healthz fails and the pod is restarted, a dead activity webhook shows up
in /activity/status.
"""
import logging
import os
import threading


def setup_logging():
    """Has to run before the other imports, they log while loading.
    LOG_LEVEL is DEBUG / INFO / WARNING / ERROR. Probe requests are kept out of the access log."""
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
    """uvicorn configs for the api, teams and activity servers. The last one can be None."""
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
    """Thread that runs one server and reports when it stops (bad cert, port in use, crash)."""
    def run():
        try:
            uvicorn.Server(config).run()
            on_failure(name, "stopped")
        except BaseException as e:  # uvicorn exits with SystemExit when the bind fails
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
