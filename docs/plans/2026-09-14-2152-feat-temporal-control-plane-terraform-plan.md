---
title: Temporal Control Plane Terraform - Plan
type: feat
date: 2026-11-02
origin: .agent-context/roadmap-jira.md
ticket: ADP-97
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: roadmap
execution: infra
---

# Temporal Control Plane Terraform - Plan

## Goal Capsule

Objective. A deployed environment runs the OSS Temporal server and UI on the platform ECS cluster, persisting to Aurora over verified TLS, reachable only by the Temporal client security group over TLS, and every platform Temporal client connects to it over TLS.

Means. Build downstream server and admin-tools images that carry the RDS CA bundle and the platform server configuration, generate or accept the TLS certificates in three modes, add a toggleable `temporal-control-plane` module that runs the server, its schema task, and the UI behind an internal NLB and ALB, route every `Client.connect` call through one TLS helper, and wire the module into `private-bedrock`.

Product authority. ADP-97 in Jira, from the v0.3.0 section of `.agent-context/roadmap-jira.md`, with sub-tasks ADP-147, ADP-148, ADP-143, ADP-68, and ADP-50.

Stop conditions. The module applies in the sandbox account in each certificate mode. The server persists to Aurora over TLS with host verification, and a client outside the client security group cannot reach the frontend. A rotation of the `temporal` role password redeploys the server. `temporal-ui` 2.58.0 runs locally and deployed. `terraform validate`, `inv terraform-test`, `inv ci`, and `inv sast` pass.

## Product Contract

### Summary

Compose runs `temporalio/server` 1.32.1 through `docker/temporal.Dockerfile`, and the one-shot `temporal-admin-tools` task applies the schemas and registers the `default` namespace. No Terraform provisions the control plane for a deployed environment. The 21 `Client.connect` calls in `services` and `scripts` pass only a host and namespace, so no client can open a TLS connection.

### Problem Frame

The control plane schedules and stores workflow state. FedRAMP Medium expects SC-7 boundary protection, SC-8 transmission confidentiality, SC-12 and SC-17 key and certificate management, SC-13 FIPS-validated cryptography, AC-3 access enforcement, and CP-9 and CP-10 durability. AWS provides the ECS, NLB, ACM, Secrets Manager, KMS, and Aurora controls as configuration. The platform owns the TLS configuration, the network admission, the certificate lifecycle, and the client behavior.

### Requirements

- R1. The server and admin-tools images are built from the upstream digests the local stack pins, carry the RDS CA bundle, and are pushed to ECR.
- R2. The server persists to the `temporal` and `temporal_visibility` databases over TLS with host verification, as the `temporal` role the secret store provisions.
- R3. A one-shot admin-tools task applies the schemas and registers the namespace before the server starts.
- R4. The frontend listener serves TLS, and the internode listeners require mutual TLS.
- R5. Each certificate comes from a repository script, from the organization's PKI, or from ACM. The NLB presents an ACM frontend certificate. ACM tracks an internode certificate for expiry, and Secrets Manager serves it, because ACM never returns the key of an imported certificate.
- R6. An internal NLB admits the frontend port only from a Temporal client security group the module creates and emits.
- R7. The server runs on the platform ECS cluster in the application tier, and the UI runs in the management tier behind an internal HTTPS ALB.
- R8. A rotation of the `temporal` role password redeploys the server through the secret store restart path.
- R9. The deployed dynamic configuration comes from module input and carries no Worker Versioning V1 key.
- R10. `temporal-ui` runs 2.58.0 pinned by digest locally and deployed.
- R11. Every Temporal client connects through one helper that applies `TEMPORAL_TLS_CA` and `TEMPORAL_TLS_SERVER_NAME`, and connects without TLS when both are absent.
- R12. The module is gated by a toggle, provisions nothing when off, and emits the endpoint, the namespace, the CA secret ARN, the server name, and the client security group.

### Scope Boundaries

- The platform ECS cluster is ADP-145. This plan consumes its ARN.
- The ECS services and the EKS workloads attach the client security group in ADP-136 and ADP-137.
- A worker polling the deployed server as a Worker Deployment version is verified in ADP-136, which first runs the workers in AWS.
- The off-hours scale-down of the server and the database pause are ADP-146.
- Server 1.32.2 is not published. The digests move to it in a later change.

### Sources

- `common/config/config_template_embedded.yaml` at `temporalio/temporal` v1.32.1, which reads the TLS, SQL, and dynamic configuration settings from the environment and takes a configuration file through `--config-file`.
- `temporalio/ui` v2.58.0 release notes. Trivy reports 16 HIGH and no CRITICAL findings for the 2.58.0 image, against 22 HIGH and one CRITICAL for 2.57.0, and the UI answers the namespace, workflow, and worker deployment endpoints against the local 1.32.1 server.
- `infra/terraform/secret-store`, whose `restart_services` input redeploys ECS services after a rotation, and whose `database_roles` include `temporal`.
- `docker/temporal.Dockerfile`, `docker/temporal-admin-tools.sh`, and `compose.yaml` for the local server, schema task, and pins.

## Planning Contract

### Key Technical Decisions

- KTD1. Build downstream images in place of running the upstream images directly. Governs R1, R2, R4. The upstream images carry no RDS CA bundle. The server image also carries a configuration file derived from the embedded template, because the embedded template drives frontend and internode client authentication from one `TEMPORAL_TLS_REQUIRE_CLIENT_AUTH` variable, and R4 needs client authentication on internode only.
- KTD2. Inject the certificates and keys from Secrets Manager through the execution role as environment values, which a start script in the server image writes to files in a task volume and removes from the environment before it execs the upstream entrypoint. The template's `*_DATA` variables take base64, not the PEM text the secrets hold. No certificate is written to an image.
- KTD3. Terraform creates the TLS secrets empty, and `inv temporal-certs put` writes the certificate material into them. Write-only arguments were rejected. The AWS provider requires `secret_string_wo` on every later plan once it is set, so every plan of `private-bedrock` would need the private keys, and a sandbox probe confirmed it. A generated directory and one from the organization's PKI go through the same `put`.
- KTD4. In the ACM mode, the NLB TLS listener terminates client TLS with the ACM certificate under the `HTTP2Only` ALPN policy that gRPC needs, and a TLS target group re-encrypts to the server, which presents the internode certificate, because the NLB does not verify target certificates and no frontend secret exists in this mode. A client then trusts the ACM chain, and `TEMPORAL_TLS_CA` is empty for a public ACM certificate.
- KTD5. Admit the frontend through a client security group the module owns, so the NLB and its rules exist before the ECS and EKS workloads, which attach the group.
- KTD6. Pass the dynamic configuration from a module input as an environment value, which the same start script writes to the task volume, so the configuration changes with a plan and apply rather than an image build. A short root container opens the volume to the unprivileged image user, because Fargate creates it owned by root and the root filesystem is read-only.
- KTD7. The connection helper enables TLS when either setting is present. A CA path adds a private root, and a server name alone uses the system trust store.

### High-Level Design

`infra/terraform/temporal-control-plane` creates the ECR repositories, the Secrets Manager secrets for the certificates, the client security group, the server task definition and ECS service, the schema RunTask definition, the target group, the internal NLB, the UI task definition and service, the internal HTTPS ALB, the KMS-encrypted log groups, and the task and execution roles. `private-bedrock` passes the platform cluster, the subnets, the Aurora endpoint, the secret store outputs, and the certificate inputs, and adds the server service to `secret_restart_services`. `services/temporal_client.py` provides the connection helper.

### Build order

1. ADP-145 provisions the platform ECS cluster.
2. U1 builds and pushes the server and admin-tools images.
3. U2 creates the TLS secrets, and the certificates are generated or supplied and written with `inv temporal-certs put`, or imported into ACM.
4. U4 creates the client security group, the server service, and the target group, then the NLB with its listener admitting only that group.
5. U5 wires the composition root and emits the outputs that ADP-136 and ADP-137 consume.

U3 has no infrastructure dependency and lands at any point.

## Implementation Units

### U1. Downstream server and admin-tools images (ADP-147)

Add the RDS global CA bundle, pinned by SHA-256, at `/etc/ssl/rds-global-bundle.pem` in the server image `docker/temporal.Dockerfile` builds. Add `docker/temporal-admin-tools.Dockerfile` from the admin-tools digest `compose.yaml` pins, with the same bundle and `docker/temporal-admin-tools.sh` as its entrypoint. Create the `temporal-control-plane` module with its two ECR repositories and wire it into `private-bedrock`. Add an `inv publish-temporal-images` task that builds both for `linux/arm64` and pushes them, tagged `<upstream version>-<content hash>-<arch>`, where the hash covers every build input, because the repositories reject a repeated tag and a script change alone must produce a new one. Keep the local stack on the same Dockerfile. The server configuration file depends on the TLS settings, so it moves to U4.

### U2. Certificates (ADP-148)

Add `scripts/temporal_certs.py` behind `inv temporal-certs`. `generate` runs `openssl` to write a P-256 CA, a frontend certificate, and an internode certificate with server and client usage, with mode 0600, to a directory outside the repository, refusing to overwrite without `--force`. `put` checks a generated or supplied directory and writes it to the secrets, imports the internode certificate into ACM with `--import-to-acm`, and refuses a certificate that differs from the tracked one or frontend files when ACM serves the frontend. Add the module KMS key, the TLS secrets with a deny-other-readers policy, `frontend_acm_certificate_arn`, `internode_acm_certificate_arn`, and an EventBridge rule and SNS topic for the ACM expiry events.

### U3. Connection helper (ADP-143)

Add `TEMPORAL_TLS_CA` and `TEMPORAL_TLS_SERVER_NAME` to the settings, add `services/temporal_client.py` with one `connect` function, and route every `Client.connect` call in `services` and `scripts` through it. Add a test that fails when a `Client.connect` call appears outside the helper.

### U4. Server, schema task, and UI (ADP-68)

Add the platform server configuration file to the server image. Run the server on the platform cluster with the Aurora TLS settings, the certificate secrets, the init container for the dynamic configuration, and the client security group admission. Run the schema task before the service through a deploy step. Run `temporal-ui` 2.58.0 behind the internal HTTPS ALB in the management tier, connecting to the frontend over TLS. Move the `compose.yaml` UI pin to 2.58.0.

### U5. Composition root and documents (ADP-50)

Add the module block to `private-bedrock` behind `create_temporal_control_plane`, add the server service to `secret_restart_services`, surface the outputs, and extend the README Deploying to AWS section with the certificate modes and the schema step. Update the local-to-AWS mapping, the FedRAMP hardening design, and the control inheritance matrix.

### U6. Sandbox verification

Apply in the sandbox in each certificate mode, connect a client over TLS in each mode, confirm that the internode listeners refuse a peer without a certificate from the configured CA, and record the results below.

## Verification Record

### U1 (ADP-147), 2026-11-03

- `inv publish-temporal-images` pushed `agentic-platform/temporal-server:1.32.1-3b61f170c0c9-arm64` and `agentic-platform/temporal-admin-tools:1.32.1-4b9166505464-arm64`. A second run reported both as pushed and built nothing. Both repositories hold immutable tags with scan on push and KMS encryption, and both scans completed.
- The admin-tools image ran as a Fargate task in `agentic-platform-ecs` against the sandbox Aurora cluster endpoint, with `SQL_TLS=true` and the credentials injected from the `database_temporal` secret. With `SQL_TLS_CA_FILE=/etc/ssl/rds-global-bundle.pem` it set up and updated both schemas. With the system CA bundle it failed with `x509: certificate signed by unknown authority`.
- A build with a wrong `RDS_CA_BUNDLE_SHA256` fails on the digest mismatch.
- The local stack rebuilt the server image and reported server 1.32.1 healthy.

### U2 (ADP-148), 2026-11-03

- The apply created the KMS key and the `tls-ca`, `tls-internode`, and `tls-frontend` secrets. `inv temporal-certs generate` wrote the six files with mode 0600, and `openssl verify` passed for both leaves. `put` wrote all three secrets, and `platform-admin` is denied `GetSecretValue` on them because no reader is listed.
- `put --import-to-acm` imported the internode certificate into ACM. Setting `temporal_internode_acm_certificate_arn` created the expiry rule and topic, and `aws events test-event-pattern` matched the approaching-expiration and expired events and not the available event. A regenerated certificate was refused without `--import-to-acm`, and with it the certificate was re-imported into the same ARN with the new serial.
- An operator-imported frontend certificate in `temporal_frontend_acm_certificate_arn` removed the frontend secret, `put` refused a directory holding frontend files, and a directory without them was written.
- A non-certificate ARN failed variable validation, and `terraform state pull` holds no `PRIVATE KEY`.
- The sandbox was returned to secrets mode with the generated certificates written, the imported certificates deleted, and a full plan reporting no changes.

### U3 (ADP-143), 2026-11-03

- All 21 `Client.connect` calls in `services` and `scripts` go through `services.temporal_client.connect`, and `test_no_client_connects_outside_the_helper` fails on a new one.
- Against an isolated Temporal 1.32.1 server whose frontend served the sandbox certificate, the helper connected with `TEMPORAL_TLS_CA` as a file path and as the inline bundle, each with `TEMPORAL_TLS_SERVER_NAME=frontend.temporal.internal`. It was refused without TLS settings, with another server name, and with only the server name, which leaves the private CA untrusted.
- The rebuilt local stack connects without TLS. `inv workflow-status`, `inv demo-durable-counter`, and `inv durability-proof` passed.
- The Temporal CLI in the admin-tools image ignored `TEMPORAL_TLS_CA_PATH` from the environment and accepted `--tls-ca-path` and `--tls-server-name` as flags, so the namespace action in U4 passes TLS as flags.

### U4 (ADP-68), 2026-11-03

- The apply created the server service at a count of zero, the internal NLB, the admin-tools task definition, the roles, the security groups, and the log groups. `inv temporal-deploy` ran the schema task to exit 0, moved the server to its current task definition, and ran the namespace task, which registered `default` through the NLB over TLS as a member of the client group. The server task and the NLB target report healthy through the HTTPS probe.
- The namespace task run without the client group could not reach the frontend and exited 1.
- A rotation of the `temporal` role password redeployed the server through the secret store restart rule, after the rule's EventBridge role was granted the document and execution resources SSM authorizes the call against.
- With a second management subnet and an imported UI certificate, the UI service came up behind its internal ALB. From a task in the management tier, the UI answered the settings, namespaces, workflows, and worker deployments endpoints with 200, listing the namespaces it read from the server.
- With a workflow started and a Worker Deployment created through the admin-tools CLI over TLS, the UI listed the `default` namespace, the running workflow, and the Worker Deployment through the ALB. Both were removed afterwards.
- After the rotation, the server deployment completed with one healthy task, and its log held no database authentication or certificate error.
- With the jumphost task group admitted to the UI ALB and the management CIDRs removed, `inv temporal-ui` forwarded `localhost:8443` through a jumphost task, and the UI answered the page, namespaces, and worker deployments requests with 200 to a client that verified the ALB certificate against the UI CA.
- With access logs enabled, both load balancers accepted the SSE-S3 log bucket at apply, the ALB wrote its `ELBAccessLogTestFile` under `ui/`, and the NLB created its prefix under `frontend/`. A full plan afterwards reported no changes.
- In a local container, the custom template required client certificates on the internode listener (TLS alert `certificate required`) and none on the frontend, and the start script left no PEM value in the server process environment.

### U5 (ADP-50), 2026-11-04

- `terraform validate` passes in `private-bedrock`. With the control plane deployed, a plan reports no changes. A plan with `create_temporal_control_plane = false` destroys the 82 control plane resources and removes the secret store's `database_temporal` restart rule, its automation roles, and the Temporal readers from the secret and key policies, and changes nothing else.
- `temporal_tls` emits the frontend and internode server names. `put` accepted the sandbox certificates against them and refused a frontend certificate that does not name the configured `temporal.example.gov`, before writing anything.
- With `temporal_fips_140_mode = true` the apply registered new server, admin-tools, and UI task definitions carrying `GODEBUG=fips140=on`, and `inv temporal-deploy` completed. The schema task reached Aurora over TLS with host verification and authenticated with SCRAM, the namespace task connected through the NLB, and the UI listed the namespaces and the workflows through the jumphost forward. Before the change the frontend and internode listeners accepted a client offering only ChaCha20-Poly1305 or only X25519. After it both listeners refused those clients with `handshake failure` and accepted AES-128-GCM over P-256. The internode listeners still refused a client without a certificate and a client with a certificate from another CA.
- `temporal-sql-tool` authenticated to a local Postgres 16 with `password_encryption = scram-sha-256` under `GODEBUG=fips140=on`.
- `go version -m` on the upstream `temporal-server` 1.32.1 binary reports Go 1.26.8 with `CGO_ENABLED=0` and no `GOFIPS140` setting, so FIPS mode runs on the in-tree Go Cryptographic Module.
- Supplied certificates in FIPS mode. A root and an issuing CA outside `inv temporal-certs generate` issued RSA 3072 frontend and internode certificates. The three-round rotation from the generated CA completed, and each namespace task connected. The server presented the leaf and issuing CA on the frontend and internode ports, both verified against the bundle, the internode ports refused a client without a certificate and a client from another CA, and both listeners refused ChaCha20-Poly1305 and X25519 and accepted AES-128-GCM over P-256. The server and admin-tools logs held no TLS or x509 error.
- ACM frontend in FIPS mode. With the supplied frontend certificate imported into ACM, the apply replaced the listener and the target group, the TLS target reported healthy, and the namespace task connected through the NLB to the FIPS-mode server. A client in the client security group received the ACM certificate with ALPN `h2` over TLS 1.3, verified it against the bundle and the frontend name, and was refused under another name. The listener refused TLS 1.1 and accepted TLS 1.2 with ECDHE-RSA-AES128-GCM-SHA256. Behind the NLB the server presented the internode certificate on its frontend port under the same algorithm restrictions, and the UI listed the namespaces and the workflows through the jumphost forward.
- The return to secrets mode replaced the listener and the target group in one apply. The sandbox then rotated back to the generated certificates in three rounds.

### U6, 2026-11-04

- Internode refusal. From a task in the server security group, the internode ports 7234, 7235, and 7239 of the server task answered a client without a certificate with the TLS alert `certificate required` and a client with a certificate from another CA with `unknown ca`. The frontend port 7233 on the same address accepted a client without a certificate, and the server certificate verified against the CA bundle. The results held for the generated and the supplied certificates.
- Generated certificates. The namespace task registered `default` through the NLB over TLS, verifying the frontend against the generated CA.
- Supplied certificates. A root and an issuing CA outside `inv temporal-certs generate` issued RSA 3072 frontend and internode certificates, each with the issuing CA in its chain. The rotation from the generated CA ran in three `put` and `inv temporal-deploy` rounds. The first wrote a CA bundle holding both CAs with the generated leaves, the second wrote the supplied leaves under the same bundle, and the third wrote the supplied CA alone. Each namespace task connected over TLS, and the server then presented the supplied chain on the frontend and internode ports. The server log held no TLS, x509, or handshake error across the rollouts.
- ACM frontend. With the supplied frontend certificate imported into ACM and set as `temporal_frontend_acm_certificate_arn`, the apply removed the frontend secret, replaced the target group with a TLS target group, and moved the listener to TLS. After `put` of the CA and internode pair and `inv temporal-deploy`, a client in the client security group received the ACM certificate with ALPN `h2` over TLS 1.3 and verified it against the CA bundle and the frontend name. The listener refused TLS 1.1 with `protocol version` and accepted TLS 1.2 with an ECDHE GCM cipher. The namespace task connected through it, and the UI listed the namespaces through the jumphost forward.
- Return to secrets mode. The first apply failed because the provider kept `ssl_policy` in the TLS to TCP listener update, which ELB rejects. The listener is now replaced whenever the frontend mode changes, and a plan with the ACM certificate set reports that replacement. The target group name carries the protocol and the replacement group is created before the old one is deleted. The sandbox returned to the generated certificates in secrets mode through the same three rounds, and a full plan reported no changes.
- `inv temporal-deploy` replaces the server and UI tasks on every run, because both read the TLS secrets only when a task starts and an update to the same task definition starts no new task.
