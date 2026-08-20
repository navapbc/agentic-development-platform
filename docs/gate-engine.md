# Integrating an external AI-CLI review engine

The platform can drive one or more external AI-CLI review engines as gates, the same way `docs/gate-plugins.md` drives external scanner tools. The adapter is engine-agnostic. It targets any engine whose harness satisfies the invocation and JSON contract below, so a deployment can point it at a different review or test-triage workflow without a code change. The `ai-common-workflows` repository is the reference example the defaults describe.

## Inert by default

The engine path is off until a deployment writes `config/<env>/gate_engine.yaml` with at least one enabled engine. With the file absent or every engine disabled, `load_gate_engine_configs` returns no engines, no engine gate registers, and neither the engine code nor the `compose.gate-engine.yaml` overlay runs. The built-in rebar `security-review` gate stays the reviewer. The repository ships only `config/local/gate_engine.yaml.example`.

## The contract an engine satisfies

The adapter invokes a harness script and reads a JSON file it writes, so the engine is a subprocess boundary rather than a linked library.

- Invocation. The adapter runs `<entrypoint> --against <ref> --json-out <path>` in the checkout under review, adding `--profile <name>` when a gate `params.profile` is set. It sets `AI_REVIEW_SCM=github` so the harness reads the local diff and carries no source-control token. The entrypoint returns `0` or `1`, and writes the JSON result to the `--json-out` path.
- Model routing. Every model call routes through the platform LiteLLM gateway so Guardrails monitors it. The adapter sets `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` to the gateway, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` to the gateway key, `AI_REVIEW_PROVIDER=api`, `AI_REVIEW_TOOL`, and `AI_REVIEW_MODEL` to a LiteLLM model-list name. The adapter fails closed when no gateway base URL is configured, so no call reaches a vendor API directly.
- Review JSON. `{"review_action": "APPROVE|COMMENT|REQUEST_CHANGES", "comments": [{"path": ..., "line": ..., "severity": "CRITICAL|HIGH|MEDIUM|LOW", "title": ..., "detail": ..., "perspective": ...}]}`. Each comment maps onto one `ScanFinding` on the shared severity scale, and the gate derives its verdict against the configured threshold.
- Classifier JSON. `{"mode": ..., "summary": ..., "classifications": [{"test": ..., "path": ..., "line": ..., "verdict": "APPLICATION_BUG|TEST_BUG|FLAKY_FAILURE|ENVIRONMENT_ISSUE", "in_scope": true, "rationale": ...}]}`. The advisory classifier feeds the CI repair loop, which patches an in-scope application or test bug, re-runs once for a flaky-only failure, and escalates an environment issue.

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

## Integrating a different engine

Because the boundary is the invocation and JSON contract, another review or classifier workflow integrates by configuration alone. Set `mode` and its source fields at the other engine, and set `review_entrypoint` and `classifier_entrypoint` to its harness paths. The engine reads the `--against` ref and the gateway env, and writes the review or classifier JSON. No adapter change is needed, and `ai-common-workflows` stands as the worked example rather than a hard dependency.

## Adding more engines and tools

`gate_engine.yaml` takes a list, so add an engine entry per external AI-CLI review or classifier workflow you want to pin and run from config without packaging it, for example the ai-common-workflows reviewer beside a wrapped SonarQube or a custom script. Each entry backs its own named gate. Separately, scanner or self-contained gate tools are added as gate plugins, an installable package per tool that a deployment names in `gates.yaml`, with no limit on how many run. See [gate-plugins.md](gate-plugins.md) and the `examples/snyk-gate` worked example. Use a plugin when the tool is a packaged scanner or gate, and use a gate engine entry when the tool is a config-pinned external CLI review or classifier workflow.
