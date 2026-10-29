# Kubernetes manifests

`mcp-gateway.yaml` and `inbound-webhook.yaml` are the base manifests for a cluster running the AWS Load Balancer Controller. `local/` is a kustomize overlay for a sandbox cluster. It deletes both Ingress resources and exposes `inbound-webhook` through a `LoadBalancer` Service instead.

## Per-environment values

Two values in the base manifests are synthetic and must be replaced for every environment.

- The container image registry `123456789012.dkr.ecr.us-east-1.amazonaws.com`, replaced through the kustomize `images` transformer.
- The ACM certificate ARN `arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000` on each Ingress, replaced through an overlay patch.

Both Ingress resources declare an HTTPS-only listener through `alb.ingress.kubernetes.io/listen-ports` and pin `alb.ingress.kubernetes.io/ssl-policy` to `ELBSecurityPolicy-TLS13-1-2-2021-06`, which negotiates TLS 1.3 and TLS 1.2 only. The controller refuses to reconcile an Ingress whose `certificate-arn` does not resolve in the account, so an unreplaced value fails provisioning instead of falling back to a plaintext listener.

### Overriding the certificate ARN

Add a patch to the environment overlay `kustomization.yaml` for each Ingress. Both need their own certificate, so both need a patch.

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
          alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:<region>:<account-id>:certificate/<webhook-certificate-id>
  - target:
      kind: Ingress
      name: mcp-gateway
    patch: |
      apiVersion: networking.k8s.io/v1
      kind: Ingress
      metadata:
        name: mcp-gateway
        annotations:
          alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:<region>:<account-id>:certificate/<gateway-certificate-id>
```

`inbound-webhook` fronts an internet-facing ALB, so its certificate must cover the public hostname registered as the GitHub webhook URL. `mcp-gateway` fronts an internal ALB, so its certificate must cover the in-VPC hostname its clients resolve.

## Target group health checks

Both Ingress resources set `alb.ingress.kubernetes.io/healthcheck-path: /health`. Each service answers `/health`, `/healthz`, and `/` with a 200 and a JSON body, so the ALB default of `GET /` also succeeds. The health check reaches the pod over HTTP on the traffic port. Terminating TLS at the listener does not change it.

## MCP gateway host header

The MCP gateway runs the FastMCP streamable-HTTP transport with its DNS rebinding protection at the library default, which accepts only loopback `Host` values. A request that an ALB forwards to `/mcp` carries the client hostname and is refused with 421. The health paths are served outside that check and answer normally. Configuring the allowed host list is tracked separately and is not covered by these manifests.

Render an overlay with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/local`.
