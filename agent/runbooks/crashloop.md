# CrashLoopBackOff
Check `get_pod_logs previous=true` for the crashed container's last output. Common causes: missing env var or secret,
bad config, failed database or dependency connection, exit code 1 on startup. Check `get_deployment` rollout history:
a crash that starts right after a new image or config revision points at that change. Fix the config or image in the
source the workload is deployed from (Helm values, manifests), then roll out again; `kubectl rollout undo` restores the
previous revision quickly.
