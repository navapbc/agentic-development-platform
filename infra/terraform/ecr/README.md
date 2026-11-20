# ecr

Reusable core module that provisions a single ECR repository with scan-on-push, immutable tags, a lifecycle policy that keeps the newest images and expires untagged images, and KMS encryption under a customer-managed key. It exists so a container registry is managed as shared infrastructure and can be reused or skipped when one already exists.

Nothing is created unless `create_repository = true`.

## Usage

```hcl
module "jumphost_ecr" {
  source = "../../ecr"

  create_repository = true
  repository_name   = "agentic-platform/jumphost-base"

  tags = { project = "agentic-dev-platform" }
}
```

To reuse an existing registry instead, set `create_repository = false` and pass the existing repository URL and ARN directly to the consuming module.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key inputs are `repository_name`, `kms_key_arn`, `retained_image_count` (default 5), `retained_signature_index_count` (default 100), and `untagged_expiry_days` (default 14). An empty `kms_key_arn` creates a module-owned key with rotation enabled. Scan-on-push and immutable tags are fixed and have no input. Changing the encryption key of an existing repository replaces the repository, so push the image again after that apply.

## Retention and signatures

The lifecycle policy has five rules. A lower `rulePriority` takes precedence.

1. Tagged images whose tag matches `sha256-*` beyond the newest `retained_signature_index_count` expire. Notation pushes each signature as an untagged manifest whose `subject` is the signed image, and it also pushes an OCI index under the referrers tag `sha256-<hex digest of the subject>` that lists the signature.
2. Tagged images whose tag matches `*-arm64` beyond the newest `retained_image_count` expire.
3. Tagged images whose tag matches `*-amd64` beyond the newest `retained_image_count` expire.
4. Tagged images whose tag matches `*` beyond the newest `retained_image_count` expire. This rule covers tags that end in neither architecture, such as the upstream tags of mirrored images and the jumphost tag.
5. Untagged images older than `untagged_expiry_days` expire.

### How ECR applies the rules

The ECR lifecycle policy evaluation rules state that every rule identifies its images at the same time, that each rule then marks images in priority order, that an image is expired by at most one rule, and that an image matching the tag requirements of a rule cannot be expired by a rule with a lower priority. A rule with a lower priority still identifies and counts the images that a higher priority rule selected. The documented example with a final `tagStatus any` rule shows this, because that rule expires image D, which is newer than images that higher priority rules selected and that it therefore cannot mark.

The outcome for this policy follows.

- A tag of the form `sha256-<hex>` never ends in `-arm64` or `-amd64`, and no tag ends in both suffixes. Rules 2 and 3 therefore count only the images of their own architecture, and each architecture keeps its newest `retained_image_count` images however often the other architecture is pushed.
- Rule 4 never expires an image that rule 1, 2, or 3 selected. It does count those images, so an image with any other tag stays only while it is among the newest `retained_image_count` tagged images of the repository, counting architecture images and referrers indexes. The platform repositories hold only architecture tags, so rule 4 expires nothing there. In a repository of signed mirrored images each push adds the image and its referrers index, so with the default count of 5 the repository keeps the newest 2 or 3 mirrored images.
- Rule 5 selects every untagged manifest, including signatures, per-architecture manifests of an image index, and attestation manifests. ECR does not expire an image that a retained index references, so a retained image index protects its children, and a retained referrers index protects the signatures it lists.

### Signatures of retained images

The referrers tag carries no architecture, so rule 1 counts the indexes of both architectures together, and the index of an expired image keeps its place until newer indexes displace it. The index of a retained image expires only after `retained_signature_index_count` signed images have been pushed to the repository after that image. A retained image of an architecture that is rarely published is the first to reach that point. Its signature then becomes an unreferenced untagged manifest, rule 5 expires it after `untagged_expiry_days`, and `notation verify` refuses the image. The default of 100 indexes defers that point by 100 signed pushes, and the validation requires at least twice `retained_image_count`. Raise `retained_signature_index_count` for a repository that one architecture publishes far more often than the other.

When rule 2, 3, or 4 expires an image, ECR removes the referrer artifacts whose subject is that image within 24 hours, and rule 1 later expires the image's referrers index.

Increase `retained_image_count` if a deployed image must survive more pushes of its architecture than the default allows, because a task or pod that pulls an expired digest fails to start.
