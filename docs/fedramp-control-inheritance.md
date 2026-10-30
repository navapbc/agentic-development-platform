# FedRAMP control inheritance and shared-responsibility matrix

This matrix records, per NIST SP 800-53 Revision 5 control family, which controls the platform inherits from the AWS FedRAMP Medium authorization, which the platform implements, and which the deploying organization owns. It is the scoping artifact the platform-owned FedRAMP features derive from. It is a reference document and provisions nothing.

The authoritative per-control inheritance for a hosted system comes from the AWS FedRAMP package and its Customer Responsibility Matrix and from the deploying agency's authorization. This document does not reproduce that package. It is the platform's shared-responsibility view, grounded in the standard FedRAMP shared-responsibility model and in the controls the platform implements, and the AWS package and the agency authorization remain authoritative for the inherited split.

## Inheritance model

The hosting account holds a FedRAMP Medium authorization. AWS owns the physical, environmental, and infrastructure controls and the validated cryptographic modules and service endpoints, and the platform inherits them. The platform owns how it configures and provisions the AWS services, which is encryption, access scoping, network isolation, audit configuration, retention, and secret handling. The deploying organization owns the operational and program controls that a running authorization requires, such as continuous monitoring, incident response, and personnel security. The platform provides the enablement those operational controls consume, such as the audit trail and the observability stack.

## Matrix

| Family | Inherited from AWS | Platform-implemented | Deploying-organization-owned |
|---|---|---|---|
| AC Access Control | Console and API infrastructure access controls | Least-privilege IAM through [bedrock-iam](../infra/terraform/bedrock-iam) and per-service task roles from the container-platform services feature, network access restriction through the [vpc-endpoints](../infra/terraform/vpc-endpoints) security group and the tiered subnets of the [private-bedrock](../infra/terraform/examples/private-bedrock) root, writer allowlists on the [evidence-store](../infra/terraform/evidence-store) bucket, separate reader and writer roles and an object-access allowlist on the [rebar-ticket-store](../infra/terraform/rebar-ticket-store) bucket, IAM database authentication in [aurora-postgresql](../infra/terraform/aurora-postgresql), and the RBAC user with the default user disabled in [elasticache](../infra/terraform/elasticache) | Account-level access policy and access approvals |
| AT Awareness and Training | None | None | Security awareness and role-based training |
| AU Audit and Accountability | Service-level logging infrastructure | Platform audit events, the [cloudtrail](../infra/terraform/cloudtrail) trail with log-file validation, write-once-read-many evidence and audit objects in [evidence-store](../infra/terraform/evidence-store), pgaudit DDL and connection logging in [aurora-postgresql](../infra/terraform/aurora-postgresql), and resolver query logging in [dns-firewall](../infra/terraform/dns-firewall), and S3 server access logs in [access-logs](../infra/terraform/access-logs) | Audit review cadence and log-retention policy approval |
| CA Assessment, Authorization, and Monitoring | Continuous monitoring of the AWS infrastructure | Observability enablement from the observability feature and the hardening design and this matrix as assessment inputs | The system authorization and the continuous-monitoring program |
| CM Configuration Management | Hypervisor and host configuration | Infrastructure as code across `infra/terraform`, immutable image tags and a lifecycle policy in [ecr](../infra/terraform/ecr), and pinned images and versioned configuration | Change-management process and approvals |
| CP Contingency Planning | Availability-zone and region durability | 30-day automated backups in [aurora-postgresql](../infra/terraform/aurora-postgresql), snapshots in [elasticache](../infra/terraform/elasticache), EFS automatic backups in [clickhouse-storage](../infra/terraform/clickhouse-storage), versioned state in [tf-state](../infra/terraform/tf-state), bucket versioning, and Object Lock retention in [evidence-store](../infra/terraform/evidence-store) | The contingency plan and its testing |
| IA Identification and Authentication | IAM authentication infrastructure and validated authenticators | Per-workload roles, authored-identity signing keyrings in [authored-identity-signing.md](authored-identity-signing.md), webhook sender authentication in [pr_events.py](../services/orchestration/webhook/pr_events.py), and the RDS-managed rotating master secret in [aurora-postgresql](../infra/terraform/aurora-postgresql), and the platform secrets, per-service database roles, and scheduled credential rotation in [secret-store](../infra/terraform/secret-store) | Human identity lifecycle and multi-factor authentication at the account |
| IR Incident Response | Infrastructure incident response | The audit trail and observability enablement | The incident-response plan and its execution |
| MA Maintenance | Infrastructure maintenance | The image rebuild and patch path through [ecr](../infra/terraform/ecr) | The maintenance process and approvals |
| MP Media Protection | Media handling and destruction | Encryption at rest across the storage modules | Media-handling policy for any exported records |
| PE Physical and Environmental Protection | Fully inherited | None | None |
| PL Planning | None | The hardening design and this matrix as planning inputs | The System Security Plan and rules of behavior |
| PM Program Management | None | None | The security program |
| PS Personnel Security | Personnel controls for AWS staff | None | Personnel screening for the deploying staff |
| PT PII Processing and Transparency | None | The platform does not collect or process personally identifiable information of its own, and no active filter removes PII that a user provides through code or ticket content. Evidence objects are content-addressed and access-scoped | Data governance for any PII introduced through code or ticket content |
| RA Risk Assessment | Infrastructure risk assessment | Vulnerability scanning through scan-on-push in [ecr](../infra/terraform/ecr), the security-review gate, and the `inv sast` CI gate with its [accepted-findings register](../security/sast-accepted.yaml), and FIPS endpoint selection from the AWS security baseline feature | The organizational risk assessment |
| SA System and Services Acquisition | The AWS software development lifecycle for its services | Pinned images and the managed registry in [ecr](../infra/terraform/ecr) | Acquisition and supplier policy |
| SC System and Communications Protection | Network fabric and FIPS-validated cryptographic modules | Network isolation through [vpc-endpoints](../infra/terraform/vpc-endpoints) and the tiered subnets and data-tier network ACL of the [private-bedrock](../infra/terraform/examples/private-bedrock) root, a single egress path in [egress](../infra/terraform/egress), deny-by-default DNS in [dns-firewall](../infra/terraform/dns-firewall), TLS in transit enforced by the database, cache, file system, and bucket policies, encryption at rest across the storage and data modules, and FIPS endpoint selection from the AWS security baseline feature | Boundary policy approval |
| SI System and Information Integrity | Flaw remediation of the AWS infrastructure | Signed review verdicts in [review-verdict-signing.md](review-verdict-signing.md), authorship signatures, webhook input validation in [pr_events.py](../services/orchestration/webhook/pr_events.py), and write-once-read-many integrity in [evidence-store](../infra/terraform/evidence-store) | The monitoring and flaw-remediation process |
| SR Supply Chain Risk Management | AWS supply chain for its services | Immutable tags, scan-on-push, and pinned bases in [ecr](../infra/terraform/ecr) | Vendor and supplier risk management |

## Platform-owned backlog

The following platform-owned controls are not yet implemented and are tracked as v0.3.0 features in the [roadmap](../ROADMAP.md).

- SC-28, SC-7, SC-7(5), and CP-9 for the network tiers, egress, and data tier, delivered by the data and cache tier terraform feature.
- SC-7 and AC-6 for the compute layer, provisioned by the container-platform services terraform feature.
- SC-7 and SC-8 for the control plane, provisioned by the Temporal control plane terraform feature.
- AU and CA-7 enablement for observability, provisioned by the observability terraform feature.
- SC-13 FIPS endpoint selection and AU multi-region audit, delivered by the AWS security baseline hardening feature.
- SI-12 retention by classification, delivered by the evidence retention classification mapping feature.

## Deploying-organization responsibilities

The organization that operates the platform owns the operational and program control families that an authorization requires and that a codebase cannot hold. These are the AT, CA program, IR, MA process, PL, PM, PS, and PT governance responsibilities above. The platform provides the audit trail, the evidence and audit object store, and the observability stack as the enablement those responsibilities consume.
