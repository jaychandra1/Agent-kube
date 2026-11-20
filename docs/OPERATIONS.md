# Operations

Running kubesupport after it's installed: routine tasks, troubleshooting, limitations and cost. Installation problems are covered in the troubleshooting table at the end of [INSTALLATION.md](INSTALLATION.md#troubleshooting).

In the commands below, `<you>` is your Docker Hub user, and `$KEY` / the port-forward come from [USAGE.md, section 3](USAGE.md#3-internal-api-operators).

---

## Day to day

| Task | How |
|---|---|
| **Pick up security updates** | Rebuild the image even without code changes (GitHub: **Actions → CI → Run workflow**), then `kubectl -n kubesupport rollout restart deploy/kubesupport`. Do this regularly, e.g. monthly, and after any scan finding. |
| **Roll out a new image** (after CI pushed a new `latest`) | `kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Pin or roll back a version** (recommended in production) | `kubectl -n kubesupport set image deployment/kubesupport agent=docker.io/<you>/kubesupport:sha-xxxxxxx`, and put the same tag in `deployment.yaml` so the next `apply` doesn't undo it. With `:latest`, any restart runs whatever was last pushed. |
| **Change a setting** | Edit the ConfigMap in `deploy/deployment.yaml`, then `kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Change a secret** (AI key, Workflows URL, Teams token) | `kubectl -n kubesupport patch secret kubesupport-secrets --type merge -p '{"stringData":{"LLM_API_KEY":"<new>"}}'` (any key name), then `kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Switch AI provider** | Put the new key in `LLM_API_KEY` (above), set `LLM_PROVIDER` (and `LLM_MODEL` / `LLM_BASE_URL` if needed) in the ConfigMap, apply, restart. Check `llm` in `/alerts/status`. See [AI_PROVIDERS.md](AI_PROVIDERS.md). |
| **Turn the AI off** | `LLM_PROVIDER: "none"`, apply, restart. Alerts keep coming with rule-based RCAs. |
| **Narrow incident alerts to some namespaces** | `ALLOWED_NAMESPACES: "payments,orders"`, apply, restart. Read access stays cluster-wide. |
| **Pause alerts** | `WATCH_ENABLED: "false"`, apply, restart |
| **Pause activity notices** | `ACTIVITY_ENABLED: "false"`, apply, restart. To remove the webhook completely: `kubectl delete validatingwebhookconfiguration kubesupport-activity` |
| **Renew the activity webhook certificate** | `bash deploy/webhook-cert.sh` (any time; valid 10 years) |
| **Change what activity is reported** | Edit the rules in `deploy/activity-webhook.yaml` (add a custom resource, drop a kind), then `bash deploy/webhook-cert.sh`. Needed once after upgrading to an image with new activity types, too: the rules live in the cluster, not in the image. |
| **Update cloudflared** | Change the image tag in `deploy/cloudflare-tunnel.yaml` to a newer release (Docker Hub `cloudflare/cloudflared` tags), then `kubectl apply -f deploy/cloudflare-tunnel.yaml`. The two pods are replaced one at a time, so questions keep working. |
| **Rotate the tunnel token** (e.g. it leaked) | Delete the tunnel in Cloudflare and redo INSTALLATION.md Steps 8.1, 8.2 and 8.4 with a new one; recreate Secret `kubesupport-tunnel` and `kubectl -n kubesupport rollout restart deploy/kubesupport-tunnel` |
| **Rotate the Teams outgoing-webhook token** | Delete and recreate the outgoing webhook in Teams (INSTALLATION.md, Step 4.3), then update `TEAMS_OUTGOING_WEBHOOK_SECRET` and restart |
| **See who asked about kubesupport itself** | The card in Teams, `curl -s localhost:8080/security/restricted -H "x-api-key: $KEY"` (since the pod started), or `kubectl -n kubesupport logs deploy/kubesupport \| grep "Refused a restricted"` |
| **Send those notices to a private channel** | Create a Workflows webhook in a private channel for administrators (as in INSTALLATION.md, Step 4.4), then `kubectl -n kubesupport patch secret kubesupport-secrets --type merge -p '{"stringData":{"TEAMS_SECURITY_WEBHOOK_URLS":"security=<URL>"}}'` and restart |
| **Keep another namespace out of Teams** | `PROTECTED_NAMESPACES: "kubesupport,<namespace>"`, apply, restart |
| **See what it sees** | `/alerts/status`, `/activity/status`, `/activity` (port-forward), `@kubesupport status` / `activity` in Teams, or `kubectl -n kubesupport logs deploy/kubesupport -f` |
| **Uninstall** | `kubectl delete validatingwebhookconfiguration kubesupport-activity` **first** (it's cluster-wide), then `kubectl delete -f deploy/cloudflare-tunnel.yaml -f deploy/deployment.yaml` (also deletes the namespace and Secrets). Finally delete the tunnel in Cloudflare (its DNS record goes with it), and the outgoing webhook and workflows in Teams. |

---

## When a vulnerability scan reports something

The image is built to scan clean ([SECURITY.md, section 2](SECURITY.md#2-the-image)). When a scanner such as Trivy reports a new finding, look at the **package** column:

| Package in the report | Where it comes from | Fix |
|---|---|---|
| A Debian package with **no fixed version** listed | The Python base image | Nothing to do yet: Debian hasn't released a fix. It disappears with a rebuild once it has. |
| A Debian package (`zlib1g`, `libssl3`, `libc6`, …) with a fixed version | The Python base image | Rebuild the image: the build runs `apt-get upgrade`. In GitHub: **Actions → CI → Run workflow**. Then restart kubesupport. |
| A Python package (`fastapi`, `requests`, `kubernetes`, …) | [`agent/requirements.txt`](../agent/requirements.txt) | Rebuilding installs the newest version inside the allowed range. If the fix is outside the range, raise the upper bound, run the tests, rebuild. |
| `pip` | Shouldn't appear: the image has no pip | Check the pod runs a current image. |
| `undici` | The Cursor SDK's bundled helper | Set `UNDICI_VERSION` and `UNDICI_INTEGRITY` in [`agent/docker/patch_cursor_bridge.py`](../agent/docker/patch_cursor_bridge.py) to the fixed release (the integrity value is `dist.integrity` at `https://registry.npmjs.org/undici/<version>`), then rebuild. Stay on the 6.x line unless the fix exists only in a later one. |
| Another Node package under `cursor_sdk/_vendor` | The Cursor SDK | Check for a newer `cursor-sdk` release ([`requirements-cursor.txt`](../agent/requirements-cursor.txt)). If you don't use Cursor, build without it: `--build-arg WITH_CURSOR=false`. |
| `cloudflared` image findings | [`deploy/cloudflare-tunnel.yaml`](../deploy/cloudflare-tunnel.yaml) | Move the pinned tag to a newer release (see *Update cloudflared* above). |

After a rebuild, restart the pod and let the scanner rescan; with the Trivy Operator the new report appears by itself a few minutes after the new pod starts.

---

## Health at a glance

```bash
kubectl -n kubesupport get pods
```

```bash
curl -s localhost:8080/alerts/status -H "x-api-key: $KEY"
```

| In `/alerts/status` | Healthy when |
|---|---|
| `watcher.running` | `true` (unless you paused alerts) |
| `watcher.open_incidents` | What you'd expect from the cluster |
| `teams.alert_targets` | Lists your target names |
| `teams.outgoing_webhook` | `true` if questions are set up |
| `teams.security_targets` | Where notices about refused questions go: your security target, or the alert targets |
| `llm.configured` | `true` with the provider and model you chose; otherwise `llm.status` says why |
| `watcher.unchecked` | Empty. Otherwise it lists what the last scan couldn't read (`pods:<ns>`, `nodes`, `events`, `jobs`); incidents there are kept open. |
| `watcher.ai_backlog` / `cards_waiting_for_retry` | Usually 0. A growing retry count means Teams refuses the cards (`/alerts/test`). |

`/healthz` answers 503 when no scan finished for 5 minutes or the Teams server died. The liveness probe then restarts the pod; the agent log says why (`Watcher tick failed`, `The teams server stopped`).

`/activity/status`: `webhook_certificate: true`, `webhook_calls_total` growing as people use `kubectl`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No alert cards at all | `/alerts/status` shows `alert_targets: []` → the Secret key is missing or empty. `/alerts/test` shows a 4xx for a target → the Workflows URL is wrong, or the workflow was turned off or deleted. |
| Alerts stopped after a while | Workflows run under the account of the person who created them. If that account was disabled or its connection expired, recreate the workflow and update the URL. |
| Teams says "Sorry, there was a problem…" after `@kubesupport` | Wrong security token (agent log: `Rejected /teams/webhook call…`), Cloudflare blocked the call (**Security → Events** in Cloudflare), or the tunnel is down. `curl -i -X POST https://<host>/teams/webhook` should give **401**. |
| "Looking into it…" but no answer card | `TEAMS_REPLY_WEBHOOK_URL` is wrong or that workflow is off (agent log: `Teams target … returned …`), or the AI provider failed (agent log). |
| Answers arrive as a new post, not in the question's thread | The answers workflow still uses "Post card in a chat or channel". Change it to "Reply with an adaptive card in a channel" with Message ID `triggerBody()?['threadMessageId']` ([INSTALLATION.md, Step 4.2](INSTALLATION.md#42-answers-in-the-questions-channel--t2)). |
| Answers are too long, or too terse | The format is rule 11 of the AI's rules ([RULES.md](RULES.md#3-the-ais-rules)). Change `ANSWER_MAX_WORDS` in the ConfigMap (default 80; `0` = no limit), apply, restart. |
| The AI answers with `kubectl` commands instead of data | An older image (Cursor's agent had no tools), or a weak model. Deploy the current image; see [AI_PROVIDERS.md](AI_PROVIDERS.md#troubleshooting). `@kubesupport get ingresses` and the other quick commands always return real data. |
| "No AI model is configured" | `/alerts/status` → `llm.status` names the missing setting. See [AI_PROVIDERS.md](AI_PROVIDERS.md#troubleshooting). |
| RCAs are rule-based although a provider is set | `llm_error` in a `/rca` response, or the agent log (`LLM investigation failed`): wrong key, model not available, the provider's rate or daily limit, or `MAX_LLM_RCA_PER_HOUR` reached. |
| AI answers take minutes | Normal on free plans: kubesupport waits out the per-minute token limit (log: `Model API returned 429; retrying in …`). |
| Someone gets "You are not authorised to ask this question" for an ordinary question | The question filter read it as a question about kubesupport itself ("you", "this bot" or "kubesupport" next to words such as "deployed" or "configured"), or the AI used its rule. The card and the log line say which layer and why. Ask again and name the workload or namespace. Examples of what is refused and what passes: [RULES.md, section 6](RULES.md#6-boundaries-what-may-not-be-asked). |
| `pods kubesupport` and other reads of kubesupport's namespace are refused in Teams | By design: the namespace is in `PROTECTED_NAMESPACES`. Use `kubectl`. Incidents there are still alerted. |
| No card when a restricted question is asked | `/alerts/status` → `teams.security_targets` is empty (no security or alert webhook), or more than 20 refusals came within an hour (then they are only logged). `/security/restricted` lists them all. |
| "namespace … not allowed" | Add it to `ALLOWED_NAMESPACES`, or set `"*"`. |
| Too many incident alerts | Narrow `ALLOWED_NAMESPACES`, or raise `ALERT_COOLDOWN_MINUTES`. |
| Pod never created; events say `violates PodSecurity "restricted"` for `opentelemetry-auto-instrumentation-…` | The CloudWatch Observability add-on is injecting OpenTelemetry containers. The manifests opt out with `instrumentation.opentelemetry.io/inject-*: "false"`; details in [INSTALLATION.md](INSTALLATION.md#troubleshooting). |
| No activity cards | `/activity/status`: `webhook_certificate: false` → run `webhook-cert.sh`. `webhook_calls_total: 0` → the EKS control plane can't reach port 8443; allow it from the cluster security group. Calls arrive but no card → the actions came from ignored callers, or the targets fail (`/alerts/test`). |
| Activity cards too noisy | The card names the caller of every line. Add a busy automation account's prefix to `ACTIVITY_IGNORE_USERS` (e.g. `system:serviceaccount:argocd:`), a namespace to `ACTIVITY_IGNORE_NAMESPACES`, or a kind to `ACTIVITY_IGNORE_KINDS` (e.g. `Pod,Job`). |
| Rollout restarts or ConfigMap changes are not reported | The cluster still has the old webhook rules: run `bash deploy/webhook-cert.sh` once. Check with `kubectl get validatingwebhookconfiguration kubesupport-activity -o yaml` that `configmaps` and `UPDATE` are in the rules. |
| `webhook-cert.sh` fails with `unknown field "matchConditions"` | The cluster is older than Kubernetes 1.28. Delete the `matchConditions` block from `deploy/activity-webhook.yaml`; `ACTIVITY_IGNORE_USERS` still filters the same callers. |
| Tunnel pods not Ready / Cloudflare error 1033 or 502 | See the tunnel rows in [INSTALLATION.md](INSTALLATION.md#troubleshooting). |
| The pod restarts every few minutes; events say `Liveness probe failed ... 503` | `curl localhost:8080/healthz` (port-forward) shows the problem: a scan that never finishes (the Kubernetes API is unreachable or very slow) or a dead Teams server. Check the agent log before the restart: `kubectl -n kubesupport logs deploy/kubesupport --previous`. |
| `/alerts/status` and other internal endpoints answer 403 | `AGENT_API_KEY` isn't in the Secret: the internal API is off until it is. |
| `/activity/status` shows `webhook_server_error` | The 8443 server couldn't start, usually a bad certificate: run `bash deploy/webhook-cert.sh` again. |

Logs worth knowing:

| Log line | Meaning |
|---|---|
| `AI model: Groq, model openai/gpt-oss-120b` | The provider loaded at startup |
| `No AI model (…)` | Rules only, and why |
| `Starting AI investigation with …` / `LLM investigation failed (…)` | An AI RCA started / fell back to the rules |
| `Model API returned 429; retrying in Ns` | Waiting for a rate limit |
| `Rejected /teams/webhook call with a missing or wrong HMAC signature` | A call that wasn't from your Teams webhook |
| `Activity webhook off: …` | No certificate mounted, or `ACTIVITY_ENABLED` is false |
| `Boundaries on: … reported to …; protected namespaces: …` | At startup: where notices about refused questions go and which namespaces are protected |
| `Refused a restricted request from <name> (<user id>), stopped by the <layer>: <why> \| <question>` | Someone asked about kubesupport itself and was refused |
| `Blocked tool call <tool>: …` | The AI model tried to read a protected namespace during a Teams question; it got no data |

---

## Limitations

| # | Item | Notes |
|---|---|---|
| 1 | **Questions only from a standard channel** | Outgoing webhooks don't work in chats or private channels. Alerts and long answers *can* go to chats and private channels. A 1:1 chat with the agent would need a registered Teams bot (Azure Bot Service). |
| 2 | **No source-control or CI lookups** | The AI sees the cluster only; it can't read commits or pipelines. A read-only Git/Bitbucket tool could be added in `tools.py` (with a read-only token), and named in the AI's rules. |
| 3 | **State is in memory** | Open incidents and chat history are lost when the pod restarts; the startup card covers the gap. Keep `replicas: 1`. |
| 4 | **Teams limits** | Replies to an outgoing webhook must come within 5 s, so long answers come through a workflow. They land in the question's thread only after the one-time workflow change in [INSTALLATION.md, Step 4.2](INSTALLATION.md#42-answers-in-the-questions-channel--t2). Workflows have request limits, so an alert storm can be throttled. |
| 5 | **Rule-based RCAs are generic** | They recognise common failure types only. An AI provider gives real root causes. |
| 6 | **`scan_cluster_for_issues` uses the old rule** | This tool (used by `cli.py scan` and offered to the model) flags any pod that *ever* restarted; the watcher uses `detect_incidents`. |
| 7 | **`/rca/latest`** | Only the last RCA from the current pod, and only internally. |
| 8 | **Exec shows the starting command only** | `kubectl exec -it pod -- sh` reports `sh`, not what is typed in the shell. Keystrokes need a runtime security tool (e.g. Falco) or EKS audit logs. |
| 9 | **Activity while kubesupport is down** | Isn't reported (by design, so nothing is blocked). Recent activity is lost on restart. |
| 10 | **Secret objects reach the webhook** | The API server sends the whole object; kubesupport keeps only the name. Remove `secrets` from the webhook rules if that's not acceptable. |
| 11 | **Free AI plans** | Per-minute and per-day token limits make AI answers slow (minutes) and stop them for the day once used up; alerts then carry rule-based RCAs. |
| 12 | **Open models are less predictable** | They sometimes skip a tool or return an incomplete RCA. kubesupport reminds the model, checks the result, and falls back to the rules if it is still unusable. |
| 13 | **Cloudflare is in the path of questions** | Questions depend on Cloudflare and the tunnel pods; alerts and activity don't. |
| 14 | **Cursor depends on a bundled helper program** | The Cursor SDK runs its own Node.js helper inside the pod (about 175 MB of the image). It starts fine under the pod's restrictions (read-only filesystem, non-root), but its packages are Cursor's to update; the image build patches the one with known vulnerabilities ([SECURITY.md](SECURITY.md#2-the-image)). |
| 15 | **Activity events can be faked from inside the cluster** | Port 8443 can't be limited to the API server's (changing) addresses, so a pod could post a made-up event. Confirm important ones in the EKS audit logs. |
| 16 | **Boundaries recognise wording** | The question filter knows English phrasing and can refuse an ordinary question by mistake or miss an unusual one; the AI's rule covers other wording and languages but is a model's judgement. What can't be worked around is the tool block: kubesupport's namespace and all Secrets are unreadable through Teams. Everyone is restricted, administrators included; refusals are kept in memory only (and in the pod's log). |
| 17 | **Activity is reported when requested** | The webhook sees a request before it is stored, so a line can describe something that was refused a moment later (an eviction blocked by a PodDisruptionBudget, a quota). Custom resources, pod updates and status changes are not reported. |

---

## Cost

| Item | Cost |
|---|---|
| **AI provider** | Free tiers (Groq, Gemini, some OpenRouter models) cost nothing but are rate-limited. Claude, OpenAI, Azure OpenAI, Mistral and Cursor bill per use. An RCA can take up to `LLM_MAX_STEPS` model calls; alerts are capped by `MAX_LLM_RCA_PER_HOUR`, questions are not. |
| **Cloudflare** | Tunnel, the WAF custom rule, one rate-limiting rule and the Universal certificate are included in the free plan. |
| **Kubernetes** | Nothing extra: the agent requests 50m CPU / 128Mi; each tunnel pod 10m / 64Mi. |
| **Docker Hub** | Free for public repositories. |
| **Teams** | Workflows webhooks and outgoing webhooks need no premium licence. |
