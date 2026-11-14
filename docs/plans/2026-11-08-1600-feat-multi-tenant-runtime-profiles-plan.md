---
title: Multi-Tenant Runtime Profiles - Plan
type: feat
date: 2026-11-08
origin: design discussion for ADP-168
ticket: ADP-171
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: roadmap
execution: infra
---

# Multi-Tenant Runtime Profiles - Plan

## Goal Capsule

Objective. Several teams share one AWS deployment of the platform. Each team brings its own repositories, ticket source, credentials, and containers, and changes its own configuration without a platform deploy. A team's credentials, checkouts, sandboxes, and images stay out of reach of every other team.

Means. A tenant is the trust boundary, provisioned in Terraform with its own worker pool, identity, secrets, sandbox namespace, ticket store, and model budget. A runtime profile is the tech stack of a repository, the sandbox, checks, and gate engine containers it runs, published by the tenant as a config bundle in S3 and loaded by the workers at run time. Images are governed in four layers, the last of which is optional.

Product authority. ADP-171 in Jira and its sub-tasks ADP-172 to ADP-179. ADP-168, the end-to-end verification of the EKS placement, waits for this work and runs its ticket through a tenant.

Stop conditions. Two tenants run in the sandbox account. One holds a Python repository and the other a TypeScript repository, each on its own runtime profile. A ticket in each tenant runs through planning, the sandbox, the gates, and review on its tenant's worker pool. Neither tenant's worker can read the other's secrets, checkouts, images, or ticket store, which the verification proves by attempting each access. A config bundle change takes effect on the next run without a deploy. `inv ci`, `inv terraform-test`, and `inv sast` pass, and the sandbox plan shows no drift.

## Product Contract

### Summary

The AWS deployment serves one team today. Every team-specific input is a file in `container_config_dir`, which Terraform copies into the `platform-config` ConfigMap and the ECS `PLATFORM_ENV_CONFIG`, so a change needs `terraform apply`, `inv platform-deploy`, and `inv eks-deploy`. One activity worker holds every credential, one EFS access point holds every checkout, one rebar store holds every ticket, and the sandbox receives the LiteLLM master key. This plan adds tenants, runtime profiles, tenant config bundles, image governance, per-tenant worker pools, per-run model keys, and per-tenant attribution.

### Problem Frame

Teams that share the deployment differ in two independent ways. Some share a Jira site and a GitHub token and differ only in their tech stack, such as a ReactJS frontend team and a FastAPI backend team. Others hold their own credentials, which must not reach another team's runs. FedRAMP Medium expects AC-4 information flow enforcement, AC-6 least privilege, and SC-7 boundary protection between those teams, CM-3 and CM-5 change control over the configuration each team supplies, CM-7 and SI-7 control over the software each team runs, and AU attribution of each action to its team.

### Requirements

- R1. A tenant is a Terraform entry that provisions its activity worker pool and Temporal task queue, Pod Identity role, Secrets Manager prefix `agentic-platform/tenants/<tenant>/`, ECR prefix `tenants/<tenant>/`, config bundle prefix and publisher role, sandbox namespace, EFS access point, rebar ticket store, evidence prefix, and LiteLLM team.
- R2. A tenant's worker reads only its own secrets, plus any shared secret whose reader list names the tenant in Terraform.
- R3. A runtime profile names the agent-server sandbox image, the checks image and command, the gate engines, and the registry mirrors for the repositories assigned to it. A profile uses a platform catalog image or an image the tenant supplies.
- R4. A tenant publishes its repositories, runtime profiles, checks, gates, and gate engines as a config bundle to its S3 prefix from its own CI. The workers activate a valid bundle within 60 seconds without a deploy, keep the last valid bundle when a new one fails validation, and record the bundle version in each run's determinism manifest.
- R5. Every image a tenant pod runs carries a digest and comes from the platform catalog, the tenant's ECR prefix, the account's pull-through caches, or a registry the tenant's `registries.yaml` names. Validation, IAM, and an admission policy each enforce the list. Amazon Inspector scans tenant images and blocks a finding at or above the configured severity unless the tenant accepts it in an exception file. Signature verification through AWS Signer is a per-deployment toggle.
- R6. A tenant's runs are limited by its concurrent run and sandbox counts, a ResourceQuota on its sandbox namespace, and a monthly model budget with request and token rate limits.
- R7. The sandbox and gate pods receive a per-run LiteLLM virtual key under the tenant's team, with the run's budget and an expiry, and never the master key.
- R8. Every model call, trace, cost rollup, and tenant-created AWS resource carries the tenant.
- R9. Platform-wide settings, such as `TICKET_BACKEND`, the authorship identity, and the default limits, are set through a `container_platform_settings` map that rejects secret names.
- R10. Every run belongs to a tenant. A deployment with EKS needs at least one tenant entry, and the local stack runs the tenants `TENANTS` names, `adp` by default, with a directory config source.

### Scope Boundaries

- Tenant onboarding is a platform team change in Terraform. A self-service tenant API is out of scope.
- The shared control plane, Temporal, Aurora, LiteLLM, Langfuse, ClickHouse, the collector, and the ECS cluster, is not duplicated per tenant.
- One Langfuse project serves every tenant, and traces are filtered by the `tenant` tag. A project per tenant is a later option.
- A dedicated node group per tenant is optional and off by default. Tenants share the tainted sandbox node group, separated by namespace, NetworkPolicy, and quota.

### Sources

- `config/local/github.yaml.example` and `config/local/jira.yaml.example`, whose `token_ref` already names a credential per repository or project.
- `services/orchestration/openhands_code_step.py`, which passes `LITELLM_MASTER_KEY` to the agent-server conversation.
- `infra/terraform/container-services/services.tf`, whose `platform_environment` the ECS tasks and the EKS `agentic-platform-env` ConfigMap receive.
- `services/integration/credentials.py`, whose `load_env_config` reads a config file from disk on every call.
- `deploy/kubernetes/aws`, the overlay that runs one activity worker against the `agent-sandbox` namespace.
- The AWS FedRAMP Services in Scope page, which lists S3, ECR, Amazon Inspector, AWS Signer, Systems Manager, and DynamoDB for East/West Moderate and High, and lists no separate AppConfig entry.

## Planning Contract

### Key Technical Decisions

- Two concepts. A tenant is the trust boundary and a runtime profile is the tech stack. Teams that share credentials share a tenant and may use different profiles. A team with its own credentials gets its own tenant.
- Tenants in Terraform, profiles in data. A tenant creates IAM roles, namespaces, and Deployments, which change rarely and need review. Profiles and repository assignments change often and belong to the tenant.
- S3 config bundles. Each tenant owns the prefix `tenants/<tenant>/` of a versioned, KMS-encrypted config bucket with CloudTrail data events, reached through the existing S3 gateway endpoint. The tenant's config repository is the review record and S3 versioning is the change record. AppConfig is not used, because the FedRAMP scope list names no AppConfig entry and local development would need a second path. Aurora and DynamoDB are not used, because tenants would lose reviewed files and the platform would carry a new API.
- One config source abstraction. The workers load every tenant-scoped file through a source that reads a directory locally and an S3 prefix in AWS, refreshes by ETag every 60 seconds, validates before activation, and keeps the last valid bundle.
- Per-tenant worker pools. Each tenant runs its own activity worker Deployment on its own Temporal task queue, `agent-platform-<tenant>`, as its own ServiceAccount and Pod Identity role. The shared workflow worker dispatches each run's activities to the queue of the tenant that owns the ticket. A tenant's credentials therefore live only in its own pods.
- Shared review worker. The ECS review worker holds no tenant credential and reads checkouts read-only. It mounts the parent of the tenant access points read-only and writes evidence under `tenants/<tenant>/` of the evidence bucket. This is recorded as an accepted cross-tenant read for review.
- A rebar store per tenant. Each tenant gets its own instance of the `rebar-ticket-store` module, so its bucket, key, and reader and writer roles are its own.
- Four image layers. Bundle validation, the tenant role's ECR pull policy, a ValidatingAdmissionPolicy on each tenant namespace, and optional AWS Signer verification through Notation. The admission policy is built into Kubernetes, so no policy engine is operated. Amazon Inspector enhanced scanning feeds the validation layer.
- LiteLLM database. LiteLLM gets a `litellm` database on the existing Aurora cluster. Each tenant is a LiteLLM team with a budget and rate limits, and the worker creates and deletes a virtual key per run.
- No default tenant. The existing deployment became the `adp` tenant, and U4 moved its worker, sandbox namespace, ticket store, and workspace to the tenant's names.

### High-Level Design

The `private-bedrock` root takes a `tenants` map. Each entry creates the tenant's AWS resources through a `tenant` module and adds the tenant to the EKS deploy inputs. The eks-deployer renders, per tenant, a worker namespace `tenant-<tenant>` with its activity worker Deployment, ServiceAccount, SecretProviderClass, and `TENANT` setting, and a sandbox namespace `tenant-<tenant>-sandbox` with its NetworkPolicies, ResourceQuota, and ValidatingAdmissionPolicy binding, and the tenant's LiteLLM team. Intake runs per tenant, polls the tenant's ticket source, and starts each run on the tenant's task queue, so every activity of the run reaches the tenant's worker. In the activity, the config source supplies the tenant's repositories, profile, checks, and gates, the profile supplies the images, and the per-run virtual key supplies the model credential.

### Build order

U1 and U2 first, because every later unit reads the tenant and its bundle. U3 and U6 next, in either order. U4 depends on U1, U2, and U3. U5 depends on U3 and U4. U5b depends on U5 and comes before U6, because the scan gate refuses every image until it is done. U7 depends on U4 and U6. U8 closes the plan and records the verification. ADP-168 runs after U8.

## Implementation Units

### U1. Tenants and platform settings (ADP-172)

Add the `tenant` Terraform module and the `tenants` map in `private-bedrock`. A tenant creates its Pod Identity role and its read policy on `agentic-platform/tenants/<tenant>/*`, an ECR repository creation template and push policy for `tenants/<tenant>/`, an EFS access point, a `rebar-ticket-store` instance, a config bucket prefix with a publisher role trusting the tenant's GitHub repository and branch through OIDC, and `tenant` tags on every resource. Add `container_platform_settings` as a map merged over `platform_environment`, with a validation that rejects any key containing KEY, TOKEN, SECRET, PASSWORD, or PAT. With an empty `tenants` map the root creates the `default` tenant from today's resources. Estimate 12-16h.

### U2. Tenant config bundles (ADP-173)

Add the config bucket, the config source abstraction with its directory and S3 readers, the 60 second ETag refresh, validation before activation, the last-valid fallback, and an alert for a rejected bundle. Route `repositories.yaml`, `checks.yaml`, `gates.yaml`, `gate_engine.yaml`, `registries.yaml`, and the profiles through it. Add `inv tenant-config-validate` and `inv tenant-config-publish`, and record the bundle's S3 version IDs in the determinism manifest. Estimate 12-16h.

### U3. Runtime profiles and the image catalog (ADP-174)

Define the runtime profile schema, the sandbox image, checks image and command, gate engines, and registry mirrors, and let each repository name its profile. Publish catalog agent-server images for Python, Node, and the JVM with `inv publish-platform-images`, built on the OpenHands agent-server image the platform pins, and ship catalog profiles that use them and the example gate engines. Document the base image contract a tenant image keeps, the agent-server entrypoint, the non-root user, and `/workspace`. Estimate 12-16h.

### U4. Per-tenant worker pools and namespaces (ADP-175)

Render a namespace, NetworkPolicies, ResourceQuota, ServiceAccount, SecretProviderClass, and activity worker Deployment per tenant in the eks-deployer. Give each worker its task queue and `TENANT` setting, its own `max_concurrent_runs` and `max_concurrent_sandboxes`, and its own intake schedule. Start each run on its tenant's task queue, so every activity of the run reaches the tenant's worker. Mount the parent of the tenant access points read-only in the review worker and write evidence under the tenant prefix. Estimate 14-20h.

### U5. Image governance (ADP-176)

Validate every image in a bundle against the tenant's allowed registries and require a digest. Limit the tenant role's ECR pull policy to the catalog, its prefix, and the pull-through caches. Add a ValidatingAdmissionPolicy with a binding per tenant namespace that enforces the same list. Turn on Amazon Inspector enhanced scanning for the tenant repositories and block an image with a finding at or above `image_finding_severity`, default HIGH, unless the platform's exception file accepts it with a reason and an expiry. Add `image_signature_verification`, default off, which installs Notation with the AWS Signer plugin in the builder image and verifies each image against the tenant's signing profile before the pull. Add a buildah `policy.json` in each builder pod that rejects every registry the policy does not admit, and scan each image with the Inspector SBOM generator and Scan API before the publish pushes it. Estimate 12-16h.

### U5b. Image vulnerability remediation (ADP-180)

Rebuild or replace every published image until the publish scan and the ECR scan report no HIGH or CRITICAL finding, with no exception, and harden every image to the CIS Amazon Linux 2023 benchmark. The [image vulnerability remediation plan](2026-11-12-1500-feat-image-vulnerability-remediation-plan.md) records the decisions and the units. Estimate 97-151h, low confidence.

### U6. LiteLLM database and per-run keys (ADP-177)

Create the `litellm` database and its secret, point LiteLLM at it, and create a LiteLLM team per tenant with its budget and rate limits. Create a virtual key per run with the run's budget and an expiry, pass it to the sandbox and gate pods in place of the master key, and delete it when the run ends. Map a budget refusal to a recorded budget stop. This unit also closes the exposure of the master key to the sandbox, so it may ship before the others. Estimate 10-14h.

### U7. Attribution (ADP-178)

Add the tenant to the LiteLLM request metadata and the Langfuse trace tags, add `--tenant` to `inv cost-rollup` and `inv reconcile-usage`, and document activating the `tenant` cost allocation tag. Estimate 4-6h.

### U8. Local parity, documents, and verification (ADP-179)

Run the local stack with two tenants from directory sources. Document tenants, profiles, bundles, image governance, and limits in the README, `docs/gate-engine.md`, the `private-bedrock` README, the local-to-AWS mapping, the FedRAMP hardening design, and the control inheritance matrix. Verify the stop conditions in the sandbox with two tenants. Estimate 6-8h.

## Verification Record

Each unit records its verification here when it completes.

### U1 (ADP-172), 2026-11-08

Decisions. No default tenant exists. `create_eks` needs at least one tenant, and the existing deployment is the `adp` tenant. A tenant's worker runs in `tenant-<tenant>` and its pods in `tenant-<tenant>-sandbox`. The running activity worker keeps its current role, secrets, and ticket store until U4 renders the tenant workers and removes the old wiring. The tenant config bucket is created in U1, because the publisher roles name it, and U2 adds its loader. The bucket and key policies name tenant roles only in conditions, so they hold no principal that may not exist yet. A tenant's ticket store bucket is named `<tenant>-rebar-tickets-<suffix>`, because S3 limits a generated name's prefix to 37 characters.

Sandbox. The `adp` tenant applied with 52 resources added. A push to `agentic-platform/tenants/adp/adp172-probe` created the repository with immutable tags, the tenant key, the `tenant` tag, and the untagged expiry rule, and the probe repository was deleted. The IAM policy simulator, evaluated with the secret's resource policy, allowed `GetSecretValue` on `agentic-platform/tenants/adp/github-token` to the `adp` worker role and denied it explicitly to `platform-admin` and to the existing activity worker role. The config bucket accepted an admin write under `tenants/adp/`, refused one at the bucket root, and stored it under the bucket key, and the simulator with the bucket policy allowed the `adp` worker to read `tenants/adp/` and denied it a write, and denied the existing activity worker both. The plan after apply showed no changes.

### U2 (ADP-173), 2026-11-08

Decisions. A bundle layers over `config/<env>/`. A tenant file in the bundle wins, a tenant file the bundle lacks is read from the environment, and a platform file is never read from a bundle. The tenant files are `repositories`, `checks`, `gates`, `gate_engine`, `registries`, `github`, `jira`, and `tickets`. `integrations.yaml` stays a platform file, because only the shared inbound webhook reads it. Validation applies the platform's own loaders and the required-content rules to the effective config. `TENANT` is required by every activity worker, with the directory source `config/<env>/tenants/<tenant>/` locally and the bucket when `TENANT_CONFIG_BUCKET` is set. A run is pinned to the bundle its manifest records through an activity interceptor, which reads the identity once per run. `docs/tenant-config.md` documents the files, the required content, and every default.

Sandbox. `container_platform_settings` sets `TENANT=adp` until U4 renders the tenant workers, and the sandbox config directory gained the `gates.yaml` the required content asks for. The rebuilt activity worker logged `serving tenant adp on config bundle f5507f8cb8bff2d2`, the empty directory bundle layered over the environment. From the workstation, under the admin principal, `inv tenant-config-publish` validated and published a `checks.yaml` to `tenants/adp/`. A process configured with the bucket source activated it, read the checks image from the bundle and `gates.yaml` from the environment, refused a published `repositories.yaml` that names no repository while keeping the active bundle, activated the next valid publish, and fetched the first bundle again by its version IDs after its local copy was deleted. The probe objects were deleted, every version included. The local cluster's activity worker started on the same tenant after `inv k8s-reload`.

### U3 (ADP-174), 2026-11-08

Decisions. The catalog holds the sandbox images `agent-server-python-node`, which is the pinned agent-server image because it carries both Python 3.13 and Node 24, `agent-server-jvm`, and `agent-server-go`, each built on the pinned image with the toolchain copied from its profile's checks image, JDK 21 with Maven 3.9.11 and Go 1.27, which is the Go release the platform's own images pin. OpenHands' `-java` and `-golang` tags hold neither toolchain on arm64. The catalog profiles are `python-fastapi`, `node-react`, `java-spring`, and `go-service`, and each enables the `security-policy` and `test-policy` engines, which became catalog engines in `config/platform/gate-engines.yaml`. A tenant may not reuse a catalog profile or engine name. A profile's gate overrides apply after the tenant's `gates.yaml`, and the activity interceptor binds the run's repository from each activity's input so the gate, checks, and sandbox lookups resolve its profile. `docs/runtime-profiles.md` lists the catalog with its base images.

Sandbox. `inv publish-platform-images` built `agent-server-jvm` and `agent-server-go` into their ECR repositories, and the sandbox config assigns `python-fastapi`, `node-react`, and `java-spring` to the demo repositories. The activity worker activated the bundle with the profiles validated, and resolved each repository's sandbox image, checks image, and gate set, with the catalog engines enabled for the profiled repositories and not for `agentic-demo-tf`. The `java-spring` checks pod pulled `maven:3.9.11-eclipse-temurin-21` by digest through the ECR pull-through cache and printed Maven 3.9.11 and JDK 21.0.9. Pods of `agent-server-jvm` and `agent-server-go` on the sandbox node printed JDK 21 with Maven 3.9.11 and Go 1.27.0, ran as `openhands`, and the JVM image started the agent-server.

### U4 (ADP-175), 2026-11-09

Decisions. Each tenant's runs use the task queue `<TEMPORAL_TASK_QUEUE>-<tenant>`. The tenant's worker polls only that queue, starts its runs there, and installs its own intake and reap schedules at startup, so a workflow's activities run in the worker of the tenant that started it without a tenant field in the workflow input. The workflow worker polls the base queue and every queue of `TENANTS`, which the container-services module sets from the tenant names. The `schedule-bootstrap` task removes the untenanted schedules. The executions table gained a `tenant` column, and admission, run counts, and reconciliation read only the worker's tenant. A tenant's `settings` carry its own limits, such as `MAX_CONCURRENT_RUNS`, and `sandbox_quota` sets the ResourceQuota of its sandbox namespace. `scripts/tenant_render.py` renders the tenant namespaces from the overlay's worker and `agent-sandbox` objects for both the EKS and the local deploy, and both deploys delete the objects it retires. The review worker mounts the `/tenants` access point read-only at `/tenants`, the path each tenant's worker writes under. The untenanted rebar ticket store was removed, and its GitHub OIDC writer trust moved to the `adp` tenant's store. Tenant evidence prefixes and repository-intelligence indexing of tenant checkouts belong to U7.

Sandbox. The apply removed the platform activity worker's Pod Identity role and the untenanted store, attached the shared EKS policies to `agentic-platform-eks-tenant-adp`, and added the `tenants` access point. The bundle reader needed `s3:ListBucketVersions` on the tenant prefix, which the worker and publisher policies now grant. `inv platform-deploy` migrated the database to `0012_execution_tenant`, rolled the workflow worker onto `agent-platform` and `agent-platform-adp`, and removed `jira-intake` and `orphan-reaper`. `inv eks-deploy` rendered `tenant-adp` and `tenant-adp-sandbox`, rolled the tenant worker out, deleted the platform worker, the `agent-sandbox` namespace, the `agentic-platform-checkouts` volume, and the worker-only ConfigMaps, and found every pod selected by a NetworkPolicy. The tenant worker ran with `TENANT=adp`, its sandbox namespace, and its paths under `/tenants/adp`, mounted only the platform secrets its role reads, and installed `jira-intake-adp` and `orphan-reaper-adp` on `agent-platform-adp`. Its service account could create pods and exec only in `tenant-adp-sandbox`. A sandbox-labeled pod in `tenant-adp-sandbox` reached LiteLLM and timed out to the tenant worker, the MCP gateway, and the inbound webhook. A file the tenant worker wrote under `/tenants/adp/repositories` was read by a one-off review worker task, which could not write to `/tenants`. The reconcile-bridge variables `AWS_BRIDGE_ROLE_ARN` and `TICKETS_S3_URL` point at the `adp` store.

Local. `inv k8s-reload` rendered the same tenant namespaces on Docker Desktop with a `hostPath` volume per tenant under `/var/lib/agentic-dev-platform/tenants`, deleted the retired objects, and ran the tenant worker as uid 10001 on its own directory. The review worker read `/tenants` read-only, and the tenant worker installed its schedules on `agent-platform-adp`, which the compose workflow worker polls.

### U5 (ADP-176), 2026-11-09

Decisions. Five layers guard a tenant pod's images: bundle validation against the platform's `image_policy.yaml`, the tenant role's ECR pull scope, a ValidatingAdmissionPolicy per tenant sandbox namespace, a buildah `policy.json` in each builder pod that rejects every registry the policy does not admit, including Dockerfile `FROM` lines, and the scan and signature checks. Every image needs an `@sha256:` digest, locally too, where `inv runtime-images` and `inv gate-engine-images` write the digests into `.env`. The Inspector gate blocks at HIGH for every image, the pull-through caches included, fails closed while a scan is pending, and honours only the platform's `image_exceptions.yaml`, keyed by digest and finding with a reason and an expiry. Signature verification runs notation with the AWS Signer plugin inside the builder pod, as an `image-verifier` identity allowed only `signer:GetRevocationStatus`. A sandbox image, which the kubelet pulls, is verified by a short-lived pod in the same namespace first. The builder is the platform's own buildah image. Signer profiles carry generated names, because Signer never frees a cancelled name, and the trust policy accepts only the platform profile for catalog repositories and only the tenant's profile for its prefix. The plugin checks revocation at `verification.signer.<region>.amazonaws.com`, which no interface endpoint serves, so the DNS Firewall admits that name and the pull pods reach 443 outside the VPC. Images with a HIGH or CRITICAL finding cannot enter a FedRAMP environment, so `inv publish-platform-images` scans each image with the Inspector SBOM generator and Scan API before the push and pushes nothing while a finding is unaccepted. ADP-180 remediates the findings before U6.

Sandbox. The apply enabled Inspector for ECR with enhanced continuous scanning, created the platform and `adp` Signer profiles, the `image-verifier` role, and the generated policy. Inspector scanned the repositories within minutes. Admission resolved each image index to its arm64 image and refused every catalog image and the python cache image, naming their findings, from 4 HIGH in python:3.13-slim to 976 in the agent-server image. The admission policy refused an image without a digest and admitted the builder by digest. `inv publish-platform-images` signed the eight catalog images from the builder image. With verification on, a pod verified the signed trivy image with the revocation check and pulled it, refused an unsigned buildah digest, and refused the same digest signed with the `adp` tenant profile, and a verification pod admitted the agent-server sandbox image. The test signature was deleted. An IAM policy simulation of the `adp` tenant worker role allowed `ecr:BatchGetImage` on the catalog, its own prefix, and the pull-through caches, denied it on another tenant's prefix and the platform repository, and denied `ecr:PutImage` everywhere. The publish scan refused the local gate engine images for their six HIGH findings. The sandbox runs with scanning and verification on, so no run executes there until ADP-180.

Local. The local cluster rendered the admission policy and its binding, refused `busybox:1.36` and the upstream buildah image, and admitted the platform builder by digest. A trivy pod and a semgrep pod ran through admission and the pod policy, which refused a pull from quay.io and admitted the scanner image.

Local run. Rebar tickets on a local-backend repository with a `checks.yaml` build image ran through planning, implementation, checks, the semgrep, trivy, and `policy-source` gates, promotion, and `promote_pr` on Bedrock, with every image admitted by digest. The runs surfaced six defects, fixed in this unit. The local policy did not admit the `public.ecr.aws/docker/library/` base image of the engine Dockerfiles, and AWS had no mirror for it, so `registries.yaml` now mirrors all of `public.ecr.aws` through the `ecr-public` cache. A redeploy left the implementation activity pending until its stage timeout, so every worker heartbeats its activities, every activity receives a 3 minute heartbeat timeout, and the implementation stage retries once after a lost worker on a reset workspace and a replaced sandbox pod. Deleting the activity worker 10 seconds into an implementation timed the attempt out 3 minutes later, and the second attempt finished and the run reached `promote_pr`. Two overlapping intake passes started two runs for one ticket, so `run_once` claims a side effect exclusively, and two concurrent passes on a retried ticket started one run while the other deferred. Per-instance database engines held 68 idle connections and exhausted Postgres, so a process shares one engine per event loop, and the worker held 2 connections after four runs. A repository with `index: false` failed context gathering with 503, so the service returns an empty package for it. The agent patch omitted binary files, so the extraction passes `--binary`. In the sandbox the mirror applied, the deployer rolled it into `registries.yaml`, and a later plan reported no changes. A review of the runs found four more defects. The binary patch fix let `__pycache__` files into a pull request, so the sandbox sets `PYTHONDONTWRITEBYTECODE=1` and the extraction leaves out untracked build output. A Dockerfile `FROM` line needed no digest and skipped the scan gate, so the worker reads each engine source and checks build Dockerfile and admits every image the build pulls. `inv publish-platform-images` published unsigned images when it found no signing profile, so it now exits 1 without one unless `--unsigned` is passed, and the `AWS_IMAGE_SIGNING_PROFILE_ARN` repository variable names the platform profile. A static test now fails CI on a blocking call inside an async activity. After a rebuild, two concurrent intake passes on a new ticket started one run and replayed it in the other, a checks build admitted its digest-pinned base image, and a run on the repository with `index: false` planned with an empty context package, built the `policy-source` engine from its `public.ecr.aws` base image, passed every gate, and reached `promote_pr` with a branch that holds only source files. ADP-180 carries the AWS repeat of these checks.
