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

## Network zones and egress

Posture. A created network separates its zones into subnet tiers, each with its own route table. The public tier holds only the NAT path. The application tier holds the services and the interface endpoints. The management tier holds the jumphost and administrative tasks. The data tier holds the databases and the ClickHouse file system, has no route to the internet, and admits only the database, cache, and NFS ports from the application and management tiers. Outbound traffic from the application and management tiers leaves through one NAT path, and the VPC resolves only an allowlist of AWS, GitHub, Atlassian, and Slack names. Data is protected by at least three challenges on separate resources: the data-tier network ACL, the per-service security groups, and database authentication over enforced TLS.

Controls. SC-7 boundary protection through the tiered subnets, the data-tier network ACL, and the data-tier security groups without egress in the [private-bedrock](../infra/terraform/examples/private-bedrock) composition root, which separates the publicly routed tier from the internal tiers. SC-7(5) deny by default through the [dns-firewall](../infra/terraform/dns-firewall) allowlist, which answers NXDOMAIN for every other name and fails closed. SC-7(3) and SC-7(4) limited, managed access points through the single NAT path in the [egress](../infra/terraform/egress) module. AC-4 information flow enforcement through the per-tier route tables, which give the data tier no default route. AU-12 audit generation through the resolver query log. CM-6 and SI-2 for the NAT instance through Amazon Linux 2023 minimal with IMDSv2 required, no SSH, Systems Manager administration, and security updates at boot.

## Data tier

Posture. Evidence and audit objects persist to an object store under a write-once-read-many retention that a delete or an overwrite cannot bypass within the window, and the Postgres tables remain the queryable index. Objects are encrypted at rest and reachable only over TLS.

Controls. AU-9 protection of audit information through S3 Object Lock in compliance mode in the [evidence-store](../infra/terraform/evidence-store) module, which forbids early delete or overwrite including by the account root. SC-28 protection at rest through SSE-KMS or SSE-S3 encryption on the bucket. SC-8 through the TLS-only bucket policy. SI-12 information retention through the default Object Lock retention window. AC-6 through the writer-allowlist bucket policy.

Posture. The platform database runs as Aurora PostgreSQL and the Langfuse queue and cache as ElastiCache for Valkey, both in the data tier, encrypted at rest, and reachable only over TLS. The database master password is generated and rotated by RDS in Secrets Manager and never enters Terraform state. The cache refuses unauthenticated connections and admits one RBAC user. ClickHouse keeps its data on an encrypted EFS file system that admits mounts only over TLS and only through its access point. Langfuse uploads land in an encrypted, TLS-only bucket. Terraform state is held in an encrypted, versioned, TLS-only bucket rather than on a workstation. The rebar ticket store syncs to its own bucket under a dedicated KMS key, fetched under a reader role and pushed under a separate writer role, and the activity worker refuses to start when the store's configuration overlaps the evidence or audit stores.

Controls. SC-28 protection at rest through Aurora storage encryption in [aurora-postgresql](../infra/terraform/aurora-postgresql), ElastiCache at-rest encryption in [elasticache](../infra/terraform/elasticache), EFS encryption in [clickhouse-storage](../infra/terraform/clickhouse-storage), and SSE on the [langfuse-blob-store](../infra/terraform/langfuse-blob-store) and [tf-state](../infra/terraform/tf-state) buckets. SC-8 transmission confidentiality through `rds.force_ssl`, ElastiCache transit encryption set to `required`, the EFS file system policy, and the TLS-only bucket policies. IA-5 authenticator management through the RDS-managed rotating master secret and the ElastiCache credential stored in Secrets Manager in the rotation format. AC-3 and AC-6 through IAM database authentication, the RBAC access string, the disabled default cache user, and the EFS access point. AU-2 and AU-12 through pgaudit DDL logging, connection logging, and the exported PostgreSQL and Valkey engine logs. CP-9 system backup through 30-day Aurora automated backups, ElastiCache snapshots, EFS automatic backups through AWS Backup, and versioned buckets. CP-10 recovery through Aurora storage replicated across three Availability Zones, with instance and node counts raised for production failover. SC-13, SC-28, SC-8, and AC-6 for the ticket store through SSE-KMS with an S3 Bucket Key, a bucket policy that requires TLS 1.2 and allows object access only to the store roles, and the startup isolation check in [rebar-ticket-store](../infra/terraform/rebar-ticket-store). AU-2 and AU-12 for the ticket store through CloudTrail data events on every object read and write, denied requests included.

## Secrets

Posture. Secret material reaches a workload from AWS Secrets Manager rather than from source. The [secret-store](../infra/terraform/secret-store) module encrypts every secret with a dedicated KMS key, allows each secret to be read only by the roles that consume it, and injects the values into tasks at start. Each service connects to PostgreSQL as its own role, limited to its own database, rather than as the master user. The database and cache credentials rotate on a quarterly schedule in an after-hours window, the consuming services redeploy when a credential changes, and the README runbook covers the event-driven rotation of every other secret. The trust-root bundle, which holds only public keys, ships as non-secret configuration.

Controls. IA-5 authenticator management through the organization-defined rotation period and event triggers of IA-5(f) and IA-5(i), generated values that never enter Terraform state, and passwords set as SCRAM verifiers so no log holds them. IA-5(7) through secrets that live only in Secrets Manager and never in source or images. AC-6 least privilege through the per-secret resource policies and the per-service database roles. SC-12 cryptographic key establishment through the KMS keys the modules consume, with automatic key rotation enabled on the secret-store key and the key in [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate). SC-28 at-rest protection of secret values under the customer managed key.

## Identity

Posture. Each workload assumes a scoped role rather than sharing a static credential, and each authoring agent signs the plan and the code change it produces under its own rebar identity. The merge gate verifies both signatures against the identity trust root and fails closed on an absent or out-of-trust-root signature.

Controls. IA-2 and IA-5 identification and authenticator management through per-identity keyrings with trust-on-first-use genesis, signed rotation, and revocation, described in [authored-identity-signing.md](authored-identity-signing.md). AU-10 non-repudiation through the authorship signatures bound to the produced artifact. AC-6 least privilege through the scoped IAM roles in [bedrock-iam](../infra/terraform/bedrock-iam) and [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate).

## Images

Posture. Container images are built from pinned bases and pushed to a registry that scans on push and forbids tag reuse, so a deployed image has a known provenance and a vulnerability record.

Controls. CM-2 and CM-6 baseline configuration through immutable tags and the lifecycle policy in the [ecr](../infra/terraform/ecr) module. RA-5 vulnerability scanning through scan-on-push in the same module and through the `inv sast` CI gate, which runs SAST, secret, infrastructure misconfiguration, and dependency scans and fails on any finding the [accepted-findings register](../security/sast-accepted.yaml) does not justify. SA-11 developer testing through the same gate on every pull request. SI-7 software and information integrity through the pinned base image and the immutable tag. SR-3 and SR-4 supply chain controls through the managed registry and the pinned provenance.

## Audit export

Posture. Platform decisions write to an append-only audit trail, control-plane API activity writes to CloudTrail, and both the evidence objects and the audit objects persist to the object store under Object Lock. The trail carries log-file integrity validation.

Controls. AU-2 and AU-12 audit generation through the platform `audit_events` records and CloudTrail. AU-3 content of audit records through the committed [audit-event schema](../schemas/audit_events/audit-event.schema.json). AU-9 protection of audit information through CloudTrail log-file validation and Object Lock on the evidence and audit objects, and the TLS-only trail bucket and the customer-managed key that encrypts the trail log files in the [cloudtrail](../infra/terraform/cloudtrail) module. AU-2 and AU-12 object-access records through S3 server access logging from every platform bucket to the bucket in the [access-logs](../infra/terraform/access-logs) module. AU-11 audit record retention through the Object Lock retention window, and for access logs through 12 months in S3 Standard followed by 18 months in Glacier Instant Retrieval.

## Webhook relay policy

Posture. The single public ingress is the webhook receiver. It authenticates each inbound delivery, rejects an unsigned or mis-signed request, and converges duplicate deliveries on one outcome through the coordination idempotency ledger.

Controls. SI-10 information input validation through the HMAC-SHA256 signature check with a constant-time comparison in [pr_events.py](../services/orchestration/webhook/pr_events.py). IA-3 device and sender authentication through the shared webhook secret. SC-8 transmission confidentiality over TLS. SC-5 denial-of-service consideration through the idempotency ledger that collapses duplicate deliveries.

## Cross-cutting integrity

Posture. Every review verdict is signed with an asymmetric SSHSIG signature carried in a DSSE envelope and verified against a public trust root before the gate accepts it, so a verifier holding only public keys cannot forge a verdict.

Controls. SI-7 software and information integrity and AU-10 non-repudiation through the signed verdicts described in [review-verdict-signing.md](review-verdict-signing.md). SC-12 and SC-13 cryptographic key management and cryptographic protection through the trust-root bundle with per-key validity windows, revocation, and an overlapping rotation window.
