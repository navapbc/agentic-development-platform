# Kubernetes manifests

`mcp-gateway.yaml` and `inbound-webhook.yaml` are the base manifests for a cluster running the AWS Load Balancer Controller. `local/` is a kustomize overlay for a sandbox cluster. It deletes both Ingress resources and exposes `inbound-webhook` through a `LoadBalancer` Service instead.

## Per-environment values

Two values in the base manifests are synthetic and must be replaced for every environment.

- The container image registry `123456789012.dkr.ecr.us-east-1.amazonaws.com`, replaced through the kustomize `images` transformer.
- The ACM certificate ARN `arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000` on each Ingress, replaced through an overlay patch.

Both Ingress resources declare an HTTPS-only listener through `alb.ingress.kubernetes.io/listen-ports` and pin `alb.ingress.kubernetes.io/ssl-policy` to `ELBSecurityPolicy-TLS13-1-2-2021-06`, which negotiates TLS 1.3 and TLS 1.2 only. The controller refuses to reconcile an Ingress whose `certificate-arn` does not resolve in the account, so an unreplaced value fails provisioning instead of falling back to a plaintext listener.

### Overriding the certificate ARN

Add a patch to the environment overlay `kustomization.yaml` for each Ingress.

```yaml
patches:
  - target:
      kind: Ingress
      name: inbound-webhook
    patch: |
      apiVersion: networking.k8s.io/v1
      kind: Ingress
      metadata:
        name: inbound-webhook
        annotations:
          alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:<region>:<account-id>:certificate/<certificate-id>
```

`inbound-webhook` fronts an internet-facing ALB, so its certificate must cover the public hostname registered as the GitHub webhook URL. `mcp-gateway` fronts an internal ALB, so its certificate must cover the in-VPC hostname its clients resolve.

Render an overlay with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/local`.
