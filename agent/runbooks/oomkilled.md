# OOMKilled (exit code 137)
Container exceeded its memory limit. Compare `top_pods` usage with the limits from `describe_pod`. Fix: raise the
memory limit in the workload's Helm values or manifest if the usage is legitimate, or fix the leak. Check
`get_deployment` rollout history for a recent revision that changed memory behaviour.
