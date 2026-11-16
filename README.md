# Agent-Kube — kubesupport for EKS with Microsoft Teams

**kubesupport** is a read-only Kubernetes support agent that runs as one pod in your EKS cluster:

- **Sees the whole cluster, read-only:** every object in every namespace, plus nodes, RBAC and CRDs. Never Secrets, and it never changes anything.
- **Watches** for crash loops, OOM kills, image-pull failures, config errors, stuck Pending pods and NotReady nodes.
- **Diagnoses** each new incident: a root cause analysis with an AI model, or with built-in rules when no API key is set. The model can be Groq (free plan available), Claude, or any OpenAI-compatible service.
- **Alerts your Microsoft Teams chat and channels** (private channels included) with the root cause and fix steps, then a "Resolved" card when it's over.
- **Reports cluster activity:** once a minute, *who* exec'd into a pod, created or deleted a service or app, changed permissions, or changed an image or replica count.
- **Answers questions in a Teams channel:** `@kubesupport status`, `@kubesupport get services payments`, `@kubesupport activity`, or any question in plain words.

Teams is wired up with configuration only (Workflows webhooks and an outgoing webhook). There is no Azure Bot Service, no Entra app registration and no Teams app package. Nothing in AWS is exposed to the internet: questions from Teams arrive through a Cloudflare Tunnel that the cluster opens outward.

| | |
|---|---|
| **Step-by-step install and configuration (start here)** | [Configuration.md](Configuration.md) |
| Full guide (architecture, setup, usage, troubleshooting) | [docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md) |
| Agent code | [agent/](agent) |
| Install files (`kubectl apply`) | [deploy/deployment.yaml](deploy/deployment.yaml), [deploy/cloudflare-tunnel.yaml](deploy/cloudflare-tunnel.yaml) |
| Activity notices setup | [deploy/webhook-cert.sh](deploy/webhook-cert.sh) (applies [deploy/activity-webhook.yaml](deploy/activity-webhook.yaml)) |
| CI (tests, public image build and push to Docker Hub) | [.github/workflows/ci.yml](.github/workflows/ci.yml) |

**Quick start:** follow [Configuration.md](Configuration.md) from Step 0 to Step 9. It covers:
- the Docker Hub image;
- EKS checks;
- the Teams webhooks;
- the Secret;
- editing and applying `deploy/deployment.yaml`;
- activity notices;
- the optional Cloudflare Tunnel for questions from Teams;
- an end-to-end test.
