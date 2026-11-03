# temporal-control-plane

Module that provisions the OSS Temporal control plane on the platform ECS cluster. Nothing is created unless `create_temporal_control_plane = true`.

## What it provisions

- ECR repositories `<name_prefix>/temporal-server` and `<name_prefix>/temporal-admin-tools` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a 14-day expiry for untagged images. `kms_key_arn` applies one key to both, and empty creates one key per repository.

## Images

`docker/temporal.Dockerfile` builds the server image and `docker/temporal-admin-tools.Dockerfile` the admin-tools image. Each starts from the upstream digest the local stack pins, and each adds the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches the bundle from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`. Set both build arguments to use a regional or GovCloud bundle. RDS publishes a new bundle when it adds a CA, so a bundle bump updates the checksum in both Dockerfiles. The admin-tools image also carries `docker/temporal-admin-tools.sh` as its entrypoint, with `schema` as the default command and `namespace` as the other action.

`inv publish-temporal-images` builds both images for `linux/arm64`, or `--arch amd64`, and pushes them. Each tag is `<upstream version>-<content hash>-<arch>`, where the hash covers the Dockerfile and every file it copies. An image whose tag is already in its repository is not rebuilt. The task prints each image reference with its digest.

The admin-tools task connects to Aurora with `SQL_TLS=true` and `SQL_TLS_CA_FILE=/etc/ssl/rds-global-bundle.pem`. `temporal-sql-tool` verifies the certificate against that bundle and the host name against `SQL_HOST`, so a connection to the cluster endpoint fails on any other CA.

## Tests

`tests/temporal_control_plane.tftest.hcl` plans the module against a mocked provider. Run it with `inv terraform-test`, which CI also runs.
