"""Answers people can take in at a glance: the format rule given to the model, the one-time
shortening of over-long answers, and the clean-up of answer text for the Teams card."""
import agent
import cards
from conftest import fake_llm
from test_agent import resp, text, tool_use
from test_provider_cursor import fake_sdk, use_cursor

SHORT = "**50 images scanned; 1 has critical findings (uat/uat-alert-service).**"
LONG = ("Trivy scans are running via Trivy Operator in trivy-system. " * 20).strip()  # 180 words of prose


def test_shortening_starts_at_one_and_a_half_times_the_limit():
    limit = agent.ANSWER_MAX_WORDS
    assert not agent._too_long(" ".join(["word"] * int(limit * 1.5)))   # a little over the limit: left alone
    assert agent._too_long(" ".join(["word"] * (int(limit * 1.5) + 1)))  # clearly over: one rewrite
    assert agent._too_long(LONG)

# What the agent actually posted (condensed): intro, a heading, a table, paragraphs, RCA-style lines, an offer.
SCREENSHOT_ANSWER = """Trivy scans are running via **Trivy Operator** in `trivy-system` (operator 0.36.1, scanner Trivy 0.76.1). \
The operator and `trivy-server-0` are Running; the latest scan job Succeeded at 2026-11-19 14:32 IST.

There are **50** `VulnerabilityReport` objects cluster-wide (images on running workloads). ClusterComplianceReport / \
ClusterVulnerabilityReport are not visible from this service account (403). There are also 62 ExposedSecretReport objects.

# Application workloads (summary counts)

| Workload | Image | Updated (IST) | Critical | High | Medium |
|---|---|---|---|---|---|
| uat/uat-alert-service | alert-service:uat-236 | 2026-11-19 13:57 | 12 | 41 | 56 |
| kubesupport/agent | kubesupport:latest | 2026-11-19 14:32 | 0 | 6 | 15 |

**Highest risk:** `uat/uat-alert-service` — 141 counted findings (12 critical). Example critical: CVE-2026-111497 \
(brotli-libs, fix 1.2.1-r0; installed 1.2.0-r0). Many OpenSSL/libexpat issues on Alpine 3.23.5.

## Other namespaces in the scan set

Reports also exist for kube-system, amazon-cloudwatch, aws-secrets-manager, headlamp, backendapi, uat and kubesupport. \
Listing showed 50 reports total; per-image counts for every system image were not fetched individually.

**SEV4** (informational posture): scanning is healthy; no scan failures observed.

**Confidence: high** — data from live VulnerabilityReport / operator status in the cluster.

If you want full CVE lists for a specific report, name the namespace and workload (for example uat + uat-alert-service)."""


# ─── The rule given to the model ─────────────────────────────────────────────

def test_answer_format_rule():
    rules = agent.SYSTEM
    for must in ("ONE bold sentence with the key numbers", "At most 5 columns and 10 rows", "+N more",
                 "at most 5 bullets", '"**Next:** ..."', "severity and confidence lines", "closing offers",
                 f"under {agent.ANSWER_MAX_WORDS} words", '"**Not checked:** ..."'):
        assert must in rules, must
    assert "no tables" not in rules
    # the example's table starts at the line start: an indented table would render as a code block
    assert "\n| Pod | State | Restarts |\n|---|---|---|\n" in rules


def test_prose_words_counts_only_what_has_to_be_read():
    answer = ("**2 of 14 pods are failing.**\n\n| Pod | State |\n|---|---|\n| api-1 | CrashLoopBackOff |\n\n"
              "```\nkubectl -n payments get pods\n```\n\n**Next:** ask rca payments/api-1.")
    assert agent.prose_words(answer) == 10  # the first sentence (6) and the Next line (4)
    assert agent.prose_words("") == 0 and agent.prose_words(None) == 0


def test_the_screenshot_answer_counts_as_too_long_and_the_target_does_not():
    assert agent.prose_words(SCREENSHOT_ANSWER) > 150 and agent._too_long(SCREENSHOT_ANSWER)
    target = SHORT + "\n\n| Workload | Critical | High | Medium |\n|---|---|---|---|\n| uat/uat-alert-service | 12 | 41 | 56 |\n" \
        "| kubesupport/agent | 0 | 6 | 15 |\n\n+48 more with no critical or high findings\n\n" \
        "**Next:** upgrade brotli-libs to 1.2.1-r0 in uat-alert-service."
    assert agent.prose_words(target) < agent.ANSWER_MAX_WORDS and not agent._too_long(target)


# ─── Shortening: chat providers (Claude, Groq, Gemini, ...) ──────────────────

def scripted_chat(monkeypatch, replies):
    """agent.chat with scripted model replies; returns (last message sent per call)."""
    monkeypatch.setattr(agent, "llm", fake_llm())
    sent = []

    def fake_create(messages, tools, **kw):
        sent.append(messages[-1]["content"])
        return replies.pop(0)
    monkeypatch.setattr(agent, "_create", fake_create)
    return sent


def test_long_answer_is_shortened_once_and_history_keeps_only_the_result(monkeypatch):
    sent = scripted_chat(monkeypatch, [resp("end_turn", [text(LONG)]), resp("end_turn", [text(SHORT)])])
    history = []
    assert agent.chat(history, "trivy results?") == SHORT
    assert sent == ["trivy results?", agent.SHORTEN_NUDGE]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == [{"type": "text", "text": SHORT}]  # follow-ups build on the short answer


def test_short_answer_costs_no_second_call(monkeypatch):
    sent = scripted_chat(monkeypatch, [resp("end_turn", [text(SHORT)])])
    assert agent.chat([], "trivy results?") == SHORT and len(sent) == 1


def test_a_rewrite_that_is_not_shorter_is_ignored(monkeypatch):
    for bad in (resp("end_turn", [text(LONG + " and more words")]),             # longer
                resp("end_turn", []),                                            # empty
                resp("tool_use", [tool_use("list_pods", {"namespace": "x"})])):  # tried to use tools again
        scripted_chat(monkeypatch, [resp("end_turn", [text(LONG)]), bad])
        history = []
        assert agent.chat(history, "q") == LONG
        assert [m["role"] for m in history] == ["user", "assistant"]


def test_failed_rewrite_keeps_the_answer(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    replies = [resp("end_turn", [text(LONG)])]

    def fake_create(messages, tools, **kw):
        if not replies:
            raise RuntimeError("rate limit")
        return replies.pop(0)
    monkeypatch.setattr(agent, "_create", fake_create)
    assert agent.chat([], "q") == LONG


def test_limit_zero_switches_shortening_off(monkeypatch):
    monkeypatch.setattr(agent, "ANSWER_MAX_WORDS", 0)
    sent = scripted_chat(monkeypatch, [resp("end_turn", [text(LONG)])])
    assert agent.chat([], "q") == LONG and len(sent) == 1


# ─── Shortening: agent providers (Cursor) ────────────────────────────────────

def test_long_cursor_answer_is_sent_back_once(monkeypatch):
    def script(a, message):
        if len(a.sent) == 1:
            a.call("list_resources", {"kind": "vulnerabilityreports"})
            return LONG
        return SHORT
    sdk = fake_sdk(script=script)
    use_cursor(monkeypatch, sdk)
    history = []
    assert agent.chat(history, "trivy results?") == SHORT
    assert sdk.created[0].sent[1] == agent.SHORTEN_NUDGE and len(sdk.created[0].sent) == 2
    assert history[-1]["content"] == [{"type": "text", "text": SHORT}]


def test_cursor_rewrite_that_comes_back_empty_keeps_the_answer(monkeypatch):
    sdk = fake_sdk(script=lambda a, message: LONG if len(a.sent) == 1 else "")
    use_cursor(monkeypatch, sdk)
    assert agent.chat([], "q") == LONG


# ─── Clean-up for the Teams card ─────────────────────────────────────────────

def test_headings_become_bold_lines():
    assert cards.tidy_answer("# Application workloads (summary counts)\ntext") == \
        "**Application workloads (summary counts)**\ntext"
    assert cards.tidy_answer("## **Other namespaces** ##") == "**Other namespaces**"


def test_closing_offer_is_dropped_but_content_is_not():
    assert cards.tidy_answer("**2 pods failing.**\n\nIf you want the logs, ask for them.") == "**2 pods failing.**"
    assert cards.tidy_answer("**2 pods failing.**\n\nLet me know if you need more.") == "**2 pods failing.**"
    only = "If you want fewer restarts, raise the memory limit of api to 512Mi."
    assert cards.tidy_answer(only) == only  # the whole answer: never dropped
    keep = "**2 pods failing.**\n\n**Next:** if you want it fixed now, roll back api."
    assert cards.tidy_answer(keep) == keep


def test_tables_and_code_blocks_are_untouched_and_blank_lines_collapse():
    table = "| Pod | State |\n|---|---|\n| api-1 | CrashLoopBackOff |"
    code = "```\n# a comment, not a heading\nkubectl get pods\n```"
    assert cards.tidy_answer(f"**Result.**\n\n\n\n{table}\n\n\n{code}\n") == f"**Result.**\n\n{table}\n\n{code}"


def test_the_screenshot_answer_after_clean_up():
    tidy = cards.tidy_answer(SCREENSHOT_ANSWER)
    assert "\n# " not in tidy and "\n## " not in tidy
    assert "**Application workloads (summary counts)**" in tidy and "**Other namespaces in the scan set**" in tidy
    assert not tidy.rstrip().endswith("uat-alert-service).") and "If you want full CVE lists" not in tidy
    assert "| uat/uat-alert-service | alert-service:uat-236 |" in tidy  # the table is kept as it was


def test_answer_card_shows_the_cleaned_text():
    card = cards.answer_card("trivy results?", "Asha", "# Results\n\n**All clean.**\n\nLet me know if you want details.")
    assert card["body"][-1]["text"] == "**Results**\n\n**All clean.**"
