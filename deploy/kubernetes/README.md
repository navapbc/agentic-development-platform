# Kubernetes manifests

`mcp-gateway.yaml` and `inbound-webhook.yaml` are the base manifests for a cluster running the AWS Load Balancer Controller. `local/` is a kustomize overlay for a sandbox cluster. It deletes both Ingress resources and exposes `inbound-webhook` through a `LoadBalancer` Service instead.

## Per-environment values

Three values in the base manifests are synthetic and must be replaced for every environment.

- The container image registry `123456789012.dkr.ecr.us-east-1.amazonaws.com`, replaced through the kustomize `images` transformer.
- The ACM certificate ARN `arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000` on each Ingress, replaced through an overlay patch.
- The `MCP_ALLOWED_HOSTS` value `mcp.internal.example,mcp.internal.example:443` on the `mcp-gateway` Deployment, replaced through an overlay patch.

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

The MCP gateway enables the FastMCP DNS rebinding protection from `MCP_ALLOWED_HOSTS`, a comma-separated list of the `Host` header values it accepts. The base manifest sets the synthetic value `mcp.internal.example,mcp.internal.example:443` on the Deployment, so replace it with the hostname its clients resolve. List the hostname both bare and with `:443`, because a client that connects on the default port sends the `Host` header without a port and one that names the port explicitly sends it with the port. A value of `host:*` accepts any port on that hostname. Each entry also contributes an allowed `Origin` of `https://<entry>`, which matches the HTTPS-only listener.

A request whose `Host` header is outside the list is refused with 421, and one whose `Origin` header is outside the derived list is refused with 403. The health paths are served outside that check and answer whatever `Host` the ALB target group health check sends.

FastMCP turns the protection on by itself only when the server binds a loopback address, and the platform image sets `BIND_HOST=0.0.0.0`. Leaving `MCP_ALLOWED_HOSTS` empty in a deployed pod therefore leaves the gateway accepting every `Host` value.

The `local/` overlay patches `MCP_ALLOWED_HOSTS` to an empty value, so the sandbox gateway accepts any `Host`. A sandbox client reaches it through `kubectl port-forward` or the in-cluster service name, and the `Host` header those produce varies with the forwarded port and the DNS form the client uses, so a fixed list would refuse valid sandbox traffic with 421 while the health paths kept the pod green. An environment overlay that fronts the gateway with an ALB sets the hostname instead, as below. Changing the base value means checking `local/` as well, because the overlay inherits anything it does not patch.

Patch the value from the environment overlay `kustomization.yaml`.

```yaml
patches:
  - target:
      kind: Deployment
      name: mcp-gateway
    patch: |
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: mcp-gateway
      spec:
        template:
          spec:
            containers:
              - name: mcp-gateway
                env:
                  - name: MCP_ALLOWED_HOSTS
                    value: mcp.<environment>.<domain>,mcp.<environment>.<domain>:443
```

## Webhook paths

The `inbound-webhook` Ingress routes `/webhook/pr` and `/webhook/jira` to the service, which serves both. Register the GitHub webhook URL as `https://<webhook-hostname>/webhook/pr` and, when `jira_intake` runs in `webhook` or `hybrid` mode in `config/<env>/integrations.yaml`, the Jira webhook URL as `https://<webhook-hostname>/webhook/jira`. The Deployment reads `JIRA_WEBHOOK_SECRET` from the `agentic-platform` secret under the key `jira_webhook_secret`. An empty value leaves the Jira signature check satisfied by any request, so set it to the secret configured at the issuer.

Render an overlay with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/local`.
