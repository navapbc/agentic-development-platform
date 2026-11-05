# container-services

Module that runs the platform containers on the platform ECS cluster. Nothing is created unless `create_container_services = true`.

## What it provisions

- ECR repositories `<name_prefix>/platform`, `<name_prefix>/repo-intelligence`, `<name_prefix>/otel-collector`, `<name_prefix>/langfuse-web`, `<name_prefix>/langfuse-worker`, and `<name_prefix>/clickhouse` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a lifecycle policy that expires untagged images. `kms_key_arn` sets one key for all of them, and empty creates one key per repository. The `tls-sidecar` repository holds the TLS image described below.
- With `internal_acm_certificate_arn` set, the internal load balancer described below, its private hosted zone, and the `<name_prefix>-internal-lb-logs-<account>` bucket, which receives its access logs under `internal/`. Load balancer log delivery requires S3-managed encryption, so the bucket uses SSE-S3, denies requests without TLS, expires logs after `load_balancer_log_retention_days`, and sends its own server access logs to `access_log_bucket`.

## Images

The ECS tasks pull only from these repositories, because DNS Firewall resolves no public registry. `inv publish-platform-images` builds or copies every image for `linux/arm64`, or `--arch amd64`, and prints each reference with its digest.

| Repository | Source | Tag |
|---|---|---|
| `platform` | `docker/Dockerfile`, built with `AGENT_BUILD_ID` | `<build ID>-<source hash>-<arch>` |
| `repo-intelligence` | `docker/repo-intelligence.Dockerfile` | `<release version>-<content hash>-<arch>` |
| `otel-collector` | `docker/otel-collector.Dockerfile` | `<collector version>-<content hash>-<arch>` |
| `tls-sidecar` | `docker/tls-sidecar.Dockerfile` | `<release version>-<content hash>-<arch>` |
| `langfuse-web`, `langfuse-worker`, `clickhouse` | The image `compose.yaml` pins for that service, copied by digest | The upstream version |

The platform image runs every worker role and the coordination service. Its build ID is the Temporal Worker Deployment version the workers register, the release version followed by a UTC timestamp, which `scripts/build_id.py` mints so that a newer build orders after an older one. The source hash covers the files `scripts/build_id.py` digests and the acli build arguments, so a publish of sources whose image is already in the repository reuses that image and its build ID, and a change mints a new build ID. `--install-acli`, with `--acli-version` and `--acli-sha256`, installs acli as `INSTALL_ACLI` does locally.

The platform image carries the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches it from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`, the same checksum the Temporal images pin.

The built images come from a copy of the files git tracks or would track, so ignored local artifacts, such as the binaries `inv fetch-acli` leaves under `vendor/bin`, never reach an image. A copied image keeps the multi-architecture digest `compose.yaml` pins, and the publish fails when the repository holds a different digest under that tag. The repository lifecycle policy does not expire the per-architecture images an index references.

Basic scanning scans the per-architecture images of a copied index, not the index itself. It cannot scan the collector image, whose distroless base has no package database, and reports `UnsupportedImageError`. Amazon Inspector enhanced scanning, an account-level setting, scans the Go binaries in it.

## Internal load balancer

The platform services that other workloads call sit behind one internal ALB in the application tier, which needs `application_subnet_ids` in two Availability Zones. Its HTTPS listener presents `internal_acm_certificate_arn` under `ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04`, which negotiates TLS 1.2 and 1.3 only, and routes each name in the private hosted zone `internal_domain` to its service. A name it does not know answers 404.

| Name | Target port | Target protocol |
|---|---|---|
| `coordination.<internal_domain>` | 8130 | HTTPS |
| `repo-intelligence.<internal_domain>` | 8120 | HTTPS |
| `otel-collector.<internal_domain>` | 4317 | HTTPS with the gRPC protocol version |
| `langfuse.<internal_domain>` | 3443 | HTTPS |

The load balancer re-encrypts to every task, and each task serves TLS with a certificate it issues when it starts, so no task key leaves the task. The load balancer does not verify that certificate.

- `coordination-service` and `repo-intelligence` serve HTTPS when `SERVER_TLS=true`, with a P-256 certificate they write under `SERVER_TLS_DIR`. `python -m services.healthcheck http PORT --tls` probes them and trusts only that certificate.
- Langfuse web serves plain HTTP, so the `tls-sidecar` image fronts it with `tls-sidecar proxy -listen :3443 -upstream http://127.0.0.1:3000`, which forwards the original `Host` and sets `X-Forwarded-Proto: https`.
- The collector reads its certificate from files, so an init container runs `tls-sidecar issue -out DIR -owner 10001:10001` as root into a task volume, and the collector's OTLP receivers serve TLS with `cert.pem` and `key.pem` from it.

The ALB admits port 443 from the `client_security_group_id` group, which callers attach, and from `internal_allowed_cidr_blocks`. Each service's target group admits only its port from the ALB, and its tasks attach the matching `target_security_group_ids` group.

Clients verify the ALB certificate against the CA that issued it. The platform image's entrypoint reads that CA from `PLATFORM_CA_PEM`, writes it with the system roots to one bundle, and points `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `OTEL_EXPORTER_OTLP_CERTIFICATE` at it, so httpx, requests, and the gRPC OTLP exporter verify both the internal names and public hosts such as GitHub. A CA certificate is public, so it travels as a plain environment value.

The private zone names fall under `*.internal`, which DNS Firewall already resolves. The `private-bedrock` root adds any other `internal_domain` to the allowlist.

### Certificate

ACM cannot issue a public certificate for a private name, so the ALB certificate is imported. Issue it from the generated CA with `inv temporal-certs generate --out DIR --internal-name coordination.platform.internal,repo-intelligence.platform.internal,otel-collector.platform.internal,langfuse.platform.internal`, or have the organization's PKI issue a server certificate with those names, and import it with `aws acm import-certificate --certificate fileb://DIR/internal.pem --private-key fileb://DIR/internal-key.pem --certificate-chain fileb://DIR/ca.pem`. The CA PEM becomes `PLATFORM_CA_PEM` for the clients.

## Outputs

| Output | Value |
|---|---|
| `repository_urls` | ECR repository of each image, keyed by the name `inv publish-platform-images` uses. Empty when disabled |
| `internal_endpoints` | The client security group, each service's target security group and target group, each service's HTTPS URL, and the domain. Null without the internal load balancer |

## Tests

`tests/container_services.tftest.hcl` and `tests/internal_lb.tftest.hcl` run the module against a mocked provider. They assert that the disabled module creates nothing, that the enabled module creates a repository for every image, that the internal listener terminates TLS under the FIPS policy with access logs, that every target group re-encrypts and each host rule forwards to its own service, that each target admits only the load balancer, and the input check. Run it with `inv terraform-test`, which CI also runs.
