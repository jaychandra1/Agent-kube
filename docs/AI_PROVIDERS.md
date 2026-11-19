# AI providers

kubesupport uses an AI model to answer questions in plain words and to write root cause analyses (RCAs). **One Docker image supports every provider below.** You choose one with a single ConfigMap setting, `LLM_PROVIDER`, and put its key in the Secret as `LLM_API_KEY`. Only that provider is ever called.

Without a provider (`LLM_PROVIDER: "none"`), kubesupport still alerts, answers commands and writes rule-based RCAs.

The code lives in one folder, [`agent/providers/`](../agent/providers); how it plugs in is shown in [ARCHITECTURE.md](ARCHITECTURE.md#4-choosing-the-ai-provider).

---

## 1. Choose a provider

| `LLM_PROVIDER` | Service | Key | Default model (`LLM_MODEL`) | Good to know |
|---|---|---|---|---|
| `groq` | [Groq](https://console.groq.com) | `gsk_…` | `openai/gpt-oss-120b` | Free plan available; small per-minute limits make answers slow (minutes). |
| `anthropic` (or `claude`) | [Anthropic Claude](https://console.anthropic.com) | `sk-ant-…` | `claude-opus-5-5` | The most thorough RCAs. Paid per token. |
| `cursor` | [Cursor](https://cursor.com) (Cursor SDK) | Cursor API key | `composer-2.5` | Needs a **paid** Cursor plan. Cursor's agent runs the investigation and calls kubesupport's tools. |
| `gemini` (or `google`) | [Google Gemini](https://aistudio.google.com) | Gemini API key | `gemini-flash-latest` | Free tier available. |
| `openai` | [OpenAI](https://platform.openai.com) | `sk-…` | none: set `LLM_MODEL` | Paid per token. |
| `azure-openai` (or `azure`, `foundry`) | [Azure OpenAI / Microsoft Foundry](https://ai.azure.com) | resource key | none: set `LLM_MODEL` to your **deployment name** | Also set `LLM_BASE_URL`. Needs an Azure subscription. |
| `openrouter` | [OpenRouter](https://openrouter.ai) | `sk-or-…` | `openai/gpt-oss-120b` | One key for many models, some free. |
| `mistral` | [Mistral AI](https://console.mistral.ai) | Mistral key | `mistral-large-latest` | |
| `ollama` | [Ollama](https://ollama.com), self-hosted | none | none: set `LLM_MODEL` | Set `LLM_BASE_URL`, e.g. `http://ollama.ollama.svc:11434/v1`. Data never leaves your network. |
| `custom` | Any OpenAI-compatible Chat Completions API | optional | none: set `LLM_MODEL` | Set `LLM_BASE_URL`. vLLM, LiteLLM, Together, DeepSeek, Amazon Bedrock's OpenAI-compatible endpoint, … |
| `none` | – | – | – | Rule-based RCAs only. |

**Requirement for `custom` and the OpenAI-compatible ones:** the model must support *tool calling* (functions). kubesupport investigates by calling tools.

### About "Microsoft Copilot" and "GitHub Copilot"

- **Microsoft 365 Copilot** can't be used. Its Chat API works only on behalf of a signed-in user who holds a Copilot licence; a background service like kubesupport has no user to sign in as.
- **Microsoft's hosted models** *can* be used: choose `azure-openai` (Azure OpenAI / Microsoft Foundry).
- **GitHub Models** was retired in July 2026.
- **GitHub Copilot** has an SDK (GA in June 2026) that could be added later as an agent provider, the same way Cursor is.

---

## 2. Configure it

Three changes, then a restart:

**1. The key**, in the Secret:

```bash
kubectl -n kubesupport patch secret kubesupport-secrets --type merge -p '{"stringData":{"LLM_API_KEY":"<your key>"}}'
```

The provider's own variable name works too (e.g. `GROQ_API_KEY`, `CURSOR_API_KEY`, `GEMINI_API_KEY`); if both are set, the provider's own one wins.

**2. The provider**, in the ConfigMap in [`deploy/deployment.yaml`](../deploy/deployment.yaml):

```yaml
  LLM_PROVIDER: "gemini"
  LLM_MODEL: ""          # "" = the provider's default model
  LLM_BASE_URL: ""       # "" = the provider's default endpoint
```

**3. Apply and restart:**

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport
```

**Check** (with the port-forward from INSTALLATION.md, Step 9.1):

```bash
curl -s localhost:8080/alerts/status -H "x-api-key: $KEY"
```

Look for `"llm": {"configured": true, "provider": "gemini", "model": "gemini-flash-latest", …}`. When `configured` is `false`, `status` says exactly what is missing, for example `Azure OpenAI (Microsoft Foundry) needs LLM_MODEL and LLM_BASE_URL`. The agent log says the same at startup (`AI model: …` or `No AI model (…)`).

### Examples per provider

| Provider | Secret | ConfigMap |
|---|---|---|
| Groq | `LLM_API_KEY=gsk_…` | `LLM_PROVIDER: "groq"` |
| Claude | `LLM_API_KEY=sk-ant-…` | `LLM_PROVIDER: "anthropic"` |
| Cursor | `LLM_API_KEY=<Cursor key>` | `LLM_PROVIDER: "cursor"` |
| Gemini | `LLM_API_KEY=<Gemini key>` | `LLM_PROVIDER: "gemini"` |
| OpenAI | `LLM_API_KEY=sk-…` | `LLM_PROVIDER: "openai"`, `LLM_MODEL: "<model name>"` |
| Azure OpenAI | `LLM_API_KEY=<resource key>` | `LLM_PROVIDER: "azure-openai"`, `LLM_BASE_URL: "https://<resource>.openai.azure.com/openai/v1"`, `LLM_MODEL: "<deployment name>"` |
| OpenRouter | `LLM_API_KEY=sk-or-…` | `LLM_PROVIDER: "openrouter"` (optionally `LLM_MODEL: "<vendor>/<model>"`) |
| Ollama in the cluster | – | `LLM_PROVIDER: "ollama"`, `LLM_BASE_URL: "http://ollama.ollama.svc:11434/v1"`, `LLM_MODEL: "<model with tool support>"` |
| Any OpenAI-compatible API | `LLM_API_KEY=<key, if needed>` | `LLM_PROVIDER: "custom"`, `LLM_BASE_URL: "https://…/v1"`, `LLM_MODEL: "<model>"` |

---

## 3. Getting a key

| Provider | Steps |
|---|---|
| **Groq** | console.groq.com → **API Keys** → **Create API Key** → copy (starts with `gsk_`, shown once). Free plan limits: https://console.groq.com/docs/rate-limits |
| **Anthropic** | console.anthropic.com → **API Keys** → **Create Key**. Needs billing set up. |
| **Cursor** | Cursor Dashboard → **API Keys** → create a key. A *personal* key bills your own plan. For a team, use a **service account** key from the team settings (Teams/Enterprise plans). Cursor doesn't allow giving third parties usage under your personal account. |
| **Gemini** | aistudio.google.com → **Get API key** → create a key in a Google Cloud project. |
| **OpenAI** | platform.openai.com → **API keys** → **Create new secret key**. |
| **Azure OpenAI / Foundry** | In the Azure portal (or ai.azure.com), create an Azure OpenAI or Foundry resource, **deploy a model** (its deployment name is `LLM_MODEL`), then copy **Keys and Endpoint**. `LLM_BASE_URL` is the endpoint plus `/openai/v1`. |
| **OpenRouter** | openrouter.ai → **Keys** → **Create Key**. |
| **Mistral** | console.mistral.ai → **API Keys**. |

---

## 4. How each kind works

kubesupport supports two provider shapes ([`providers/base.py`](../agent/providers/base.py)):

| Shape | Providers | Who runs the investigation loop |
|---|---|---|
| **Chat provider** | Claude, and every OpenAI-compatible API (Groq, Gemini, OpenAI, Azure OpenAI, OpenRouter, Mistral, Ollama, custom) | kubesupport: it asks the model, runs the tools the model picks, sends the results back, up to `LLM_MAX_STEPS` times. |
| **Agent provider** | Cursor | The provider's own agent. kubesupport gives it the question and its read-only tools as *custom tools*; Cursor's own shell, file and web tools are switched off. |

### Claude ([`claude.py`](../agent/providers/claude.py))
- Anthropic Messages API, with the `submit_rca` tool's **strict schema**, so RCAs always have every field.
- `LLM_EFFORT` (default `high`; also `low`, `medium`, `xhigh`, `max`) controls how much the model thinks.
- `LLM_FALLBACKS: "default"` re-runs a refused request on Anthropic's recommended fallback model; `"off"` disables it.

### OpenAI-compatible APIs ([`openai_compat.py`](../agent/providers/openai_compat.py))
- kubesupport converts its conversation to Chat Completions messages on every call, and the reply back.
- **Rate limits:** on "too many requests" it waits for the `retry-after` time and retries (up to 5 attempts). When the wait would be longer than a minute (a daily limit), it gives up at once and the rules take over.
- **Request size:** each request is kept under `LLM_MAX_REQUEST_CHARS` by shortening the oldest tool results first. A request the server calls too large is resent with less history.
- **Compatibility:** if a server rejects an optional field (a forced tool choice, `reasoning_effort`, `max_completion_tokens`), the request is resent once without it.
- **Azure OpenAI** sends the key as an `api-key` header; all others use `Authorization: Bearer`.
- `reasoning_effort` is sent for Groq's `openai/gpt-oss*` models by default, and for any model when you set `LLM_EFFORT`.

### Cursor ([`cursor.py`](../agent/providers/cursor.py))
- Uses the official Cursor SDK (`cursor-sdk`, included in the image). On Cursor's forum, a Cursor team answer says using the SDK as a backend AI service inside your product is a supported use, billed under the plan of the key's account.
- Each question or RCA creates a short-lived Cursor agent with:
  - `tools=["mcp"]`: Cursor offers custom tools to the model through its `mcp` tool group, so that group is the only one switched on. No shell, no file access, no web; `shell` is also explicitly disallowed. (`tools=[]` would leave the model with no tools at all, able only to answer in text.)
  - kubesupport's read-only tools as its only tools, plus `submit_rca`: no MCP server is configured, so nothing else is in that group;
  - an empty working folder (`/tmp/kubesupport-cursor`), no project or user settings loaded.
- The SDK starts a helper process (its "bridge") on first use. `HOME` is `/tmp` in the image, the only writable place in the pod.
- If the agent answers with `kubectl` instructions without using a tool, it is told once to look the answer up; an RCA submitted without any tool call is rejected and the rules write it instead.
- One Cursor run at a time; `LLM_TIMEOUT_SECONDS` (default 600) cancels a run that takes too long. If the agent doesn't call `submit_rca`, it is reminded `LLM_MAX_STEPS` times (default 2), then the rules take over.
- The helper works under the pod's restrictions: started in the built image as the non-root user, with a read-only filesystem and all capabilities dropped, it comes up in about a second and keeps its state under `/tmp/.cursor`. What tests can't cover is a full run against Cursor's service, which needs a real key.
- The SDK's helper bundles its own Node.js packages. One of them, `undici`, is an unmaintained version with known vulnerabilities, so the image build replaces it with a fixed, checksum-verified release and checks that the helper still loads ([SECURITY.md, section 2](SECURITY.md#2-the-image)).
- Building the image without Cursor: `docker build --build-arg WITH_CURSOR=false agent` (the SDK has a proprietary licence; check Cursor's terms before publishing an image that includes it).

---

## 5. Limits per provider

Defaults from [`providers/catalog.py`](../agent/providers/catalog.py). Every value can be overridden in the ConfigMap.

| Setting | Groq (free tier) | Claude | Cursor | All others |
|---|---|---|---|---|
| `LLM_MAX_STEPS`: model calls per answer or RCA | 8 | 14 | 2 (reminders to submit the RCA) | 12 |
| `LLM_TOOL_OUTPUT_CHARS`: one tool result | 2,500 | 15,000 | 15,000 | 8,000 |
| `LLM_MAX_REQUEST_CHARS`: one request | 14,000 | 200,000 | – | 60,000 |
| `LLM_MAX_OUTPUT_TOKENS`: one reply | 2,500 | 16,000 | – | 4,000 |
| `LLM_TIMEOUT_SECONDS` | 120 per call | 300 per call | 600 per run | 120 per call |
| `LLM_EFFORT` | `medium` (gpt-oss models) | `high` | – | not sent |

For every provider, `LLM_DEADLINE_SECONDS` (default 300) caps a whole answer or RCA; after it, the built-in rules answer, so an alert is never held up for long.

On a **paid Groq plan**, raise `LLM_MAX_REQUEST_CHARS` and `LLM_TOOL_OUTPUT_CHARS` for fuller answers. To save a free plan's daily limit, lower `MAX_LLM_RCA_PER_HOUR` (AI RCAs for alerts per hour; after that, alerts use the rules).

---

## 6. Data and cost

- **What is sent:** the question, and the output of the tools the model calls: pod lists, events, log lines, object listings. Passwords, tokens, API keys, AWS keys and JWTs are replaced by `[REDACTED]` first, and Secrets are never read. Everything goes over HTTPS to the provider you chose, and to no other.
- **Self-hosted option:** with `ollama` (or `custom` pointing inside your network), nothing leaves your network.
- **Cost:** free tiers (Groq, Gemini, some OpenRouter models) cost nothing but are rate-limited. Paid providers bill per token (Cursor: per your plan's usage pricing). Alerts are capped by `MAX_LLM_RCA_PER_HOUR`; questions from Teams are not.

---

## 7. Adding a provider

- **OpenAI-compatible API:** add one `Spec(...)` line to `CATALOG` in [`providers/catalog.py`](../agent/providers/catalog.py) with its name, endpoint, key variable and default model, then a line for it in the `READY` table of [`tests/test_provider_catalog.py`](../agent/tests/test_provider_catalog.py).
- **Anything else:** write a class in `providers/` that implements `ChatProvider.create()` or `AgentProvider.session()` ([`base.py`](../agent/providers/base.py)), add a `kind` for it in `catalog._build()`, and add tests with a fake of its SDK, as [`tests/test_provider_cursor.py`](../agent/tests/test_provider_cursor.py) does.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `"configured": false` in `/alerts/status` | Read `llm.status`: it names the missing setting (`LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`) or the unknown provider name. |
| `unknown LLM_PROVIDER '…'` | Typo; the message lists the valid names. |
| RCAs say `generated_by: heuristic` although a provider is set | See `llm_error` in a `/rca` response or the agent log (`LLM investigation failed`). Wrong key (HTTP 401/403), model name not available to your account (HTTP 404), daily limit used up (`rate limit, retry after …`), or `MAX_LLM_RCA_PER_HOUR` reached. |
| Answers take minutes | Normal on free plans: kubesupport waits out the per-minute limit (log: `Model API returned 429; retrying in …`). |
| OpenAI-compatible model never calls tools | The model doesn't support tool calling; pick another `LLM_MODEL`. |
| Cursor: `the cursor-sdk package is not in this image` | The image was built with `WITH_CURSOR=false`. |
| Cursor: `ConfigurationError` / `AuthenticationError` in the log | Wrong or missing `CURSOR_API_KEY` / `LLM_API_KEY`; a repository-scoped Cursor key (use a user or service-account key); or the model name isn't available on your plan (try another `LLM_MODEL`). |
| Cursor: `Cursor run took longer than …` | Raise `LLM_TIMEOUT_SECONDS`. |
| Cursor answers with `kubectl` commands or says it "could not run any cluster tools" | The pod runs an older image in which the agent's tools were switched off (`tools=[]`). Deploy the current image. The log line `still used no cluster tool` means the agent still isn't reaching the tools. |
| Cursor fails right after start with a file-system error | The helper writes only under `/tmp` (`HOME=/tmp`). Check the pod still mounts the `tmp` volume and that it isn't full (`sizeLimit` in `deployment.yaml`). |
| Azure OpenAI: HTTP 404 | `LLM_MODEL` must be the **deployment name**, and `LLM_BASE_URL` must end in `/openai/v1`. |
| Outbound traffic is restricted | Allow HTTPS to the provider's host: `api.groq.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`, `api.openai.com`, `<resource>.openai.azure.com`, `openrouter.ai`, `api.mistral.ai`. For Cursor, allow the hosts in Cursor's network documentation. |
