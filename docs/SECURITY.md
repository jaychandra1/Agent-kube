# Security model

kubesupport can read the whole cluster and talks to the internet (Teams, Cloudflare, an AI provider), so it is built to be **read-only, closed by default, and explicit about what leaves the cluster**.

---

## 1. Layers

```mermaid
flowchart LR
    internet(["Internet"]) --> cf["Cloudflare WAF<br/>only POST /teams/webhook,<br/>rate limit"]
    cf --> tun["cloudflared<br/>(outbound tunnel,<br/>nothing can connect in)"]
    tun -->|"NetworkPolicy:<br/>only the tunnel pods"| p8080["kubesupport :8080<br/>serves only /teams/webhook"]
    p8080 --> hmac{"HMAC valid?<br/>tenant and user allowed?"}
    hmac -- yes --> work["read-only investigation"]
    hmac -- no --> reject["401 / polite refusal"]
    work -->|"RBAC: view + cluster-read,<br/>get/list/watch, never Secrets"| k8s["Kubernetes API"]
```

| Layer | What it does |
|---|---|
| **Kubernetes RBAC** | Cluster-wide **read-only**: the built-in `view` role in every namespace, plus `kubesupport-cluster-read` for nodes, namespaces, volumes, storage, CRDs, RBAC objects, webhook configurations and metrics. Only `get`, `list`, `watch`. **Never Secrets**, never writes; `test_manifests.py` enforces both. |
| **No public endpoint in AWS** | No load balancer, no public IP, no inbound port. Teams' requests arrive through a Cloudflare Tunnel that the cluster opens outward. All Services are `ClusterIP`. |
| **Cloudflare** | A WAF rule blocks everything except `POST /teams/webhook` on the hostname; a rate-limit rule cuts off floods; TLS 1.2+. |
| **Teams-only port** | The tunnel reaches port 8080, which serves nothing but `POST /teams/webhook` (not even `/docs`). The internal API is on 8000 and can't be reached through the tunnel even if a Cloudflare rule is wrong. |
| **Network policies** | Only the tunnel pods may connect to 8080; no pod may connect to the internal API (8000); nothing may connect to the tunnel pods. Enforced when the cluster runs a network-policy engine (EKS VPC CNI network policy, Calico, Cilium). |
| **HMAC signature** | Every Teams request must carry a valid `Authorization: HMAC …` header made with the outgoing webhook's security token; otherwise 401. |
| **Who may ask** | Only members of the team holding the outgoing webhook can mention it; `TEAMS_TENANT_ID` and `TEAMS_ALLOWED_USER_IDS` narrow it to your tenant and to named people. |
| **Boundaries** | People in Teams can't ask about kubesupport itself. A question about its setup, AI model, rules or prompt is refused by a filter; its own namespace and cluster-wide objects can't be read through Teams by any command or by the AI; the AI has a rule of its own for the rest. The person is told they are not authorised, and the administrators get a card with who asked what ([RULES.md, section 6](RULES.md#6-boundaries-what-may-not-be-asked)). |
| **Internal API key** | `/rca`, `/chat`, `/rca/latest`, `/alerts/*`, `/activity*`, `/security/restricted` require `x-api-key`. Without `AGENT_API_KEY` the internal API is off (closed by default). |
| **Namespace allowlist and input validation** | Tools refuse namespaces outside `ALLOWED_NAMESPACES`. Namespace, pod, container and object names must be valid Kubernetes names, so a Teams command can't change the API path (e.g. with `..`). |
| **Redaction** | Passwords, tokens, API keys, AWS access keys, JWTs and credentials in URLs (`scheme://user:password@host`) become `[REDACTED]` in logs, object listings and exec commands. **Every environment-variable value** in `get` output is replaced by `[REDACTED]` (names and `valueFrom` references stay). All of this happens before an AI model or Teams sees it. |
| **Prompt-injection guard** | All tool output reaches the AI model wrapped in `<untrusted_data>`, with instructions never to follow text inside it. The model's only tools are read-only, so a manipulated model still can't change anything. |
| **Cursor provider** | Of Cursor's built-in tool groups only `mcp` is on, because that is how Cursor offers kubesupport's tools to the model; the shell, file and web tools are off (`shell` is also explicitly disallowed). No MCP server is configured, no project or user settings are loaded, and the working folder is empty, so the agent can only call kubesupport's read-only tools. |
| **Activity webhook** | Always answers "allowed"; `failurePolicy: Ignore` and a 3-second timeout mean the cluster never depends on kubesupport. The cluster's own components (kubelets, control plane, `kube-system` controllers) and its busiest objects (status, Events, Leases, Endpoints) are never sent to it. HTTPS with its own CA. Events hold names only: for a ConfigMap the keys that changed, for a Secret the name and how many keys changed, never a value. At most 2,000 events wait between cards, so a flood can't exhaust memory. |
| **Timeouts and limits** | Every Kubernetes call times out (5 s connect, 30 s read); lists are capped at the API server; chat histories are bounded (200 conversations, idle ones expire after a day); at most 6 AI questions wait at a time. |
| **Container** | Non-root (UID 10001; cloudflared 65532), read-only root filesystem, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp; namespace Pod Security level `restricted`. The tunnel pods don't mount a Kubernetes token. |
| **Secrets handling** | Keys and webhook URLs live only in Kubernetes Secrets created with `kubectl`, never in Git or the image. Logs show webhook *names*, never URLs; error messages never include API keys. |

---

## 2. The image

What [`agent/Dockerfile`](../agent/Dockerfile) does so that an image scan (Trivy, for example) stays clean:

| Step | Why |
|---|---|
| `apt-get upgrade` at build time | Installs the security fixes Debian published after the Python base image was built (for example `zlib1g`). |
| pip is upgraded for the build, then **removed** | Nothing needs pip at runtime, and its CVEs would otherwise appear in every scan. The copy Python keeps for re-installing pip is removed too. |
| The Cursor SDK's bundled `undici` is replaced | The SDK's helper program ships `undici` 5.29.0 (with `@fastify/busboy` 2.1.1), a release line that gets no more security fixes. [`docker/patch_cursor_bridge.py`](../agent/docker/patch_cursor_bridge.py) swaps in a pinned, checksum-verified `undici` 6.x, removes `@fastify/busboy`, and then loads the helper's modules to prove they still work. If any check fails, the image build fails. |
| Build helpers and tests are left out | `docker/` and `tests/` are not in the final image. |

About the `undici` findings: in the SDK, `undici` is only imported as a fallback for Node.js older than 18, and the SDK runs on its own bundled Node.js 22+. So that code was loaded but never used, and the practical risk was low. Replacing it is still right: scanners report by version, and unused vulnerable code shouldn't ship.

**Fixable and unfixed findings.** With these steps a Trivy scan of the image reports no finding that has a fix (checked with Trivy 0.76.1 on 2026-11-19). A scan that also lists advisories *without* a fix shows many entries for Debian base packages (glibc, util-linux, OpenSSL, …): Debian hasn't released fixes for those, they are in every Debian-based image, and they go away by rebuilding once Debian publishes the fix.

A scan is a snapshot: new advisories appear all the time. Rebuild the image regularly (re-run CI) so the OS updates are picked up, and rescan after every rebuild. What to do with a new finding is in [OPERATIONS.md](OPERATIONS.md#when-a-vulnerability-scan-reports-something).

---

## 3. What leaves the cluster

| To | What | When |
|---|---|---|
| Microsoft Teams (Workflows webhooks) | Alert, activity and answer cards | Always (that's the product) |
| Cloudflare | The text of questions and quick answers (Cloudflare terminates HTTPS) | Only with the tunnel, for questions |
| The AI provider you chose | The question and the redacted output of the tools the model calls (pod lists, events, log lines, object listings) | Only when `LLM_PROVIDER` is set |
| Docker Hub | Nothing; nodes only pull the public image | – |

With `LLM_PROVIDER: "none"` (or a self-hosted model through `ollama` / `custom`), no cluster data goes to an AI service.

---

## 4. Things to know

- **Activity webhook and Secrets.** The API server sends the whole object to an admission webhook, Secrets included, on every create, change and delete. kubesupport compares old and new in memory, keeps the name and the number of changed keys, and drops the rest. To keep Secret objects away entirely, remove `secrets` from the core rule in `deploy/activity-webhook.yaml` and re-run `webhook-cert.sh`; Secret changes are then not reported.
- **Exec content.** Only the starting command of `kubectl exec` is visible (e.g. `sh`), not what is typed inside the shell.
- **Public image.** The image contains only code, no keys or URLs. It includes the Cursor SDK (proprietary licence) unless built with `--build-arg WITH_CURSOR=false`.
- **What the boundaries guarantee.** The hard part is the tool block: through Teams, nothing in kubesupport's namespace can be read, and Secrets can never be read at all, so keys, webhook URLs and settings are out of reach whatever someone types. The question filter and the AI's rule are best effort: they recognise wording, and wording can be varied. The most a clever question could get out of the AI model is what the model itself knows, which is its rules (they are in [RULES.md](RULES.md)) and the cluster's name. Send the notices to a private channel (`TEAMS_SECURITY_WEBHOOK_URLS`): by default they go to the alert targets, where the people who asked may read them.
- **Questions only from a standard channel** of the team with the outgoing webhook; anyone in that team can ask unless `TEAMS_ALLOWED_USER_IDS` is set.
- **Activity events can be faked from inside the cluster.** Port 8443 must accept the API server, whose addresses aren't fixed, so any pod could send a made-up "exec" event to it. Treat activity cards as a notification and confirm important ones in the EKS audit logs.
- **Rotating secrets:** see [OPERATIONS.md](OPERATIONS.md#day-to-day) (Teams token, Workflows URLs, AI key, tunnel token).
