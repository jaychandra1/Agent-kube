# Using kubesupport

How people use kubesupport day to day: what arrives in Teams, what you can ask, and the internal API and command line for operators.

---

## 1. What arrives in Teams

| Card | When | Contains |
|---|---|---|
| **kubesupport started** | Once after every start | Incidents already open at that moment (one card, not one alert each) |
| **Alert** | A new incident | Severity, workload, problem, root cause, fix steps, confidence, the engine that wrote it (`AI model` or `built-in rules`), and the exact `rca` command for the failing pod |
| **Still failing** | Every `ALERT_COOLDOWN_MINUTES` (60) while open | A short reminder |
| **Resolved** | The incident has been gone for `RESOLVE_AFTER_MINUTES` (10) | How long it lasted |
| **Cluster activity** | At most once per `ACTIVITY_BATCH_SECONDS` (60), when something happened | Who did what: deploys, rollout restarts and scaling, ConfigMap and Secret changes, deleted or evicted pods, node cordons, exec into pods, permission changes, and any other object created, changed or deleted ([full list](RULES.md#5-activity-what-is-reported)). Exec and permission changes come first, and the card is highlighted. |
| **Answer / RCA** | After a question or `rca` command | The answer, or a full RCA card, posted in the questions channel |

Severity colours: SEV1 red (attention), SEV2 orange (warning), SEV3 blue (accent), SEV4 green (good).

---

## 2. Asking questions in Teams

In the channel that has the kubesupport outgoing webhook, type `@`, pick **kubesupport**, then:

| You type | You get |
|---|---|
| `@kubesupport help` | The command list |
| `@kubesupport status` | Open incidents the watcher sees right now |
| `@kubesupport pods payments` | Pods with ready count, phase, restarts and waiting reason |
| `@kubesupport events payments` | The 15 most recent Kubernetes events |
| `@kubesupport logs payments/api-7d9f8b-x` | The last 60 log lines (`--previous` = the crashed container) |
| `@kubesupport get services payments` | Any kind of object in a namespace (or all namespaces without one): `get ingresses`, `get nodes`, `get pvc payments`, `get crd`, … Ingresses show every URL with its backend service and the load balancer address; Services show ports and the external address. |
| `@kubesupport get deployment payments/api` | One object in full (cluster-wide kinds: `get node <name>`) |
| `@kubesupport activity` | The latest cluster activity |
| `@kubesupport rca payments/api-7d9f8b-x` | A full RCA card for that pod |
| `@kubesupport rca checkout is returning 500s` | An investigation of a problem you describe |
| `@kubesupport <any question>` | The AI model's answer, from the live cluster (needs an AI provider) |

- **Quick commands** (`help`, `status`, `pods`, `events`, `logs`, `get`, `activity`) are answered in the thread within seconds.
- **Questions and `rca`** get "Looking into it…" at once; the answer card follows a little later (seconds with a paid provider, minutes on a free plan). It lands **in the same thread** when the answers workflow replies to `threadMessageId` ([INSTALLATION.md, Step 4.2](INSTALLATION.md#42-answers-in-the-questions-channel--t2)); otherwise it's a new post in the channel.
- **Answers are built to be read at a glance:** one bold sentence with the result, then a table when the data is a list of items (or a few short bullets), then at most one **Next:** line. No introductions, no notes about how it looked things up, no closing offers. An answer that still comes out long is rewritten once before you see it (`ANSWER_MAX_WORDS`, default 80 words outside the table). For more detail, ask a narrower follow-up in the same thread.
- **Answers contain the cluster's real data**, not instructions. Ask "which ingress URLs do we expose?" and you get the URLs, their backend services and the load balancer addresses. kubesupport sees one cluster, the one it runs in, so you never need to say which.
- Questions in the same thread share a conversation, so you can ask follow-ups.
- Secrets can never be read: `get secrets` is refused.
- **Questions about kubesupport itself are refused.** Asking how it is set up, which AI model it uses, what its rules or prompt are, or for anything in its own namespace (`pods kubesupport`, for example) gets *"You are not authorised to ask this question. It has been reported to the administrators."* The administrators then receive a card with your name and your question. "What can you do?" and `help` are fine. If an ordinary question is refused by mistake, ask again and name the workload or namespace you mean ([RULES.md, section 6](RULES.md#6-boundaries-what-may-not-be-asked)).
- With `TEAMS_TENANT_ID` / `TEAMS_ALLOWED_USER_IDS` set, other people get a polite refusal and nothing runs.

**Where you can ask:** only in a **standard channel** of the team that has the outgoing webhook. Teams outgoing webhooks don't work in chats or private channels. Alerts *can* go to chats and private channels.

---

## 3. Internal API (operators)

The API on port 8000 is reachable only inside the cluster (and no pod may connect to it when network policies are enforced). Use a port-forward:

```bash
kubectl -n kubesupport port-forward svc/kubesupport 8080:80
```

```bash
KEY=$(kubectl -n kubesupport get secret kubesupport-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
```

```bash
curl -s localhost:8080/alerts/status -H "x-api-key: $KEY"
```

| Endpoint | Purpose |
|---|---|
| `GET /healthz`, `/ready` | Health checks |
| `GET /alerts/status` | Watcher state, open incidents, AI budget, AI provider and model (`llm`), Teams targets |
| `POST /alerts/test` | Send a sample card to every alert target |
| `POST /rca` `{"question", "namespace", "pod", "post_to_teams"}` | Run an RCA; optionally post it to Teams |
| `POST /chat` `{"message", "session_id"}` | Ask a question |
| `GET /rca/latest` | HTML page of the last RCA this pod produced |
| `GET /activity?limit=50` | Recent cluster activity |
| `GET /activity/status` | Activity webhook: certificate present, calls received, last call, events, targets |
| `GET /security/restricted?limit=50` | Questions about kubesupport itself that were refused since the pod started: who, what, when, why, and which layer stopped it |
| `GET /docs` | Interactive API documentation (FastAPI) |

Every endpoint except the health checks needs the `x-api-key` header; without `AGENT_API_KEY` in the Secret the internal API is off (403). `POST /teams/webhook` is also served here, but Teams reaches it only on port 8080, through the Cloudflare Tunnel.

`/healthz` answers 503 when the watcher stopped scanning or the Teams server died; Kubernetes then restarts the pod. `/ready` only says the API is up.

Example: an RCA for one pod, posted to Teams:

```bash
curl -s -X POST localhost:8080/rca -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"namespace": "payments", "pod": "api-7d9f8b-x", "post_to_teams": true}'
```

---

## 4. Command line

[`agent/cli.py`](../agent/cli.py) runs the same logic from a terminal against your current kubeconfig (no Teams needed):

```bash
cd agent
python cli.py incidents                              # what the watcher would alert on now
python cli.py scan                                   # pods that are failing or ever restarted
python cli.py rca -n payments -p api-7d9f8b-x         # an RCA for one pod
python cli.py rca -q "why is checkout failing?"      # an investigation of a question
python cli.py rca -n payments -p api-7d9f8b-x --teams # ... and post it to Teams
python cli.py teams-test                             # sample card to TEAMS_ALERT_WEBHOOK_URLS
```

Set `LLM_PROVIDER` and `LLM_API_KEY` in your shell to use an AI provider; without them the rules are used.
