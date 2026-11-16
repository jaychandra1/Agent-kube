# kubesupport

The Python app that runs in the cluster. Full documentation: [docs/IMPLEMENTATION_GUIDE.md](../docs/IMPLEMENTATION_GUIDE.md).

| File | What it does |
|---|---|
| `main.py` | Entrypoint (`python main.py`): the internal API on :8000, the Teams webhook alone on :8080 (the Cloudflare Tunnel's target) and, with a mounted certificate, the activity webhook on :8443 |
| `app.py` | FastAPI apps: the internal API (`app`), and `teams_app`, which serves only `/teams/webhook`; starts the watcher and the activity feed |
| `activity.py` | Activity webhook: turns API-server admission requests into "who did what" events; one Teams card per minute |
| `watcher.py` | Every minute: find incidents, run an RCA for new ones, post Teams alerts, post "resolved" notices |
| `tools.py` | Read-only Kubernetes tools, including any kind of object (`list_resources`, `get_resource`); namespace allowlist, redaction, `detect_incidents()` |
| `agent.py` | AI tool-use loop that ends in a structured RCA; picks the provider (Groq, Claude, OpenAI-compatible); rule-based fallback |
| `llm_openai.py` | Groq / OpenAI-compatible client: converts the conversation, keeps requests within rate limits, retries |
| `teams.py` | Teams: HMAC check, commands, 5-second reply rule, posting cards to Workflows webhooks |
| `cards.py` | Adaptive Cards for alerts, resolved notices, RCAs, answers and cluster activity |
| `report.py`, `templates/` | HTML version of an RCA (internal `/rca/latest`) |
| `cli.py` | The same features from a terminal |

## Run it locally against any cluster

Uses your current kubeconfig. Teams settings are optional; without them the agent only logs.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
export ALLOWED_NAMESPACES=default                       # namespaces to read and watch
export GROQ_API_KEY=...                                 # optional: AI answers and RCAs (or ANTHROPIC_API_KEY)
export TEAMS_ALERT_WEBHOOK_URLS=https://...             # optional: Workflows webhook for alerts
export REPORT_DIR=$PWD/reports
uvicorn app:app --reload                                # http://localhost:8000/docs
```

```bash
python cli.py incidents                                 # what the watcher would alert on now
python cli.py rca -n default -p <pod>                   # RCA for one pod
python cli.py teams-test                                # sample card to your Teams alert webhook
```

## Tests

```bash
pytest tests -q            # no cluster, AI model or Teams needed
flake8 . --select=F,E9
```
