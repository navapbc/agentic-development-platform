---
title: Tenant Repository Intelligence - Plan
type: feat
date: 2026-11-14
origin: the ADP-179 two-tenant local run
ticket: ADP-179
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: roadmap
execution: infra
---

# Tenant Repository Intelligence - Plan

## Goal Capsule

Objective. Repository intelligence serves every tenant's repositories, locally and in AWS, and serves each tenant only its own. A run of any tenant receives a context package for its repository, and an indexed tenant repository is indexed from that tenant's checkout.

Means. Repository intelligence and its index worker load each tenant's `repositories.yaml` from the tenant's config bundle, read each tenant's checkouts read-only under `/tenants/<tenant>/repositories`, and key their registry and indexes by tenant and repository. Each request carries a bearer token that names its tenant, and the server answers only for repositories of that tenant.

Product authority. ADP-179 in Jira, the local parity sub-task of ADP-171. ADP-168 verifies the AWS side in the sandbox account.

Stop conditions. On the local stack with the tenants `adp` and `beta`, a `beta` ticket on a repository with `index: false` and an `adp` ticket on a repository with `index: true` each pass `gather_repo_context` and reach `promote_pr`. A request with `adp`'s token for a `beta` repository receives 403, and a request without a token receives 401. Two tenants that name a repository the same receive their own packages. In the sandbox account the same runs pass on EKS against the ECS repository intelligence service. `inv ci`, `inv terraform-test`, and `inv sast` pass.

## Problem

Repository intelligence is a shared platform service, compose locally and ECS Fargate in AWS. It allowlists and indexes the repositories of the platform `config/<env>/repositories.yaml` through `source_control_repos` and `load_repository_registry` in [server.py](../../services/repository_intelligence/server.py) and the [index worker](../../services/repository_intelligence/indexing/worker.py). Tenant repositories live in each tenant's config bundle, which only the tenant's activity worker reads. The two-tenant local run showed the result. `gather_repo_context` for the `beta` repository `beta-web` received 403 `repo not allowlisted` three times, and the run ended before planning.

The checkouts diverge as well. In AWS, repository intelligence and the index worker mount the `checkouts` access point at `/repositories`, while each tenant worker keeps its checkouts on its own access point under `/tenants/<tenant>`. Locally, `inv repos-compose` mounts only the `path` repositories of the platform `repositories.yaml`.

Requests carry no identity. [repo_intelligence_client.py](../../services/orchestration/repo_intelligence_client.py) posts `repo`, `commit`, `focus`, and `detail` with no header, so any caller that reaches the service could read any repository's context once the allowlist spans tenants.

## Decisions

- D1. Tenant registry. The server and the index worker load the `repositories.yaml` of each tenant `TENANTS` names through the tenant config source of [tenant_config.py](../../services/integration/tenant_config.py), the directory `config/<env>/tenants/<tenant>/` locally and `tenants/<tenant>/` of `TENANT_CONFIG_BUCKET` in AWS, read-only, refreshed on `TENANT_CONFIG_REFRESH_SECONDS` and kept at the last valid bundle. Each entry resolves its checkout under `/tenants/<tenant>/repositories`, the root the tenant worker already uses through `REPOSITORY_ROOT`. The platform `repositories.yaml` no longer authorizes a repository for repository intelligence.
- D2. Tenant keys. The registry, the index directory `<index root>/<tenant>/<repo>`, the warm service cache, and the index worker's commit record are keyed by tenant and repository, so two tenants may name a repository the same.
- D3. Caller identity. Each tenant has a repository intelligence token. In AWS the `tenant` module creates it as `agentic-platform/tenants/<tenant>/repo-intelligence-token`, the tenant worker receives it as `REPO_INTELLIGENCE_TOKEN` through its SecretProviderClass, and the repository intelligence task role reads that one secret name of every tenant. Locally the render derives each token as HMAC-SHA256 of a local secret and the tenant name and writes it into that tenant's settings secret and the server's settings. The client sends `Authorization: Bearer <token>`, the server maps the token to its tenant in constant time, and answers 401 without a known token and 403 for a repository outside that tenant's registry. The request body gains no tenant field.
- D4. Alternatives not taken for D3. A presigned STS `GetCallerIdentity` request would reuse each tenant's Pod Identity role, but the local workers share one AWS profile, so local and AWS would authenticate differently. A projected ServiceAccount token verified through the Kubernetes TokenReview API would make the ECS service depend on the EKS API endpoint and an access entry. Mutual TLS would need a per-tenant certificate authority the platform does not run.
- D5. Checkouts in AWS. The `repo-intelligence` and `repo-index-worker` services mount the `tenants` access point at `/tenants` read-only, as the review worker does, recorded as an accepted cross-tenant read for indexing beside the review worker's. The `checkouts` access point and its `/repositories` mount are removed once U3 confirms that nothing writes them.
- D6. Checkouts locally. `inv repos-compose` reads every tenant's bundle and mounts each repository that names a `path` into repository intelligence and the index worker at `/tenants/<tenant>/repositories/<repo>`, read-only, the path the server resolves in D1.
- D7. Review grounding. A review request carries the run's workspace path, which the review worker reads through its read-only `/tenants` mount. The fallback in `_resolve_repo_root` of [review.py](../../services/orchestration/temporal_activities/review.py), which looks a repository up in the platform registry when the path is empty, resolves through the run's tenant and accepts only a path under `/tenants/<tenant>/`.
- D8. MCP gateway. The gateway's `repo_get_context_package` tool calls the same client. The gateway is a platform service that holds no tenant token, so with more than one tenant the tool is refused until the gateway authenticates its clients to a tenant. With one tenant the gateway uses that tenant's token.

## Units

### U1. Tenant registry and keys in repository intelligence

Load each tenant's registry in the server and the index worker through the tenant config source, with the refresh and last-valid behaviour of D1. Key the registry, the index directory, the warm cache, and the commit record by tenant and repository as D2 describes, and move an existing index under the `adp` tenant on first start so the current index is reused. Grant the repository intelligence task role read on every tenant prefix of the config bucket and decrypt on its key. Tests cover two tenants with the same repository name, a bundle that fails validation, a bundle refresh that adds a repository, and the index move. Estimate 8-12h.

### U2. Tenant tokens

Add the `repo-intelligence-token` secret to the `tenant` module with a generated value, deliver it to the tenant worker as `REPO_INTELLIGENCE_TOKEN`, and grant the repository intelligence task role read on that secret name of every tenant. Derive the local tokens in the render as D3 describes. Send the bearer token from [repo_intelligence_client.py](../../services/orchestration/repo_intelligence_client.py), and authenticate `/context_package` and `/warm` in the server with the 401 and 403 answers of D3. Apply D8 to the MCP gateway tool. Tests cover a missing token, an unknown token, another tenant's repository, the constant-time comparison, and the gateway refusal with two tenants. Estimate 6-10h.

### U3. Tenant checkouts

Mount `/tenants` read-only on `repo-intelligence` and `repo-index-worker` in [services.tf](../../infra/terraform/container-services/services.tf) and add the workloads to the workspace file system's NFS ingress. Confirm what writes the `checkouts` access point in the sandbox account, and remove the access point and its mounts when nothing does, as D5 describes. Confirm that the tenant worker keeps the checkout of each `index: true` repository at its `index_ref` under `/tenants/<tenant>/repositories/<repo>`, and add that sync to the worker when it does not. Change `inv repos-compose` to D6. Apply D7 to the review fallback. Tests cover the rendered mounts, the compose file for two tenants, and the review fallback refusing a path outside the tenant. Estimate 6-10h.

### U4. Documents and verification

Document the tenant registry, the token, and the accepted cross-tenant read in the README, `docs/local-to-aws-mapping.md`, the `private-bedrock` and `tenant` module READMEs, the FedRAMP hardening design under AC-4 and IA-9, and the control inheritance matrix. Run the stop conditions on the local stack and record them in the verification record of the [multi-tenant plan](2026-11-08-1600-feat-multi-tenant-runtime-profiles-plan.md). Add the AWS stop conditions to the ADP-168 sandbox session. Estimate 3-5h.

## Related gaps

The same audit of platform-namespace services found two more that read only platform configuration. They are recorded here so that each is scoped on its own.

- The inbound webhook service admits `/webhook/pr` only for the GitHub repositories of the platform `repositories.yaml` in [server.py](../../services/orchestration/webhook/server.py), and its Jira intake uses the platform's Jira allowlist and credentials. A tenant's pull request or Jira event is refused or handled under the platform's configuration.
- The MCP gateway holds the platform's Jira and GitHub credentials and reads evidence by artifact ID with no tenant, so with more than one tenant its other tools cross the tenant boundary as well.

## Verification

The local stop conditions run on the Docker Desktop cluster with `TENANTS=adp,beta`, an `adp` repository with `index: true`, and the `beta-web` repository with `index: false`. The AWS stop conditions run in the ADP-168 sandbox session with two tenants after `terraform apply` and `inv aws-release`.
