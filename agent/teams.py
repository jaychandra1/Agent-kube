"""Microsoft Teams integration without a bot registration.

* Alerts and long answers go OUT through Teams Workflows webhooks (plain HTTPS POSTs
  of Adaptive Cards; set up in the Teams "Workflows" app). A webhook can post to a chat,
  a standard channel or a private channel; the agent just needs its URL.
* Questions come IN through a Teams *outgoing webhook*: a team owner creates it in
  Teams (Manage team -> Apps -> Create an outgoing webhook). When someone @mentions
  it in a channel, Teams POSTs the message here, signed with HMAC-SHA256, and shows
  whatever we return within 5 seconds as the reply. Slow work (AI answers, RCA) is
  acknowledged at once and the result is posted to the channel's Workflows webhook.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import requests

import agent
import cards
import tools

logger = logging.getLogger("kubesupport.teams")


def _split(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,\s]+", value or "") if v.strip()]


_NAMED_TARGET = re.compile(r"^([A-Za-z0-9._-]+)=(https?://\S+)$")


def parse_targets(value: str, prefix: str) -> list[tuple[str, str]]:
    """Webhook targets from a comma-separated list; each entry may carry a name.

    'chat=https://a, https://b' -> [('chat', 'https://a'), ('alert-2', 'https://b')]
    Names show up in logs and in /alerts/status and /alerts/test results instead of the
    URLs, which work like passwords."""
    targets = []
    for i, entry in enumerate(_split(value), start=1):
        named = _NAMED_TARGET.match(entry)
        targets.append((named.group(1), named.group(2)) if named else (f"{prefix}-{i}", entry))
    return targets


ALERT_TARGETS = parse_targets(os.getenv("TEAMS_ALERT_WEBHOOK_URLS", ""), "alert")
_reply = parse_targets(os.getenv("TEAMS_REPLY_WEBHOOK_URL", ""), "reply")
REPLY_TARGET = _reply[0] if _reply else (ALERT_TARGETS[0] if ALERT_TARGETS else None)
OUTGOING_SECRET = os.getenv("TEAMS_OUTGOING_WEBHOOK_SECRET", "").strip()
TENANT_ID = os.getenv("TEAMS_TENANT_ID", "").strip()
ALLOWED_USER_IDS = {u.lower() for u in _split(os.getenv("TEAMS_ALLOWED_USER_IDS", ""))}

QUICK_TIMEOUT_SECONDS = 4.0      # Teams waits 5 s for an outgoing-webhook reply
MAX_REPLY_CHARS = 3500
POST_ATTEMPTS = 3                # Workflows throttles >4 requests/s (HTTP 429); retry with backoff

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="teams")
_seen_ids = deque(maxlen=500)    # Teams can resend the same message; answer once
_seen_lock = threading.Lock()
_sessions: dict[str, list] = {}  # AI chat history per channel thread
_session_locks: dict[str, threading.Lock] = {}
_sessions_lock = threading.Lock()


# ─── Outbound: Workflows webhooks ───────────────────────────────────────────

def _post_one(label: str, url: str, payload: dict) -> dict:
    """POST to one webhook. Retries throttling (429), server errors and network errors.
    Logs only the target's name and the error type: the URL (and errors that quote it)
    contains the webhook's signature."""
    result = {"target": label, "status": None, "ok": False}
    for attempt in range(1, POST_ATTEMPTS + 1):
        delay = 2 ** attempt
        try:
            r = requests.post(url, json=payload, timeout=15)
            result["status"] = r.status_code
            result.pop("error", None)
            if 200 <= r.status_code < 300:
                result["ok"] = True
                return result
            retryable = r.status_code == 429 or r.status_code >= 500
            retry_after = (r.headers or {}).get("Retry-After", "")
            if retry_after.isdigit():
                delay = min(int(retry_after), 30)
            logger.warning("Teams target '%s' returned %s (attempt %d/%d): %s",
                           label, r.status_code, attempt, POST_ATTEMPTS, r.text[:300])
        except Exception as e:
            result["status"], result["error"] = None, type(e).__name__
            retryable = True
            logger.warning("Teams target '%s' failed (attempt %d/%d): %s",
                           label, attempt, POST_ATTEMPTS, type(e).__name__)
        if not retryable or attempt == POST_ATTEMPTS:
            return result
        time.sleep(delay)
    return result


def post_card(targets, adaptive_card: dict) -> list[dict]:
    """POST a card to each (name, url) target. Never raises; returns one result per target."""
    if isinstance(targets, tuple):
        targets = [targets]
    payload = cards.message(adaptive_card)
    return [_post_one(label, url, payload) for label, url in (targets or [])]


def post_alert(adaptive_card: dict) -> list[dict]:
    if not ALERT_TARGETS:
        logger.info("No TEAMS_ALERT_WEBHOOK_URLS set; alert not sent")
        return []
    return post_card(ALERT_TARGETS, adaptive_card)


# ─── Inbound: outgoing webhook ──────────────────────────────────────────────

def verify_hmac(raw_body: bytes, auth_header: str | None, secret_b64: str = None) -> bool:
    """Teams signs the body with HMAC-SHA256 using the base64-decoded security token
    and sends `Authorization: HMAC <base64 digest>`."""
    secret_b64 = OUTGOING_SECRET if secret_b64 is None else secret_b64
    if not secret_b64 or not auth_header or not auth_header.startswith("HMAC "):
        return False
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, auth_header[5:].strip())


def strip_mentions(text: str) -> str:
    """'<at>kubesupport</at>&nbsp;pods  web' -> 'pods web'"""
    text = re.sub(r"<at>.*?</at>", " ", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)  # Teams sends the message as HTML
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_command(text: str) -> tuple[str, list[str]]:
    words = text.split()
    if not words:
        return "help", []
    head = words[0].lower()
    if head in {"help", "status", "incidents", "pods", "events", "logs", "rca", "get", "activity"}:
        return head, words[1:]
    return "ask", words


HELP = """**kubesupport** - mention me with one of these:

- `status` - open incidents the watcher sees right now
- `pods <namespace>` - pods with readiness and restarts
- `events <namespace>` - recent Kubernetes events
- `logs <namespace>/<pod> [--previous]` - last log lines (`--previous` = the crashed container)
- `get <kind> [namespace]` - list any kind of object, e.g. `get services payments`, `get nodes`, `get ingresses`
- `get <kind> <namespace>/<name>` - one object in full (cluster-wide kinds: `get node <name>`)
- `activity` - the latest cluster activity: exec into pods, created/deleted objects, RBAC changes, updates
- `rca <namespace>/<pod>` - full root cause analysis of one pod
- `rca <question>` - investigate a problem you describe
- anything else - ask a question about the cluster in plain words

Long answers (AI answers, RCA) arrive as a separate message in this channel."""


def _code(text: str) -> str:
    text = text or "(empty)"
    if len(text) > MAX_REPLY_CHARS:
        text = "…" + text[-MAX_REPLY_CHARS:]
    return f"```\n{text}\n```"


def _ns_pod(arg: str) -> tuple[str, str]:
    if "/" not in arg:
        raise ValueError("use <namespace>/<pod>, for example `logs default/web-7d9f8b-abcde`")
    ns, pod = arg.split("/", 1)
    return ns, pod


def _status_text() -> str:
    import watcher  # local import: watcher imports this module
    incidents = watcher.WATCHER.current_incidents()
    source = "watcher"
    if incidents is None:  # watcher not running: detect once
        found, _ = tools.detect_incidents({})
        incidents, source = list(found.values()), "live scan"
    if not incidents:
        return f"No open incidents ({source}). Watching: {tools.watched_namespaces_label()}."
    lines = [f"**{len(incidents)} open incident(s)** ({source}):"]
    for i in incidents[:20]:
        lines.append(f"- `{i.get('namespace') or 'cluster'}` {i['workload']} - **{i['reason']}** - {i.get('detail', '')[:200]}")
    return "\n".join(lines)


def _pods_text(ns: str) -> str:
    rows = tools.list_pods(ns)
    if not rows:
        return f"No pods in `{ns}`."
    lines = [f"{'NAME':<48} {'READY':<6} {'PHASE':<10} {'RESTARTS':<8} WAITING"]
    for r in rows[:60]:
        lines.append(f"{r['name'][:48]:<48} {r['ready']:<6} {str(r['phase']):<10} {r['restarts']:<8} "
                     f"{','.join(r['waiting'])}")
    return _code("\n".join(lines))


def _events_text(ns: str) -> str:
    evs = tools.get_events(ns, limit=15)
    if not evs:
        return f"No recent events in `{ns}`."
    return _code("\n".join(f"{e['time'][:19]} {e['type']:<7} {e['reason']:<18} {e['object']}: {e['message']}"
                           for e in evs))


def _logs_text(args: list[str]) -> str:
    if not args:
        raise ValueError("use `logs <namespace>/<pod> [--previous]`")
    ns, pod = _ns_pod(args[0])
    previous = "--previous" in args
    return _code(tools.get_pod_logs(ns, pod, tail_lines=60, previous=previous))


def _get_text(args: list[str]) -> str:
    """get <kind> [namespace]  |  get <kind> <namespace>/<name>  |  get <kind> <cluster-scoped name>"""
    if not args:
        raise ValueError("use `get <kind> [namespace]`, e.g. `get services payments` or `get nodes`")
    kind, target = args[0], (args[1] if len(args) > 1 else None)
    if target and "/" in target:
        ns, name = target.split("/", 1)
        return _code(tools.get_resource(kind, name, ns))
    if target and not tools._resolve_kind(kind).namespaced:  # e.g. `get node ip-10-0-1-1`
        return _code(tools.get_resource(kind, target))
    listing = tools.list_resources(kind, target)
    if not listing["items"]:
        return f"No {listing['kind']} objects{' in `' + target + '`' if target else ''}."
    lines = [f"{'NAMESPACE':<20} {'NAME':<45} {'AGE':>7}  STATUS"]
    for row in listing["items"]:
        age = f"{row['age_min'] // 1440}d" if (row["age_min"] or 0) >= 1440 else f"{row['age_min'] or 0}m"
        lines.append(f"{(row['namespace'] or '-')[:20]:<20} {str(row['name'])[:45]:<45} {age:>7}  {row['status']}")
    if listing.get("note"):
        lines.append(listing["note"])
    return f"**{listing['count']} {listing['kind']}**\n" + _code("\n".join(lines))


def _activity_text() -> str:
    import activity  # local import: activity imports this module
    events = activity.FEED.latest(20)
    if not events:
        status = activity.FEED.status()
        hint = "" if status["webhook_certificate"] else " (the activity webhook isn't set up: run deploy/webhook-cert.sh)"
        return f"No cluster activity recorded yet{hint}."
    return "**Latest cluster activity** (newest last):\n" + "\n".join(
        f"- {e['time'][11:19]} UTC - {activity.describe(e)}" for e in events)


def _session_for(conv_id: str):
    with _sessions_lock:
        return _sessions.setdefault(conv_id, []), _session_locks.setdefault(conv_id, threading.Lock())


def _answer_question(conv_id: str, question: str) -> str:
    msgs, lock = _session_for(conv_id)
    with lock:
        answer = agent.chat(msgs, question)
        if len(msgs) > 40:
            _sessions[conv_id] = agent.trim_history(msgs, keep=20)
    return answer


def _run_rca(args: list[str]) -> dict:
    if args and "/" in args[0] and len(args) == 1:
        ns, pod = _ns_pod(args[0])
        tools._ns(ns)  # allowlist check before doing any work
        target = {"namespace": ns, "pod": pod, "workload": None, "reason": "requested from Teams", "detail": ""}
        return agent.investigate_and_rca(f"Investigate pod {pod} in namespace {ns} and produce an RCA.", target)
    question = " ".join(args) or "Scan the cluster, diagnose any failing pods and produce an RCA."
    return agent.investigate_and_rca(question)


def _reply(text: str) -> dict:
    return {"type": "message", "text": text}


def _deliver_later(future, asked_by: str, question: str, is_rca: bool):
    """Post the result of slow work to the channel's Workflows webhook."""
    def done(f):
        try:
            result = f.result()
            card = cards.rca_card(result, asked_by, question) if is_rca else \
                cards.answer_card(question, asked_by, str(result))
        except Exception as e:
            logger.exception("Teams request failed")
            card = cards.answer_card(question, asked_by, f"Sorry, that failed: {type(e).__name__}: {e}")
        post_card(REPLY_TARGET, card)
    future.add_done_callback(done)


def handle_outgoing(activity: dict) -> dict:
    """Handle one outgoing-webhook message (HMAC already verified). Returns the reply."""
    msg_id = activity.get("id")
    with _seen_lock:
        if msg_id and msg_id in _seen_ids:
            return _reply("(already answered)")
        if msg_id:
            _seen_ids.append(msg_id)

    sender = activity.get("from") or {}
    tenant = ((activity.get("channelData") or {}).get("tenant") or {}).get("id", "")
    if TENANT_ID and tenant.lower() != TENANT_ID.lower():
        logger.warning("Rejected Teams message from tenant %s", tenant)
        return _reply("This agent only answers its own organization.")
    if ALLOWED_USER_IDS and (sender.get("aadObjectId") or "").lower() not in ALLOWED_USER_IDS:
        return _reply(f"Sorry {sender.get('name', '')}, you're not on this agent's allowed-users list.")

    text = strip_mentions(activity.get("text", ""))
    cmd, args = parse_command(text)
    asked_by = sender.get("name")
    conv_id = (activity.get("conversation") or {}).get("id", "default")
    logger.info("Teams request from %s: %s", asked_by, cmd)

    if cmd == "help":
        return _reply(HELP)

    quick = {
        "status": lambda: _status_text(),
        "incidents": lambda: _status_text(),
        "pods": lambda: _pods_text(args[0]) if args else "use `pods <namespace>`",
        "events": lambda: _events_text(args[0]) if args else "use `events <namespace>`",
        "logs": lambda: _logs_text(args),
        "get": lambda: _get_text(args),
        "activity": lambda: _activity_text(),
    }
    if cmd in quick:
        future = _pool.submit(quick[cmd])
        try:
            return _reply(future.result(timeout=QUICK_TIMEOUT_SECONDS))
        except FutureTimeout:
            if not REPLY_TARGET:
                return _reply("That's taking longer than Teams allows, and no reply webhook is set up.")
            future.add_done_callback(lambda f: post_card(REPLY_TARGET, cards.answer_card(
                text, asked_by, f.result() if not f.exception() else f"Failed: {f.exception()}")))
            return _reply("Still working on it; I'll post the result in this channel.")
        except ValueError as e:  # bad arguments or a namespace outside the allowlist
            return _reply(str(e))
        except Exception as e:
            logger.exception("Teams quick command failed")
            return _reply(f"Sorry, that failed: {type(e).__name__}: {e}")

    # Slow work: AI questions and RCAs
    if cmd == "ask" and not agent.client:
        return _reply("No AI model is configured for this agent (GROQ_API_KEY or ANTHROPIC_API_KEY), "
                      "so I can only run commands.\n\n" + HELP)
    if not REPLY_TARGET:
        return _reply("I need a Teams reply webhook (TEAMS_REPLY_WEBHOOK_URL) to send answers that take "
                      "longer than a few seconds. Commands like `status` and `pods <namespace>` still work.")
    if cmd == "rca":
        future = _pool.submit(_run_rca, args)
        _deliver_later(future, asked_by, text, is_rca=True)
    else:
        future = _pool.submit(_answer_question, conv_id, text)
        _deliver_later(future, asked_by, text, is_rca=False)
    return _reply("Looking into it - I'll post the answer in this channel.")


def parse_body(raw_body: bytes) -> dict:
    return json.loads(raw_body.decode("utf-8"))
