# github-image-publisher

Module that lets the `publish-images` GitHub Actions workflow push the platform images to ECR without a long-lived AWS key. Nothing is created unless `create_github_image_publisher = true`.

## What it provisions

- The `<name_prefix>-github-image-publisher` role, which the workflow assumes through the GitHub OIDC provider with `sts:AssumeRoleWithWebIdentity`. Its trust policy requires the `sts.amazonaws.com` audience and a `sub` claim of `repo:<github_repository>:ref:<ref>` for one of `allowed_refs`, so a workflow in another repository, or one running on another branch, tag, or pull request, cannot assume it. The default refs are `refs/heads/main` and every `v` tag. A repository whose tokens carry GitHub's immutable subject, `repo:<owner>@<owner_id>/<name>@<repository_id>:ref:<ref>`, sets `github_repository_ids`, so the trust also refuses a repository re-created under the same name. `gh api repos/<owner>/<name>/actions/oidc/customization/sub` reports which form a repository uses.
- An inline policy that grants `ecr:GetAuthorizationToken`, which has no resource scope, and the push and read actions on `repository_arns` only.
- The GitHub OIDC provider for `token.actions.githubusercontent.com`, when `oidc_provider_arn` is empty. An account holds one provider per URL, so pass the ARN of the existing provider when another stack created it.

A session lasts at most one hour, which covers a full publish.

## Using the role

Set the `role_arn` output as the `AWS_PUBLISH_ROLE_ARN` repository variable and the region as `AWS_PUBLISH_REGION`, for example `gh variable set AWS_PUBLISH_ROLE_ARN --body <role_arn>`. A variable is not a secret, because the role trusts only this repository's workflows. See the `publish-images` workflow in `.github/workflows`.

## Outputs

| Output | Value |
|---|---|
| `role_arn` | The role the workflow assumes. Null when disabled |
| `oidc_provider_arn` | The provider the role trusts. Null when disabled |
