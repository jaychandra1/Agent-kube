# Configuration reference

Every setting kubesupport reads. Plain settings live in the ConfigMap `kubesupport-config` in [`deploy/deployment.yaml`](../deploy/deployment.yaml); secrets live in the Secret `kubesupport-secrets`, created with `kubectl` (INSTALLATION.md, Step 5). Both reach the pod as environment variables with the same names.

After a change:

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport
```

For a Secret value, see *Change a secret* in [OPERATIONS.md](OPERATIONS.md#day-to-day).

---

## Secret `kubesupport-secrets`

| Key | Default | Meaning |
|---|---|---|
| `LLM_API_KEY` | – | API key of the AI provider chosen with `LLM_PROVIDER`. The provider's own name works too (`GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `CURSOR_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `OLLAMA_API_KEY`) and wins when both are set. |
| `AGENT_API_KEY` | – | Required `x-api-key` header for the internal API. Without it the internal API is **off** (403): nothing can trigger RCAs or Teams posts through it. |
| `TEAMS_ALERT_WEBHOOK_URLS` | – | Workflows webhook URL(s) for alerts: chats, standard or private channels. Comma-separated; each entry optionally `name=URL` (the name appears in logs instead of the URL). Without it no alerts are sent. |
| `TEAMS_REPLY_WEBHOOK_URL` | first alert URL | Workflows webhook of the questions channel, for answers that take longer than 5 s. Each answer carries `threadMessageId` and `channelId`, so the workflow can reply in the question's thread. |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | – | The outgoing webhook's security token. Without it `/teams/webhook` is off (404). |
| `TEAMS_ACTIVITY_WEBHOOK_URLS` | the alert targets | Send activity cards somewhere other than the alerts (same format as the alert URLs) |
| `TEAMS_SECURITY_WEBHOOK_URLS` | the alert targets | Where the "someone asked about kubesupport itself" notices go (same format as the alert URLs). Use the Workflows webhook of a private channel that only administrators can read. |

The Secret `kubesupport-tunnel` holds `TUNNEL_TOKEN` for the Cloudflare Tunnel pods (INSTALLATION.md, Step 8).

---

## ConfigMap `kubesupport-config`

### Cluster and watcher

| Setting | Default | Meaning |
|---|---|---|
| `CLUSTER_NAME` | `""` | Shown on every Teams message, e.g. `[prod-eks]`; also tells the AI which cluster it works on |
| `TIMEZONE` | `UTC` | Time zone for times in RCAs and reports: an IANA name such as `Asia/Kolkata` or `Europe/London` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Health-check requests are never logged. |
| `ALLOWED_NAMESPACES` | `*` | Namespaces for incident alerts and Teams answers; `*` = all. Read permissions are cluster-wide either way. |
| `WATCH_ENABLED` | `true` | `false` = no incident alerts (questions still work) |
| `WATCH_INTERVAL_SECONDS` | `60` | How often the cluster is checked |
| `ALERT_COOLDOWN_MINUTES` | `60` | Reminder interval for a still-open incident |
| `RESOLVE_AFTER_MINUTES` | `10` | How long an incident must be gone before "Resolved" |
| `PENDING_GRACE_MINUTES` | `5` | A pod Pending longer than this is an incident |
| `MAX_LLM_RCA_PER_HOUR` | `10` | AI RCAs per hour for alerts; after that, rule-based RCAs until the hour rolls over |

### AI provider ([AI_PROVIDERS.md](AI_PROVIDERS.md))

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `none` in code; `groq` in `deployment.yaml` | `groq`, `anthropic`, `cursor`, `gemini`, `openai`, `azure-openai`, `openrouter`, `mistral`, `ollama`, `custom` or `none`. Only this provider is used. |
| `LLM_MODEL` | provider default | Model name; Azure OpenAI: the deployment name. Required for `openai`, `azure-openai`, `ollama`, `custom`. |
| `LLM_BASE_URL` | provider default | API endpoint. Required for `azure-openai`, `ollama`, `custom`. |
| `LLM_EFFORT` | provider default | Reasoning effort: `low`, `medium`, `high` (Claude also `xhigh`, `max`); `none` = not sent |
| `LLM_MAX_STEPS` | provider default | Model calls per answer or RCA (Cursor: reminders to submit the RCA) |
| `LLM_TOOL_OUTPUT_CHARS` | provider default | Characters of one tool result given to the model |
| `LLM_MAX_REQUEST_CHARS` | provider default | Size of one request (OpenAI-compatible providers) |
| `LLM_MAX_OUTPUT_TOKENS` | provider default | Longest reply |
| `LLM_TIMEOUT_SECONDS` | provider default | One model call (Cursor: one whole run) |
| `LLM_FALLBACKS` | `default` | Claude only: server-side refusal fallback; `off` to disable |
| `LLM_DEADLINE_SECONDS` | `300` | Longest an AI answer or RCA may take; after that the built-in rules answer |
| `ANSWER_MAX_WORDS` | `80` | How many words an answer to a question may have outside its table. The model is told this limit; an answer more than one and a half times over it is sent back once to be shortened. `0` = no limit and no rewrite. Raise it for more detailed answers. |

Provider defaults are in the table in [AI_PROVIDERS.md, section 5](AI_PROVIDERS.md#5-limits-per-provider).

### Cluster activity

| Setting | Default | Meaning |
|---|---|---|
| `ACTIVITY_ENABLED` | `true` | Activity notices on/off (they also need `deploy/webhook-cert.sh`) |
| `ACTIVITY_BATCH_SECONDS` | `60` | One activity card per this many seconds |
| `ACTIVITY_IGNORE_USERS` | Kubernetes/EKS components and kubesupport | Comma-separated caller prefixes whose actions aren't reported. The first four defaults are also filtered at the API server (`matchConditions` in `deploy/activity-webhook.yaml`); to see one of those callers, remove it there too and re-run `webhook-cert.sh`. |
| `ACTIVITY_IGNORE_NAMESPACES` | `kube-node-lease` | Namespaces whose activity isn't reported |
| `ACTIVITY_IGNORE_KINDS` | `""` | Comma-separated kinds whose activity isn't reported, e.g. `Pod,Job` when a pipeline or operator creates them all day. Exec into pods counts as kind `Pod`. |

### Teams access

| Setting | Default | Meaning |
|---|---|---|
| `TEAMS_TENANT_ID` | `""` | Only answer messages from this Microsoft 365 tenant (recommended) |
| `TEAMS_ALLOWED_USER_IDS` | `""` | Only answer these people: comma-separated Entra object IDs (recommended) |

### Boundaries

What people in Teams may not ask ([RULES.md, section 6](RULES.md#6-boundaries-what-may-not-be-asked)).

| Setting | Default | Meaning |
|---|---|---|
| `BOUNDARIES_ENABLED` | `true` | Refuse and report questions about kubesupport itself: its setup, AI model, rules, prompt and namespace. `false` switches all three layers off (not recommended). |
| `PROTECTED_NAMESPACES` | `kubesupport` | Comma-separated namespaces that can't be read through Teams, by command or by the AI. Cluster-wide objects whose name is one of these or starts with it (`kubesupport-cluster-read`) are covered too. Set it to the namespace kubesupport is installed in; add others you want to keep out of Teams. `none` protects no namespace: questions are still filtered. |

### Other

| Setting | Default | Meaning |
|---|---|---|
| `REPORT_DIR` | `/tmp/reports` | Where HTML RCA reports are written |

---

## Set in the image or manifest, not the ConfigMap

| Setting | Where | Meaning |
|---|---|---|
| `WEBHOOK_TLS_DIR` | code default `/etc/kubesupport/webhook-tls` | Where the activity webhook's certificate is mounted |
| `RUNBOOK_DIR` | code default `agent/runbooks` | Notes searched by the `search_runbooks` tool |
| `HOME` | `/tmp` (Dockerfile) | The only writable place in the pod; the Cursor SDK keeps its state there |
| `WITH_CURSOR` | build argument, `true` | `false` builds the image without the Cursor SDK |
| `UNDICI_VERSION`, `UNDICI_INTEGRITY` | constants in `agent/docker/patch_cursor_bridge.py` | The fixed `undici` release the image build puts into the Cursor SDK, and its SHA-512 |
| Image, replicas, resources, probes | `deploy/deployment.yaml` | Edited directly in the Deployment |
| Cloudflare route and firewall rules | Cloudflare dashboard | INSTALLATION.md, Step 8 |
