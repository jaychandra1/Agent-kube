# Configuration — install kubesupport in EKS and connect it to Teams

**How it's installed:**

- The agent is a **public Docker image** on your Docker Hub account.
- You install it with **one file**, [`deploy/deployment.yaml`](deploy/deployment.yaml). That file holds the namespace, the **cluster-wide read-only permissions** (every object in every namespace, never Secrets), the settings and the Deployment.
- **Activity notices** (who exec'd into a pod, created or deleted a service or app, changed permissions, changed an image or scaled) come from an admission webhook. Set it up with one script, [`deploy/webhook-cert.sh`](deploy/webhook-cert.sh) (Step 7b).
- The optional [`deploy/ingress.yaml`](deploy/ingress.yaml) lets you ask questions from a Teams channel.
- Alerts go to a Teams chat and channel that you own.

Follow the steps in order. Each one says **what to do**, **how to check it worked**, and **what to write down**. For how the agent works, see [docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md).

> **Where to run the commands:** everything below is **bash**. On Windows, use **Git Bash** (installed with Git for Windows). Replace every `<…>` placeholder with your value.

---

## Step 0 — Fill in this worksheet first

| #   | Value                                                  | Example                                                 | Yours                  |
| --- | ------------------------------------------------------ | ------------------------------------------------------- | ---------------------- |
| W1  | Docker Hub user or organization                        | `jaychandra1`                                           | jayachandra261020      |
| W2  | EKS cluster name                                       | `prod-eks`                                              | dev-env-cluster        |
| W3  | AWS region of the cluster                              | `ap-south-1`                                            | eu-west-1              |
| W4  | Namespaces to watch: **all**, or a list                | `all` or `payments,orders`                              | all                    |
| W5  | Public hostname for questions from Teams               | `kubesupport.example.com`                               | kubesupport.appmco.xyz |
| W6  | Your ingress class (Step 3.4)                          | `nginx` or `alb`                                        | alb                    |
| W7  | TLS for that ingress                                   | cert-manager ClusterIssuer name, or ACM certificate ARN | ACM Certificate        |
| W8  | Groq API key (optional: AI answers and RCAs; Step 5.1) | `gsk_…`                                                 |                        |

Collected along the way (keep them safe; D1 and T1–T3 work like passwords):

| # | Value | From step |
|---|---|---|
| D1 | Docker Hub access token | 2.2 |
| T1 | Workflows URL: alerts to your chat | 4.1 |
| T2 | Workflows URL: answers in the questions channel | 4.2 |
| T3 | Outgoing webhook security token | 4.3 |
| T4 | Workflows URL: alerts to a private channel (optional) | 4.4 |

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

### 3.4 Ingress class (only for questions from Teams) → W6

```bash
kubectl get ingressclass
```

📝 **Write down the NAME** (e.g. `nginx` or `alb`). The ingress must be **internet-facing**, because Teams calls it from the internet.

- NGINX: `kubectl get svc -A | grep -i ingress` should show a public `*.elb.amazonaws.com` address.
- ALB: you'll set `scheme: internet-facing` in Step 8.

### 3.5 Optional

- **Pod CPU/memory for the AI model** needs **metrics-server**. Check with `kubectl top pods -A`. To add it: `aws eks create-addon --region <W3> --cluster-name <W2> --addon-name metrics-server`, or use its official Helm chart if the add-on isn't offered for your version.
- **Restricted outbound traffic:** allow the agent HTTPS (443) to `api.groq.com` (or `api.anthropic.com` if you use Claude) and to your Workflows URL hosts (T1/T2, e.g. `*.powerplatform.com` or `*.logic.azure.com`).

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
2. In that channel: **…** → **Workflows** → **"Send webhook alerts to a channel"** → select that team and channel → **Create** → **copy the URL**. 📝 **T2**

### 4.3 The outgoing webhook (questions) → T3

1. Team **…** → **Manage team** → **Apps** tab → **Create an outgoing webhook** (bottom right).
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

### 5.1 AI model key (W8, optional)

kubesupport uses an AI model to answer questions in plain words and to write root cause analyses. It works with **Groq** (it has a free plan), with **Claude** (an Anthropic key), or with any other OpenAI-compatible service. Without a key it still sends alerts, answers commands and writes rule-based RCAs.

To get a Groq key:

1. Sign in at **https://console.groq.com**.
2. Open **API Keys** → **Create API Key**, name it `kubesupport`, and copy the key. It starts with `gsk_` and is shown only once. That's W8.
3. Optional check from your laptop. A list of models means the key works:

```bash
curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer <W8>" | head -c 300
```

**Groq's free plan is rate-limited:** a small number of tokens per minute and per day (current numbers: https://console.groq.com/docs/rate-limits).

- kubesupport keeps each request small and waits whenever Groq says "too many requests", so an AI answer or RCA can take a few minutes.
- When the daily limit is used up, alerts carry a rule-based RCA until it resets, and questions get an error message.
- A paid Groq plan removes most of the waiting.

### 5.2 Namespace and Secret

Every key is optional; leaving one out turns that feature off. Use **single quotes**, because the Workflows URLs contain `&` and `%`. The key names are the environment variables the agent reads.

```bash
kubectl create namespace kubesupport

kubectl -n kubesupport create secret generic kubesupport-secrets \
  --from-literal=GROQ_API_KEY='<W8>' \
  --from-literal=AGENT_API_KEY="$(openssl rand -hex 16)" \
  --from-literal=TEAMS_ALERT_WEBHOOK_URLS='chat=<T1>,oncall-private=<T4>' \
  --from-literal=TEAMS_REPLY_WEBHOOK_URL='<T2>' \
  --from-literal=TEAMS_OUTGOING_WEBHOOK_SECRET='<T3>'
```

| Key | What it's for | If you leave it out |
|---|---|---|
| `GROQ_API_KEY` | AI answers and RCAs, with Groq's `openai/gpt-oss-120b` model (5.1) | Rule-based RCAs only; free-form questions are refused |
| `AGENT_API_KEY` | Protects the internal API (`/alerts/test`, `/rca`, …) | Internal API open to anything in the cluster |
| `TEAMS_ALERT_WEBHOOK_URLS` | Where alerts go: chats, standard or private channels. Comma-separated; each entry `name=URL` (name optional, shown in logs and test results instead of the secret URL). Leave out `,oncall-private=<T4>` if you skipped 4.4. | No alerts sent |
| `TEAMS_REPLY_WEBHOOK_URL` | Where slow answers go (questions channel) | Uses the first alert URL |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | Verifies questions come from your Teams webhook | `/teams/webhook` switched off |

Using **Claude** instead of Groq? Replace the `GROQ_API_KEY` line with `--from-literal=ANTHROPIC_API_KEY='sk-ant-…'`. If both keys are in the Secret, Claude is used, unless you set `LLM_PROVIDER: "groq"` in 6.2.

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

The other settings have sensible defaults and a comment each in the file:

- check interval and reminder interval;
- the AI model: `LLM_PROVIDER` `"auto"` uses whichever key is in the Secret; `AGENT_MODEL` `""` means the provider's default model;
- the request-size limits for Groq;
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

## Step 7b — Activity notices (exec, new services, security changes, updates)

kubesupport can tell you **who** did what in the cluster:

| Reported | Example line in Teams |
|---|---|
| Exec / attach / port-forward into a pod | `alice@corp exec into pod payments/api-7d9f8b-x (container app): sh` |
| Created/deleted Services, Ingresses, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, Namespaces | `Created Service payments/api-public by alice@corp` |
| Security: Roles/Bindings/ClusterRoles/ClusterRoleBindings created/changed/deleted; ServiceAccounts, Secrets, ConfigMaps created/deleted (names only) | `Created ClusterRoleBinding temp-admin by bob@corp` |
| Workload updates: image changes, scaling | `Updated Deployment payments/api image app: api:1.2 → api:1.3 by ci-deployer` |

**How it works:**

- The Kubernetes API server sends kubesupport a copy of those requests through a *validating admission webhook*.
- kubesupport **always answers "allowed"**. With `failurePolicy: Ignore`, nothing is ever blocked, even if kubesupport is down.
- Changes made by Kubernetes' and EKS's own components are filtered out (pods created by a Deployment, for example).
- Everything from one minute is grouped into **one "Cluster activity" card**, sent to the same Teams targets as alerts.

### 7b.1 Run the setup script

After Step 7, from the repo folder:

```bash
bash deploy/webhook-cert.sh
```

It does four things:

1. creates a private certificate (valid 10 years) and stores it in Secret `kubesupport-webhook-tls`;
2. applies [`deploy/activity-webhook.yaml`](deploy/activity-webhook.yaml) with the certificate's CA filled in;
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
> **Secrets:** Kubernetes includes the whole object in the webhook request; kubesupport keeps only the Secret's *name*. If Secret objects must never reach kubesupport, delete `"secrets"` from the second rule in `deploy/activity-webhook.yaml` and run the script again.
>
> **Noise:** to stop reporting a busy automation account, add its name prefix to `ACTIVITY_IGNORE_USERS` in the ConfigMap (e.g. `system:serviceaccount:argocd:`), apply, restart.

---

## Step 8 — Public address for questions (skip if alerts only)

### 8.1 Edit `deploy/ingress.yaml`

- Set `host` to W5 in **both** places.
- Set `ingressClassName` to W6.
- Set TLS:
  - **NGINX + cert-manager:** set the `cert-manager.io/cluster-issuer` annotation to your ClusterIssuer (`kubectl get clusterissuer`). Keep the `tls:` section.
  - **AWS ALB + ACM:** follow the comment in the file. Delete the cert-manager line and the `tls:` section, set `ingressClassName: alb`, and uncomment the `alb.ingress.kubernetes.io/`* annotations with your certificate ARN. Keep `healthcheck-path: /healthz`: the agent returns 404 on `/`, so without it the load balancer marks the pod unhealthy.

To find the ACM certificate ARN:

```bash
aws acm list-certificates --region <W3> --query "CertificateSummaryList[].[DomainName,CertificateArn]" --output table
```

### 8.2 Apply, then point DNS at it

```bash
kubectl apply -f deploy/ingress.yaml
kubectl -n kubesupport get ingress kubesupport     # wait for the ADDRESS column, then copy it
```

In your DNS (Route 53 or other), create a **CNAME** (or Route 53 **alias**) from W5 to that address. Wait a few minutes, then:

```bash
curl -i -X POST https://<W5>/teams/webhook
```

✅ **Check:** `401` (meaning "missing signature", which is good). A timeout, certificate error or 404 means DNS, TLS or the ingress isn't right yet.

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
- `/alerts/status` shows `"running": true` and, if you set W8, `"llm": {"configured": true, "provider": "groq", "model": "openai/gpt-oss-120b"}`.

### 9.2 Questions (if you did Step 8)

In the channel from 4.2, type `@`, pick **kubesupport**, then try:

1. `@kubesupport help`: the command list appears in the thread within seconds.
2. `@kubesupport status`: open incidents.
3. `@kubesupport pods <namespace>`: a pod table.
4. `@kubesupport is anything unhealthy in <namespace>?`: "Looking into it…", then an answer card in the channel. This needs W8. On Groq's free plan the answer can take a few minutes.

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

| Task | How |
|---|---|
| **Roll out a new image** (after CI pushed a new `latest`) | `kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Pin or roll back a version** | `kubectl -n kubesupport set image deployment/kubesupport agent=docker.io/<W1>/kubesupport:sha-xxxxxxx`, and put the same tag in `deployment.yaml` so the next `apply` doesn't undo it |
| **Change a setting** | Edit the ConfigMap in `deployment.yaml`, then `kubectl apply -f deploy/deployment.yaml && kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Narrow incident alerts to some namespaces** | Set `ALLOWED_NAMESPACES` (e.g. `"payments,orders"`) in the ConfigMap, apply, restart. Read access stays cluster-wide. |
| **Pause activity notices** | `ACTIVITY_ENABLED: "false"` in the ConfigMap, apply, restart. To remove the webhook completely: `kubectl delete validatingwebhookconfiguration kubesupport-activity` |
| **Renew the webhook certificate** | `bash deploy/webhook-cert.sh` (any time; it's valid 10 years) |
| **Change a secret** (new key, new Workflows URL) | Re-run the Step 5 `create secret` command with `--dry-run=client -o yaml \| kubectl apply -f -` on the end, then `kubectl -n kubesupport rollout restart deploy/kubesupport` |
| **Pause alerts** | `WATCH_ENABLED: "false"` in the ConfigMap, apply, restart |
| **See what it sees** | `/alerts/status`, `/activity/status`, `/activity` (Step 9.1 port-forward), `@kubesupport activity` in Teams, or `kubectl -n kubesupport logs deploy/kubesupport -f` |
| **Uninstall** | `kubectl delete validatingwebhookconfiguration kubesupport-activity` **first** (it's cluster-wide), then `kubectl delete -f deploy/ingress.yaml -f deploy/deployment.yaml` (this also deletes the namespace and the Secrets). Finally remove the outgoing webhook and the workflows in Teams. |

---

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| CI fails at **Docker login** | `DOCKERHUB_USERNAME` must be a **variable**, `DOCKERHUB_TOKEN` a **secret**, and the token must have **Read & Write** (Step 2.3) |
| Pod `InvalidImageName` | The image still says `<dockerhub-user>` (Step 6.1) |
| Pod `ImagePullBackOff` | The repository isn't public; the tag doesn't exist; nodes have no internet/NAT route to Docker Hub (Step 3.3); or Docker Hub's anonymous pull limit was hit (`toomanyrequests`): pin a tag and set `imagePullPolicy: IfNotPresent`. See `kubectl -n kubesupport describe pod -l app.kubernetes.io/name=kubesupport` |
| Pod `CrashLoopBackOff` | `kubectl -n kubesupport logs deploy/kubesupport --previous` |
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
| `curl https://<W5>/teams/webhook` times out | DNS not pointing at the ingress yet, or the load balancer is internal-only (Step 3.4) |
| Certificate error | cert-manager hasn't issued yet (`kubectl -n kubesupport get certificate`), or the ACM certificate doesn't cover W5 |
| `404` instead of `401` | `TEAMS_OUTGOING_WEBHOOK_SECRET` not in the Secret, or the pod not restarted after adding it; or `host` in `ingress.yaml` ≠ W5 |
| Teams says "Sorry, there was a problem…" after `@kubesupport` | Wrong token (agent log: `Rejected /teams/webhook call`), or the URL isn't reachable |
| "Looking into it…" but no answer card | `TEAMS_REPLY_WEBHOOK_URL` wrong, or that workflow is off (agent log: `Teams webhook … returned …`) |
| "namespace … not allowed" | Add it to `ALLOWED_NAMESPACES` (or set it to `"*"`) |
| Answers say "No AI model is configured" | `GROQ_API_KEY` (or `ANTHROPIC_API_KEY`) missing from the Secret, or the pod not restarted after adding it |
| RCAs say `generated_by: heuristic` although a key is set | See `llm_error` in a `/rca` response. Possible causes: wrong key; Groq's daily limit used up (`rate limit, retry after …`); `MAX_LLM_RCA_PER_HOUR` reached; outbound HTTPS to `api.groq.com` (or `api.anthropic.com`) blocked |
| AI answers take a few minutes | Normal on Groq's free plan: kubesupport waits for the per-minute token limit (agent log: `Model API returned 429; retrying in …`). A paid Groq plan is faster; then also raise `LLM_MAX_REQUEST_CHARS` and `LLM_TOOL_OUTPUT_CHARS` for fuller answers. |
| Too many incident alerts | Narrow `ALLOWED_NAMESPACES`, or raise `ALERT_COOLDOWN_MINUTES` |

---

## Checklist

- [ ] 0  Worksheet W1–W8 filled in
- [ ] 1  Git, AWS CLI, kubectl installed; `aws sts get-caller-identity` works
- [ ] 2  Docker Hub repo `kubesupport` (public) + token; GitHub variable/secret set; pushed; CI green; image on Docker Hub
- [ ] 3  `kubectl get nodes` works; can create cluster roles; `pull-check` printed `pull ok`; ingress class known
- [ ] 4  Teams: T1 (alerts chat), T2 (answers channel), T3 (outgoing webhook token)
- [ ] 5  Namespace `kubesupport` + Secret `kubesupport-secrets` created
- [ ] 6  `deployment.yaml` edited: image, CLUSTER_NAME (ALLOWED_NAMESPACES optional)
- [ ] 7  Applied; pod Running; "kubesupport started" card; `kubectl auth can-i` checks as expected
- [ ] 7b `bash deploy/webhook-cert.sh` run; `/activity/status` shows the certificate; test Service produced a "Cluster activity" card
- [ ] 8  `ingress.yaml` edited and applied; DNS record; `curl -X POST` returns 401
- [ ] 9  Test card received; `@kubesupport help` answered; crash test alerted and resolved
