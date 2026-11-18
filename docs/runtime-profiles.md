# Runtime profiles

A runtime profile is the tech stack of a repository. It names the sandbox image the coding agent runs in, the default checks of a repository without its own `checks.yaml` entry, and the gate overrides its runs apply. The platform ships a catalog of profiles and images, a tenant adds its own profiles in its config bundle, and each repository names its profile in `repositories.yaml`. A tenant is the trust boundary and a profile is only the stack, so one tenant may hold a ReactJS repository and a FastAPI repository on different profiles, served by the same worker.

## The catalog

Every catalog image is built for arm64 and pinned by digest.

### Sandbox images

| Catalog name | Base image | Adds | Toolchains |
|---|---|---|---|
| `agent-server-python-node` | Amazon Linux 2023 minimal, the platform's `OPENHANDS_AGENT_SERVER_IMAGE` | The `openhands-agent-server` package the platform's SDK version pins, installed from a hash-locked requirements file | Python 3.13, pip, Node 24, npm, git, tmux |
| `agent-server-jvm` | `agent-server-python-node` | Corretto 21 and Maven 3.8.4 from the Amazon Linux packages | Everything above, plus `java`, `javac`, and `mvn`. Gradle projects use their `./gradlew` wrapper |
| `agent-server-go` | `agent-server-python-node` | Go 1.26 from the Amazon Linux package | Everything above, plus `go` |

The platform builds all three as targets of [agent-server.Dockerfile](../docker/agent-server.Dockerfile). `agent-server-python-node` serves both Python and Node, including ReactJS. The agent runs in CLI mode, which turns the browser tools off, so the images carry no browser, no editor server, and no kernel packages. [docker/agent-server/requirements.txt](../docker/agent-server/requirements.txt) locks the agent server's Python dependencies with hashes. Regenerate it with `uv pip compile --no-config docker/agent-server/requirements.in --override docker/agent-server/overrides.txt --python-platform linux --python-version 3.13 --generate-hashes -o docker/agent-server/requirements.txt` after a change to the SDK version.

### Checks images

| Catalog name | Profile | Toolchain from the Amazon Linux packages | Default command |
|---|---|---|---|
| `checks-python` | `python-fastapi` | Python 3.13 and pip | `pip install -r requirements-dev.txt && pytest -q` |
| `checks-node` | `node-react` | Node 24 and npm | `npm ci && npm test -- --watchAll=false` |
| `checks-jvm` | `java-spring` | Corretto 21 and Maven 3.8.4 | `mvn -B -q verify` |
| `checks-go` | `go-service` | Go 1.26 | `go vet ./... && go test ./...` |

The platform builds all four as targets of [checks.Dockerfile](../docker/checks.Dockerfile) on Amazon Linux 2023 minimal, each with bash, git, and the archive tools. A profile names its checks image as `catalog:<name>`, which resolves through `RUNTIME_IMAGES` the way a sandbox image does, and a `checks.yaml` entry may name one the same way. The checks pods pull the dependencies a command installs through the registry mirrors that [gate-engine.md](gate-engine.md#registries) describes. Maven 3.8.4 defaults to version 3.1 of the compiler plugin, which ignores `maven.compiler.release`, so a project whose parent POM does not pin the plugin, as the Spring Boot parent does, pins version 3.6 or later itself.

### Gate engines

`security-policy` and `test-policy`, the [example gate engines](../examples/gate-engines), are catalog engines defined in [config/platform/gate-engines.yaml](../config/platform/gate-engines.yaml). Every catalog profile enables `security-policy` as a required gate and `test-policy` as an advisory one.

### Profiles

| Profile | Sandbox image | For |
|---|---|---|
| `python-fastapi` | `agent-server-python-node` | Python services such as FastAPI |
| `node-react` | `agent-server-python-node` | Node and ReactJS applications |
| `java-spring` | `agent-server-jvm` | Java services such as Spring Boot |
| `go-service` | `agent-server-go` | Go services |

[config/platform/runtime-profiles.yaml](../config/platform/runtime-profiles.yaml) holds the catalog profiles in full.

## Assigning a profile

```yaml
# repositories.yaml
config_version: 1
defaults:
  backend: github
  profile: python-fastapi
repositories:
  payments-api: {}
  portal-web:
    profile: node-react
```

A repository without a profile runs in `OPENHANDS_AGENT_SERVER_IMAGE`, with its `checks.yaml` entry and the tenant's `gates.yaml` alone.

## Tenant profiles

A tenant defines its own profiles in `profiles.yaml` in its config bundle, under names the catalog does not use. [profiles.yaml.example](../config/local/profiles.yaml.example) shows the format.

```yaml
config_version: 1
profiles:
  claims-batch:
    description: Spring Batch jobs built with the Gradle wrapper.
    sandbox_image: catalog:agent-server-jvm
    checks:
      image: docker.io/library/eclipse-temurin:21-jdk@sha256:<digest>
      command: "./gradlew --no-daemon check"
    gates:
      - name: security-policy
        enabled: true
        required: true
```

| Field | Meaning |
|---|---|
| `description` | What the profile is for |
| `sandbox_image` | `catalog:<name>` for a catalog image, or a digest-pinned reference to an image the tenant publishes. Empty runs `OPENHANDS_AGENT_SERVER_IMAGE` |
| `checks` | The default checks, in the `checks.yaml` entry format |
| `gates` | Gate overrides, in the `gates.yaml` entry format |

## Resolution

- The sandbox image is the profile's `sandbox_image`. `catalog:agent-server-python-node` is `OPENHANDS_AGENT_SERVER_IMAGE`, and any other catalog name resolves through `RUNTIME_IMAGES`, a JSON map of catalog name to reference.
- The checks are the repository's `checks.yaml` entry when it has one, and the profile's `checks` otherwise. A GitHub repository whose profile has checks satisfies the CI requirement at intake.
- The gates are the tenant's `gates.yaml` with the profile's `gates` entries applied after it, so a profile entry for a gate the tenant also lists wins for the profile's repositories.

The worker binds the run's repository for each activity from the activity input, so every lookup resolves the profile of the repository the run works on. The run's determinism manifest records the profile's name, and the gate configuration hash covers its gate overrides.

A tenant config bundle is refused when a repository names a profile that neither the catalog nor `profiles.yaml` defines, when a profile's catalog image is not published in the deployment, when a profile's checks or gates do not parse, when a profile enables a gate that does not register, such as a catalog engine whose image `GATE_ENGINE_IMAGES` does not name, or when any image the effective config names lacks a digest or comes from a repository the image policy does not admit. A tenant image goes under the tenant's ECR prefix, `<name_prefix>/tenants/<tenant>/`, signed with the tenant's Signer profile when signature verification is on. See [image governance](image-governance.md).

## Bringing your own sandbox image

A tenant image builds `FROM` a catalog image by digest, so it keeps the agent-server contract.

- The entrypoint stays the agent-server's, `tini -- /usr/local/bin/openhands-agent-server`.
- The image runs as the non-root user `openhands`, uid 10001. Switch to `root` only to install packages, and switch back.
- The workspace is mounted at `/workspace`, and the agent writes nowhere else that must persist.
- Every tool installs at build time, because a sandbox pod in AWS cannot install packages at run time.

The tenant's CI pushes the image to `<name_prefix>/tenants/<tenant>/` in ECR through its publisher role, or to its own registry through `registries.yaml`, and names it by digest in `sandbox_image`.

## Publishing the catalog

`inv publish-platform-images` and the `publish-images` workflow build `agent-server-jvm`, `agent-server-go`, and the four checks images into their ECR repositories and write them to `eks_runtime_images` in `images.auto.tfvars.json`, which `inv eks-deploy` sets as `RUNTIME_IMAGES` on the activity worker. The same command builds `agent-server-python-node` into the `openhands-agent-server` repository and writes it to `eks_agent_server_image`. Locally, `inv runtime-images` builds the catalog images, the buildah builder, and the semgrep and trivy images into the cluster's image store and writes `RUNTIME_IMAGES`, `OPENHANDS_AGENT_SERVER_IMAGE`, `GATE_ENGINE_BUILDER_IMAGE`, `SEMGREP_IMAGE`, and `TRIVY_IMAGE` into `.env`, each pinned by digest.
