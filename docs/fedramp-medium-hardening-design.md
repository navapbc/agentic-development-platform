# FedRAMP Medium hardening design

This design ties each platform component to the FedRAMP Medium control families it satisfies, grounded in the controls the platform implements today. It is a design reference. It provisions nothing and changes no runtime code. The platform-owned controls that are not yet met are tracked as dedicated v0.3.0 features in the [roadmap](../ROADMAP.md), not in this document.

Control family labels follow NIST SP 800-53 Revision 5. A mapping to a family cites the terraform module or the code that implements the control, so the posture is verifiable rather than aspirational. The companion [local-to-AWS mapping](local-to-aws-mapping.md) records where each component runs in a deployed environment.

## Trust boundary and deployment posture

The platform runs inside the target VPC. It reaches the private `bedrock-runtime` interface endpoint with the direct connectivity provider and no hop, and it has no public ingress except the webhook receiver, which authenticates every request. Private reachability comes from the shared interface and gateway endpoints in the [vpc-endpoints](../infra/terraform/vpc-endpoints) module, whose security group admits port 443 only from in-VPC subnets. This establishes the boundary the SC family protects.

## Control inheritance

The platform is hosted on an AWS account that holds a FedRAMP Medium authorization. Under the FedRAMP shared-responsibility model the platform inherits the controls AWS owns and implements the controls it configures on top. The physical and environmental families, the hypervisor and host controls, the FIPS-validated cryptographic modules behind KMS and the FIPS service endpoints, the durability and availability of the managed services, and the underlying network fabric are inherited from the AWS authorization. The platform is responsible for how it configures and uses those services, which is encryption enabled, least-privilege IAM, audit logging turned on, retention set, network isolation applied, FIPS endpoints selected, and secrets managed. The control inheritance and shared-responsibility matrix in [fedramp-control-inheritance.md](fedramp-control-inheritance.md) records the per-family split of inherited, platform-implemented, and deploying-organization controls. The sections below describe the platform-implemented portion. The platform-owned controls that are not yet met are tracked as v0.3.0 features in the [roadmap](../ROADMAP.md).

## Temporal control plane

Posture. The workflow control plane runs in-VPC with no public exposure, and workers reach it over the private network. Access is scoped to the workers and operators that hold VPC network reachability.

Controls. SC-7 boundary protection through the in-VPC placement and the endpoint security group in [vpc-endpoints](../infra/terraform/vpc-endpoints). SC-8 transmission confidentiality over TLS. AC-3 and AC-4 through the network path that admits only in-VPC callers. CM-6 configuration settings through the pinned compose and image definitions.

## Edge endpoints

Posture. The platform reaches AWS services through interface and gateway VPC endpoints rather than the public internet, so traffic to Bedrock, the object store, and the other consumed services stays on the AWS network. Bedrock inference is authorized by a dedicated IAM policy.

Controls. SC-7 boundary protection and AC-4 information flow enforcement through the [vpc-endpoints](../infra/terraform/vpc-endpoints) interface endpoints with private DNS and the S3 gateway endpoint. SC-8 transmission confidentiality over TLS to every endpoint. AC-6 least privilege for inference through the `bedrock:InvokeModel` policy in [bedrock-iam](../infra/terraform/bedrock-iam), attached to the role that signs Bedrock calls.

## Data tier

Posture. Evidence and audit objects persist to an object store under a write-once-read-many retention that a delete or an overwrite cannot bypass within the window, and the Postgres tables remain the queryable index. Objects are encrypted at rest and reachable only over TLS.

Controls. AU-9 protection of audit information through S3 Object Lock in compliance mode in the [evidence-store](../infra/terraform/evidence-store) module, which forbids early delete or overwrite including by the account root. SC-28 protection at rest through SSE-KMS or SSE-S3 encryption on the bucket. SC-8 through the TLS-only bucket policy. SI-12 information retention through the default Object Lock retention window. AC-6 through the writer-allowlist bucket policy.

## Secrets

Posture. Secret material reaches a workload from the deployment secret store rather than from source. Signing keys, model API keys, and webhook secrets are mounted or fetched at runtime, and the trust-root bundle, which holds only public keys, ships as non-secret configuration.

Controls. IA-5 authenticator management through the out-of-band provisioning of signing keys and API keys described in the [local-to-AWS mapping](local-to-aws-mapping.md). SC-12 cryptographic key establishment through the KMS keys the modules consume, with automatic key rotation enabled on the KMS key in [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate). SC-28 at-rest protection of secret values in the secret store.

## Identity

Posture. Each workload assumes a scoped role rather than sharing a static credential, and each authoring agent signs the plan and the code change it produces under its own rebar identity. The merge gate verifies both signatures against the identity trust root and fails closed on an absent or out-of-trust-root signature.

Controls. IA-2 and IA-5 identification and authenticator management through per-identity keyrings with trust-on-first-use genesis, signed rotation, and revocation, described in [authored-identity-signing.md](authored-identity-signing.md). AU-10 non-repudiation through the authorship signatures bound to the produced artifact. AC-6 least privilege through the scoped IAM roles in [bedrock-iam](../infra/terraform/bedrock-iam) and [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate).

## Images

Posture. Container images are built from pinned bases and pushed to a registry that scans on push and forbids tag reuse, so a deployed image has a known provenance and a vulnerability record.

Controls. CM-2 and CM-6 baseline configuration through immutable tags and the lifecycle policy in the [ecr](../infra/terraform/ecr) module. RA-5 vulnerability scanning through scan-on-push in the same module. SI-7 software and information integrity through the pinned base image and the immutable tag. SR-3 and SR-4 supply chain controls through the managed registry and the pinned provenance.

## Audit export

Posture. Platform decisions write to an append-only audit trail, control-plane API activity writes to CloudTrail, and both the evidence objects and the audit objects persist to the object store under Object Lock. The trail carries log-file integrity validation.

Controls. AU-2 and AU-12 audit generation through the platform `audit_events` records and CloudTrail. AU-3 content of audit records through the committed [audit-event schema](../schemas/audit_events/audit-event.schema.json). AU-9 protection of audit information through CloudTrail log-file validation and Object Lock on the evidence and audit objects, and the TLS-only encrypted trail bucket in the [cloudtrail](../infra/terraform/cloudtrail) module. AU-11 audit record retention through the Object Lock retention window.

## Webhook relay policy

Posture. The single public ingress is the webhook receiver. It authenticates each inbound delivery, rejects an unsigned or mis-signed request, and converges duplicate deliveries on one outcome through the coordination idempotency ledger.

Controls. SI-10 information input validation through the HMAC-SHA256 signature check with a constant-time comparison in [pr_events.py](../services/orchestration/webhook/pr_events.py). IA-3 device and sender authentication through the shared webhook secret. SC-8 transmission confidentiality over TLS. SC-5 denial-of-service consideration through the idempotency ledger that collapses duplicate deliveries.

## Cross-cutting integrity

Posture. Every review verdict is signed with an asymmetric SSHSIG signature carried in a DSSE envelope and verified against a public trust root before the gate accepts it, so a verifier holding only public keys cannot forge a verdict.

Controls. SI-7 software and information integrity and AU-10 non-repudiation through the signed verdicts described in [review-verdict-signing.md](review-verdict-signing.md). SC-12 and SC-13 cryptographic key management and cryptographic protection through the trust-root bundle with per-key validity windows, revocation, and an overlapping rotation window.
