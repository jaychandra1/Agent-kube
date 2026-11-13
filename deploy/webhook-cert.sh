#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Sets up kubesupport's activity webhook (exec / new services / RBAC changes / updates):
#   1. creates a private CA and a TLS certificate for kubesupport-webhook.kubesupport.svc
#      (valid 10 years),
#   2. stores it in Secret kubesupport-webhook-tls (namespace kubesupport),
#   3. applies deploy/activity-webhook.yaml with the CA filled in,
#   4. restarts kubesupport so it starts the HTTPS webhook on port 8443.
#
# Run it after `kubectl apply -f deploy/deployment.yaml`. Safe to re-run (it replaces the
# certificate). Needs kubectl (with cluster-admin rights), openssl and base64; Git Bash on
# Windows has openssl and base64.
#
# Usage:  ./deploy/webhook-cert.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

NS=kubesupport
SVC=kubesupport-webhook
DAYS=3650
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for tool in kubectl openssl base64 sed; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }
done
kubectl get namespace "$NS" >/dev/null 2>&1 || {
  echo "Namespace $NS not found. Run: kubectl apply -f deploy/deployment.yaml"; exit 1; }

# Work with relative file names inside $WORK: avoids path translation issues in Git Bash.
cd "$WORK"

cat > ca.conf <<EOF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = kubesupport-webhook-ca
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

cat > server.conf <<EOF
[req]
distinguished_name = dn
prompt = no
req_extensions = v3_req
[dn]
CN = ${SVC}.${NS}.svc
[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
DNS.1 = ${SVC}
DNS.2 = ${SVC}.${NS}
DNS.3 = ${SVC}.${NS}.svc
DNS.4 = ${SVC}.${NS}.svc.cluster.local
EOF

echo "==> Creating CA and server certificate"
openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" -config ca.conf -keyout ca.key -out ca.crt 2>/dev/null
openssl req -newkey rsa:2048 -nodes -config server.conf -keyout tls.key -out tls.csr 2>/dev/null
openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days "$DAYS" \
  -extensions v3_req -extfile server.conf -out tls.crt 2>/dev/null

echo "==> Storing it in Secret $NS/kubesupport-webhook-tls"
kubectl -n "$NS" create secret tls kubesupport-webhook-tls --cert=tls.crt --key=tls.key \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Applying the webhook (Service + ValidatingWebhookConfiguration)"
CA_BUNDLE="$(base64 < ca.crt | tr -d '\n')"
sed "s|<CA_BUNDLE>|${CA_BUNDLE}|" "$HERE/activity-webhook.yaml" | kubectl apply -f -

echo "==> Restarting kubesupport so it serves the webhook on :8443"
kubectl -n "$NS" rollout restart deploy/kubesupport
kubectl -n "$NS" rollout status deploy/kubesupport --timeout=180s

echo
echo "Done. Check it with:"
echo "  kubectl -n $NS port-forward svc/kubesupport 8080:80"
echo "  curl -s localhost:8080/activity/status -H \"x-api-key: <AGENT_API_KEY>\""
echo "Then run e.g. 'kubectl -n default create service clusterip kubesupport-test --tcp=80' and expect"
echo "a 'Cluster activity' card in Teams within about a minute."
