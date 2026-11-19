"""Adaptive Cards for Teams.

Posted to Workflows webhooks, which want a message with Adaptive Card attachments.
Schema 1.2 works on every Teams client. Text gets truncated so a card stays under
Teams' ~28 KB limit.
"""
import json
import os
import re

import boundaries
import timefmt

CLUSTER_NAME = os.getenv("CLUSTER_NAME", "").strip()
MAX_CARD_BYTES = 24000

SEV_STYLE = {"SEV1": "attention", "SEV2": "warning", "SEV3": "accent", "SEV4": "good"}
SEV_COLOR = {"SEV1": "Attention", "SEV2": "Warning", "SEV3": "Accent", "SEV4": "Good"}


def _t(text, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(text, limit=1500, **props) -> dict:
    return {"type": "TextBlock", "text": _t(text, limit), "wrap": True, **props}


def _facts(pairs) -> dict:
    return {"type": "FactSet", "facts": [{"title": k, "value": _t(v, 300)} for k, v in pairs if v]}


def _bullets(items, limit_items=5, limit_chars=400) -> dict:
    lines = [f"- {_t(i, limit_chars)}" for i in (items or [])[:limit_items]]
    return _text("\n".join(lines) or "-", limit=limit_items * (limit_chars + 4))


def _cluster_prefix() -> str:
    return f"[{CLUSTER_NAME}] " if CLUSTER_NAME else ""


def card(body: list) -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.2",
        "body": body,
        "msteams": {"width": "Full"},
    }


def message(adaptive_card: dict) -> dict:
    """Wrap a card in the envelope Workflows expects. Shrinks it if it's too big."""
    payload = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": adaptive_card}]}
    if len(json.dumps(payload)) > MAX_CARD_BYTES:
        # drop blocks from the end until it fits, and say so
        body = adaptive_card["body"]
        while len(body) > 1 and len(json.dumps(payload)) > MAX_CARD_BYTES - 200:
            body.pop()
        body.append(_text("(Details were too long for a Teams message and were cut.)", isSubtle=True))
    return payload


def _header(title: str, severity: str | None, subtitle: str | None = None) -> dict:
    items = [_text(f"{_cluster_prefix()}{title}", 300, weight="Bolder", size="Medium",
                   color=SEV_COLOR.get(severity or "", "Default"))]
    if subtitle:
        items.append(_text(subtitle, 300, isSubtle=True, spacing="None"))
    return {"type": "Container", "style": SEV_STYLE.get(severity or "", "emphasis"), "items": items}


def _engine(rca: dict) -> str:
    """Engine label for the cards. Doesn't name the provider or model on purpose (see
    boundaries.py), the full value is still in the API response and the log."""
    by = str(rca.get("generated_by") or "?")
    return "AI model" if by.startswith("llm:") else "built-in rules" if by == "heuristic" else by


def alert_card(incident: dict, rca: dict | None, reminder: bool = False) -> dict:
    """Alert for a new incident, or a reminder. Carries the RCA summary if we have one."""
    sev = (rca or {}).get("severity", "SEV2")
    kind = "Still failing" if reminder else "New incident"
    title = (rca or {}).get("title") or f"{incident['reason']} in {incident['workload']}"
    body = [
        _header(title, sev, f"{kind} - {sev}"),
        _facts([
            ("Namespace", incident.get("namespace") or "(cluster)"),
            ("Workload", incident.get("workload")),
            ("Problem", incident.get("reason")),
            ("Pods", ", ".join(incident.get("pods") or [])),
            ("Detail", incident.get("detail")),
        ]),
    ]
    if rca and not reminder:
        body += [
            _text("Root cause", weight="Bolder", spacing="Medium"),
            _text(rca.get("root_cause"), 1200),
            _text("What to do", weight="Bolder", spacing="Medium"),
            _bullets(rca.get("resolution")),
            _text(f"Confidence: {rca.get('confidence', '?')} - engine: {_engine(rca)}",
                  200, isSubtle=True, size="Small"),
        ]
    if incident.get("pod") and not boundaries.is_protected(incident.get("namespace")):  # would be refused anyway
        body.append(_text(f"Ask in Teams: @kubesupport rca {incident['namespace']}/{incident['pod']}",
                          300, isSubtle=True, size="Small"))
    return card(body)


def resolved_card(incident: dict, minutes_open: int) -> dict:
    return card([
        _header(f"Resolved: {incident['reason']} in {incident['workload']}", "SEV4",
                f"Open for about {minutes_open} min"),
        _facts([("Namespace", incident.get("namespace") or "(cluster)"), ("Workload", incident.get("workload"))]),
    ])


def startup_card(incidents: list) -> dict:
    if not incidents:
        return card([_header("kubesupport started - no open incidents", "SEV4")])
    lines = [f"{i.get('namespace') or '(cluster)'} - {i['workload']} - {i['reason']}" for i in incidents[:15]]
    if len(incidents) > 15:
        lines.append(f"... and {len(incidents) - 15} more")
    return card([
        _header(f"kubesupport started - {len(incidents)} ongoing incident(s)", "SEV3",
                "These were already failing; you'll get alerts for new ones and a notice when these resolve."),
        _bullets(lines, limit_items=16, limit_chars=200),
    ])


def rca_card(rca: dict, asked_by: str | None = None, question: str | None = None) -> dict:
    sev = rca.get("severity", "SEV3")
    body = [_header(rca.get("title", "Root cause analysis"), sev, f"{sev} - confidence {rca.get('confidence', '?')}")]
    if asked_by or question:
        body.append(_text(f"Asked by {asked_by or 'someone'}: {question or ''}", 400, isSubtle=True))
    body += [
        _text(rca.get("stakeholder_summary") or rca.get("summary"), 1000),
        _text("Root cause", weight="Bolder", spacing="Medium"),
        _text(rca.get("root_cause"), 1500),
        _text("What to do", weight="Bolder", spacing="Medium"),
        _bullets(rca.get("resolution")),
        _text("Prevention", weight="Bolder", spacing="Medium"),
        _bullets(rca.get("prevention"), limit_items=3),
    ]
    evidence = rca.get("evidence") or []
    if evidence:
        body.append(_text("Evidence", weight="Bolder", spacing="Medium"))
        for e in evidence[:3]:
            body.append(_text(f"{e.get('source')}:\n{_t(e.get('excerpt'), 600)}", 700, fontType="Monospace",
                              size="Small"))
    body.append(_text(f"Engine: {_engine(rca)}", 200, isSubtle=True, size="Small"))
    return card(body)


def restricted_card(event: dict) -> dict:
    """Card for the admins: who asked about kubesupport itself, what, and why it was refused.
    event comes from boundaries.record."""
    question = (event.get("question") or "(empty)").replace("](", "] (")  # break markdown links
    body = [
        _header("Restricted question refused", "SEV2",
                "Someone asked kubesupport about itself and was told they are not authorised"),
        _facts([
            ("Asked by", event.get("user")),
            ("User ID", event.get("user_id")),
            ("When", timefmt.fmt(event.get("time"))),
            ("Why", event.get("reason")),
            ("Stopped by", event.get("stopped_by")),
        ]),
        _text("Question", weight="Bolder", spacing="Medium"),
        _text(question, 700),
    ]
    if event.get("last_card_this_hour"):
        body.append(_text(f"That was card {boundaries.MAX_CARDS_PER_HOUR} within an hour. Further refusals in this hour "
                          "are only logged and kept in the list (GET /security/restricted).", 300, isSubtle=True))
    return card(body)


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)[\s#]*$")
_OFFER = re.compile(r"^(if you(?:'d| would)? (?:want|like|need)|let me know|would you like|i can also|feel free)", re.I)


def tidy_answer(text: str) -> str:
    """Clean up an AI answer for the card: headings become bold lines (Teams draws "# x" as a
    huge title), a trailing "If you want ..." offer is dropped and blank lines are collapsed.
    Tables and code blocks are left alone."""
    lines, in_code = [], False
    for line in (text or "").strip().splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        heading = None if in_code else _HEADING.match(line)
        if heading:
            line = f"**{heading.group(1).strip().strip('*').strip()}**"
        if line.strip() or (lines and lines[-1].strip()):  # max one blank line in a row
            lines.append(line.rstrip())
    paragraphs = "\n".join(lines).strip().split("\n\n")
    last = paragraphs[-1].strip()
    if len(paragraphs) > 1 and _OFFER.match(last) and "|" not in last and "```" not in last and len(last.split()) <= 40:
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def answer_card(question: str, asked_by: str | None, answer: str) -> dict:
    return card([
        _header("kubesupport answer", None, f"Asked by {asked_by or 'someone'}"),
        _text(f"> {question}", 400, isSubtle=True),
        _text(tidy_answer(answer), 12000),
    ])


def activity_card(groups: list, total: int, more: int, highlight: bool, window_seconds: int = 60) -> dict:
    """groups is [(title, [line, ...]), ...] in display order."""
    when = "in the last minute" if window_seconds == 60 else f"in the last {window_seconds} s"
    body = [_header(f"Cluster activity: {total} event(s)", "SEV2" if highlight else "SEV3",
                    f"{when} - who did what (read-only notice; nothing was blocked)")]
    for title, lines in groups:
        body.append(_text(title, weight="Bolder", spacing="Medium"))
        body.append(_bullets(lines, limit_items=len(lines), limit_chars=300))
    if more:
        body.append(_text(f"... and {more} more. Ask in Teams: @kubesupport activity", 200, isSubtle=True))
    return card(body)


def test_card() -> dict:
    return card([
        _header("Test alert from kubesupport", "SEV4", "If you can read this, Teams alerts work."),
        _text("Real alerts look like this, with the namespace, workload, problem, root cause and fix steps."),
    ])
