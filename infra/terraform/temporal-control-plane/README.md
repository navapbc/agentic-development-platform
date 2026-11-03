# temporal-control-plane

Module that provisions the OSS Temporal control plane on the platform ECS cluster. Nothing is created unless `create_temporal_control_plane = true`.

## What it provisions

- ECR repositories `<name_prefix>/temporal-server` and `<name_prefix>/temporal-admin-tools` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a 14-day expiry for untagged images. `kms_key_arn` applies one key to both, and empty creates one key per repository.
- A KMS key, `alias/<name_prefix>-temporal`, and the TLS secrets `<name_prefix>/temporal/tls-ca`, `<name_prefix>/temporal/tls-internode`, and, unless the frontend comes from ACM, `<name_prefix>/temporal/tls-frontend`. Each secret policy denies `GetSecretValue` to every principal outside `tls_secret_readers`.
- With an ACM certificate to track, an EventBridge rule on its `ACM Certificate Approaching Expiration` and `ACM Certificate Expired` events and a KMS-encrypted SNS topic, `certificate_expiry_topic_arn`, for the operators to subscribe to.

## TLS certificates

The frontend presents a certificate to the workers and the UI, and the Temporal services authenticate each other over mutual TLS with the internode certificate. Each certificate comes from one of three sources.

| Source | Frontend | Internode |
|---|---|---|
| Generated | `inv temporal-certs generate --out DIR` issues a self-signed CA and both pairs | Same |
| Your PKI | Your PEM files in a directory, under the file names below | Same |
| ACM | Set `frontend_acm_certificate_arn`, and the NLB presents that certificate, issued by ACM or imported into it. No frontend secret is created | Import it with `inv temporal-certs put --import-to-acm`, or import it yourself, and set `internode_acm_certificate_arn` to track its expiry. The server still reads it from Secrets Manager |

ACM never returns the private key of an imported certificate, and the internode certificate must reach the server process, so ACM tracks the internode certificate and Secrets Manager serves it.

`inv temporal-certs put --dir DIR` writes a directory to the secrets. The directory holds `ca.pem`, `internode.pem`, `internode-key.pem`, and, unless the frontend comes from ACM, `frontend.pem` and `frontend-key.pem`. `ca.pem` may hold the whole chain. Before it writes anything, `put` checks that each certificate verifies against `ca.pem`, that each key belongs to its certificate, that the internode certificate carries both the server and client authentication usages, and that no certificate has expired. It refuses frontend files when the frontend comes from ACM, and it refuses an internode certificate that differs from the one `internode_acm_certificate_arn` tracks. `--import-to-acm` re-imports into that ARN, or imports a new certificate and prints the ARN to set.

`generate` refuses a directory inside the repository and refuses to replace existing files without `--force`. It writes P-256 keys and certificates with mode 0600. The frontend certificate names `frontend.temporal.internal`, and the internode certificate `internode.temporal.internal`, unless `--frontend-name` and `--internode-name` set others. The CA key stays in the directory so the operator can issue again. Keep it offline.

Certificate material never passes through Terraform, so a plan or apply needs no key and the Terraform state holds none. A rotation is a `put` followed by a redeploy of the server.

## Images

`docker/temporal.Dockerfile` builds the server image and `docker/temporal-admin-tools.Dockerfile` the admin-tools image. Each starts from the upstream digest the local stack pins, and each adds the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches the bundle from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`. Set both build arguments to use a regional or GovCloud bundle. RDS publishes a new bundle when it adds a CA, so a bundle bump updates the checksum in both Dockerfiles. The admin-tools image also carries `docker/temporal-admin-tools.sh` as its entrypoint, with `schema` as the default command and `namespace` as the other action.

`inv publish-temporal-images` builds both images for `linux/arm64`, or `--arch amd64`, and pushes them. Each tag is `<upstream version>-<content hash>-<arch>`, where the hash covers the Dockerfile and every file it copies. An image whose tag is already in its repository is not rebuilt. The task prints each image reference with its digest.

The admin-tools task connects to Aurora with `SQL_TLS=true` and `SQL_TLS_CA_FILE=/etc/ssl/rds-global-bundle.pem`. `temporal-sql-tool` verifies the certificate against that bundle and the host name against `SQL_HOST`, so a connection to the cluster endpoint fails on any other CA.

## Tests

`tests/temporal_control_plane.tftest.hcl` plans the module against a mocked provider. Run it with `inv terraform-test`, which CI also runs.
