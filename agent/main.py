"""kubesupport entrypoint.

* Port 8000 (HTTP, internal only): the API and health checks (app.app).
* Port 8080 (HTTP): only POST /teams/webhook (app.teams_app). This is the one port the
  Cloudflare Tunnel forwards to; the NetworkPolicy lets only the tunnel pods reach it.
* Port 8443 (HTTPS): the activity webhook the Kubernetes API server calls (activity.py).
  It starts only when the certificate from deploy/webhook-cert.sh is mounted and
  ACTIVITY_ENABLED is true.
The extra servers run in background threads, so the main server keeps handling
shutdown signals.
"""
import logging
import threading

import uvicorn

import activity
from app import app, teams_app

logger = logging.getLogger("kubesupport.main")

API_PORT, TEAMS_PORT, WEBHOOK_PORT = 8000, 8080, 8443


def build_configs():
    """(API config, Teams webhook config, activity webhook config or None)."""
    api = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level="info")
    public = uvicorn.Config(teams_app, host="0.0.0.0", port=TEAMS_PORT, log_level="warning")
    tls = activity.tls_files() if activity.ENABLED else None
    if not tls:
        reason = "ACTIVITY_ENABLED is false" if not activity.ENABLED else \
            f"no certificate in {activity.TLS_DIR} (run deploy/webhook-cert.sh)"
        logger.info("Activity webhook off: %s", reason)
        return api, public, None
    webhook = uvicorn.Config(activity.webhook_app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="warning",
                             ssl_certfile=tls[0], ssl_keyfile=tls[1])
    return api, public, webhook


def main():
    api, public, webhook = build_configs()
    threading.Thread(target=uvicorn.Server(public).run, name="teams-webhook", daemon=True).start()
    logger.info("Teams webhook listening on :%d (POST /teams/webhook only)", TEAMS_PORT)
    if webhook:
        threading.Thread(target=uvicorn.Server(webhook).run, name="activity-webhook", daemon=True).start()
        logger.info("Activity webhook listening on :%d (HTTPS)", WEBHOOK_PORT)
    uvicorn.Server(api).run()


if __name__ == "__main__":
    main()
