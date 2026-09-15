# Integrating an external gate

The platform drives every external review or scanner tool as a gate through one config-driven mechanism. A tool is never installed into the platform. It is named in `config/<env>/gate_engine.yaml`, resolved from a config-selected source, and run as a subprocess whose output the platform parses onto the shared finding scale. The adapter is tool-agnostic, so a SARIF-emitting scanner such as Semgrep, Snyk, or SonarQube and an AI-CLI reviewer such as `ai-common-workflows` are added the same way, with no packaging and no code change. This is the only way to add an external gate. There is no plugin or pip-install path.

## Inert by default

The external-gate path is off until a deployment writes `config/<env>/gate_engine.yaml` with at least one enabled engine. With the file absent or every engine disabled, `load_gate_engine_configs` returns no engines and no engine gate registers. The built-in rebar `security-review` gate stays the reviewer. The repository ships only `config/local/gate_engine.yaml.example`.

## The contract a tool satisfies

The adapter invokes an entrypoint and reads a JSON file it writes, so the tool is a subprocess boundary rather than a linked library.

- Invocation. The adapter runs `<entrypoint> --against <ref> --json-out <path>` in the checkout under review, adding `--profile <name>` when a gate `params.profile` is set. It sets `AI_REVIEW_SCM=github` so the entrypoint reads the local diff and carries no source-control token. The entrypoint returns `0` or `1` and writes its result to the `--json-out` path.
- Output format. The `output_format` field selects how the platform parses that file. `sarif` reads SARIF 2.1.0, the native output of Semgrep, Snyk, CodeQL, Trivy, and most scanners, so a SARIF-emitting tool needs no wrapper beyond a one-line entrypoint that runs it and writes SARIF to the given path. `review-json` reads the AI-review JSON below. Both map onto one `ScanFinding` per result on the shared severity scale, and the gate derives its verdict against the configured threshold.
- Severity. For SARIF the platform reads the numeric `security-severity` property when a tool emits it (the CVSS 0-10 scale: 9.0+ critical, 7.0+ high, 4.0+ medium, above 0 low), and otherwise maps the SARIF `level` (`error` to high, `warning` to medium, `note` to low). For review JSON the `severity` field is `CRITICAL|HIGH|MEDIUM|LOW`.
- Model routing (AI-CLI tools only, `output_format: review-json`). Every model call routes through the platform LiteLLM gateway. The adapter sets `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` to the gateway, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` to the gateway key, `AI_REVIEW_PROVIDER=api`, `AI_REVIEW_TOOL`, and `AI_REVIEW_MODEL` to a LiteLLM model-list name. The adapter fails closed when no gateway base URL is configured, so no call reaches a vendor API directly. A `sarif` tool needs no model, so none of these are set and no model configuration is required.
- Review JSON. `{"review_action": "APPROVE|COMMENT|REQUEST_CHANGES", "comments": [{"path": ..., "line": ..., "severity": "CRITICAL|HIGH|MEDIUM|LOW", "title": ..., "detail": ..., "perspective": ...}]}`.
- Classifier JSON. `{"mode": ..., "summary": ..., "classifications": [{"test": ..., "path": ..., "line": ..., "verdict": "APPLICATION_BUG|TEST_BUG|FLAKY_FAILURE|ENVIRONMENT_ISSUE", "in_scope": true, "rationale": ...}]}`. The advisory classifier feeds the CI repair loop, which patches an in-scope application or test bug, re-runs once for a flaky-only failure, and escalates an environment issue.

## A SARIF scanner, config only

A scanner that emits SARIF is added with a config entry and a one-line wrapper in its own source, no packaging and no model. The wrapper runs the tool and writes SARIF to the `--json-out` path.

```yaml
config_version: 1
engines:
  - name: semgrep-sarif
    enabled: true
    output_format: sarif
    mode: tag
    repo_url: https://github.com/your-org/semgrep-gate.git
    tag: v1.0.0
    cache_root: .agent-context/engine-cache
    review_entrypoint: bin/scan        # runs `semgrep --sarif` and writes it to --json-out
    classifier_entrypoint: ""
```

```yaml
# gates.yaml
gates:
  - name: semgrep-sarif
    enabled: true
    required: true
    params:
      threshold: high
```

A Snyk or SonarQube gate is the same shape, with `review_entrypoint` pointing at a wrapper that runs `snyk test --sarif-file-output` or the SonarQube scanner and writes SARIF.

## Enabling engines

`config/<env>/gate_engine.yaml` holds a list of engines. Each entry backs one gate, named by `name`, so several engines run side by side.

```yaml
config_version: 1
engines:
  - name: ai-security-review
    enabled: true
    tool: claude           # claude, codex, or copilot
    review_model: security-review-model
    triage_model: review-model
    max_output_tokens: 8192
    mode: local            # local, tag, or zip
    # Harness paths relative to the engine checkout root. These default to the ai-common-workflows
    # layout. Point them at another engine's harness to integrate a different workflow. Set
    # classifier_entrypoint to "" for a review-only engine that runs no classifier.
    review_entrypoint: engines/security-compliance-review/harness/ai-pr-review
    classifier_entrypoint: engines/test-classifier/harness/ai-test-classifier
    local_path: ../ai-common-workflows
  - name: sonarqube-review
    enabled: true
    mode: tag
    repo_url: https://github.com/your-org/sonar-review-engine.git
    tag: v1.0.0
    cache_root: .agent-context/engine-cache
    review_entrypoint: bin/review
    classifier_entrypoint: ""
```

Each enabled engine registers a scanner gate under its `name`. Enable it in `gates.yaml` with its own `enabled` and `required` flags. The advisory test classifier runs for the first enabled engine that sets a `classifier_entrypoint`.

## Selecting a source mode

`mode` is a per-engine field. Set it to `local`, `tag`, or `zip` inside an engine entry, then fill the fields that mode reads. All three record the resolved commit or content hash into the determinism manifest, so a run pins the exact engine version.

`local` points at a checkout already on disk. Use it while the engine has no release yet.

```yaml
engines:
  - name: ai-security-review
    enabled: true
    mode: local
    local_path: /path/to/engine-checkout
```

`tag` clones a tag once into `cache_root`, and before a run's gates start it compares the cached commit against the remote tag through a `git ls-remote` and pulls only when the tag moved. A clone or fetch failure, a missing tag, or an unreachable remote fails the run before any engine work begins.

```yaml
engines:
  - name: ai-security-review
    enabled: true
    mode: tag
    repo_url: https://github.com/your-org/your-engine.git
    tag: v1.4.0
    cache_root: .agent-context/engine-cache
```

`zip` consumes an uploaded snapshot pinned by content hash and descends into a single wrapper directory, so a GitHub archive resolves to the same layout as a checkout. Produce the archive from a ref and set `zip_path` at it.

```bash
gh api repos/your-org/your-engine/zipball/v1.4.0 > /path/to/engine.zip
```

```yaml
engines:
  - name: ai-security-review
    enabled: true
    mode: zip
    zip_path: /path/to/engine.zip
    cache_root: .agent-context/engine-cache
```

Add each engine's gate to `config/<env>/gates.yaml` by its `name`, with its own `enabled` and `required` flags, the same as any other gate, so a deployment runs it advisory or blocking, beside or in place of the built-in reviewer.

```yaml
gates:
  - name: ai-security-review
    enabled: true
    required: true
    params:
      profile: cms-ars
```

## Pod privilege

Every pod path, the external gates, the project checks, and the built-in scanners below, unpacks an image with buildah inside the pod. Some clusters refuse that unprivileged, so `GATE_ENGINE_PRIVILEGED` sets `securityContext.privileged` on the pods the gate engine creates.

```bash
GATE_ENGINE_PRIVILEGED=true
```

It defaults to `false` and should stay there wherever the cluster allows it, because a privileged container gets host device access and the host kernel's full capability set, which removes most of the isolation the pod would otherwise provide. Turn it on only when image unpacking fails without it, as it does under the local k3s used for development, and prefer a cluster configured for rootless unpacking in any shared environment.

## Running the built-in scanners from their own images

The `semgrep` and `trivy` gates are built in, so they carry no engine config. They read `SCANNER_EXECUTION` instead, which follows the same subprocess-or-pod split the external gates use.

`subprocess`, the default, invokes the scanner binary on the worker. When the binary is absent the gate reports a `ScannerError` naming the binary and pointing at pod mode, rather than surfacing a bare `FileNotFoundError`.

`pod` runs the scanner from the image its maintainers publish, in an ephemeral pod, so the worker image carries no scanner binary and the platform's release stays decoupled from theirs. The checkout is copied into the pod at `/workspace`, and the location fields in the tool's findings are rewritten back to the worker's checkout path so results match a subprocess run.

```bash
SCANNER_EXECUTION=pod
SEMGREP_IMAGE=docker.io/semgrep/semgrep:1.177.0
TRIVY_IMAGE=docker.io/aquasec/trivy:0.74.0
```

Both image settings are pinned by tag so a scan is reproducible, and both must be fully qualified. The builder resolves short names only for official Docker Hub images, so an unqualified `semgrep/semgrep` fails the pull.

## Integrating a different engine

Because the boundary is the invocation and JSON contract, another review or classifier workflow integrates by configuration alone. Set `mode` and its source fields at the other engine, and set `review_entrypoint` and `classifier_entrypoint` to its harness paths. The engine reads the `--against` ref and the gateway env, and writes the review or classifier JSON. No adapter change is needed, and `ai-common-workflows` stands as the worked example rather than a hard dependency.

## Adding more engines and tools

`gate_engine.yaml` takes a list, so add an entry per external tool you want to pin and run from config, for example the `ai-common-workflows` reviewer beside a Semgrep, Snyk, or SonarQube SARIF gate and a custom script. Each entry backs its own named gate, enabled in `gates.yaml`, with no limit on how many run and no package to install.

## Runtime provisioning and AWS

Config selects and runs a gate, but the tool's own executable must exist in the worker that runs it, because the gate runs as a subprocess inside the activity worker. This is the one operational step config cannot cover.

- Source, not runtime. The `local`, `tag`, and `zip` modes fetch the tool's entrypoint and wrapper into the worker, and they pin the exact version. They do not install the tool the wrapper calls. A wrapper that runs `semgrep` or an AI CLI needs that binary on the worker's `PATH`.
- Base image. The base worker image ships `git` and `bash` but not scanner binaries or AI CLIs. In subprocess execution mode the external-gate path is inert until both the config and the runtime are present. In pod execution mode the gate builds the tool image in an isolated build-and-run pod, so no tool binary is needed in the worker.
- Providing the binary. Build a derived worker image from the base that installs the tools a deployment enables, for example `FROM <platform-worker>` then `RUN pip install semgrep` or a `COPY` of a vendored CLI, and deploy that image. On EKS this is the worker image the deployment runs. There is no in-pod install step, because the container is immutable, so the binary is baked into the image the same way in local Docker and on AWS.
- Model-only gates need no binary. An `output_format: review-json` AI-CLI gate still needs its CLI (`AI_REVIEW_TOOL`) in the image. A gate whose wrapper calls only tools already present needs no image change.

The rule of thumb: adding or swapping a gate is config alone, but introducing a new tool binary is a one-time worker-image change, identical in local Docker and on AWS.
