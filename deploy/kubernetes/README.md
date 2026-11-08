# Kubernetes manifests

The files in this directory are the base manifests for the platform workloads on a cluster running the AWS Load Balancer Controller. `local/` is a kustomize overlay for a workstation cluster such as Docker Desktop Kubernetes, and `aws/` is the overlay for the EKS cluster of the `private-bedrock` composition root.

| Manifest | Workloads |
| --- | --- |
| `activity-worker.yaml` | The activity worker with its ServiceAccount, the `workspaces` volume claim, and OPA as a sidecar init container bound to the pod's loopback |
| `review-worker.yaml` | The review worker, which mounts the `workspaces` claim read-only |
| `litellm.yaml` | LiteLLM with a ClusterIP Service on 4000 |
| `sandbox.yaml` | The activity worker's Role and RoleBinding in `agent-sandbox` for the sandbox and gate pods, and the gate pod egress policy |
| `mcp-gateway.yaml`, `inbound-webhook.yaml` | The MCP gateway and the webhook front door, each with its ServiceAccount and an Ingress |

The workers read every platform setting from the `agentic-platform-env` Secret, the environment's config files from the `platform-config` ConfigMap and the `platform-config-secrets` Secret, and the compose or AWS endpoints from the `agentic-platform` ConfigMap. The OpenHands sandbox egress policies live in `openhands/policies`. No manifest mounts a `hostPath` volume. The platform containers set `args` and leave the image entrypoint in place, because the entrypoint trusts the platform CA, writes the environment's config files, and exports the mounted secrets before the process starts.

## The local overlay

The overlay replaces each image with the local build, deletes both Ingress resources, narrows the `workspaces` claim to `ReadWriteOnce`, exposes `inbound-webhook` on 8140 and LiteLLM on 4000 through `LoadBalancer` Services, and mounts the `aws-credentials` Secret into LiteLLM and the review worker through `credential_process`. `inv k8s-reload` and `inv k8s-deploy` render it through `scripts/k8s_render.py`, which adds what derives from the workstation at deploy time:

- the `agentic-platform-env` Secret from `.env`, with each compose service address replaced by `K8S_COMPOSE_ADDRESS` and the port compose publishes, and the workstation-only keys such as `AWS_PROFILE` left out
- the `platform-config` ConfigMap from the YAML files in `config/<env>`, and the `platform-config-secrets` Secret from its other files, such as the signing keys and the ticket store's AWS profiles
- the `promotion-policies` ConfigMap from `config/policies`, and the `litellm-config` ConfigMap from `config/platform`
- the image tag of the current build and its `AGENT_BUILD_ID`
- a Bedrock relay sidecar in LiteLLM and the review worker under the `jumphost` and `ssh-bastion` connectivity providers
- the namespaces `K8S_NAMESPACE` and `OPENHANDS_SANDBOX_NAMESPACE` name

Nothing derived is written to disk. The renderer pipes the manifests to `kubectl apply`.

## The AWS overlay

`aws/` runs the activity worker with its OPA sidecar, LiteLLM, the inbound webhook, and the MCP gateway on EKS. The review worker runs on ECS, so the overlay leaves it out. No rendered object is a Kubernetes Secret.

- Each workload runs as its ServiceAccount, which the `eks-cluster` module binds to a Pod Identity role. A `SecretProviderClass` per workload mounts the Secrets Manager values that role may read through the Secrets Store CSI driver at `/mnt/secrets`, one file per variable. The platform entrypoint exports each file as the variable its name names when `PLATFORM_SECRETS_DIR` is set, and the LiteLLM container does the same in its command. The database secret is mounted as `PGUSER` and `PGPASSWORD`, and the Temporal CA as `TEMPORAL_TLS_CA`. Every secret a `SecretProviderClass` names must hold a current value, or the pod waits in `ContainerCreating`.
- The platform containers read their settings from two ConfigMaps. `agentic-platform-env` holds the environment the ECS tasks receive, from the `platform_environment` of the `eks` output, which the deploy writes. `agentic-platform-eks` sets the EKS settings, the in-cluster OpenHands sandbox, the gate and scanner pods, the FIPS endpoints, the direct Bedrock path, the `sandbox` node pool, and the ECR copies of the agent-server, buildah, semgrep, and trivy images. The `platform-config` ConfigMap, which the deploy writes from `environment_config`, holds the environment's config files at `/app/config/env`.
- LiteLLM receives only the region, the FIPS setting, the platform CA, and the Langfuse host, because LiteLLM treats `DATABASE_URL` as its own store.
- The `workspaces` claim binds to the static `agentic-platform-checkouts` volume, the checkouts access point of the workspace file system, mounted with TLS and IAM authorization. The activity worker mounts it at `/repositories`, the path the ECS review worker and repository intelligence mount it at, so a checkout path the activity worker records resolves in those tasks.
- The `temporal-clients` SecurityGroupPolicy gives the activity worker and inbound webhook pods the cluster, Temporal client, and internal load balancer client groups. The `internal-clients` policy gives LiteLLM and the MCP gateway the cluster and internal load balancer client groups.
- The activity worker reads the ticket store through the `rebar-tickets-reader` and `rebar-tickets-writer` profiles in `rebar-store-aws-config`, whose `credential_source = EcsContainer` the Pod Identity credential endpoint satisfies.
- With `SANDBOX_NODE_POOL=sandbox`, the sandbox, gate, checks, and scanner pods the activity worker creates select the `sandbox` node group and tolerate its taint.
- The gate, checks, and scanner pods carry no AWS identity, and buildah inside them pulls their images. For an image in ECR, the activity worker requests an ECR authorization token with its Pod Identity role and copies a containers auth file into the pod over the exec stream, so the token stays out of every command line and the API server audit log. The pod reads it as `REGISTRY_AUTH_FILE` for the pull and deletes it before any project or scanner code runs. The role pulls only from the semgrep and trivy repositories and the repositories in `eks_gate_image_repository_arns`, so the token reaches nothing else.
- `network-policies.yaml` selects every pod that is not host-networked in `kube-system`, `aws-secrets-manager`, `agentic-dev-platform`, and `agent-sandbox`, so the cluster runs in strict network policy mode, where a pod no policy selects is denied. The baselines admit DNS, HTTPS, and the EKS Pod Identity agent at `169.254.170.23` on 80, the platform pods also reach the database, Temporal, LiteLLM, and the sandbox agent-server, and only CoreDNS, the controller webhook, LiteLLM, and the two load balancer targets accept ingress.
- The `gate-engine-egress` policy admits DNS, LiteLLM, and HTTPS to the application subnets, which hold the ECR interface endpoints, and to the S3 ranges that carry the image layers through the S3 gateway endpoint. In the `aws` registry mode it also admits HTTPS to every address outside the VPC, for the package indexes DNS Firewall resolves, and in the `external` mode HTTPS to the registry's ranges.
- With a `registries.yaml` in `platform-config`, the activity worker copies a buildah `registries.conf` drop-in into each gate, checks, and scanner pod that sends every mirrored upstream prefix to its mirror, so an image named `python:3.12-slim` is pulled from the mirror unchanged. In the `external` mode the pod also receives the package index variables and a netrc and npmrc built from `REGISTRY_TOKEN`, which the activity worker mounts from the `registry_token` secret.

Render it with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/aws`. The rendered manifests carry the synthetic values below. `inv eks-deploy` runs the [eks-deployer](../../infra/terraform/eks-deployer) task, which replaces them with the environment's values in `scripts/eks_render.py`, adds the generated ConfigMaps, and applies the result from inside the VPC.

## Per-environment values

Four values in the base manifests are synthetic and must be replaced for every environment.

- The container image registry `123456789012.dkr.ecr.us-east-1.amazonaws.com`, replaced through the kustomize `images` transformer.
- The ACM certificate ARN `arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000` on each Ingress, replaced through an overlay patch.
- The `MCP_ALLOWED_HOSTS` value `mcp.internal.example,mcp.internal.example:443` on the `mcp-gateway` Deployment, replaced through an overlay patch.
- The `alb.ingress.kubernetes.io/inbound-cidrs` value `192.0.2.0/24` on the `mcp-gateway` Ingress, a documentation range that admits no client, replaced through an overlay patch with the CIDRs of the subnets that hold the gateway's clients. See [MCP gateway authentication](#mcp-gateway-authentication).

The `aws/` overlay adds these synthetic values, each of which the `eks` output of `private-bedrock` names for the environment.

- The ECR account `123456789012` and the tags of the platform, OPA, and LiteLLM images, and the image settings in `agentic-platform-eks`, from `images` and `image_settings`.
- The volume handle `fs-00000000000000000::fsap-00000000000000000` of `agentic-platform-checkouts`, from `workspace_file_system_id` and `workspace_access_point_id`.
- The groups `sg-00000000000000001`, `sg-00000000000000002`, and `sg-00000000000000003` in the SecurityGroupPolicies, from `cluster_security_group_id`, `temporal_client_group_id`, and `internal_client_group_id`.
- The role ARNs in `rebar-store-aws-config`, from the `rebar_ticket_store_reader_role_arn` and `rebar_ticket_store_writer_role_arn` outputs.
- The `agentic-platform/` prefix of each `SecretProviderClass` object, the `name_prefix` of the root.
- The application subnets `10.0.1.0/24` and `10.0.2.0/24` and the documentation range `192.0.2.0/24` in `gate-engine-egress`, replaced with the application subnet CIDRs and the ranges of the region's S3 prefix list.
- The certificate ARNs, the gateway client CIDRs, and the gateway host list of the base manifests, from `webhook_certificate_arn`, `mcp_gateway_certificate_arn`, and `mcp_gateway_client_cidrs`. In an environment without a public domain, `inv webhook-certs issue --dir DIR` writes a self-signed certificate for `*.<region>.elb.amazonaws.com`, and `inv webhook-certs import --dir DIR` imports it into ACM for `eks_webhook_certificate_arn`. A sender then delivers to the webhook only with TLS verification turned off for it.

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

## MCP gateway authentication

Every request to `/mcp` must carry `Authorization: Bearer <MCP_GATEWAY_TOKEN>`. The gateway compares the token in constant time and answers any other request with 401 before the MCP app sees it, so a refused request lists and invokes no tool. `/health`, `/healthz`, and `/` answer without a token for the target group and the pod probes. The gateway exits at startup when `MCP_GATEWAY_TOKEN` is empty.

The Deployment reads the token from the `mcp_gateway_token` key of the `agentic-platform` Secret. The local overlay's renderer fills it from `MCP_GATEWAY_TOKEN` in `.env`. The `aws/` overlay mounts it instead from the `mcp_gateway_token` secret the [secret-store](../../infra/terraform/secret-store) module generates, through the gateway's `SecretProviderClass`. Give each client the same value.

The Ingress sets `alb.ingress.kubernetes.io/inbound-cidrs`, which the AWS Load Balancer Controller writes as the only inbound rules of the load balancer's security group. Patch it to the client subnets from the environment overlay.

```yaml
patches:
  - target:
      kind: Ingress
      name: mcp-gateway
    patch: |
      apiVersion: networking.k8s.io/v1
      kind: Ingress
      metadata:
        name: mcp-gateway
        annotations:
          alb.ingress.kubernetes.io/inbound-cidrs: 10.20.16.0/22,10.20.20.0/22
```

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

The `inbound-webhook` Ingress routes `/webhook/pr` and `/webhook/jira` to the service, which serves both. Register the GitHub webhook URL as `https://<webhook-hostname>/webhook/pr` and, when `jira_intake` runs in `webhook` or `hybrid` mode in `config/<env>/integrations.yaml`, the Jira webhook URL as `https://<webhook-hostname>/webhook/jira`. The Deployment reads `JIRA_WEBHOOK_SECRET` from the `agentic-platform` secret under the key `jira_webhook_secret`. Set it to the secret configured at the issuer whenever `jira_intake` runs in `webhook` or `hybrid` mode. The key is optional, so an environment that runs `jira_intake` in polling mode starts without it.

Render the static part of the overlay with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/local`, or the full set with `python -m scripts.k8s_render --build-id <id>`, which prints the Secrets with their values.
