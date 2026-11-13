# ImagePullBackOff / ErrImagePull
Wrong image tag, tag never pushed (CI failed), private registry without access, or registry rate limiting.
Verify the tag in the deployment against the registry and the last CI run. On EKS with ECR, the node role needs
AmazonEC2ContainerRegistryReadOnly (managed node groups have it); other registries need an imagePullSecret.
