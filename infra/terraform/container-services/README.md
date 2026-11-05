# container-services

Module that runs the platform containers on the platform ECS cluster. Nothing is created unless `create_container_services = true`.

## What it provisions

- ECR repositories `<name_prefix>/platform`, `<name_prefix>/repo-intelligence`, `<name_prefix>/otel-collector`, `<name_prefix>/langfuse-web`, `<name_prefix>/langfuse-worker`, and `<name_prefix>/clickhouse` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a lifecycle policy that expires untagged images. `kms_key_arn` sets one key for all of them, and empty creates one key per repository.

## Images

The ECS tasks pull only from these repositories, because DNS Firewall resolves no public registry. `inv publish-platform-images` builds or copies every image for `linux/arm64`, or `--arch amd64`, and prints each reference with its digest.

| Repository | Source | Tag |
|---|---|---|
| `platform` | `docker/Dockerfile`, built with `AGENT_BUILD_ID` | `<build ID>-<source hash>-<arch>` |
| `repo-intelligence` | `docker/repo-intelligence.Dockerfile` | `<release version>-<content hash>-<arch>` |
| `otel-collector` | `docker/otel-collector.Dockerfile` | `<collector version>-<content hash>-<arch>` |
| `langfuse-web`, `langfuse-worker`, `clickhouse` | The image `compose.yaml` pins for that service, copied by digest | The upstream version |

The platform image runs every worker role and the coordination service. Its build ID is the Temporal Worker Deployment version the workers register, the release version followed by a UTC timestamp, which `scripts/build_id.py` mints so that a newer build orders after an older one. The source hash covers the files `scripts/build_id.py` digests and the acli build arguments, so a publish of sources whose image is already in the repository reuses that image and its build ID, and a change mints a new build ID. `--install-acli`, with `--acli-version` and `--acli-sha256`, installs acli as `INSTALL_ACLI` does locally.

The platform image carries the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches it from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`, the same checksum the Temporal images pin.

The built images come from a copy of the files git tracks or would track, so ignored local artifacts, such as the binaries `inv fetch-acli` leaves under `vendor/bin`, never reach an image. A copied image keeps the multi-architecture digest `compose.yaml` pins, and the publish fails when the repository holds a different digest under that tag. The repository lifecycle policy does not expire the per-architecture images an index references.

Basic scanning scans the per-architecture images of a copied index, not the index itself. It cannot scan the collector image, whose distroless base has no package database, and reports `UnsupportedImageError`. Amazon Inspector enhanced scanning, an account-level setting, scans the Go binaries in it.

## Outputs

| Output | Value |
|---|---|
| `repository_urls` | ECR repository of each image, keyed by the name `inv publish-platform-images` uses. Empty when disabled |

## Tests

`tests/container_services.tftest.hcl` runs the module against a mocked provider and asserts that the disabled module creates nothing and that the enabled module creates a repository for every image. Run it with `inv terraform-test`, which CI also runs.
