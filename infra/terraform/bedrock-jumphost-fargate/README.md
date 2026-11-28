# bedrock-jumphost-fargate

Optional, toggleable Terraform module that provisions an ephemeral ECS Fargate jumphost for reaching the private `bedrock-runtime` interface endpoint from a laptop during local testing. It is scoped to port-forwarding to the Bedrock endpoint and, when the `temporal-control-plane` module admits its task group, to the Temporal UI load balancer. Nothing is created unless `create_jumphost = true`.

There is no standing host and no SSH. A per-session task runs a minimal image that sleeps for a TTL, and access is ECS Exec over an SSM data channel. The connect script starts a task, waits for it, and opens a port-forward to the Bedrock endpoint.

This module provisions the application resources only. The shared VPC endpoints come from the `vpc-endpoints` module and the container registry comes from the `ecr` module, both passed in as inputs.

## When you do not need this

In a deployed environment the platform runs in-VPC and reaches the endpoint with `BEDROCK_CONNECTIVITY_PROVIDER=direct` and no hop. Use this module only for a laptop path in a test or development account. For a prod-grade laptop path without any host, AWS Client VPN puts the caller inside the VPC and also uses `direct`, at a higher standing cost.

## Usage

```hcl
module "bedrock_jumphost" {
  source = "../../bedrock-jumphost-fargate"

  create_jumphost             = true
  region                      = "us-east-1"
  vpc_id                      = var.vpc_id
  subnet_ids                  = var.private_subnet_ids
  endpoints_security_group_id = module.vpc_endpoints.security_group_id
  image_uri                   = "${module.jumphost_ecr.repository_url}:v1"
  ecr_repository_arn          = module.jumphost_ecr.repository_arn
  access_log_bucket           = var.access_log_bucket

  tags = { project = "agentic-dev-platform" }
}
```

## Image delivery

The task runs the image referenced by `image_uri`, pulled from the `ecr` module's repository. Push it before connecting.

1. `terraform apply` creates the ECR repository (in the `ecr` module) and the jumphost resources.
2. Publish the platform images with `inv publish-platform-images`, which pushes the buildah builder image that signs the jumphost image and writes its reference to `images.auto.tfvars.json`.
3. Build, scan, push, and sign the image with `inv publish-jumphost-image --tag <jumphost_image_tag>`. `--tag` is required and names a tag the repository does not hold yet. Pass `--builder` to sign in another builder image, or `--unsigned` in a deployment that has no platform signing profile.
4. Connect with `inv tunnel-start` using the `jumphost` connectivity provider.

`inv publish-jumphost-image` runs `scripts.publish_jumphost_image`, which publishes through the same `Publisher` and `Signer` as `inv publish-platform-images`. It builds the image from the repository root on the hardened base, scans it with the Amazon Inspector SBOM generator and the Inspector Scan API and against the CIS benchmark, and pushes nothing when a finding at `HIGH` or above is not accepted by the platform image exceptions or a CIS rule fails. It then signs the pushed digest with the platform AWS Signer profile, the `image_signing_profile_arn` output of the composition root or `IMAGE_SIGNING_PROFILE_ARN`. Notation runs inside the builder image on the architecture that ends the builder's tag, `-arm64` or `-amd64`, independent of `--arch`, and a builder whose tag names no architecture is refused before the build. Only a digest the run built, scanned, and pushed is signed. The repository holds immutable tags, so a tag it already holds is not rebuilt. The run accepts such a tag when it already carries a notation signature, as after a completed publish, and otherwise refuses it, because nothing shows the image was scanned. An image pushed by the earlier unscanned publish path, such as `v1`, is refused this way, so publish under a new tag and set `jumphost_image_tag` to it. Bump `--tag` and `jumphost_image_tag` together to ship a new image.

To pin a scanned image for production, set `image_uri` to a `repo@sha256` digest.

## Hardened posture

- Image provenance. `docker/jumphost-base/Dockerfile` defaults `AL2023_IMAGE` to `public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:75a2d4226d9b98c914d7eed878a03bef4f365ad548be5d28d464ccba504cec79` and `AL2023_RELEASE` to `2023.12.20261120`. The publish replaces `AL2023_IMAGE` with the hardened base that `docker/al2023/base.Dockerfile` builds from the same digest, which `docker/al2023/versions.env` pins for every platform image.
- CIS hardening. The hardened base applies the CIS Amazon Linux 2023 Level 2 Server remediation, and the jumphost Dockerfile runs `cis-harden` again as its last root step. The publish scans the base and the image with OpenSCAP and writes the ARF results, the HTML report, the per-rule results, and the blocking rules under `--cis-evidence`, `cis-evidence/al2023-base/` and `cis-evidence/jumphost-base/` by default, the directory the `publish-images` workflow uploads for the platform images. `docker/al2023/cis-accepted.txt` lists the accepted rules with their reasons.
- Runtime user. The image ends with `USER 65534:65534`, so the entrypoint runs with UID and GID 65534 and no root privileges.
- Registry. The `ecr` module creates the repository with `scan_on_push = true`, `image_tag_mutability = "IMMUTABLE"`, and KMS encryption under a customer-managed key, a key the module owns or the `kms_key_arn` it receives. These settings are fixed and not inputs.
- Signature. The pushed digest carries a notation signature from the platform AWS Signer profile.

## Role statements

Each statement is an inline policy on its role. A statement with a `*` resource holds only actions that the AWS Service Authorization Reference lists with no resource type, so IAM cannot scope them to a resource. The ECS Exec actions follow the task role requirements in the [Amazon ECS Exec guide](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html).

| Role | Sid | Actions | Resource | Purpose |
|------|-----|---------|----------|---------|
| Execution | `EcrAuth` | `ecr:GetAuthorizationToken` | `*` | Fetch the registry token for the image pull. [Amazon ECR actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecr.html) list `GetAuthorizationToken` with no resource type. |
| Execution | `EcrPull` | `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`, `ecr:BatchCheckLayerAvailability` | `ecr_repository_arn` | Pull the image from its repository. |
| Execution | `Logs` | `logs:CreateLogStream`, `logs:PutLogEvents` | `<log group>:log-stream:ecs/*` | Write the container output through the `awslogs` driver, whose streams start with the `ecs` prefix. The ECS Exec session streams are out of reach. |
| Execution | `Secrets` | `secretsmanager:GetSecretValue` | Each `secrets[*].secret_arn` | Inject the configured secrets. Present only when `secrets` is not empty. |
| Task | `EcsExecChannel` | `ssmmessages:CreateControlChannel`, `ssmmessages:CreateDataChannel`, `ssmmessages:OpenControlChannel` | `*` | Register and open the ECS Exec channels that carry the port forward. [Amazon Message Gateway Service actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssmmessages.html) list these three with no resource type. |
| Task | `EcsExecDataChannel` | `ssmmessages:OpenDataChannel` | `arn:<partition>:ssm:<region>:<account>:session/*` | Open the data channel of a Session Manager session in this account and region, the `session` resource type the reference lists for the action. |
| Task | `DecryptExecChannel` | `kms:Decrypt` | The exec KMS key | Decrypt the KMS-encrypted ECS Exec channel. |
| Task | `ExecLoggingCloudWatch` | `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogStreams` | The exec log group and its streams | Write the ECS Exec session log to CloudWatch. |
| Task | `ExecLoggingDescribeGroups` | `logs:DescribeLogGroups` | `*` | Let the session agent find the exec log group. [Amazon CloudWatch Logs actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_logs.html) list `DescribeLogGroups` with no resource type. |
| Task | `ExecLoggingS3Put` | `s3:PutObject` | `<exec log bucket>/exec-output/*` | Upload the ECS Exec session log under the cluster's `exec-output` key prefix. |
| Task | `ExecLoggingS3Bucket` | `s3:GetBucketLocation`, `s3:GetEncryptionConfiguration` | The exec log bucket | Resolve the bucket region and confirm its encryption before the upload, which the cluster requires. |
| Task | `ExecLoggingS3Encrypt` | `kms:GenerateDataKey`, `kms:Decrypt` | `logs_bucket_kms_key_arn` | Encrypt the session log upload under the bucket's customer-managed key. Present only when `logs_bucket_kms_key_arn` is set. The session agent uploads with the bucket's SSE-KMS key through the S3 transfer manager, which switches to a multipart upload above 5 MiB, and the [Amazon S3 SSE-KMS permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html) require `kms:GenerateDataKey` for an upload and `kms:Decrypt` as well for a multipart upload. The key policy must also admit the task role. |

`tests/bedrock_jumphost_fargate.tftest.hcl` asserts that only `EcrAuth`, `EcsExecChannel`, and `ExecLoggingDescribeGroups` take every resource, that every other statement names only the resources above, and that `ExecLoggingS3Encrypt` exists only with `logs_bucket_kms_key_arn` set.

## Security summary

- Task security group has no ingress. Egress is 443 to the shared endpoint security group, to S3 through the gateway endpoint prefix list when `attach_s3_egress` is true, to `additional_egress_cidrs`, and to the destinations other modules admit the group to, such as the Temporal UI load balancer.
- Task role holds only the ECS Exec channel actions, `kms:Decrypt` on the exec key, and the session log write. The execution role holds ECR pull scoped to `ecr_repository_arn`, the container log write, and secret reads scoped to injected ARNs. See [Role statements](#role-statements).
- The ECS Exec data channel is KMS-encrypted and every session is logged to CloudWatch and to a hardened S3 bucket with versioning, encryption, a public-access block, a TLS-only policy, and read access restricted to an allowlist.
- The CloudWatch log group is encrypted with the same customer-managed key, whose policy admits the CloudWatch Logs service for this log group only. The cluster has Container Insights enabled.
- The exec-log bucket sends S3 server access logs to the required `access_log_bucket`.
- Private reachability for the task depends on the `vpc-endpoints` module providing `ssmmessages`, `ecr.api`, `ecr.dkr`, `logs`, `kms`, and the S3 gateway. The `logs` and `kms` endpoints are required because the task ships container logs to CloudWatch and the ECS Exec channel is KMS-encrypted.
- The `secrets` input injects Secrets Manager values by ARN when a future jumphost needs a credential. A Bedrock port-forward needs none, so the default is empty.

## FedRAMP portability

Set `permissions_boundary_arn` and `iam_path` for a delegated-admin account, set `logs_bucket_kms_key_arn` to a customer-managed key, and point the image build at the approved base registry. None of these change the module shape.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key inputs are `create_jumphost` (master off switch), `endpoints_security_group_id`, `attach_endpoints_egress` (default true), `image_uri`, and `ecr_repository_arn`.
