# Installation and configuration: kubesupport in EKS, connected to Teams

**How it's installed:**

- The agent is a **public Docker image** on your Docker Hub account.
- You install it with **one file**, [`deploy/deployment.yaml`](../deploy/deployment.yaml). That file holds the namespace, the **cluster-wide read-only permissions** (every object in every namespace, never Secrets), the settings and the Deployment.
- **Activity notices** (who exec'd into a pod, created or deleted a service or app, changed permissions, changed an image or scaled) come from an admission webhook. Set it up with one script, [`deploy/webhook-cert.sh`](../deploy/webhook-cert.sh) (Step 7b).
- The optional [`deploy/cloudflare-tunnel.yaml`](../deploy/cloudflare-tunnel.yaml) lets you ask questions from a Teams channel through a Cloudflare Tunnel. The cluster opens that connection outward, so nothing in AWS is exposed to the internet.
- Alerts go to a Teams chat and channel that you own.

Follow the steps in order. Each one says **what to do**, **how to check it worked**, and **what to write down**. For how kubesupport works, see the [documentation index](../README.md#documentation).

> **Where to run the commands:** everything below is **bash**. On Windows, use **Git Bash** (installed with Git for Windows). Replace every `<…>` placeholder with your value.

---

## Step 0 — Fill in this worksheet first

| #   | Value                                                  | Example                                | Yours                                     |
| --- | ------------------------------------------------------ | -------------------------------------- | ----------------------------------------- |
| W1  | Docker Hub user or organization                        | `jaychandra1`                          | jayachandra261020                         |
| W2  | EKS cluster name                                       | `prod-eks`                             | dev-env-cluster                           |
| W3  | AWS region of the cluster                              | `ap-south-1`                           | eu-west-1                                 |
| W4  | Namespaces to watch: **all**, or a list                | `all` or `payments,orders`             | all                                       |
| W5  | Public hostname for questions from Teams               | `kubesupport.example.com`              | [app.appgpc2.xyz](http://app.appgpc2.xyz) |
| W6  | Your domain in Cloudflare (it holds W5)                | `example.com`                          | appgpc2.xyz                               |
| W7  | Microsoft 365 tenant ID (Step 8.5)                     | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |                                           |
| W8  | AI provider and its API key (optional; Step 5.1) | `groq` + `gsk_…` | |

Collected along the way (keep them safe; D1, T1–T3 and C1 work like passwords):

| # | Value | From step |
|---|---|---|
| D1 | Docker Hub access token | 2.2 |
| T1 | Workflows URL: alerts to your chat | 4.1 |
| T2 | Workflows URL: answers in the questions channel | 4.2 |
| T3 | Outgoing webhook security token | 4.3 |
| T4 | Workflows URL: alerts to a private channel (optional) | 4.4 |
| C1 | Cloudflare tunnel token | 8.1 |

---

## Step 1 — Tools on your PC

In **PowerShell**:

```powershell
winget install -e --id Git.Git
winget install -e --id Amazon.AWSCLI
winget install -e --id Kubernetes.kubectl
```

Close and reopen your terminal afterwards so the tools are on the `PATH`. Docker Desktop is only needed if you want to build the image yourself (2.5); GitHub Actions normally builds it.

Sign in to AWS (needed to reach the EKS cluster):

```bash
aws configure            # or: aws configure sso   /   aws sso login --profile <profile>
aws sts get-caller-identity
```

✅ **Check:** shows your AWS account.

---

## Step 2 — Public image on Docker Hub

### 2.1 Create the repository

On **hub.docker.com** (signed in as W1): **Repositories → Create repository**.

- **Name:** `kubesupport`
- **Visibility:** **Public**

> The image contains only the agent's code: no keys, no URLs, no cluster details. Those live in the Kubernetes Secret you create in Step 5. But a public image means anyone can download that code.

### 2.2 Create an access token for GitHub Actions → D1

Docker Hub → your avatar → **Account settings → Personal access tokens → Generate new token**.

- **Description:** `github-actions-kubesupport`
- **Access permissions:** **Read & Write**

Copy the token. 📝 **D1**

### 2.3 Tell GitHub about Docker Hub

On GitHub, open `jaychandra1/Agent-kube` → **Settings → Secrets and variables → Actions**:

| Tab | Name | Value |
|---|---|---|
| **Variables** | `DOCKERHUB_USERNAME` | W1 |
| **Secrets** | `DOCKERHUB_TOKEN` | D1 |

### 2.4 Push the code; CI builds and pushes the image

From the repo folder:

```bash
git add -A
git commit -m "kubesupport: public Docker image + kubectl deployment"
git push origin main
```

Then open GitHub → **Actions** → the latest **CI** run. **Lint & tests** and **Build & push image to Docker Hub** should both go green.

✅ **Check:** `https://hub.docker.com/r/<W1>/kubesupport/tags` shows `latest` and `sha-xxxxxxx`, built for `linux/amd64` and `linux/arm64`.

### 2.5 (Alternative) build and push from your PC

Only if you don't want to use GitHub Actions. This needs Docker Desktop.

```bash
docker login -u <W1>
docker buildx build --platform linux/amd64,linux/arm64 -t docker.io/<W1>/kubesupport:latest --push agent
```

---

## Step 3 — Connect to the EKS cluster and check it's ready

### 3.1 kubectl access

```bash
aws eks update-kubeconfig --region <W3> --name <W2>
kubectl get nodes
```

✅ **Check:** your nodes are listed as `Ready`.

> **If kubectl complains about an invalid kube-config** (this PC's `~/.kube/config` currently has an empty context entry), move it aside and try again:
> `mv ~/.kube/config ~/.kube/config.bak && aws eks update-kubeconfig --region <W3> --name <W2>`
>
> **If you get** `Unauthorized`, ask a cluster admin for an EKS access entry for your IAM identity (`AmazonEKSClusterAdminPolicy`).

### 3.2 You may create cluster-wide permissions

`deployment.yaml` creates ClusterRoles and bindings, so you need this:

```bash
kubectl auth can-i create clusterrolebindings
```

✅ **Check:** prints `yes`.

### 3.3 Nodes can pull from Docker Hub

Nodes need outbound internet access (directly, or through a NAT gateway) to reach `docker.io`. Quick test:

```bash
kubectl run pull-check --image=docker.io/<W1>/kubesupport:latest --restart=Never --command -- python -c "print('pull ok')"
kubectl logs pull-check        # → pull ok
kubectl delete pod pull-check
```

✅ **Check:** prints `pull ok`. If the pod stays in `ImagePullBackOff`, see the troubleshooting table.

### 3.4 Network policies (recommended)

`deploy/deployment.yaml` and `deploy/cloudflare-tunnel.yaml` include **NetworkPolicies**:

- only the tunnel pods may connect to kubesupport's Teams port (8080);
- no pod may connect to kubesupport's internal API (8000);
- nothing may connect to the tunnel pods.

EKS enforces NetworkPolicies only when the VPC CNI's network policy feature is on. Check:

```bash
kubectl -n kube-system get daemonset aws-node -o yaml | grep -e "--enable-network-policy"
```

✅ **Check:** `--enable-network-policy=true`.

If it says `false` (and `vpc-cni` is an EKS add-on in your cluster), turn it on:

```bash
aws eks update-addon --region <W3> --cluster-name <W2> --addon-name vpc-cni \
  --configuration-values '{"enableNetworkPolicy":"true"}'
```

- **What this changes:** only pods that a NetworkPolicy selects are affected. If other namespaces already contain NetworkPolicies that were never enforced, they start being enforced too, so check with your cluster admin first.
- **Other engines:** a cluster running Calico or Cilium enforces the policies already.
- **No engine at all:** kubesupport still works, but the policies are not enforced.

### 3.5 Optional

- **Pod CPU/memory for the AI model** needs **metrics-server**. Check with `kubectl top pods -A`. To add it: `aws eks create-addon --region <W3> --cluster-name <W2> --addon-name metrics-server`, or use its official Helm chart if the add-on isn't offered for your version.
- **Restricted outbound traffic:**
  - allow the agent HTTPS (443) to your AI provider's API host (e.g. `api.groq.com`; see [docs/AI_PROVIDERS.md](AI_PROVIDERS.md)) and to your Workflows URL hosts (T1/T2, e.g. `*.powerplatform.com` or `*.logic.azure.com`);
  - allow the tunnel pods outbound TCP **and** UDP port **7844** to Cloudflare (`region1.v2.argotunnel.com`, `region2.v2.argotunnel.com`).

---

## Step 4 — Microsoft Teams (you're the owner, so it's all clicks)

### 4.1 Alerts to your chat → T1

1. In Teams, open **Apps → Workflows** (or **…** on a chat → **Workflows**).
2. Pick the template **"Send webhook alerts to a chat"**.
3. Name it `kubesupport alerts` and choose the chat for alerts: your own chat with the Workflows bot, or a group chat.
4. **Create**, then **copy the URL**. 📝 **T1**

Want alerts in a channel too, including a private one? See 4.4. You can give the agent several URLs.

### 4.2 Answers in the questions channel → T2

1. Use a **standard channel** in a team you own, e.g. `#kubesupport`. *Asking questions* needs a standard channel, because Teams outgoing webhooks don't work in private channels. Alerts can still go to private channels (4.4).

   For security, make it a **private team** with only the people who may ask kubesupport as members: only a team's members can @mention its outgoing webhook (8.5).
2. In that channel: **…** → **Workflows** → **"Send webhook alerts to a channel"** → select that team and channel → **Create** → **copy the URL**. 📝 **T2**
3. **Make answers appear under the question (same thread).** As created, this workflow starts a *new post* for every answer. kubesupport sends the question's thread with each answer (the field `threadMessageId`), so change the workflow's last step once:
   1. In Teams, open the **Workflows** app, find the workflow from step 2 and choose **Edit**.
   2. Open the step **Send each adaptive card**. Inside it is the action **Post card in a chat or channel**. Delete that action.
   3. In its place, add the Microsoft Teams action **Reply with an adaptive card in a channel** and fill in:
      - **Post as:** `Flow bot`
      - **Post in:** `Channel`
      - **Team** and **Channel:** your team and the questions channel
      - **Message ID:** switch the field to **Expression** and enter `triggerBody()?['threadMessageId']`
      - **Adaptive Card:** switch to **Expression** and enter `items('Send_each_adaptive_card')?['content']`
   4. **Save**.

   Use this workflow **only for answers** (T2). The alert workflows (T1, T4) keep **Post card in a chat or channel**: an alert doesn't belong to a thread. The field names above are how the designer showed them when this was written; if yours differ slightly, the two expressions are what matters.

### 4.3 The outgoing webhook (questions) → T3

1. Team **…** → **Manage team** → **Apps** tab → click the **Upload an app ⌄** dropdown (top left, next to **Get more apps**) → **Create an outgoing webhook**. (Older Teams versions showed it as a link at the bottom right of the Apps tab.)
   - **Not in the dropdown?** Custom apps are switched off for you:
     - First, as team owner: team **…** → **Manage team** → **Settings** → **Member permissions** → tick **Allow members to upload custom apps**.
     - If it's still missing, a Teams admin must allow custom apps: Teams admin center → **Teams apps** → **Manage apps** → **Org-wide app settings** → **Let users interact with custom apps** on. Then **Teams apps** → **Setup policies** → your policy → **Upload custom apps** on.
     - Admin changes can take a few hours to reach your Teams client.
2. Fill in:
   - **Name:** `kubesupport` (what people @mention)
   - **Callback URL:** `https://<W5>/teams/webhook`
   - **Description:** `kubesupport for <W2>`
3. **Create**, then **copy the security token**. It's shown only once. 📝 **T3**

### 4.4 (Optional) Alerts to a private channel → T4

Microsoft's docs state that Workflows webhooks support private channels. As the channel's owner:

1. In the **private channel**: **…** → **Workflows** → **"Send webhook alerts to a channel"**. Select the team and the private channel → **Create** → **copy the URL**. 📝 **T4**
2. **If the private channel isn't offered in the template:**
   1. Create the workflow for any channel.
   2. Open **make.powerautomate.com** → **My flows** → that flow → **Edit**.
   3. In the **Post card in a chat or channel** step, pick the private channel, then **Save**. The URL stays the same.
3. **If posts fail later** with *BotNotInConversationRoster* in the flow's run history, change **Post as** in that step from **Flow bot** to **User**.

Repeat for as many chats and channels as you like. In Step 5 they all go into `TEAMS_ALERT_WEBHOOK_URLS`, each with a name you choose.

> Alerts only? Skip 4.2, 4.3 and Step 8.

---

## Step 5 — Namespace and Secret

### 5.1 AI provider and key (W8, optional)

kubesupport uses an AI model to answer questions in plain words and to write root cause analyses. One image supports many providers; you pick one with `LLM_PROVIDER` (Step 6.2) and put its key in the Secret as `LLM_API_KEY`. Without a provider it still sends alerts, answers commands and writes rule-based RCAs.

| Provider | `LLM_PROVIDER` | Where to get the key |
|---|---|---|
| Groq (free plan available) | `groq` | console.groq.com → API Keys |
| Anthropic Claude | `anthropic` | console.anthropic.com → API Keys |
| Cursor (paid plan) | `cursor` | Cursor Dashboard → API Keys |
| Google Gemini (free tier available) | `gemini` | aistudio.google.com → Get API key |
| OpenAI | `openai` | platform.openai.com → API keys (also set `LLM_MODEL`) |
| Azure OpenAI / Microsoft Foundry | `azure-openai` | Azure portal → your resource → Keys and Endpoint (also `LLM_BASE_URL`, `LLM_MODEL`) |
| OpenRouter, Mistral, Ollama, any OpenAI-compatible API | `openrouter`, `mistral`, `ollama`, `custom` | see [docs/AI_PROVIDERS.md](AI_PROVIDERS.md) |

📝 **W8** = the provider name and its key. Every provider, its defaults, limits and quirks: [docs/AI_PROVIDERS.md](AI_PROVIDERS.md).

**Example: Groq.**
1. Sign in at **https://console.groq.com**.
2. Open **API Keys** → **Create API Key**, name it `kubesupport`, and copy the key. It starts with `gsk_` and is shown only once.
3. Optional check from your laptop. A list of models means the key works:

```bash
curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer <W8 key>" | head -c 300
```

**Groq's free plan is rate-limited** (tokens per minute and per day: https://console.groq.com/docs/rate-limits). kubesupport keeps each request small and waits when Groq says "too many requests", so an AI answer or RCA can take a few minutes. When the daily limit is used up, alerts carry a rule-based RCA until it resets.

### 5.2 Namespace and Secret

Every key is optional; leaving one out turns that feature off. Use **single quotes**, because the Workflows URLs contain `&` and `%`. The key names are the environment variables the agent reads.

```bash
kubectl create namespace kubesupport

kubectl -n kubesupport create secret generic kubesupport-secrets \
  --from-literal=LLM_API_KEY='<W8 key>' \
  --from-literal=AGENT_API_KEY="$(openssl rand -hex 16)" \
  --from-literal=TEAMS_ALERT_WEBHOOK_URLS='chat=<T1>,oncall-private=<T4>' \
  --from-literal=TEAMS_REPLY_WEBHOOK_URL='<T2>' \
  --from-literal=TEAMS_OUTGOING_WEBHOOK_SECRET='<T3>'
```

| Key | What it's for | If you leave it out |
|---|---|---|
| `LLM_API_KEY` | The API key of the AI provider chosen with `LLM_PROVIDER` (5.1). The provider's own variable name (e.g. `GROQ_API_KEY`, `CURSOR_API_KEY`) works too. | Rule-based RCAs only; free-form questions are refused |
| `AGENT_API_KEY` | Protects the internal API (`/alerts/test`, `/rca`, …) and is needed for the checks in Step 9 | Internal API off (answers 403) |
| `TEAMS_ALERT_WEBHOOK_URLS` | Where alerts go: chats, standard or private channels. Comma-separated; each entry `name=URL` (name optional, shown in logs and test results instead of the secret URL). Leave out `,oncall-private=<T4>` if you skipped 4.4. | No alerts sent |
| `TEAMS_REPLY_WEBHOOK_URL` | Where slow answers go (questions channel). With the workflow change in 4.2, they land in the question's thread. | Uses the first alert URL |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | Verifies questions come from your Teams webhook | `/teams/webhook` switched off |

Switching provider later? Put the new key in `LLM_API_KEY` (Step 10, *Change a secret*) and change `LLM_PROVIDER` in the ConfigMap.

✅ **Check:** `kubectl -n kubesupport describe secret kubesupport-secrets` lists the five keys with their sizes in bytes (the values aren't shown).

---

## Step 6 — Edit `deploy/deployment.yaml`

Three edits, all marked `EDIT` in the file.

### 6.1 The image

Point it at your Docker Hub repository:

```bash
sed -i 's#<dockerhub-user>#<W1>#' deploy/deployment.yaml
grep "image: docker.io" deploy/deployment.yaml      # → image: docker.io/<W1>/kubesupport:latest
```

With `:latest` and `imagePullPolicy: Always`, a restart picks up the newest image. To pin a version instead, use a `sha-xxxxxxx` tag from Docker Hub.

### 6.2 The settings (ConfigMap `kubesupport-config`)

| Setting | Set to |
|---|---|
| `CLUSTER_NAME` | W2. Shown on every alert, so you know which cluster it's about. |
| `ALLOWED_NAMESPACES` | `"*"` (default) for incident alerts and Teams answers in every namespace, or W4 as a comma-separated list to narrow them. Permissions are cluster-wide either way (6.3). |
| `LLM_PROVIDER` | The provider from W8, e.g. `"groq"`; `"none"` for rules only. Only this provider is used. |
| `LLM_MODEL`, `LLM_BASE_URL` | Leave `""` for the provider's defaults. Required for `openai` (model), `azure-openai` (both), `ollama` and `custom` (both). |
| `TIMEZONE` | Your time zone for RCA times, e.g. `"Asia/Kolkata"` (default UTC). |

The other settings have sensible defaults and a comment each in the file:

- check interval and reminder interval;
- the AI model's effort, step and size limits (`LLM_*`; `""` = the provider's defaults);
- the hourly cap on AI RCAs;
- optional Teams tenant and user restrictions.

On Groq's free plan, lowering `MAX_LLM_RCA_PER_HOUR` to `3` saves the daily token limit for the incidents that come later in the day.

### 6.3 Permissions: cluster-wide, read-only, never Secrets

Nothing to edit here. This is what the file grants:

| Object in the file                                                       | Grants                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClusterRoleBinding `kubesupport-view` → Kubernetes' built-in `view` role | Read **every namespaced object in every namespace**: pods, logs, services, endpoints, ingresses, deployments, statefulsets, daemonsets, jobs, cronjobs, configmaps, PVCs, HPAs, network policies, events, and custom resources that register with `view`. `view` **never includes Secrets.** |
| ClusterRole + ClusterRoleBinding `kubesupport-cluster-read`              | Read what `view` leaves out: nodes, namespaces, persistent volumes, storage classes, ingress classes, priority/runtime classes, CRDs, API services, webhook configurations, **RBAC** (roles, bindings, cluster roles), and pod/node metrics                                                  |

Nothing grants Secrets, and no rule allows creating, changing or deleting anything.

---

## Step 7 — Deploy

```bash
kubectl apply -f deploy/deployment.yaml
```

✅ **Check:**

```bash
kubectl -n kubesupport get pods                 # kubesupport-…   1/1   Running
kubectl -n kubesupport logs deploy/kubesupport    # "Watcher started (every 60.0s, 1 Teams alert target(s))"
```

Your Teams chat (T1) shows a card: **"[] kubesupport started - …"**.

✅ **Check the permissions** (cluster-wide read, never Secrets, never writes):

```bash
SA=system:serviceaccount:kubesupport:kubesupport
kubectl auth can-i list pods            --as=$SA --all-namespaces   # yes
kubectl auth can-i list services        --as=$SA --all-namespaces   # yes
kubectl auth can-i get pods/log         --as=$SA -n kube-system     # yes
kubectl auth can-i list nodes           --as=$SA                    # yes
kubectl auth can-i list clusterroles    --as=$SA                    # yes
kubectl auth can-i get secrets          --as=$SA -n default         # no
kubectl auth can-i delete pods          --as=$SA -n default         # no
kubectl auth can-i create deployments   --as=$SA -n default         # no
```

---

## Step 7b — Activity notices (deploys, restarts, config changes, exec, …)

kubesupport can tell you **who** did what in the cluster:

| Reported | Example line in Teams |
|---|---|
| Exec / attach / port-forward into a pod, and `kubectl debug` containers | `alice@corp exec into pod payments/api-1 (container app): sh` |
| Rollout restarts | `Restarted Deployment payments/api (rollout restart) by alice@corp` |
| Every other change to a Deployment, StatefulSet or DaemonSet: images, environment variables (names only), resources, anything else in the pod template, strategy, pause and resume | `Updated Deployment payments/api (image app: api:1.2 → api:1.3; env app: LOG_LEVEL) by ci-deployer` |
| Scaling | `Scaled Deployment payments/api 2 → 5 by alice@corp` |
| Helm installs and upgrades | `Helm install/upgrade of payments/api (revision 12) by alice@corp` |
| ConfigMaps created, changed, deleted: the keys that changed, never the values | `Changed ConfigMap payments/api-config (keys: LOG_LEVEL) by alice@corp` |
| Secrets created, changed, deleted: the name and how many keys changed, nothing else | `Changed Secret payments/db (1 key(s) changed) by bob@corp` |
| Pods deleted or started by hand, and evictions (`kubectl drain`) | `Deleted Pod payments/api-7d9f8b-x by alice@corp` |
| Nodes cordoned, uncordoned, changed or deleted | `Cordoned Node ip-10-0-1-1 by alice@corp` |
| RBAC, ServiceAccounts, NetworkPolicies and CustomResourceDefinitions created, changed, deleted | `Created ClusterRoleBinding temp-admin by bob@corp` |
| Every other kind in the webhook rules, created, changed, deleted: Services, Ingresses, Namespaces, Jobs, CronJobs, PVCs, PVs, HPAs, PodDisruptionBudgets, quotas, storage and priority classes. A change names the fields, labels or annotations that differ. | `Changed Service payments/api (spec.type) by alice@corp` |

**How it works:**

- The Kubernetes API server sends kubesupport a copy of those requests through a *validating admission webhook*.
- kubesupport **always answers "allowed"**. With `failurePolicy: Ignore`, nothing is ever blocked, even if kubesupport is down.
- Changes made by Kubernetes' and EKS's own components are filtered out (the pods a rollout replaces, for example), so you see what people and pipelines did.
- Lines carry names only: which keys of a ConfigMap changed, never their values; for a Secret just the name and how many keys changed.
- Everything from one minute is grouped into **one "Cluster activity" card**, sent to the same Teams targets as alerts.

### 7b.1 Run the setup script

After Step 7, from the repo folder:

```bash
bash deploy/webhook-cert.sh
```

It does four things:

1. creates a private certificate (valid 10 years) and stores it in Secret `kubesupport-webhook-tls`;
2. applies [`deploy/activity-webhook.yaml`](../deploy/activity-webhook.yaml) with the certificate's CA filled in;
3. restarts kubesupport;
4. waits until it's ready.

It's safe to run again; for example, to renew the certificate.

### 7b.2 Check it's on

```bash
kubectl -n kubesupport port-forward svc/kubesupport 8080:80      # second terminal
KEY=$(kubectl -n kubesupport get secret kubesupport-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
curl -s localhost:8080/activity/status -H "x-api-key: $KEY"
```

✅ **Check:** `"webhook_certificate": true`.

### 7b.3 Test it

```bash
kubectl -n default create service clusterip kubesupport-test --tcp=80
kubectl -n default delete service kubesupport-test
```

✅ **Check:**

- `/activity/status` shows `webhook_calls_total` above 0;
- within about a minute, a **"Cluster activity: 2 event(s)"** card arrives in Teams, naming you;
- `kubectl exec` into any pod appears the same way, under **Exec / attach / port-forward**.

> **If** `webhook_calls_total` **stays 0:**
>
> - the EKS control plane can't reach the pod on port **8443**. Managed node groups using the cluster security group allow this. With custom node security groups, add an inbound rule for TCP 8443 from the cluster security group (`aws eks describe-cluster --name <W2> --query cluster.resourcesVpcConfig.clusterSecurityGroupId`);
> - your `kubectl` requests go ahead regardless.
>
> **Secrets:** Kubernetes includes the whole object in the webhook request; kubesupport keeps only the Secret's *name*. If Secret objects must never reach kubesupport, delete `"secrets"` from the core rule in `deploy/activity-webhook.yaml` and run the script again.
>
> **Noise:** to stop reporting a busy automation account, add its name prefix to `ACTIVITY_IGNORE_USERS` in the ConfigMap (e.g. `system:serviceaccount:argocd:`), or a kind to `ACTIVITY_IGNORE_KINDS` (e.g. `Pod,Job`), apply, restart.
>
> **Upgrading:** the webhook rules live in the cluster. After deploying an image that reports new kinds of activity, run `bash deploy/webhook-cert.sh` once more so the API server sends them.

---

## Step 8 — Questions from Teams through Cloudflare Tunnel (skip if alerts only)

Teams sends every `@kubesupport` message from Microsoft's cloud to `https://<W5>/teams/webhook`. With Cloudflare Tunnel, **nothing in AWS accepts connections from the internet**: no load balancer, no public IP, no open port. Two small `cloudflared` pods open the connection *outward* to Cloudflare, and Cloudflare passes Teams' requests down it.

```text
Teams (Microsoft's cloud) ──HTTPS──▶ Cloudflare <W5>: firewall lets only POST /teams/webhook through
   ──▶ tunnel, opened outward by 2 cloudflared pods (namespace kubesupport)
   ──▶ Service kubesupport-teams ──▶ kubesupport pod port 8080 (serves only POST /teams/webhook)
   ──▶ HMAC signature check ──▶ your tenant and allowed people only ──▶ answer
```

| Layer | What it stops |
|---|---|
| No load balancer, no public IP, no inbound port in AWS | Nobody on the internet can reach the cluster directly |
| Cloudflare firewall and rate-limit rules (8.3) | Anything except `POST /teams/webhook` is blocked at Cloudflare; floods are cut off |
| Port 8080 serves only `/teams/webhook` | Even if a Cloudflare setting is wrong, the internal API can't be reached through the tunnel |
| NetworkPolicies (3.4) | Inside the cluster, only the tunnel pods can reach port 8080 |
| HMAC signature (T3) | Only your Teams outgoing webhook can make kubesupport do anything |
| Private team, `TEAMS_TENANT_ID`, `TEAMS_ALLOWED_USER_IDS` (8.5) | Only the people you choose can ask |

> **Started the ALB setup earlier?** Remove it:
>
> - `kubectl -n kubesupport delete ingress kubesupport --ignore-not-found`;
> - delete any existing DNS record for W5;
> - the ACM certificate is no longer needed.

### 8.1 Create the tunnel → C1

1. Cloudflare dashboard → **Networking** → **Tunnels** → **Create a tunnel**. On older dashboards the path is **Zero Trust** → **Networks** → **Tunnels**; there, choose the **Cloudflared** type.
2. Name it `kubesupport-<W2>` and create it.
3. On the install screen, choose **Docker**. Copy **only the token**: the long string after `--token` in the command shown. Don't run the command. 📝 **C1**

### 8.2 Route W5 to kubesupport

In the tunnel, open the **Routes** tab (older dashboards: **Public Hostname**) → **Add route** → **Published application**.

> Not **Private hostname** or **Private network**: those only work for devices running the Cloudflare One (WARP) client. Teams calls from Microsoft's cloud without it, so it would never reach kubesupport.

Fill in:

| Field | Value |
|---|---|
| Hostname | subdomain `kubesupport`, domain W6 (`appmco.xyz`), giving W5 |
| Path (if the form shows it) | `^/teams/webhook$` |
| Service | `http://kubesupport-teams.kubesupport.svc.cluster.local` |

Save the route.

- Cloudflare creates the DNS record for W5 itself: a proxied CNAME to `<tunnel-id>.cfargotunnel.com`.
- If W5 already has a DNS record (for example from the ALB setup), delete it first, or the route can't be saved.

### 8.3 Cloudflare firewall rules for W5

In the Cloudflare dashboard, open the domain W6.

**1. Only the Teams webhook gets through.** **Security** → **WAF** (newer dashboards: **Security rules**) → **Custom rules** → **Create rule**:

- Name: `kubesupport: only POST /teams/webhook`;
- **Edit expression**, and paste:

```text
(http.host eq "kubesupport.appmco.xyz" and not (http.request.method eq "POST" and http.request.uri.path eq "/teams/webhook"))
```

- Action: **Block** → **Deploy**.

**2. Rate limit.** Same place → **Rate limiting rules** → **Create rule**:

- Name `kubesupport: rate limit`; match `http.host eq "kubesupport.appmco.xyz"`;
- counting by IP, more than **20** requests per **10 seconds** → **Block** for 10 seconds.

**3. HTTPS.** **SSL/TLS** → **Edge Certificates**:

- **Always Use HTTPS**: on;
- **Minimum TLS Version**: `TLS 1.2`.

Cloudflare's free Universal certificate covers W5, so you need no certificate in AWS.

**Don't:**

- put a **Cloudflare Access** application on W5: Teams can't sign in, so every question would be blocked;
- allowlist your office or VPN IP addresses: Teams calls from Microsoft's cloud, not from your users' network.

### 8.4 Store the token and start the tunnel

kubesupport must run an image built from this version, which listens on port 8080. If CI pushed a new image since your last deploy:

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport
```

Then:

```bash
kubectl -n kubesupport create secret generic kubesupport-tunnel --from-literal=TUNNEL_TOKEN='<C1>'
kubectl apply -f deploy/cloudflare-tunnel.yaml
kubectl -n kubesupport rollout status deploy/kubesupport-tunnel
```

✅ **Check:** two `kubesupport-tunnel-…` pods are Running and Ready, and the tunnel shows **Healthy** in the Cloudflare dashboard.

From your laptop:

```bash
curl -i -X POST https://<W5>/teams/webhook     # → 401: reached kubesupport, refused without a Teams signature
curl -i https://<W5>/alerts/status             # → 403: blocked by Cloudflare (8.3)
```

✅ **Check:** `401`, then `403`. Anything else: see Troubleshooting.

The outgoing webhook's **Callback URL** (Step 4.3) is exactly `https://<W5>/teams/webhook`.

### 8.5 Only the people you choose → W7

1. **Private team:** hold the questions channel (4.2) in a **private** team whose members are only the people allowed to ask. Only members of a team can @mention its outgoing webhook.
2. **Your organization only:** Entra admin center (entra.microsoft.com) → **Overview** → copy the **Tenant ID**. 📝 **W7**
3. **Named people only:** Entra admin center → **Users** → open each person → copy their **Object ID**.
4. Put both in the ConfigMap in `deploy/deployment.yaml`:

```yaml
  TEAMS_TENANT_ID: "<W7>"
  TEAMS_ALLOWED_USER_IDS: "<object-id-1>,<object-id-2>"
```

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport
```

✅ **Check:** someone not on the list who @mentions kubesupport gets *"you're not on this agent's allowed-users list"*, and nothing is run.

### 8.6 Questions about kubesupport itself

Nothing to switch on: `BOUNDARIES_ENABLED: "true"` and `PROTECTED_NAMESPACES: "kubesupport"` are already in the ConfigMap. Anyone who asks how kubesupport is set up, which AI model it uses, for its rules, or for anything in its own namespace is told *"You are not authorised to ask this question. It has been reported to the administrators."*, and a card with their name and question goes to the administrators ([RULES.md, section 6](RULES.md#6-boundaries-what-may-not-be-asked)).

By default that card goes to the alert targets. To send it to a channel only administrators can read (recommended):

1. Create a Workflows webhook in a private channel, as in Step 4.4.
2. Add its URL to the Secret and restart:

```bash
kubectl -n kubesupport patch secret kubesupport-secrets --type merge -p '{"stringData":{"TEAMS_SECURITY_WEBHOOK_URLS":"security=<URL>"}}'
```

```bash
kubectl -n kubesupport rollout restart deploy/kubesupport
```

If kubesupport runs in a namespace with another name, put that name in `PROTECTED_NAMESPACES`.

✅ **Check:** `@kubesupport how are you set up?` is answered with the "not authorised" sentence, and the card "Restricted question refused" arrives with your name. `@kubesupport pods kubesupport` gives the same. `@kubesupport what can you do?` is answered normally.

---

## Step 9 — Test end to end

### 9.1 Alerts

Open a second terminal and start a port-forward:

```bash
kubectl -n kubesupport port-forward svc/kubesupport 8080:80
```

In the first terminal:

```bash
KEY=$(kubectl -n kubesupport get secret kubesupport-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
curl -s -X POST localhost:8080/alerts/test -H "x-api-key: $KEY"
curl -s localhost:8080/alerts/status -H "x-api-key: $KEY"
```

✅ **Check:**

- a **"Test alert from kubesupport"** card arrives in your chat;
- `/alerts/test` shows `"ok": true` for every target (`chat`, `oncall-private`, …), and the card appears in each chat and channel;
- `/alerts/status` shows `"running": true` and, if you set W8, `"llm": {"configured": true, "provider": "groq", "model": "openai/gpt-oss-120b", …}`. If `configured` is `false`, its `status` says what is missing.

### 9.2 Questions (if you did Step 8)

In the channel from 4.2, type `@`, pick **kubesupport**, then try:

1. `@kubesupport help`: the command list appears in the thread within seconds.
2. `@kubesupport status`: open incidents.
3. `@kubesupport pods <namespace>`: a pod table.
4. `@kubesupport is anything unhealthy in <namespace>?`: "Looking into it…", then an answer card **in the same thread** (if you did 4.2 step 3; otherwise as a new post in the channel). This needs W8. On a free plan (Groq, Gemini) the answer can take a few minutes.
5. `@kubesupport which ingress URLs does the cluster expose?`: the real URLs, backends and load balancer addresses, not instructions. (`@kubesupport get ingresses` gives the same list at once, without the AI.)

### 9.3 A real incident

In any namespace kubesupport watches (all of them with the default `ALLOWED_NAMESPACES: "*"`):

```bash
NS=default
kubectl -n $NS create deployment crash-test \
  --image=public.ecr.aws/docker/library/busybox:latest -- sh -c 'echo boom; exit 1'
```

✅ **Check:** within about 1–2 minutes, an alert card about `crash-test` arrives, with the root cause and fix steps.

```bash
kubectl -n $NS delete deployment crash-test
```

✅ **Check:** about 10 minutes later, a **"Resolved"** card arrives.

**Done.** kubesupport watches the cluster, alerts your chat and channels, reports cluster activity, and answers in your channel.

---

## Step 10 — Day-to-day

Everything after installation (new images, changing settings and secrets, switching the AI provider, rotating tokens, uninstalling) is in [docs/OPERATIONS.md](OPERATIONS.md#day-to-day). How people use kubesupport is in [docs/USAGE.md](USAGE.md).

---

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| CI fails at **Docker login** | `DOCKERHUB_USERNAME` must be a **variable**, `DOCKERHUB_TOKEN` a **secret**, and the token must have **Read & Write** (Step 2.3) |
| Pod `InvalidImageName` | The image still says `<dockerhub-user>` (Step 6.1) |
| Pod `ImagePullBackOff` | The repository isn't public; the tag doesn't exist; nodes have no internet/NAT route to Docker Hub (Step 3.3); or Docker Hub's anonymous pull limit was hit (`toomanyrequests`): pin a tag and set `imagePullPolicy: IfNotPresent`. See `kubectl -n kubesupport describe pod -l app.kubernetes.io/name=kubesupport` |
| Pod `CrashLoopBackOff` | `kubectl -n kubesupport logs deploy/kubesupport --previous` |
| No pod at all; `rollout status` times out at "0 out of 1 new replicas have been updated"; `kubectl -n kubesupport get events` says `violates PodSecurity "restricted"` for a container `opentelemetry-auto-instrumentation-…` | The **CloudWatch Observability add-on** (Application Signals *auto monitor*) adds OpenTelemetry containers to pods that back a Service, and they don't meet the namespace's "restricted" level. `deployment.yaml` opts kubesupport out (`instrumentation.opentelemetry.io/inject-*: "false"`): `kubectl apply -f deploy/deployment.yaml`, then check with `kubectl -n kubesupport get deploy kubesupport -o jsonpath='{.spec.template.metadata.annotations}'` that they say `"false"`. If the add-on sets them back to `"true"` (older add-on versions), exclude the namespace in the add-on instead: add `"exclude": {"java": {"namespaces": ["kubesupport"]}, "python": {…}, "nodejs": {…}, "dotnet": {…}}` under `manager.applicationSignals.autoMonitor` in the add-on's configuration. |
| Agent logs show `Forbidden` / `403` | The ClusterRoleBindings from `deployment.yaml` weren't applied, or were deleted. Re-apply it and run the Step 7 `auth can-i` checks. |
| `/activity/status` shows `"webhook_certificate": false` | `deploy/webhook-cert.sh` hasn't run, or the pod wasn't restarted afterwards. The agent log says `Activity webhook off: …`. |
| `webhook_calls_total` stays 0 | The EKS control plane can't reach port 8443 on the pod; allow it from the cluster security group (Step 7b.3) |
| An action is missing from the activity card | It was done by an ignored caller (`ACTIVITY_IGNORE_USERS`; Kubernetes' own controllers are ignored on purpose), it's not one of the reported kinds, or kubesupport was restarting at that moment |
| Activity cards too noisy | Add the busy account's prefix to `ACTIVITY_IGNORE_USERS`, or a namespace to `ACTIVITY_IGNORE_NAMESPACES` |
| No "kubesupport started" card | `TEAMS_ALERT_WEBHOOK_URLS` missing or wrong; `/alerts/test` shows the HTTP status. Check the workflow is still **on** in the Workflows app. |
| Alerts worked, then stopped | Workflows run under your Microsoft 365 account; if its connection expired, recreate the workflow and update the Secret |
| One target fails in `/alerts/test` (e.g. `oncall-private`) | That workflow is off or deleted, or it can't post there. If its run history shows *BotNotInConversationRoster*, set **Post as** to **User** (Step 4.4). |
| Private channel not offered in the Workflows template | Create the workflow for any channel, then switch the channel in Power Automate (Step 4.4) |
| Some alerts missing during a burst | Teams throttles more than 4 posts per second per webhook. The agent retries 3 times; look for `Teams target '…' returned 429` in the agent log. |
| Tunnel pods `CreateContainerConfigError` | Secret `kubesupport-tunnel` is missing or has no `TUNNEL_TOKEN` key (8.4) |
| Tunnel pods restart or stay not Ready | `kubectl -n kubesupport logs deploy/kubesupport-tunnel`. An authentication error means a wrong token: recreate the Secret with C1. Connection errors mean outbound TCP and UDP port 7844 to Cloudflare is blocked (Step 3.5). |
| Cloudflare error **1033** | The tunnel isn't connected: no tunnel pod is running (see the two rows above) |
| `502 Bad Gateway` from Cloudflare | The tunnel is up but can't reach kubesupport. Check: the route's Service is `http://kubesupport-teams.kubesupport.svc.cluster.local` (8.2); the kubesupport pod runs the new image with port 8080 (8.4); the tunnel pods carry the label `app.kubernetes.io/name: kubesupport-tunnel`, which the NetworkPolicy allows |
| `403` on `POST /teams/webhook`, or Teams says "Sorry, there was a problem…" | Cloudflare blocked it: **Security** → **Events** shows which rule. Check the WAF expression (8.3). **Bot Fight Mode** on the domain can also block Teams' calls; switch it off for the domain if Events shows it. |
| `curl https://<W5>/…` can't resolve the name | The route wasn't saved, so there is no DNS record (8.2); `nslookup <W5>` should return Cloudflare addresses |
| `404` instead of `401` | `TEAMS_OUTGOING_WEBHOOK_SECRET` isn't in the Secret, or the pod wasn't restarted after adding it; or the route's Path doesn't match `/teams/webhook` (8.2) |
| A teammate gets "not on this agent's allowed-users list" | Add their Entra Object ID to `TEAMS_ALLOWED_USER_IDS` (8.5), apply, restart |
| Teams says "Sorry, there was a problem…" after `@kubesupport` | Wrong token (agent log: `Rejected /teams/webhook call`), or the URL isn't reachable |
| "Looking into it…" but no answer card | `TEAMS_REPLY_WEBHOOK_URL` wrong, or that workflow is off (agent log: `Teams target … returned …`). After the thread change (4.2 step 3), also open the workflow's **run history**: a failed **Reply with an adaptive card** step shows why (wrong Team/Channel, or the Message ID expression mistyped). |
| Answers arrive as a new post instead of in the question's thread | The answers workflow still uses **Post card in a chat or channel**: do 4.2 step 3. kubesupport already sends `threadMessageId`. |
| The AI answers with `kubectl` commands, or says it "could not run any cluster tools" | An older image, in which the Cursor agent had no access to kubesupport's tools. Deploy the current image (Step 10). If it persists, the agent log has a warning `still used no cluster tool`. |
| "namespace … not allowed" | Add it to `ALLOWED_NAMESPACES` (or set it to `"*"`) |
| Answers say "No AI model is configured" | `LLM_PROVIDER` is `none` or misspelled, or the key/model/endpoint it needs is missing. `/alerts/status` → `llm.status` says exactly what; so does the agent log at startup (`No AI model (…)`). Restart the pod after changing the Secret. |
| RCAs say `generated_by: heuristic` although a key is set | See `llm_error` in a `/rca` response. Possible causes: wrong key; the provider's daily limit used up (`rate limit, retry after …`); `MAX_LLM_RCA_PER_HOUR` reached; outbound HTTPS to the provider blocked. Per-provider notes: [docs/AI_PROVIDERS.md](AI_PROVIDERS.md#troubleshooting) |
| AI answers take a few minutes | Normal on free plans such as Groq's: kubesupport waits for the per-minute token limit (agent log: `Model API returned 429; retrying in …`). A paid Groq plan is faster; then also raise `LLM_MAX_REQUEST_CHARS` and `LLM_TOOL_OUTPUT_CHARS` for fuller answers. |
| Too many incident alerts | Narrow `ALLOWED_NAMESPACES`, or raise `ALERT_COOLDOWN_MINUTES` |

---

## Checklist

- [ ] 0  Worksheet W1–W8 filled in
- [ ] 1  Git, AWS CLI, kubectl installed; `aws sts get-caller-identity` works
- [ ] 2  Docker Hub repo `kubesupport` (public) + token; GitHub variable/secret set; pushed; CI green; image on Docker Hub
- [ ] 3  `kubectl get nodes` works; can create cluster roles; `pull-check` printed `pull ok`; network policies enforced (3.4)
- [ ] 4  Teams: T1 (alerts chat), T2 (answers channel), T3 (outgoing webhook token)
- [ ] 5  Namespace `kubesupport` + Secret `kubesupport-secrets` created
- [ ] 6  `deployment.yaml` edited: image, CLUSTER_NAME (ALLOWED_NAMESPACES optional)
- [ ] 7  Applied; pod Running; "kubesupport started" card; `kubectl auth can-i` checks as expected
- [ ] 7b `bash deploy/webhook-cert.sh` run; `/activity/status` shows the certificate; test Service produced a "Cluster activity" card
- [ ] 8  Tunnel created (C1); route W5 → `kubesupport-teams`; WAF and rate-limit rules; Secret `kubesupport-tunnel`; tunnel pods Ready; `curl` gives 401 and 403; private team; `TEAMS_TENANT_ID` and `TEAMS_ALLOWED_USER_IDS` set; "how are you set up?" is refused and reported (8.6)
- [ ] 9  Test card received; `@kubesupport help` answered; crash test alerted and resolved
