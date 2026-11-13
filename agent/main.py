"""kubesupport entrypoint.

* Port 8000 (HTTP): the API, Teams outgoing webhook, health checks (app.py).
* Port 8443 (HTTPS): the activity webhook the Kubernetes API server calls (activity.py).
  It starts only when the certificate from deploy/webhook-cert.sh is mounted and
  ACTIVITY_ENABLED is true. The webhook runs in a background thread, so the main
  server keeps handling shutdown signals.
"""
import logging
import threading

import uvicorn

import activity
from app import app

logger = logging.getLogger("kubesupport.main")


def build_configs():
    """(API config, webhook config or None)."""
    api = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    tls = activity.tls_files() if activity.ENABLED else None
    if not tls:
        reason = "ACTIVITY_ENABLED is false" if not activity.ENABLED else \
            f"no certificate in {activity.TLS_DIR} (run deploy/webhook-cert.sh)"
        logger.info("Activity webhook off: %s", reason)
        return api, None
    webhook = uvicorn.Config(activity.webhook_app, host="0.0.0.0", port=8443, log_level="warning",
                             ssl_certfile=tls[0], ssl_keyfile=tls[1])
    return api, webhook


def main():
    api, webhook = build_configs()
    if webhook:
        threading.Thread(target=uvicorn.Server(webhook).run, name="activity-webhook", daemon=True).start()
        logger.info("Activity webhook listening on :8443 (HTTPS)")
    uvicorn.Server(api).run()


if __name__ == "__main__":
    main()
