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

`config/<environment>/image_exceptions.yaml` lists the Inspector findings the platform accepts. The platform owns it, and a tenant bundle never overrides it. In AWS, put it in `container_config_dir`.

```yaml
config_version: 1
exceptions:
  - image: docker.io/library/python@sha256:<digest>
    finding: CVE-2025-12345
    reason: The vulnerable package is not reachable from the checks command.
    expires: 2027-01-31
```

Each entry accepts one finding in one image digest until its expiry date. The image may be the reference as configured or the pull-through cache reference it resolves to. An expired entry no longer accepts its finding, so the image is refused again until the finding is fixed or the entry is renewed.

## Scanning before the push

`inv publish-platform-images` scans every image before it pushes it. It loads the built image, or pulls a mirrored upstream image, into the local Docker, inventories it with the Amazon Inspector SBOM generator, and evaluates the SBOM with the Inspector Scan API, which uses the vulnerability data the ECR scan uses. An image with an unaccepted finding at `--block-severity` or above, `high` by default, is not pushed, and the run writes no references and exits 1. `--exceptions` names an `image_exceptions.yaml` whose entries the scan honours. The generator runs only on Linux, so the publish step runs it from `docker/image-scan.Dockerfile`, and the host needs only Docker and AWS credentials allowed `inspector-scan:ScanSbom`. `inv image-scan --image REF` runs the same scan on any image the local Docker holds, which pushes nothing and costs one Scan API call per image.

## Scanning in ECR

With `image_scanning_enabled`, the `private-bedrock` root enables Amazon Inspector for ECR and sets the account's registry scanning to enhanced and continuous for every repository. Enhanced scanning is an account and region setting, billed per image scanned.

Before a pod pulls an ECR image, the worker reads the image's Inspector coverage and active findings for the platform the cluster's nodes run, resolving an image index to its platform image. A step whose image has no completed scan waits, heartbeating, for up to `IMAGE_SCAN_WAIT_SECONDS`, 600 by default, and is then retried. An upstream image the cache has never served is first pulled into the cache by the worker, which starts its scan. A refused image fails its step without a retry, and the refusal names each blocking finding and the two remedies, rebuilding the image on a patched base or accepting the finding in `image_exceptions.yaml`.

Inspector's read actions take no resource, so a tenant's worker can read the findings of every image in the account. Findings are metadata about images, not their contents.

## Signatures

The `private-bedrock` root creates an AWS Signer profile for the platform and one per tenant, each with the `Notation-OCI-SHA384-ECDSA` platform. Signer cancels a destroyed profile and never frees its name, so each name is generated.

- `inv publish-platform-images` signs every catalog image with the platform profile after it pushes them, from `--signing-profile`, `IMAGE_SIGNING_PROFILE_ARN`, or the root's `image_signing_profile_arn` output. It runs notation from the buildah builder image it publishes, so the host needs neither notation nor the plugin, and it skips an image that already carries a signature. The `publish-images` workflow reads the profile from the `AWS_IMAGE_SIGNING_PROFILE_ARN` repository variable, and the publisher role may sign only with the platform profile. When it finds no profile it publishes nothing and exits 1, unless `--unsigned` declares a deployment that does not verify signatures.
- A tenant signs its own images with its profile, which only its publisher role may use, and pushes them under its prefix.

With `image_signature_verification`, the builder pods run as the `image-verifier` ServiceAccount of the tenant's sandbox namespace. Pod Identity binds it to a role whose only permission is `signer:GetRevocationStatus`, which the AWS Signer plugin calls while it verifies. Immediately before each pull, the pod runs `notation verify` on each signed image against a trust policy that accepts only the platform profile for catalog repositories and only the tenant's profile for its own prefix. A tenant's signature over a platform image therefore never satisfies verification. Cache images carry no signature and are not verified. A sandbox image is verified by a short-lived pod in the same namespace before the sandbox pod is created, because the kubelet pulls a pod's image before any of its code runs. The plugin checks revocation at `verification.signer.<region>.amazonaws.com`, a public name no interface endpoint serves, so the root adds that name to the DNS Firewall allowlist and lets the pull pods reach addresses outside the VPC on 443, and the gate pod network policy admits the Pod Identity agent. ECR repositories keep immutable tags, so notation stores each signature under a `sha256-<digest>` tag, and an image is signed once.

## The builder image

The gate, checks, and scanner pods start from the platform's buildah image, `docker/buildah.Dockerfile`, which adds notation and the AWS Signer plugin with its trust store to the pinned buildah image. The plugin is published only under a `latest` path, so its checksum in the Dockerfile pins the release.

## Local development

The workstation runs the same layers with `scan.gate: off` and `signatures.verification: false`, because it has no Inspector and no Signer. `inv runtime-images` builds the catalog sandbox images and the builder image and writes `RUNTIME_IMAGES` and `GATE_ENGINE_BUILDER_IMAGE` into `.env`, each pinned by digest. On Docker Desktop it also pulls each image into the cluster's nodes by tag, because the nodes resolve a digest only for an image they hold. `inv gate-engine-images` pushes the example engines to the local registry and writes `GATE_ENGINE_IMAGES` with their digests. The local policy example admits `public.ecr.aws/docker/library/`, because the example engine Dockerfiles start `FROM` it, and the workstation pulls it directly. In AWS the `ecr-public` pull-through cache mirrors all of `public.ecr.aws`, so the same line resolves through the cache.
