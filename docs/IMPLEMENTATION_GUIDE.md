# Agent-Kube — kubesupport for EKS with Microsoft Teams

This is the all-in-one guide to the repository: what the agent does, how it works, how to set it up in your EKS cluster and in Microsoft Teams, and how to run and troubleshoot it.

> Written against the code as of 2026-11-13. The Teams setup uses only features you configure in the Teams UI: **no Azure Bot Service, no Entra app registration, no Teams app package**.

---

## Contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Repository map](#3-repository-map)
4. [How the agent works](#4-how-the-agent-works)
5. [Setup, step by step](#5-setup-step-by-step)
6. [Using it from Teams](#6-using-it-from-teams)
7. [Configuration reference](#7-configuration-reference)
8. [Operations and troubleshooting](#8-operations-and-troubleshooting)
9. [Local development](#9-local-development)
10. [Limitations and known issues](#10-limitations-and-known-issues)
11. [Cost](#11-cost)
12. [Glossary](#12-glossary)

---

## 1. What it does

kubesupport runs as **one pod inside your EKS cluster**:

- **It watches the cluster.** Every minute it checks the namespaces you choose for pods that are crash-looping, OOM-killed, unable to pull their image, missing a ConfigMap/Secret, stuck Pending, or nodes that are NotReady.
- **It diagnoses new problems.** For each new incident it produces a root cause analysis (RCA): with an **AI model** (Groq, Claude or any OpenAI-compatible service, whichever key you set), or with built-in rules.
- **It alerts you in Microsoft Teams.** An alert card with the root cause and fix steps goes to your Teams chat (and any channels you add). You get a reminder if the incident is still open after an hour, and a "Resolved" card when it's gone.
- **It answers questions from Teams.** In a Teams channel you type `@kubesupport status`, `@kubesupport logs payments/api-7d9f8b-x`, or a plain question such as `@kubesupport why does the orders deployment keep restarting?`, and it answers from the live cluster.
- **It reports cluster activity.** Once a minute, a "Cluster activity" card says **who** exec'd into a pod, created or deleted a service or app, changed permissions, or changed an image or replica count ([4.5](#45-cluster-activity-notices)).
- **It can read the whole cluster, read-only.** Every object in every namespace, plus nodes, RBAC and CRDs, but never Secrets. It never changes anything.

---

## 2. Architecture

```text
 ┌──────────────────────── Microsoft Teams ───────────────────────────┐
 │  your chat / channels   ◀── alert cards + activity cards ──────┐     │
 │  #sre channel  "@kubesupport status" ──┐   ◀── long answers ──┐ │     │
 └────────────────────────────────────────┼─────────────────────┼─┼─────┘
     outgoing webhook (HMAC-signed POST)   │   Workflows webhooks (HTTPS POST, outbound)
                                           ▼                     │ │
 your ingress controller ── only /teams/webhook                  │ │
                                           │                     │ │
 ┌──────────── EKS cluster ── namespace kubesupport ─────────────┼─┼─────┐
 │  kubesupport pod (1 replica, Python 3.12)                      │ │     │
 │   ├─ :8000 /teams/webhook  verify HMAC → command or question ──┘ │     │
 │   ├─ watcher: every 60 s detect → RCA → alert card ──────────────┤     │
 │   ├─ :8443 /admission ◀── Kubernetes API server (webhook):       │     │
 │   │     exec, creates, deletes, RBAC, updates, with WHO;          │     │
 │   │     always "allowed" → one activity card per minute ──────────┘     │
 │   ├─ AI tool-use loop (Groq or Claude) or rule-based diagnosis         │
 │   ├─ internal API (ClusterIP): /rca /chat /alerts/* /activity*         │
 │   └─ read-only RBAC ("view" + cluster-read) ──▶ Kubernetes API:        │
 │        every object in every namespace, never Secrets                  │
 └────────────────────────────────────────────────────────────────────────┘
          ▲ public image from Docker Hub (built and pushed by GitHub Actions)
```

**Alert flow:** watcher finds a new incident → RCA → Adaptive Card → POST to your Workflows webhook(s) → card appears in Teams.

**Question flow:** you @mention the agent in a channel → Teams POSTs the message to `https://<your host>/teams/webhook`, signed with HMAC → the agent checks the signature →
- **quick commands** (`status`, `pods`, `events`, `logs`) are answered in the same thread within Teams' 5-second limit;
- **slow work** (AI questions, `rca`) gets "Looking into it…" right away, and the full answer arrives a little later as a card in the channel, through the channel's Workflows webhook.

**Activity flow:** someone runs `kubectl exec`, creates a Service, changes a ClusterRoleBinding, … → the API server sends kubesupport a copy (who, what, where) on port 8443 → kubesupport answers "allowed" immediately → once a minute everything collected goes to Teams as one "Cluster activity" card.

**Why this design:**
- Teams bots need **Azure Bot Service**, which isn't available here.
- An **Entra app on its own can't post ordinary Teams messages**: Microsoft Graph only allows app-only sending for migrations.
- Teams' own **Workflows webhooks** (for posting) and **outgoing webhooks** (for questions) need neither, and are set up with a few clicks.

---

## 3. Repository map

```text
Agent-kube/
├── agent/                          The agent (Python 3.12)
│   ├── main.py                     Entrypoint: API on :8000, activity webhook on :8443 (HTTPS)
│   ├── app.py                      FastAPI: /teams/webhook + internal API; starts the watcher and activity feed
│   ├── activity.py                 Activity webhook: who exec'd/created/deleted/changed what → one card per minute
│   ├── watcher.py                  Incident loop: new → RCA → alert; reminders; resolved; startup summary
│   ├── tools.py                    Read-only Kubernetes tools (incl. any kind of object) + detect_incidents()
│   ├── agent.py                    AI tool-use loop → structured RCA; provider choice; rule-based fallback
│   ├── llm_openai.py               Groq / OpenAI-compatible model client: request limits, retries
│   ├── teams.py                    Teams: HMAC check, commands, 5-second rule, posting cards
│   ├── cards.py                    Adaptive Cards (alert, resolved, startup, RCA, answer, activity)
│   ├── report.py, templates/       HTML version of an RCA (internal /rca/latest)
│   ├── cli.py                      Same features from a terminal
│   ├── runbooks/                   Short notes the AI model can search
│   ├── tests/                      pytest suite (no cluster, AI model or Teams needed)
│   ├── Dockerfile, requirements*.txt
├── deploy/deployment.yaml          Install with kubectl: namespace, cluster-wide read-only RBAC, settings, Deployment, Service
├── deploy/activity-webhook.yaml    Activity webhook (Service + ValidatingWebhookConfiguration); applied by webhook-cert.sh
├── deploy/webhook-cert.sh          Creates the webhook's certificate, applies activity-webhook.yaml, restarts kubesupport
├── deploy/ingress.yaml             Optional: exposes only /teams/webhook, for questions from Teams
├── .github/workflows/ci.yml        Lint + tests (incl. manifest checks); on main: build & push the image to Docker Hub
├── Configuration.md                Step-by-step install and configuration
└── docs/IMPLEMENTATION_GUIDE.md    This guide
```

---

## 4. How the agent works

### 4.1 Incident detection (the watcher)

[`tools.detect_incidents()`](../agent/tools.py) reads pods in `ALLOWED_NAMESPACES` (all namespaces with `*`) plus all nodes. It returns incidents keyed `namespace/workload/problem`, for example `payments/Deployment/api/CrashLoop`. Pods of the same Deployment, StatefulSet, DaemonSet or Job are grouped into **one** incident.

| Problem | Raised when | Notes |
|---|---|---|
| `CrashLoop` | A container is waiting in `CrashLoopBackOff`, **or** its restart count went up since the last scan (non-zero exit) | Clean exits (code 0) are ignored |
| `OOMKilled` | Same as above, but the last termination was `OOMKilled` | |
| `ImagePull` | Waiting in `ImagePullBackOff`, `ErrImagePull` or `InvalidImageName` | |
| `ContainerConfig` | Waiting in `CreateContainerConfigError` or `CreateContainerError` | Usually a missing ConfigMap/Secret |
| `ContainerStart` | Waiting in `RunContainerError` | |
| `Pending` | Pod Pending longer than `PENDING_GRACE_MINUTES`, without one of the problems above | Shows the scheduler's reason |
| `NodeNotReady` | A node's `Ready` condition is not `True` | Cluster-wide |

Old restarts don't count. A pod that crashed once last week is not an incident; only restarts **since the previous scan** are, or, for a pod seen for the first time, a crash in the last few minutes.

[`watcher.py`](../agent/watcher.py) compares each scan with what it already knows:

| Situation | What you get in Teams |
|---|---|
| First scan after the agent starts | **One** "kubesupport started — N ongoing incidents" card (not N alerts) |
| New incident | Alert card with the RCA (root cause, fix steps, confidence, engine) |
| Still open after `ALERT_COOLDOWN_MINUTES` (60) | Short "Still failing" reminder |
| Gone for `RESOLVE_AFTER_MINUTES` (10) | "Resolved" card. The delay stops a crash-looping pod, which looks fine between restarts, from flapping. |

RCAs run one at a time. The AI model is used for at most `MAX_LLM_RCA_PER_HOUR` alerts per hour (default 10); after that alerts use the rule-based RCA until the hour rolls over. All state is in memory: the agent never writes to the cluster.

### 4.2 Diagnosis: AI model or rules

**With an AI model** ([`agent._llm_rca`](../agent/agent.py)):
- The model gets the question (for alerts: the pod, workload and problem) and a limited number of rounds of read-only tools (`LLM_MAX_STEPS`: 8 on Groq, 14 on Claude):
  - `list_pods`, `describe_pod`, `get_events`, `get_pod_logs` (including `previous=true` for the crashed container);
  - `get_deployment` (rollout history), `top_pods`, `search_runbooks` (the notes in `agent/runbooks/`), `scan_cluster_for_issues`.
- It must finish by calling `submit_rca` with every field the cards need: title, severity SEV1–4, root cause, evidence, resolution, prevention, confidence, a plain-language summary. With Claude a strict schema guarantees the shape. With other models kubesupport checks the answer, fills in missing fields, and rejects an RCA without a title or root cause (`normalize_rca`).
- If anything fails (wrong key, rate limit, refusal, no `submit_rca` within the step limit), the agent falls back to the rules and says so in `llm_error`.

**Which AI model.** `LLM_PROVIDER` is `auto` by default: the first key found in the Secret, in this order:

| Provider | Key in the Secret | Default `AGENT_MODEL` | Notes |
|---|---|---|---|
| Claude | `ANTHROPIC_API_KEY` | `claude-opus-5-5` | Effort `high` (`AGENT_EFFORT`); server-side refusal fallback on (`AGENT_FALLBACKS`) |
| Groq | `GROQ_API_KEY` | `openai/gpt-oss-120b` | Reasoning effort `medium`. Each request (conversation plus tool definitions) is kept under about 14,000 characters and each reply under 2,500 tokens, so one request fits the free plan's per-minute token limit. Older tool results are shortened first. kubesupport waits and retries on "too many requests"; a request Groq calls too large is resent with less history. When the wait is longer than a minute (daily limit), it gives up at once and the rules take over. |
| Any OpenAI-compatible service | `LLM_API_KEY`, plus `LLM_BASE_URL` in the ConfigMap | none: set `AGENT_MODEL` | For example OpenAI, or a self-hosted vLLM or Ollama (`http://<host>:11434/v1`, no key needed) |

Other Groq models work too: set `AGENT_MODEL`, e.g. `llama-3.3-70b-versatile` or `openai/gpt-oss-20b`. Answer quality depends on the model.

**Without an AI model** ([`agent.generate_heuristic_rca`](../agent/agent.py)): rules look at the pod's container states, last termination, events and logs, and produce one of:
- *Image pull failure*, *Container configuration error*, *Pod cannot be scheduled*;
- *OOMKilled* (SEV1), *Database connection pool exhaustion*;
- *Crash and restart loop*;
- or *healthy* when nothing is wrong.

Every RCA carries `generated_by` (`llm:<provider>:<model>`, e.g. `llm:groq:openai/gpt-oss-120b`, or `heuristic`), so you always know which one you're reading.

### 4.3 Teams

**Alerts and long answers go out** through **Workflows webhooks**: an HTTPS URL that the Teams *Workflows* app gives you. The agent POSTs an Adaptive Card to it, and the card appears in the chat or channel you chose when creating the workflow. Templates: "Send webhook alerts to a chat" and "Send webhook alerts to a channel". No premium licence is needed.
- **Targets:** a webhook can post to a chat, a standard channel or a **private channel**; Microsoft's docs state "Workflows webhooks support private channels".
- **Several targets:** list them in `TEAMS_ALERT_WEBHOOK_URLS`, each optionally named (`chat=https://…,oncall-private=https://…`), so logs and `/alerts/status` say which is which without showing the URLs.
- **Retries:** Teams throttles more than 4 requests per second per webhook. The agent retries a throttled or failed post up to 3 times, with backoff.

**Questions come in** through a Teams **outgoing webhook**, which a team owner creates in the team's settings. It works like this:
1. When someone @mentions it in a **public channel** of that team, Teams sends the message to the callback URL.
2. The request carries an `Authorization: HMAC …` header, computed with the webhook's security token; the agent rejects anything without a valid signature.
3. Teams waits **5 seconds** for the reply. [`teams.py`](../agent/teams.py) answers quick commands within 4 seconds. For slow work it replies "Looking into it…" and posts the answer later through `TEAMS_REPLY_WEBHOOK_URL`.

Optional extra checks: `TEAMS_TENANT_ID` (only your organization) and `TEAMS_ALLOWED_USER_IDS` (only these people, by Entra object ID).

### 4.4 Safety model

| Layer | What it does |
|---|---|
| Kubernetes RBAC | Read-only and cluster-wide: the built-in `view` role in every namespace, plus `kubesupport-cluster-read` (nodes, namespaces, volumes, storage, CRDs, RBAC, webhook configurations, metrics). Only `get/list/watch`, **never Secrets**, never writes. |
| Namespace allowlist | Tools refuse namespaces outside `ALLOWED_NAMESPACES`; Teams users get "not allowed". |
| Redaction | Passwords, tokens, API keys, AWS access keys and JWTs in logs and in object listings (e.g. ConfigMap values) are replaced by `[REDACTED]` before the AI model or Teams sees them. |
| Prompt-injection guard | All tool output reaches the AI model wrapped in `<untrusted_data>`, with instructions never to follow text inside it. |
| Data sent to the AI provider | With a key set, the question and the (redacted) tool output, such as pod lists, events, log lines and object listings, go to that provider (Groq or Anthropic) over HTTPS. Without a key, nothing leaves the cluster except Teams cards. |
| Public exposure | Only `/teams/webhook` is exposed (Ingress path `Exact`), and every call needs a valid HMAC. Everything else is ClusterIP. |
| Internal API | `/rca`, `/chat`, `/alerts/*` require `x-api-key` when `AGENT_API_KEY` is set. |
| Activity webhook | Always answers "allowed". `failurePolicy: Ignore` and a 3 s timeout mean no API request is ever blocked or noticeably slowed, even when kubesupport is down. HTTPS with its own CA. For Secrets and ConfigMaps only the name is kept. |
| Container | Non-root (UID 10001), read-only root filesystem, no privilege escalation, all capabilities dropped. |

### 4.5 Cluster activity notices

[`activity.py`](../agent/activity.py) receives, through a *validating admission webhook* ([`deploy/activity-webhook.yaml`](../deploy/activity-webhook.yaml)), a copy of these API requests, including **who** made them:

| Reported | Example line |
|---|---|
| **Exec / attach / port-forward** into a pod | `alice@corp exec into pod payments/api-7d9f8b-x (container app): sh` |
| **Created/deleted** Services, Ingresses, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, Namespaces | `Created Service payments/api-public by alice@corp` |
| **Security changes:** Roles, RoleBindings, ClusterRoles, ClusterRoleBindings created/changed/deleted; ServiceAccounts, Secrets, ConfigMaps created/deleted | `Created ClusterRoleBinding temp-admin by bob@corp` |
| **Workload updates:** image changes and scaling (also `kubectl scale`) | `Updated Deployment payments/api image app: api:1.2 → api:1.3 by ci-deployer` |

- **Always allowed.** kubesupport answers "allowed" at once and does its own work afterwards. With `failurePolicy: Ignore`, requests go ahead even if kubesupport is down or slow.
- **Noise filtered out:**
  - callers starting with the prefixes in `ACTIVITY_IGNORE_USERS`: Kubernetes' and EKS's own components, such as the controllers that create pods and ReplicaSets, and kubesupport itself. CI/CD service accounts outside `kube-system` **are** reported;
  - dry runs;
  - updates that change neither an image nor a replica count;
  - namespaces in `ACTIVITY_IGNORE_NAMESPACES`.
- **Batched.** Every `ACTIVITY_BATCH_SECONDS` (60), one card goes to the alert targets (or `TEAMS_ACTIVITY_WEBHOOK_URLS`). Exec and security lines come first, and the card is highlighted when there are any. A card holds at most 30 lines, then "… and N more".
- **History.** The last 500 events are kept in memory: `@kubesupport activity` in Teams, or `GET /activity` internally.
- **Secrets.** The API server includes the whole object in a webhook request; kubesupport keeps only the name of a Secret or ConfigMap. To keep Secret objects away from kubesupport entirely, remove `secrets` from the webhook rules.
- **Setup.** [`deploy/webhook-cert.sh`](../deploy/webhook-cert.sh) creates a private CA and a certificate for `kubesupport-webhook.kubesupport.svc`, stores it in Secret `kubesupport-webhook-tls` (mounted into the pod), applies the webhook configuration with the CA filled in, and restarts kubesupport. [`main.py`](../agent/main.py) serves port 8443 only when that certificate is present.

---

## 5. Setup, step by step

The full checklist, with every command and a check after each step, is in [Configuration.md](../Configuration.md). In short:

1. **Image:** CI ([`ci.yml`](../.github/workflows/ci.yml)) builds `docker.io/<you>/kubesupport` for amd64 and arm64, and pushes it to your **public Docker Hub** repository. It needs the GitHub variable `DOCKERHUB_USERNAME` and the secret `DOCKERHUB_TOKEN`.
2. **Teams (clicks only):** you create three things:
   - a Workflows webhook for alerts to your chat;
   - a Workflows webhook for answers in the questions channel;
   - an outgoing webhook "kubesupport" in that channel's team, with callback `https://<host>/teams/webhook`.
3. **Secret** `kubesupport-secrets` in namespace `kubesupport`, with the keys `GROQ_API_KEY` (or `ANTHROPIC_API_KEY`), `AGENT_API_KEY`, `TEAMS_ALERT_WEBHOOK_URLS`, `TEAMS_REPLY_WEBHOOK_URL` and `TEAMS_OUTGOING_WEBHOOK_SECRET` (all optional).
4. **Edit [`deploy/deployment.yaml`](../deploy/deployment.yaml):** the image and `CLUSTER_NAME` (optionally `ALLOWED_NAMESPACES`). Then `kubectl apply -f deploy/deployment.yaml`.
5. **Activity notices:** `bash deploy/webhook-cert.sh`.
6. **Questions from Teams:** edit and apply [`deploy/ingress.yaml`](../deploy/ingress.yaml), which exposes only `/teams/webhook`, and point DNS at it.
7. **Test:**
   - `POST /alerts/test` through a port-forward;
   - `@kubesupport help` in the channel;
   - create and delete a test Service, and expect an activity card;
   - a deliberately crashing deployment.

### What `deploy/deployment.yaml` creates

| Object | Purpose |
|---|---|
| Namespace `kubesupport` | Where the agent runs (Pod Security level "restricted") |
| ServiceAccount `kubesupport` | The agent's identity for the Kubernetes API |
| ClusterRoleBinding `kubesupport-view` → built-in `view` | Read every namespaced object in every namespace (never Secrets) |
| ClusterRole + ClusterRoleBinding `kubesupport-cluster-read` | Read nodes, namespaces, volumes, storage classes, CRDs, RBAC, webhook configurations, metrics |
| ConfigMap `kubesupport-config` | Plain settings ([section 7](#7-configuration-reference)) |
| Deployment `kubesupport` | One pod, `Recreate` strategy, non-root, read-only filesystem; ports 8000 (API) and 8443 (activity webhook); settings from the ConfigMap and the Secret; the webhook certificate is mounted when present |
| Service `kubesupport` | ClusterIP, port 80 → 8000 (internal API; the Ingress points here) |

`deploy/activity-webhook.yaml` (applied by `webhook-cert.sh`) adds Service `kubesupport-webhook` (443 → 8443) and the cluster-wide ValidatingWebhookConfiguration `kubesupport-activity`.

---

## 6. Using it from Teams

Mention the agent in the channel that has the outgoing webhook:

| You type | You get |
|---|---|
| `@kubesupport help` | The command list |
| `@kubesupport status` | Open incidents the watcher sees right now |
| `@kubesupport pods payments` | Pods with ready count, phase, restarts and waiting reason |
| `@kubesupport events payments` | The 15 most recent Kubernetes events |
| `@kubesupport logs payments/api-7d9f8b-x` | The last 60 log lines (`--previous` = the crashed container) |
| `@kubesupport get services payments` | Any kind of object in a namespace (or all namespaces without one): `get ingresses`, `get nodes`, `get pvc payments`, `get crd`, … |
| `@kubesupport get deployment payments/api` | One object in full (cluster-wide kinds: `get node <name>`) |
| `@kubesupport activity` | The latest cluster activity: exec into pods, created/deleted objects, security changes, updates |
| `@kubesupport rca payments/api-7d9f8b-x` | A full RCA card for that pod (posted to the channel) |
| `@kubesupport rca checkout is returning 500s` | An investigation of a problem you describe |
| `@kubesupport <any question>` | The AI model's answer, using the live cluster (posted to the channel) |

- Questions in the same thread share a conversation, so you can ask follow-ups.
- Alert cards end with the exact `rca` command for the failing pod.

**Internal API** (ClusterIP; use `kubectl port-forward`):

| Endpoint | Purpose |
|---|---|
| `GET /healthz`, `/ready` | Probes |
| `GET /alerts/status` | Watcher state, open incidents, AI-model budget, AI provider and model, Teams setup |
| `POST /alerts/test` | Send a sample card to the alert webhooks |
| `POST /rca` `{"question", "namespace", "pod", "post_to_teams"}` | Run an RCA; optionally post it to Teams |
| `POST /chat` `{"message", "session_id"}` | Ask a question (AI model) |
| `GET /rca/latest` | HTML of the last RCA this pod produced |
| `GET /activity?limit=50` | Recent cluster activity (who did what) |
| `GET /activity/status` | Activity webhook: certificate present, calls received, last call, events, targets |
| `POST /teams/webhook` | Teams outgoing webhook (the only public path) |

---

## 7. Configuration reference

Plain settings live in the ConfigMap `kubesupport-config` in [`deploy/deployment.yaml`](../deploy/deployment.yaml); the five secrets live in the Secret `kubesupport-secrets`. Both reach the pod as environment variables with the same names, so after a change run `kubectl -n kubesupport rollout restart deploy/kubesupport`.

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `GROQ_API_KEY` | Secret | — | Groq AI model. Without an AI key the agent uses rules only and can't answer free-form questions. |
| `ANTHROPIC_API_KEY` | Secret | — | Claude, instead of Groq (wins when both are set and `LLM_PROVIDER` is `auto`) |
| `LLM_API_KEY` | Secret | — | Key for another OpenAI-compatible service (with `LLM_BASE_URL`) |
| `AGENT_API_KEY` | Secret | — | Required `x-api-key` for the internal API |
| `TEAMS_ALERT_WEBHOOK_URLS` | Secret | — | Workflows webhook URL(s) for alerts: chats, standard or private channels. Comma-separated; each optionally `name=URL` |
| `TEAMS_REPLY_WEBHOOK_URL` | Secret | first alert URL | Workflows webhook of the questions channel (answers that take longer than 5 s) |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | Secret | — | The outgoing webhook's security token; without it `/teams/webhook` is off |
| `CLUSTER_NAME` | ConfigMap | `""` | Prefix on every alert, e.g. `[prod-eks]` |
| `ALLOWED_NAMESPACES` | ConfigMap | `*` | Namespaces for incident alerts and Teams answers; `*` = all. Read permissions are cluster-wide either way. |
| `LLM_PROVIDER` | ConfigMap | `auto` | `auto`, `groq`, `anthropic`, `openai` (any OpenAI-compatible service) or `none` (rules only) |
| `AGENT_MODEL` | ConfigMap | `""` = provider default | Groq `openai/gpt-oss-120b`; Claude `claude-opus-5-5` |
| `AGENT_EFFORT` | ConfigMap | `""` = provider default | `low` / `medium` / `high` (Claude also `xhigh` / `max`); Groq `medium`, Claude `high`; `none` = not sent. On Groq it applies to `openai/gpt-oss*` models only. |
| `LLM_MAX_STEPS` | ConfigMap | Groq `8`, Claude `14` | Model calls per answer or RCA |
| `LLM_TOOL_OUTPUT_CHARS` | ConfigMap | Groq `2500`, Claude `15000` | Characters of one tool result given to the model |
| `LLM_MAX_REQUEST_CHARS` / `LLM_MAX_OUTPUT_TOKENS` | ConfigMap | Groq `14000` / `2500` | Size of one request and reply (Groq and OpenAI-compatible only); raise on a paid Groq plan |
| `LLM_BASE_URL` | ConfigMap | `""` | Endpoint for `LLM_PROVIDER` `openai`, e.g. `https://api.openai.com/v1` |
| `AGENT_FALLBACKS` | ConfigMap | `default` | Claude only: server-side refusal fallback; `off` to disable |
| `WATCH_ENABLED` | ConfigMap | `true` | Turn the watcher (alerts) off/on |
| `WATCH_INTERVAL_SECONDS` | ConfigMap | `60` | Scan period |
| `ALERT_COOLDOWN_MINUTES` | ConfigMap | `60` | Reminder interval for a still-open incident |
| `RESOLVE_AFTER_MINUTES` | ConfigMap | `10` | How long an incident must be gone before "Resolved" |
| `PENDING_GRACE_MINUTES` | ConfigMap | `5` | Pending longer than this = incident |
| `MAX_LLM_RCA_PER_HOUR` | ConfigMap | `10` | AI-model RCAs per hour for alerts; then rule-based (Groq free plan: try `3`) |
| `ACTIVITY_ENABLED` | ConfigMap | `true` | Activity notices on/off (they also need `webhook-cert.sh`) |
| `ACTIVITY_BATCH_SECONDS` | ConfigMap | `60` | One activity card per this many seconds |
| `ACTIVITY_IGNORE_USERS` | ConfigMap | Kubernetes/EKS components + kubesupport | Comma-separated caller prefixes whose actions aren't reported |
| `ACTIVITY_IGNORE_NAMESPACES` | ConfigMap | `kube-node-lease` | Namespaces whose activity isn't reported |
| `TEAMS_ACTIVITY_WEBHOOK_URLS` | Secret (optional) | the alert targets | Send activity cards somewhere other than the alerts |
| `TEAMS_TENANT_ID` | ConfigMap | `""` | Only accept questions from this tenant |
| `TEAMS_ALLOWED_USER_IDS` | ConfigMap | `""` | Only accept questions from these Entra object IDs |
| `REPORT_DIR` | ConfigMap | `/tmp/reports` | Where HTML reports are written |

The image, permissions and resources are set directly in [`deploy/deployment.yaml`](../deploy/deployment.yaml); the Ingress is in [`deploy/ingress.yaml`](../deploy/ingress.yaml).

---

## 8. Operations and troubleshooting

| Want to… | Command |
|---|---|
| Agent logs | `kubectl -n kubesupport logs deploy/kubesupport -f` |
| What the watcher sees | `curl localhost:8080/alerts/status -H "x-api-key: $KEY"` (after port-forward) |
| Test the Teams alert URL | `curl -X POST localhost:8080/alerts/test -H "x-api-key: $KEY"` |
| Re-read the Secret | `kubectl -n kubesupport rollout restart deploy/kubesupport` |
| Narrow incident alerts to some namespaces | change `ALLOWED_NAMESPACES` in `deployment.yaml`, `kubectl apply -f deploy/deployment.yaml`, then restart |
| Activity webhook state | `curl localhost:8080/activity/status -H "x-api-key: $KEY"` |
| Renew the webhook certificate | `bash deploy/webhook-cert.sh` |
| Remove the activity webhook | `kubectl delete validatingwebhookconfiguration kubesupport-activity` |

| Symptom | Likely cause / fix |
|---|---|
| No alert cards at all | `/alerts/status` shows `alert_targets: []` → the Secret key is missing or empty. `/alerts/test` shows a target with a 4xx → the Workflows URL is wrong, or the workflow was turned off or deleted. |
| Alerts stopped after a while | Workflows run under the account of the person who created them. If that account was disabled or its connection expired, recreate the workflow and update the URL. |
| Teams says "There was a problem…" when you mention the agent | Wrong security token (agent log: "Rejected /teams/webhook call…"), the callback URL isn't reachable from the internet, or TLS is invalid. Test with `curl -i -X POST https://<host>/teams/webhook`: you should get **401** (missing signature), not a timeout or 404. |
| `404` from `/teams/webhook` | `TEAMS_OUTGOING_WEBHOOK_SECRET` isn't set in the Secret, or the pod wasn't restarted after adding it |
| "Looking into it" but no answer appears | `TEAMS_REPLY_WEBHOOK_URL` points to another channel, or that workflow is broken (agent log: "Teams webhook … returned …") |
| "namespace … not allowed" | Add the namespace to `ALLOWED_NAMESPACES` |
| Too many alerts | Narrow `ALLOWED_NAMESPACES`, raise `ALERT_COOLDOWN_MINUTES` |
| RCAs are rule-based although a key is set | `llm_error` in `/rca` responses, or the agent log ("LLM investigation failed"). Typical causes: wrong key; Groq's daily token limit used up (`llm_error` says `rate limit, retry after …`); a model that doesn't accept the effort or fallback settings (try `AGENT_EFFORT=none` or `AGENT_FALLBACKS=off`); `MAX_LLM_RCA_PER_HOUR` reached. |
| AI answers take minutes | Normal on Groq's free plan: kubesupport waits out the per-minute token limit (log: `Model API returned 429; retrying in …`). A paid plan is faster; raise `LLM_MAX_REQUEST_CHARS` and `LLM_TOOL_OUTPUT_CHARS` with it. |
| Agent pod not Ready | `kubectl -n kubesupport describe pod …`. Check the image name and tag, and that the nodes can reach Docker Hub. |
| No activity cards | `/activity/status`: `webhook_certificate: false` → run `webhook-cert.sh`. `webhook_calls_total: 0` → the EKS control plane can't reach port 8443; allow it from the cluster security group. Calls arriving but no card → the actions came from ignored callers, or the alert targets are failing (`/alerts/test`). |
| Activity cards too noisy | Add the busy caller's prefix to `ACTIVITY_IGNORE_USERS` (e.g. `system:serviceaccount:argocd:`) |

---

## 9. Local development

Run the agent against any cluster from your kubeconfig. Teams settings are optional; without them alerts are only logged.

```bash
cd agent
python -m venv .venv && source .venv/bin/activate       # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
export ALLOWED_NAMESPACES=default REPORT_DIR=$PWD/reports
export GROQ_API_KEY=...                                  # optional (or ANTHROPIC_API_KEY)
export TEAMS_ALERT_WEBHOOK_URLS='https://...'            # optional: real alerts to Teams
uvicorn app:app --reload                                 # http://localhost:8000/docs
python cli.py incidents                                  # what the watcher would alert on
pytest tests -q                                          # no cluster, AI model or Teams needed
```

To try the outgoing webhook locally, expose port 8000 with a tunnel (for example `cloudflared tunnel` or VS Code dev tunnels), and use that HTTPS URL plus `/teams/webhook` as the callback in a test team.

---

## 10. Limitations and known issues

| # | Item | Notes |
|---|---|---|
| 1 | **Questions only from a standard channel** | Outgoing webhooks don't work in 1:1 chats, group chats or private channels. A private 1:1 chat with the agent would need a registered Teams bot (Azure Bot Service). Alerts and long answers *can* go to your personal chat, group chats and private channels, through their Workflows webhooks. |
| 2 | **"Bitbucket recent commits" in the system prompt** | The prompt in `agent.py` asks the model to check recent Bitbucket commits, but the agent has no Bitbucket tool, so it can't actually look them up. Adding one means a read-only Bitbucket API token and a new tool in `tools.py`. |
| 3 | **State is in memory** | Open incidents and chat history are lost when the pod restarts. The startup summary card covers the gap. Keep `replicas: 1`, as in `deployment.yaml`. |
| 4 | **Teams limits** | Outgoing-webhook replies must come within 5 s, so long answers arrive as separate channel posts rather than in the thread. Workflows have Power Automate request limits, so an alert storm across many workloads can be throttled. |
| 5 | **Rule-based RCAs are generic** | They recognise the common failure types listed in 4.2. For real root causes, set a Groq or Anthropic key. |
| 6 | **`scan_cluster_for_issues` uses the old rule** | This tool (used by `cli.py scan` and offered to the AI model) still flags any pod that *ever* restarted. The watcher and `status` use the new `detect_incidents`. |
| 7 | **`/rca/latest`** | Shows only the last RCA produced by the current pod, and only internally. |
| 8 | **Exec shows the starting command only** | For `kubectl exec -it pod -- sh`, Kubernetes reports `sh`, not what is typed inside the shell. Recording keystrokes needs a runtime security tool (e.g. Falco) or EKS audit logs. |
| 9 | **Activity while kubesupport is down** | Isn't reported, because the webhook is skipped (by design, so nothing is blocked). Recent activity is held in memory and lost on restart. |
| 10 | **Secret objects reach the webhook** | The API server sends the whole object; kubesupport keeps only the name. Remove `secrets` from the webhook rules if that's not acceptable. |
| 11 | **Groq free plan** | Per-minute and per-day token limits make AI answers and RCAs slow (minutes), and they stop for the day once the daily limit is used up; alerts then carry rule-based RCAs. Requests are kept small, so the model sees shortened tool output. |
| 12 | **Open models are less predictable** | Groq's models sometimes skip a tool or return an incomplete RCA. kubesupport reminds the model to call `submit_rca`, then requires it, checks the result, and falls back to the rules if it is still unusable. Claude gives the most thorough RCAs. |

---

## 11. Cost

- **AI model:**
  - **Groq:** the free plan costs nothing, with token limits per minute and per day; paid plans bill per token (https://groq.com/pricing);
  - **Anthropic:** billed per token, only when a key is set. An RCA can take up to 14 model calls; `claude-opus-5-5` costs $4 / $20 per million input/output tokens (at the time of writing);
  - alerts are capped by `MAX_LLM_RCA_PER_HOUR`; questions from Teams are not capped;
  - lower `AGENT_EFFORT` or pick a cheaper `AGENT_MODEL` to spend less.
- **Infrastructure:** nothing extra. The image is on Docker Hub (free for public repositories), and the agent runs on your existing nodes (50m CPU / 128Mi requested).
- **Teams:** Workflows webhooks and outgoing webhooks need no premium licence.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **RCA** | Root cause analysis: what broke, why, the evidence, how to fix and prevent it |
| **SEV1–SEV4** | Severity, SEV1 most severe; sets the alert card colour |
| **Incident** | One problem on one workload, e.g. `payments/Deployment/api/CrashLoop`; several failing pods of the same Deployment are one incident |
| **Workflows webhook** | A URL from the Teams Workflows app; whatever is POSTed to it appears as a card in a chosen chat or channel |
| **Outgoing webhook** | A Teams feature: @mentioning it in a channel sends the message to your HTTPS endpoint, which must reply within 5 s |
| **HMAC** | The signature Teams adds to outgoing-webhook requests, so the agent can tell they really came from your webhook |
| **CrashLoopBackOff** | A container keeps crashing, and Kubernetes waits longer before each restart |
| **OOMKilled (exit 137)** | The container used more memory than its limit and the kernel killed it |
| **ImagePullBackOff** | Kubernetes can't pull the image (wrong tag, no access, registry down) |
| **Effort** | How much the AI model thinks (reasons) before answering; lower is faster and cheaper |
| **Groq** | A hosted AI service that runs open models, such as `openai/gpt-oss-120b`, behind an OpenAI-compatible API. kubesupport uses it when `GROQ_API_KEY` is set. |
| **Token** | The unit AI services count text in, roughly 3–4 characters; rate limits and prices are per token |
