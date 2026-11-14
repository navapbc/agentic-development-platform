# Image governance

Every image a tenant's pod runs passes five checks, each enforced by a different component, so a single misconfiguration does not admit an image. This document describes the checks, the configuration they read, and how to operate them. It covers the gate, checks, and scanner pods, which buildah runs inside a builder pod, and the sandbox pods, which the kubelet pulls.

## The five layers

| Layer | Enforced by | What it refuses |
| --- | --- | --- |
| Bundle validation | The tenant's activity worker, before it activates a config bundle | A config bundle that names an image without an `@sha256:` digest or from a repository `image_policy.yaml` does not admit |
| ECR pull scope | IAM, through the ECR token the worker hands each pod | A pull from any ECR repository outside the catalog, the tenant's own prefix, and the pull-through caches |
| Admission policy | The `agentic-dev-platform-tenant-images` ValidatingAdmissionPolicy on each tenant sandbox namespace | A pod whose container image lacks a digest or comes from a repository the image policy does not admit |
| Pull policy | buildah's `policy.json`, which the worker copies into each builder pod | A pull from any registry the image policy does not admit, including a `FROM` line of a Dockerfile the pod builds |
| Scan and signature | The worker, before it creates the pod, and notation inside the pod, before each pull | An image Amazon Inspector has not scanned, an image with a blocking finding, and, with signature verification on, a catalog or tenant image without a valid AWS Signer signature |

Before a builder pod builds an engine source or a repository's checks `build:` Dockerfile, the worker reads the Dockerfile and collects every image the build pulls, the `FROM` base image of each stage, each `COPY --from` image, and each `RUN --mount` `from=` image. Each one needs an `@sha256:` digest and passes the image policy and the scan gate with the pod's own images, so the build refuses a base image the pod could not run. A `FROM` that names an earlier stage or `scratch` pulls nothing. Only the global `ARG` defaults before the first `FROM` reach a `FROM` line, and the platform passes no build arguments, so a reference that needs any other value is refused. The image the build produces stays in the pod's local store, never reaches ECR, and is not scanned.

The admission policy matches each image as the pod spec writes it, because the kubelet pulls the written reference. A Docker Hub entry in the image policy also admits its short form, so `docker.io/library/python` admits `python@sha256:...`.

## The image policy

`config/<environment>/image_policy.yaml` belongs to the platform. A tenant's bundle never overrides it. In AWS the `private-bedrock` root generates it. Locally, copy `config/local/image_policy.yaml.example`.

| Key | Meaning |
| --- | --- |
| `catalog` | The platform's own repositories, which `signatures.platform_profile` signs |
| `tenant_prefix` | The tenant's ECR prefix, where `{tenant}` stands for the tenant's name. `signatures.tenant_profiles` names the profile that signs each tenant's images |
| `caches` | The pull-through cache prefixes, whose images carry no signature |
| `registries` | Any other repository or prefix the deployment admits, such as a workstation registry |
| `platform_registries` | The registry hosts whose repositories only the entries above admit |
| `scan.gate` | `inspector` refuses an image Inspector has not scanned or whose scan holds a blocking finding. `off` skips the scan |
| `scan.severity` | The lowest severity that blocks an image: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL` |
| `signatures.verification` | Verify the signature of each catalog and tenant image before the pod pulls it |

An entry ending in `/` admits every repository under it, and any other entry admits exactly that repository. Every reference must carry an `@sha256:` digest.

A tenant's `registries.yaml` may add mirrors. A mirror that points to a host outside `platform_registries`, such as the tenant's own Artifactory, is admitted. A mirror that points into a platform registry host is admitted only when an image policy entry admits its location, so a tenant cannot reach another tenant's prefix through a mirror.

## Finding exceptions

A finding exception accepts one Inspector finding in one image digest until its expiry date. Exceptions live in two files with the same layout locally and in AWS.

| File | Accepts findings in | Local path | AWS key in the tenant config bucket |
|---|---|---|---|
| Platform | The `catalog` images and the `platform_registries` repositories outside `tenant_prefix` | `config/<environment>/image_exceptions.yaml` | `platform/image_exceptions.yaml` |
| Tenant | The tenant's `tenant_prefix` and its `registries.yaml` mirrors | `config/<environment>/tenants/<tenant>/image_exceptions.yaml` | `tenants/<tenant>/image_exceptions.yaml` |

With `TENANT_CONFIG_BUCKET` unset the platform reads the local paths, and with it set the bucket keys. The pull gate applies the platform entries and the run tenant's entries, each limited to its own scope. The pre-push scan of both publish commands applies the platform entries, or the local file `--exceptions` names. A loader refuses an entry outside its file's scope and names it.

```yaml
config_version: 1
exceptions:
  - image: <registry host>/<name_prefix>/semgrep@sha256:<digest>
    finding: CVE-2025-12345
    reason: The vulnerable package is not reachable from the checks command.
    created: 2026-11-12
    expires: 2026-12-10
    requester: octocat
    approver: hubot
```

Every entry needs a non-empty `reason`, a `created` date, and an `expires` date no more than 30 days after `created`. A platform entry also needs `requester` and `approver`. The image may be the reference as configured or the pull-through cache reference it resolves to. An expired entry no longer accepts its finding, so the image is refused again until the finding is fixed or a new entry is approved.

In AWS the platform file has three writers, and each writes with a conditional put that retries when another writer changed the object first.

- The `image-exception` workflow adds an entry. Run `gh workflow run image-exception.yml -f image=<repository@digest> -f finding=<id> -f reason=<text> -f expiry=<YYYY-MM-DD>`. A required reviewer of the `image-exception` environment approves the run, and the workflow confirms that the finding is active in the image before it records the requester and the approver.
- The daily review in `publish-images` removes expired entries and entries whose finding no longer appears in the image, and lists each removal with the new object version in the run report.
- Terraform creates the file once as `config_version: 1` with `exceptions: []` and never rewrites it. It also writes the image policy to `platform/image_policy.yaml`, which the two workflows read to check scope.

A tenant adds its own entries to `image_exceptions.yaml` in its bundle and publishes it with `inv tenant-config-publish`, which applies the same scope and field checks. The daily review reports on the tenant entries but does not edit them.

Locally, `inv image-exceptions-add --scope platform|tenant` and `inv image-exceptions-prune --scope platform|tenant` edit the files with the same checks and refuse to run while `TENANT_CONFIG_BUCKET` is set. When the platform file is missing or unreadable, the pull gate accepts no finding, the daily review fails its report job, and `inv local-up` and `inv tenant-config-validate` name the path or key they looked for.

## Base image and pinned components

Every image the platform builds starts from the hardened Amazon Linux 2023 base, or from `scratch` for a static binary. Amazon Linux reports a CVE to Inspector only once a fixed package exists, so a finding on the base is fixed by the next `dnf upgrade`. `docker/al2023/versions.env` pins the base digest and release, and every third-party component with its checksums. `python -m scripts.base_versions` fails when a Dockerfile `ARG` default differs from its pin, and `--update` rewrites the defaults from the file.

A Go tool such as kubectl, helm, or trivy has a `<NAME>_MODE` pin. `release` installs the upstream release archive by its checksum. `source` builds the tagged source with the current Go toolchain and the module upgrades `<NAME>_GO_GET` lists, for a release whose binary carries findings that a rebuild fixes. trivy, semgrep, ClickHouse, LiteLLM, and Langfuse web and worker have a `<NAME>_IMAGE_MODE` pin. `build` builds the image from its Dockerfile, and `release` copies the upstream image that `<NAME>_UPSTREAM_IMAGE` pins, or `CLICKHOUSE_IMAGE` for ClickHouse, by its digest.

## Daily publish

The `publish-images` workflow runs daily on the default branch, in addition to a tag push and a manual dispatch. It rebuilds, scans, and publishes the platform images for arm64 and does not deploy them, so an operator deploys with `inv aws-release`. A separate `report` job then writes a run report to the job summary and uploads it, with the scan logs, as the `image-report-<arch>` artifact. The report job fails on its own when the exceptions review fails, so a report failure never fails the publish, and the report states the outcome of the publish job.

`scripts/upstream_releases.py` reads the newest release of each component that `docker/al2023/versions.env` pins from the GitHub releases API with the workflow token, from the highest version tag for a repository that publishes no GitHub release, from go.dev for Go, and from the release tags in ECR Public for Amazon Linux 2023. A rate-limited answer is retried after the wait the API names. For each component the report lists the pinned version, the newest version, and whether a newer release exists. For a component in `source` or `build` mode the run pulls the upstream image of the newest release and scans it with `inv image-scan`, and the report states whether that release scans clean and can return to `release` mode. A release that the check cannot read appears in the report with its error. `scripts/exception_review.py` then re-checks every platform and tenant exception, as [Finding exceptions](#finding-exceptions) describes.

## CIS hardening

`docker/al2023/base.Dockerfile` builds the hardened base from the pinned Amazon Linux 2023 minimal image. It applies the ComplianceAsCode remediation of the CIS Amazon Linux 2023 Benchmark Level 2 Server profile, from the `scap-security-guide` release that `SSG_VERSION` pins and verifies by SHA-512, and keeps that remediation as `cis-harden`. Every image runs `cis-harden` as its last step as root, so the rules that the packages the image installed made applicable are remediated too. `cis-harden` installs the tools the remediation calls, removes every package it added afterwards, and fails the build when the remediation reports a missing command, so no fix is skipped without notice. The content skips the machine rules for the boot loader, kernel parameters, partitions, services, and audit daemon inside a container, and the host or node configuration owns those.

The build compiles OpenSCAP from the release tarball that `OPENSCAP_VERSION` pins in a separate stage, because Amazon Linux does not package it, so the scanner never enters an image. `inv publish-platform-images` and `inv publish-temporal-images` build the base into the local image store once per content hash of its inputs, scan it, and pass it to every Dockerfile that starts `FROM ${AL2023_IMAGE}`. Each built image is then scanned with its root file system mounted into the OpenSCAP stage. An image that fails a rule is not pushed unless `docker/al2023/cis-accepted.txt` lists the rule with its reason. The one accepted rule is `root_path_no_dot`, which reads the PATH of running processes that an offline scan has none of, so the publish checks the PATH in each image's configuration for empty and relative entries instead. The ARF results, the HTML report, the per-rule results, and the blocking rules of the base and of each image are written under `--cis-evidence`, `cis-evidence/` by default, and the `publish-images` workflow uploads them with the image references. A base input change retags every image that starts from the base. `inv local-up` and `inv temporal-upgrade` build the same base, and Compose passes it to its builds as `AL2023_IMAGE`.

## Scanning before the push

`inv publish-platform-images` scans every image before it pushes it. It loads the built image, or pulls the upstream image of a component in release mode, into the local Docker, inventories it with the Amazon Inspector SBOM generator, and evaluates the SBOM with the Inspector Scan API, which uses the vulnerability data the ECR scan uses. An image with an unaccepted finding at `--block-severity` or above, `high` by default, is not pushed, and the run writes no references and exits 1. `--exceptions` names an `image_exceptions.yaml` whose entries the scan honours. The generator runs only on Linux, so the publish step runs it from `docker/image-scan.Dockerfile`, and the host needs only Docker and AWS credentials allowed `inspector-scan:ScanSbom`. `inv image-scan --image REF` runs the same scan on any image the local Docker holds, which pushes nothing and costs one Scan API call per image.

## Scanning in ECR

With `image_scanning_enabled`, the `private-bedrock` root enables Amazon Inspector for ECR and sets the account's registry scanning to enhanced and continuous for every repository. Enhanced scanning is an account and region setting, billed per image scanned.

Before a pod pulls an ECR image, the worker reads the image's Inspector coverage and active findings for the platform the cluster's nodes run, resolving an image index to its platform image. A step whose image has no completed scan waits, heartbeating, for up to `IMAGE_SCAN_WAIT_SECONDS`, 600 by default, and is then retried. An upstream image the cache has never served is first pulled into the cache by the worker, which starts its scan. A refused image fails its step without a retry, and the refusal names each blocking finding and the two remedies, rebuilding the image on a patched base or accepting the finding in `image_exceptions.yaml`.

Inspector's read actions take no resource, so a tenant's worker can read the findings of every image in the account. Findings are metadata about images, not their contents.

## Signatures

The `private-bedrock` root creates an AWS Signer profile for the platform and one per tenant, each with the `Notation-OCI-SHA384-ECDSA` platform. Signer cancels a destroyed profile and never frees its name, so each name is generated.

- `inv publish-platform-images` signs every catalog image with the platform profile after it pushes them, from `--signing-profile`, `IMAGE_SIGNING_PROFILE_ARN`, or the root's `image_signing_profile_arn` output. It runs notation from the buildah builder image it publishes, so the host needs neither notation nor the plugin, and it skips an image that already carries a signature. The `publish-images` workflow reads the profile from the `AWS_IMAGE_SIGNING_PROFILE_ARN` repository variable, and the publisher role may sign only with the platform profile. When it finds no profile it publishes nothing and exits 1, unless `--unsigned` declares a deployment that does not verify signatures.
- A tenant signs its own images with its profile, which only its publisher role may use, and pushes them under its prefix.

With `image_signature_verification`, the builder pods run as the `image-verifier` ServiceAccount of the tenant's sandbox namespace. Pod Identity binds it to a role whose only permission is `signer:GetRevocationStatus`, which the AWS Signer plugin calls while it verifies. Immediately before each pull, the pod runs `notation verify` on each signed image against a trust policy that accepts only the platform profile for catalog repositories and only the tenant's profile for its own prefix. A tenant's signature over a platform image therefore never satisfies verification. Cache images carry no signature and are not verified. A sandbox image is verified by a short-lived pod in the same namespace before the sandbox pod is created, because the kubelet pulls a pod's image before any of its code runs. The plugin checks revocation at `verification.signer.<region>.amazonaws.com`, a public name no interface endpoint serves, so the root adds that name to the DNS Firewall allowlist and lets the pull pods reach addresses outside the VPC on 443, and the gate pod network policy admits the Pod Identity agent. ECR repositories keep immutable tags, so notation stores each signature under a `sha256-<digest>` tag, and an image is signed once.

Each platform repository keeps its 5 most recently pushed images of each architecture, counted separately for the `-arm64` and `-amd64` tags, so frequent publishing of one architecture never expires the images of the other. ECR stores each signature as an untagged manifest, which the untagged expiry would remove after 14 days, so the lifecycle policy also keeps the 100 most recently pushed notation referrers indexes. Each index references the signatures of one image and protects them. When an image expires, ECR removes its signatures within 24 hours. A retained image loses its signature only after 100 newer signed images reach its repository, so raise `retained_signature_index_count` for a repository that one architecture publishes far more often than the other.

## The builder image

The gate, checks, and scanner pods start from the platform's buildah image, `docker/buildah.Dockerfile`, which adds notation and the AWS Signer plugin with its trust store to the pinned buildah image. The plugin is published only under a `latest` path, so its checksum in the Dockerfile pins the release.

## Local development

The workstation runs the same layers with `scan.gate: off` and `signatures.verification: false`, because it has no Inspector and no Signer. `inv runtime-images` builds the catalog sandbox images and the builder image into the cluster's image store, pushes the checks, semgrep, and trivy images to the local registry the gate pods pull from, and writes `RUNTIME_IMAGES`, `OPENHANDS_AGENT_SERVER_IMAGE`, `GATE_ENGINE_BUILDER_IMAGE`, `SEMGREP_IMAGE`, and `TRIVY_IMAGE` into `.env`, each pinned by digest. `inv k8s-reload` builds the LiteLLM image beside the platform and OPA images, and Compose builds the Langfuse web, Langfuse worker, and ClickHouse images. On Docker Desktop it also pulls each image into the cluster's nodes by tag, because the nodes resolve a digest only for an image they hold. `inv gate-engine-images` pushes the example engines to the local registry and writes `GATE_ENGINE_IMAGES` with their digests. The local policy example admits `public.ecr.aws/amazonlinux/`, because the example engine Dockerfiles start `FROM` the Amazon Linux 2023 base, and the workstation pulls it directly. In AWS the `ecr-public` pull-through cache mirrors all of `public.ecr.aws`, so the same line resolves through the cache.
