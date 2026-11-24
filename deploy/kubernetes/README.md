# Kubernetes manifests

The files in this directory are the base manifests for the platform workloads on a cluster running the AWS Load Balancer Controller. `local/` is a kustomize overlay for a workstation cluster such as Docker Desktop Kubernetes, and `aws/` is the overlay for the EKS cluster of the `private-bedrock` composition root.

| Manifest | Workloads |
| --- | --- |
| `activity-worker.yaml` | The activity worker with its ServiceAccount, the `workspaces` volume claim, and OPA as a sidecar init container bound to the pod's loopback |
| `review-worker.yaml` | The review worker with its ServiceAccount, which mounts the `workspaces` claim read-only and which the local renderer copies into each tenant's review namespace |
| `litellm.yaml` | LiteLLM with a ClusterIP Service on 4000 |
| `sandbox.yaml` | The activity worker's Role and RoleBinding in `agent-sandbox` for the sandbox and gate pods, the `image-verifier` ServiceAccount the gate pods run as while image signature verification is on, and the gate pod egress policy |
| `mcp-gateway.yaml`, `inbound-webhook.yaml` | The MCP gateway, which both renderers copy into each tenant's worker namespace, and the webhook front door, each with its ServiceAccount and an Ingress |
| `slack-poster.yaml` | The Slack posting service of the shared Slack app, with its ServiceAccount, a ClusterRole that grants only `create` on `tokenreviews`, and a ClusterIP Service on 8160. It serves the `slack-poster-tls` certificate, which every tenant worker verifies against the `slack-poster-ca` ConfigMap at `/etc/slack-poster-ca`. The renders drop it when `slack-shared-app.yaml` is absent |

The workers read every platform setting from the `agentic-platform-env` Secret, the environment's config files from the `platform-config` ConfigMap and the `platform-config-secrets` Secret, and the compose or AWS endpoints from the `agentic-platform` ConfigMap. The OpenHands sandbox egress policies live in `openhands/policies`. No manifest mounts a `hostPath` volume. The platform containers set `args` and leave the image entrypoint in place, because the entrypoint trusts the platform CA, writes the environment's config files, and exports the mounted secrets before the process starts.

## Tenants

The activity worker and `sandbox.yaml` are a template. Both renderers pass the manifests through `scripts/tenant_render.py`, which renders for each tenant a `tenant-<tenant>` namespace and a `tenant-<tenant>-sandbox` namespace, labeled `agentic-dev-platform/tenant` and `agentic-dev-platform/namespace-role`. The worker namespace holds a copy of the activity worker with its ServiceAccount, secret provider, the config only the worker reads, its `workspaces` claim, its SecurityGroupPolicy, and the network policies that select it. The sandbox namespace holds a copy of the Role, the RoleBinding to the tenant's worker, the network policies of `agent-sandbox`, and a `sandbox` ResourceQuota when the tenant sets one. Each worker receives `TENANT`, its sandbox namespace as `OPENHANDS_SANDBOX_NAMESPACE` and `GATE_ENGINE_NAMESPACE`, and `REPOSITORY_ROOT`, `EXECUTION_WORKSPACE_ROOT`, and `GATE_ENGINE_ROOT` under `/tenants/<tenant>`, where its workspace claim is mounted, and reaches the platform Services by their cluster names. The platform namespace keeps LiteLLM, the webhook, and the shared config, and its policies admit the tenant namespaces by label where they admitted `agent-sandbox`.

The MCP gateway is a template as well. Each worker namespace holds a copy of the `mcp-gateway` Deployment and Service, and in AWS its Ingress and `SecretProviderClass`. The copy runs as the tenant's `activity-worker` ServiceAccount, reads the worker's settings and config bundle in place of each platform ConfigMap and Secret the worker namespace does not hold, and receives `TENANT`, which the gateway requires. Each platform NetworkPolicy and SecurityGroupPolicy that selects the gateway is copied into each worker namespace, scoped to the tenant's gateway, and a copied ingress rule keeps only the gateway's port 8150. In AWS, the worker namespace's `platform-baseline` covers the gateway's egress to DNS, Postgres, and HTTPS.

The review worker is a template too. For each tenant the renderer adds a `tenant-<tenant>-review` namespace with the `review` namespace role, holding a copy of the `review-worker` Deployment and ServiceAccount, the config the review worker reads, and the platform NetworkPolicies and SecurityGroupPolicies that select it. The copy receives the same `TENANT`, sandbox namespace, and `/tenants/<tenant>` paths as the tenant's worker, and mounts its `workspaces` claim read-only at `/tenants/<tenant>`. The tenant's sandbox namespace holds a `review-worker-sandbox` RoleBinding of the sandbox Role to the review worker's ServiceAccount, so the review worker starts the tenant's gate and scanner pods there and in no other namespace.

The platform activity worker, its ServiceAccount, secret provider, claim, and worker-only config, the platform `mcp-gateway` Deployment, Service, ServiceAccount, Ingress, and `SecretProviderClass`, the platform `review-worker` Deployment and ServiceAccount, and the `agent-sandbox` namespace are not rendered. An apply does not prune, so both deploys delete those objects by name after the apply.

Each tenant sandbox namespace also receives an `image-policy` ConfigMap listing the image prefixes its pods may run and a binding of the `agentic-dev-platform-tenant-images` ValidatingAdmissionPolicy, which refuses a pod whose image lacks a digest or comes from another prefix. The local renderer reads the prefixes from `config/local/image_policy.yaml`, and the EKS deploy from each tenant's `image_prefixes` input. See [image governance](../../docs/image-governance.md).

## The local overlay

The overlay replaces each image with the local build, deletes both Ingress resources, narrows the `workspaces` claim to `ReadWriteOnce`, exposes `inbound-webhook` on 8140 and LiteLLM on 4000 through `LoadBalancer` Services, and mounts the `aws-credentials` Secret into LiteLLM and the review worker through `credential_process`. `inv k8s-reload` and `inv k8s-deploy` render it through `scripts/k8s_render.py`, which adds what derives from the workstation at deploy time:

- the `agentic-platform-env` Secret from `config/local/platform.env`, with each compose service address replaced by `K8S_COMPOSE_ADDRESS` and the port compose publishes, and the workstation-only keys such as `AWS_PROFILE` left out
- the `agentic-platform` Secret with the workload values, among them the platform `database_url` and the `litellm_database_url` LiteLLM reads as `DATABASE_URL`, which names the `litellm` database on the same Compose Postgres server and role
- the `platform-config` ConfigMap from the platform YAML files in `config/local`, and the `platform-config-secrets` Secret from its other files, such as the ticket store's AWS profiles. The render refuses a `config/local/review-signing-key` file and directs each tenant's key to `REVIEW_SIGNING_KEY` of the tenant's settings file
- the `promotion-policies` ConfigMap from `config/policies`, and the `litellm-config` ConfigMap from `config/platform`
- the image tag of the current build and its `AGENT_BUILD_ID`
- a Bedrock relay sidecar in LiteLLM and the review worker under the `jumphost` and `ssh-bastion` connectivity providers
- the namespaces `K8S_NAMESPACE` names, and the namespaces of each local tenant, a folder of `config/local/tenants/` with its settings file
- for each tenant, a `hostPath` PersistentVolume at `/var/lib/agentic-dev-platform/tenants/<tenant>` on the node, bound to its `workspaces` claim, and a `workspace-owner` init container that hands the directory the kubelet creates as root to uid 10001
- for each tenant, a second `ReadOnlyMany` `hostPath` PersistentVolume at the same directory, bound to the `workspaces` claim of `tenant-<tenant>-review`, which the tenant's review worker mounts read-only at `/tenants/<tenant>`, so it reads the tenant's checkouts at the path the tenant's worker passes it and no other tenant's
- for each tenant's review worker, the tenant's config bundle, the tenant's settings without its integration credentials, MCP gateway token, and repository intelligence token, the tenant's `REGISTRY_TOKEN` when set, and the tenant's `REVIEW_SIGNING_KEY`, which no other pod receives

No pod mounts a `hostPath` volume directly. `inv k8s-reload` and `inv k8s-deploy` wait for each tenant's worker, MCP gateway, and review worker in their namespaces, `inv aws-login` writes the `aws-credentials` Secret to the platform namespace and each tenant's review namespace, and the scripted tasks and one-off commands that run in the activity worker use the worker of the tenant `--tenant` names, which may be omitted only when one local tenant exists.

Nothing derived is written to disk. The renderer pipes the manifests to `kubectl apply`.

## The AWS overlay

`aws/` runs the activity worker with its OPA sidecar, LiteLLM, the inbound webhook, each tenant's MCP gateway, and each tenant's review worker on EKS. No rendered object is a Kubernetes Secret.

- `scripts/eks_render.py` places one review worker per tenant in `tenant-<tenant>-review`, through the renderer the local overlay uses, with `TENANT=<tenant>`, the tenant's paths and sandbox namespace, and its own `review-worker` ServiceAccount, which the `private-bedrock` root binds to the tenant's review worker role. Its own `review-worker` SecretProviderClass mounts the platform database secret, the Temporal CA, `review_attestation_secret`, the tenant's `registry_token` when the tenant holds one, and the tenant's `review_signing_key` as `REVIEW_SIGNING_KEY`. No other provider names the review signing key. It also lists `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, which the render keeps only when the tenant's `review_platform_secrets` input names them, which `private-bedrock` does when Langfuse runs without the collector. The review worker then exports its generation spans to the `LANGFUSE_HOST` of `agentic-platform-eks` itself. With the collector or without Langfuse it holds neither key. A RoleBinding in `tenant-<tenant>-sandbox` binds the review worker's ServiceAccount to the sandbox Role the activity worker holds, so it starts the tenant's gate and scanner pods there. The platform namespace keeps no review worker, and `inv eks-deploy` waits for each tenant's review worker after the activity workers.
- Each workload runs as its ServiceAccount, which the `eks-cluster` module binds to a Pod Identity role. Each tenant's MCP gateway runs as the tenant's `activity-worker` ServiceAccount, and its `SecretProviderClass` also mounts the worker's `github_token`, `jira_api_token`, `jira_pat`, and `repo_intelligence_token` secrets that the tenant holds. A `SecretProviderClass` per workload mounts the Secrets Manager values that role may read through the Secrets Store CSI driver at `/mnt/secrets`, one file per variable. The platform entrypoint exports each file as the variable its name names when `PLATFORM_SECRETS_DIR` is set, and the LiteLLM container does the same in its command. Both skip a file whose name is not a variable name, such as the file of a whole database secret. The platform database secret is mounted as `PGUSER` and `PGPASSWORD`, the LiteLLM database secret as `DATABASE_USERNAME` and `DATABASE_PASSWORD`, and the Temporal CA as `TEMPORAL_TLS_CA`. Every secret a `SecretProviderClass` names must hold a current value, or the pod waits in `ContainerCreating`.
- The platform containers read their settings from two ConfigMaps. `agentic-platform-env` holds the environment the ECS tasks receive, from the `platform_environment` of the `eks` output, which the deploy writes. `agentic-platform-eks` sets the EKS settings, the in-cluster OpenHands sandbox, the gate and scanner pods, the FIPS endpoints, the direct Bedrock path, the `sandbox` node pool, the ECR copies of the agent-server, buildah, semgrep, and trivy images, and the `GATE_ENGINE_IMAGES` map of the published gate engine images. The `platform-config` ConfigMap, which the deploy writes from `environment_config`, holds the environment's config files at `/app/config/env`.
- LiteLLM receives only the region, the FIPS setting, the platform CA, the Langfuse host, and its own database settings, because LiteLLM treats `DATABASE_URL` as its own store. It reads `DATABASE_HOST` and `DATABASE_PORT` from the `LITELLM_DATABASE_HOST` and `LITELLM_DATABASE_PORT` keys of `agentic-platform-eks`, which the deploy writes from the Aurora writer endpoint, and sets `DATABASE_NAME=litellm`, `DATABASE_SSLMODE=verify-full`, and `DATABASE_SSLROOTCERT=/etc/ssl/rds-global-bundle.pem`. LiteLLM assembles its URL from these and pins the RDS root that verifies the writer, so Prisma checks the certificate chain and host name.
- Each tenant's `workspaces` claim binds to the static `tenant-<tenant>-workspace` volume, the tenant's access point of the workspace file system, mounted with TLS and IAM authorization. The tenant's worker mounts it at `/tenants/<tenant>`. The `workspaces` claim of `tenant-<tenant>-review` binds a second, `ReadOnlyMany` volume `tenant-<tenant>-review-workspace` of the same access point, which the tenant's review worker mounts read-only at `/tenants/<tenant>`, so a checkout path the worker records resolves in the review worker and no other tenant's checkout does.
- The `temporal-clients` SecurityGroupPolicy gives the activity worker, review worker, and inbound webhook pods the cluster, Temporal client, and internal load balancer client groups, and each review namespace holds a copy for its review worker. The `internal-clients` policy gives LiteLLM the cluster and internal load balancer client groups, and each worker namespace holds a copy that gives the tenant's MCP gateway the same groups.
- Each tenant's worker reads the tenant's ticket store through the `rebar-tickets-reader` and `rebar-tickets-writer` profiles in its namespace's `rebar-store-aws-config`, whose `credential_source = EcsContainer` the Pod Identity credential endpoint satisfies.
- With `SANDBOX_NODE_POOL=sandbox`, the sandbox, gate, checks, and scanner pods the activity worker creates select the `sandbox` node group and tolerate its taint.
- The gate, checks, and scanner pods carry no AWS identity, and buildah inside them pulls their images. For an image in ECR, the activity worker requests an ECR authorization token with its Pod Identity role and copies a containers auth file into the pod over the exec stream, so the token stays out of every command line and the API server audit log. The pod reads it as `REGISTRY_AUTH_FILE` for the pull and deletes it before any project or scanner code runs. The role pulls only from the semgrep, trivy, and example gate engine repositories and the repositories in `eks_gate_image_repository_arns`, so the token reaches nothing else.
- `network-policies.yaml` and `kube-system-network-policies.yaml` select every pod that is not host-networked in `kube-system`, `aws-secrets-manager`, `agentic-dev-platform`, and `agent-sandbox`, so the cluster runs in strict network policy mode, where a pod no policy selects is denied. `kube-system-network-policies.yaml` holds only the `kube-system` policies, which must live in that namespace because a NetworkPolicy applies only in its own namespace. The baselines admit DNS, HTTPS, and the EKS Pod Identity agent at `169.254.170.23` on 80, the platform pods also reach the database, Temporal, LiteLLM, and the sandbox agent-server, and only CoreDNS, the controller webhook, LiteLLM, and the load balancer targets accept ingress. The load balancer targets are the inbound webhook and each tenant's MCP gateway, which accepts the VPC on port 8150 only.
- The `gate-engine-egress` policy admits DNS, LiteLLM, the Pod Identity agent, which serves the `image-verifier` credential, and HTTPS to the application subnets, which hold the ECR interface endpoints, and to the S3 ranges that carry the image layers through the S3 gateway endpoint. In the `aws` registry mode, or with image signature verification on, it also admits HTTPS to every address outside the VPC, for the package indexes and the Signer revocation host DNS Firewall resolves, and in the `external` mode HTTPS to the registry's ranges.
- With a `registries.yaml` in `platform-config`, the activity worker copies a buildah `registries.conf` drop-in into each gate, checks, and scanner pod that sends every mirrored upstream prefix to its mirror, so an image named `python:3.14-slim` is pulled from the mirror unchanged. In the `external` mode the pod also receives the package index variables and a netrc and npmrc built from `REGISTRY_TOKEN`, which the activity worker mounts from the `registry_token` secret.

- Every tenant's activity worker mounts a projected service account token for the `slack-posting-service` audience at `/var/run/secrets/slack-posting/token`, which it presents to `slack-poster`. The `platform-baseline` policy admits egress to `slack-poster` on 8160, and the `slack-poster-ingress` policy in `slack-poster.yaml`, which both environments apply, admits only `activity-worker` pods in tenant worker namespaces, since the tenant's MCP gateway runs as the same ServiceAccount. `slack-poster` reads the shared bot token through its own `slack-poster` SecretProviderClass.

Render it with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/aws`. The rendered manifests carry the synthetic values below. `inv eks-deploy` runs the [eks-deployer](../../infra/terraform/eks-deployer) task, which replaces them with the environment's values in `scripts/eks_render.py`, adds the generated ConfigMaps, and applies the result from inside the VPC.

## Per-environment values

Four values in the base manifests are synthetic and must be replaced for every environment.

- The container image registry `123456789012.dkr.ecr.us-east-1.amazonaws.com`, replaced through the kustomize `images` transformer.
- The ACM certificate ARN `arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000` on each Ingress, replaced through an overlay patch.
- The host `mcp.internal.example` of the `mcp-gateway` Ingress rule and the `MCP_ALLOWED_HOSTS` value `mcp.internal.example,mcp.internal.example:443` on the `mcp-gateway` Deployment, both replaced through overlay patches with the hostname the gateway's clients resolve. See [MCP gateway host header](#mcp-gateway-host-header).
- The `alb.ingress.kubernetes.io/inbound-cidrs` value `192.0.2.0/24` on the `mcp-gateway` Ingress, a documentation range that admits no client, replaced through an overlay patch with the CIDRs of the subnets that hold the gateway's clients. See [MCP gateway authentication](#mcp-gateway-authentication).

The `aws/` overlay adds these synthetic values, each of which the `eks` output of `private-bedrock` names for the environment.

- The ECR account `123456789012` and the tags of the platform, OPA, and LiteLLM images, and the image settings in `agentic-platform-eks`, from `images` and `image_settings`.
- The volume handle `fs-00000000000000000::fsap-00000000000000000` of the template volume, which becomes each tenant's `tenant-<tenant>-workspace` with the handle of the tenant's access point from the deploy inputs.
- The groups `sg-00000000000000001`, `sg-00000000000000002`, and `sg-00000000000000003` in the SecurityGroupPolicies, from `cluster_security_group_id`, `temporal_client_group_id`, and `internal_client_group_id`.
- The role ARNs in `rebar-store-aws-config`, which each tenant's copy replaces with the reader and writer roles of the tenant's ticket store.
- The `agentic-platform/` prefix of each `SecretProviderClass` object, the `name_prefix` of the root.
- The application subnets `10.0.1.0/24` and `10.0.2.0/24` and the documentation range `192.0.2.0/24` in `gate-engine-egress`, replaced with the application subnet CIDRs and the ranges of the region's S3 prefix list.
- The certificate ARNs and the gateway client CIDRs of the base manifests, from `webhook_certificate_arn`, `mcp_gateway_certificate_arn`, and `mcp_gateway_client_cidrs`, and the gateway host, from each tenant's `mcp_gateway_host`. Every tenant's gateway Ingress takes the same certificate and client CIDRs. The deploy sets the host of each tenant's gateway Ingress rule to the tenant's host, `mcp-<tenant>.<eks_mcp_gateway_domain>` unless the tenant's `mcp_gateway_host` names another `<label>.<eks_mcp_gateway_domain>`, and its `MCP_ALLOWED_HOSTS` to that host bare and with `:443`, and `inv eks-deploy` points the host at the tenant's load balancer in the gateway's private hosted zone. In an environment without a public domain, `inv webhook-certs issue --dir DIR` writes a self-signed certificate for `*.<region>.elb.amazonaws.com`, and `inv webhook-certs import --dir DIR` imports it into ACM for `eks_webhook_certificate_arn`. A sender then delivers to the webhook only with TLS verification turned off for it. `inv mcp-gateway-certs` signs the gateway certificate with the platform CA instead, as [Issuing the MCP gateway certificate](#issuing-the-mcp-gateway-certificate-in-an-environment-without-a-public-domain) describes.

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

`inbound-webhook` fronts an internet-facing ALB, so its certificate must cover the public hostname registered as the GitHub webhook URL. `mcp-gateway` fronts an internal ALB, one per tenant, so its certificate must cover the in-VPC hostname each tenant's clients resolve. In AWS that hostname is `mcp-<tenant>.<eks_mcp_gateway_domain>`, so a certificate for `*.<eks_mcp_gateway_domain>` covers every tenant, and `inv eks-deploy` refuses a certificate that does not cover each tenant's host before it starts the deploy task. A certificate for the load balancer's `*.elb.amazonaws.com` name does not serve, because the gateway refuses that `Host` with 421, so the gateway may not share the inbound webhook certificate, and the private-bedrock plan refuses the same ARN for both.

### Issuing the MCP gateway certificate in an environment without a public domain

`inv mcp-gateway-certs` signs the gateway certificate with the platform CA that `inv temporal-certs generate --internal-name` wrote, which MCP clients already trust through `container_platform_ca_file`. It writes `mcp-gateway.pem` and `mcp-gateway-key.pem` next to `ca.pem` and refuses a directory inside the repository. `import` checks the certificate against the CA and each tenant's host in the `eks` output, and imports it into ACM with the CA as its chain. It re-imports into the `mcp_gateway_certificate_arn` of the `eks` output, so a renewal keeps the ARN. When that ARN is unset or is the inbound webhook certificate, it creates a new ARN and prints the tfvars line to set, and it never imports into the webhook ARN. For the sandbox, whose CA directory is `~/.agentic-platform/internal-tls-sandbox`, the steps are:

1. `inv mcp-gateway-certs issue --dir ~/.agentic-platform/internal-tls-sandbox --domain <eks_mcp_gateway_domain>`. Without `--domain`, the task reads `mcp_gateway_domain` from the `eks` output.
2. `inv mcp-gateway-certs import --dir ~/.agentic-platform/internal-tls-sandbox`.
3. Set `eks_mcp_gateway_domain` and the `eks_mcp_gateway_certificate_arn` it prints in the tfvars file.
4. Apply.
5. `inv eks-deploy`, which checks the certificate, deploys, and points each tenant's host at its ALB.

To renew, run `issue` with `--force` and `import` again. The ARN stays the same, so neither the tfvars file nor the Ingress changes.

## Target group health checks

Both Ingress resources set `alb.ingress.kubernetes.io/healthcheck-path: /health`. Each service answers `/health`, `/healthz`, and `/` with a 200 and a JSON body, so the ALB default of `GET /` also succeeds. The health check reaches the pod over HTTP on the traffic port. Terminating TLS at the listener does not change it.

## MCP gateway authentication

Every request to `/mcp` must carry `Authorization: Bearer <MCP_GATEWAY_TOKEN>`. The gateway compares the token in constant time and answers any other request with 401 before the MCP app sees it, so a refused request lists and invokes no tool. `/health`, `/healthz`, and `/` answer without a token for the target group and the pod probes. The gateway exits at startup when `MCP_GATEWAY_TOKEN` is empty.

Each tenant's gateway accepts only its own tenant's token, so give each client the token of the tenant it reads for. Locally the gateway reads `MCP_GATEWAY_TOKEN` from the tenant's settings Secret, `agentic-platform-env` in `tenant-<tenant>`, which the local overlay's renderer fills from `config/local/tenants/<tenant>.env`. The renderer refuses a tenant without a token of its own and two tenants that share one. The Service is a ClusterIP Service and the local overlay deletes the Ingress, so reach a tenant's gateway with `kubectl -n tenant-<tenant> port-forward svc/mcp-gateway 8150:80`. The `aws/` overlay mounts the token instead from the secret `<name_prefix>/tenants/<tenant>/mcp-gateway-token`, which the [tenant](../../infra/terraform/tenant) module generates and only the tenant's worker role and the operators in the tenant's `mcp_gateway_token_readers` read, through the gateway's `SecretProviderClass`. An operator reads it with `aws secretsmanager get-secret-value` to configure the tenant's clients.

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

The MCP gateway enables the FastMCP DNS rebinding protection from `MCP_ALLOWED_HOSTS`, a comma-separated list of the `Host` header values it accepts. The base manifest sets the synthetic value `mcp.internal.example,mcp.internal.example:443` on the Deployment, so replace it with the hostname its clients resolve. List the hostname both bare and with `:443`, because a client that connects on the default port sends the `Host` header without a port and one that names the port explicitly sends it with the port. A value of `host:*` accepts any port on that hostname. Each entry also contributes an allowed `Origin` of `https://<entry>`, which matches the HTTPS-only listener. In AWS, the deploy sets each tenant gateway's value to the tenant's host, `mcp-<tenant>.<eks_mcp_gateway_domain>`, which is also the host of the gateway's Ingress rule, so the load balancer forwards only that host and the gateway accepts only it. Without `eks_mcp_gateway_certificate_arn` the gateway has no Ingress and its value is empty.

A request whose `Host` header is outside the list is refused with 421, and one whose `Origin` header is outside the derived list is refused with 403. The health paths are served outside that check and answer whatever `Host` the ALB target group health check sends.

FastMCP turns the protection on by itself only when the server binds a loopback address, and the platform image sets `BIND_HOST=0.0.0.0`. Leaving `MCP_ALLOWED_HOSTS` empty in a deployed pod therefore leaves the gateway accepting every `Host` value.

The `local/` overlay patches `MCP_ALLOWED_HOSTS` to an empty value, so the sandbox gateway accepts any `Host`. A sandbox client reaches it through `kubectl port-forward` or the in-cluster service name, and the `Host` header those produce varies with the forwarded port and the DNS form the client uses, so a fixed list would refuse valid sandbox traffic with 421 while the health paths kept the pod green. An environment overlay that fronts the gateway with an ALB sets the hostname on both the Ingress rule and the Deployment instead, as below. Changing the base value means checking `local/` as well, because the overlay inherits anything it does not patch.

Patch both values from the environment overlay `kustomization.yaml`.

```yaml
patches:
  - target:
      kind: Ingress
      name: mcp-gateway
    patch: |
      - op: replace
        path: /spec/rules/0/host
        value: mcp.<environment>.<domain>
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

The `inbound-webhook` Ingress routes the `/webhook/pr` and `/webhook/jira` prefixes to the service. Register `https://<webhook-hostname>/webhook/pr` as the GitHub webhook URL of every tenant's repositories. The webhook routes each pull-request delivery to the tenant whose `repositories.yaml` lists the repository and verifies it with that tenant's `GITHUB_WEBHOOK_SECRET_<TENANT>`, where `<TENANT>` is the tenant name in upper case. When `jira_intake` runs in `webhook` or `hybrid` mode in the tenant's effective `integrations.yaml`, register each tenant's Jira webhook URL as `https://<webhook-hostname>/webhook/jira/<tenant>`, which the webhook verifies with `JIRA_WEBHOOK_SECRET_<TENANT>`. Set each secret to the value configured at the issuer. With one tenant, `/webhook/jira` is the same route as `/webhook/jira/<tenant>`. A tenant without its own secret has its deliveries refused, whatever the tenant count. The Jira secret is optional, so a tenant that runs `jira_intake` in polling mode needs none.

The local renderer adds the Secret `inbound-webhook-tenants`, which holds only each tenant's `GITHUB_WEBHOOK_SECRET_<TENANT>` and `JIRA_WEBHOOK_SECRET_<TENANT>`, taken from `GITHUB_WEBHOOK_SECRET` and `JIRA_WEBHOOK_SECRET` of `config/local/tenants/<tenant>.env`, which no other file supplies, and the ConfigMap `inbound-webhook-tenant-config` with each tenant's `repositories.yaml` and `jira.yaml`. It refuses two tenants that share a secret, and a hash of the secrets in a pod annotation restarts the webhook when one changes. The EKS render mounts each tenant's `<name_prefix>/tenants/<tenant>/github-webhook-secret` and `jira-webhook-secret` under the same names through the webhook's `SecretProviderClass`, leaves out a secret without a value, and sets `TENANT_CONFIG_BUCKET`, so the webhook reads each tenant's routing files from `tenants/<tenant>/` of the config bucket.

Render the static part of the overlay with `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kubernetes/local`, or the full set with `python -m scripts.k8s_render --build-id <id>`, which prints the Secrets with their values.
