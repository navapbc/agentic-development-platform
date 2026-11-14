# github-image-publisher

Module that lets the `publish-images` GitHub Actions workflow push the platform images to ECR without a long-lived AWS key. Nothing is created unless `create_github_image_publisher = true`.

## What it provisions

- The `<name_prefix>-github-image-publisher` role, which the workflow assumes through the GitHub OIDC provider with `sts:AssumeRoleWithWebIdentity`. Its trust policy requires the `sts.amazonaws.com` audience and a `sub` claim of `repo:<github_repository>:ref:<ref>` for one of `allowed_refs`, so a workflow in another repository, or one running on another branch, tag, or pull request, cannot assume it. The default refs are `refs/heads/main` and every `v` tag. A repository whose tokens carry GitHub's immutable subject, `repo:<owner>@<owner_id>/<name>@<repository_id>:ref:<ref>`, sets `github_repository_ids`, so the trust also refuses a repository re-created under the same name. `gh api repos/<owner>/<name>/actions/oidc/customization/sub` reports which form a repository uses.
- An inline policy that grants `ecr:GetAuthorizationToken`, which has no resource scope, and the push and read actions on `repository_arns` only.
- When `config_store` names the tenant config bucket and its KMS key, statements on the publish role that read, prune, and rewrite `platform/image_exceptions.yaml`, read `platform/image_policy.yaml` and every `tenants/<tenant>/image_exceptions.yaml`, and list `tenants/`, so the daily exception review can remove expired and resolved platform entries and report on the tenant entries. The KMS grant covers `kms:Decrypt` and `kms:GenerateDataKey` through S3 only.
- When `config_store` is set, the `<name_prefix>-github-image-exception` role for the `image-exception` workflow. Its trust policy requires the `sts.amazonaws.com` audience and a `sub` claim of `repo:<github_repository>:environment:<exception_environment>`, so only a job that runs in that GitHub environment can assume it, after the environment's required reviewers approve the run. It may pull from `repository_arns` and `pull_repository_arns` and scan the SBOM to confirm the finding, read `platform/image_policy.yaml`, and read and write `platform/image_exceptions.yaml`. It holds no other access to the bucket.
- The GitHub OIDC provider for `token.actions.githubusercontent.com`, when `oidc_provider_arn` is empty. An account holds one provider per URL, so pass the ARN of the existing provider when another stack created it.

A session lasts at most one hour, which covers a full publish.

## Using the role

Set the `role_arn` output as the `AWS_PUBLISH_ROLE_ARN` repository variable and the region as `AWS_PUBLISH_REGION`, for example `gh variable set AWS_PUBLISH_ROLE_ARN --body <role_arn>`. A variable is not a secret, because the role trusts only this repository's workflows. See the `publish-images` workflow in `.github/workflows`.

For finding exceptions, also set `AWS_CONFIG_BUCKET` to the tenant config bucket name and `AWS_IMAGE_EXCEPTION_ROLE_ARN` to the `exception_role_arn` output. Create a GitHub environment named `image-exception` under Settings, Environments, add the required reviewers who approve platform exceptions, and enable Prevent self-review so the requester cannot approve their own run. The workflow records the approving reviewer from the run's approvals, and the role trusts no job outside that environment.

## Outputs

| Output | Value |
|---|---|
| `role_arn` | The role the workflow assumes. Null when disabled |
| `oidc_provider_arn` | The provider the role trusts. Null when disabled |
| `exception_role_arn` | The role the `image-exception` workflow assumes. Null without `config_store` |
