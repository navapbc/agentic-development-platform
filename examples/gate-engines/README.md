# Example gate engines

Two engines that show how an organization plugs its own security and testing processes into the platform as gates. Each one is a directory with a Dockerfile, an entrypoint, and a rules file, and the platform runs it through the same contract as any external tool. See [docs/gate-engine.md](../../docs/gate-engine.md) for the contract and the source modes.

| Engine | Decides findings with | Output | Model call |
|---|---|---|---|
| [security-policy](security-policy) | Regular expressions from `rules.toml`, matched against the lines the change adds | SARIF 2.1.0 | None |
| [test-policy](test-policy) | Path mappings from `rules.toml` that pair each changed source file with the test files that cover it | review-json | Optional, for suggested test cases only |

## The contract

The platform runs the entrypoint in the checkout under review.

```bash
bash /engine/bin/scan --against <base ref> --json-out <file>
```

- The engine compares the checkout with the merge base of `--against` and `HEAD`, uncommitted and untracked files included, and writes its result to the `--json-out` file.
- Exit status 0 means the engine ran, with or without findings, and 2 means it could not load its rules. The platform accepts 0 and 1 and treats any other status as an engine failure.
- In a gate pod the engine runs as a non-root user with the checkout at `/workspace`, `git` ownership checks disabled, and no AWS identity or source-control token.
- A `review-json` engine also receives the model gateway in `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, and `AI_REVIEW_MODEL`, and the Langfuse headers of the run in `ANTHROPIC_CUSTOM_HEADERS`.

## security-policy

Each `[[rule]]` names an `id`, a `description`, a `severity` of `critical`, `high`, `medium`, or `low`, a `pattern`, and optional `paths` and `exclude` fnmatch patterns. A finding names the rule and the file and line, never the matched text, so a committed credential is not copied into the gate evidence. The severity becomes the SARIF `security-severity` the platform reads. The shipped rules cover AWS access keys, private keys, `shell=True` subprocess calls, disabled TLS verification in Python and Node.js, and plaintext HTTP endpoints. Add a rule to `rules.toml` and publish the image again to enforce a new organization policy.

## test-policy

Each `[[mapping]]` names `sources` and `exclude` patterns that select source files, and `tests` patterns in which `{stem}` is the source file name without its extension. A changed source file that no changed file matches through those patterns becomes one finding at the mapping's `severity`. The shipped mappings cover Python, TypeScript and JavaScript, Go, and Java.

When there are findings, the engine calls the gateway's Anthropic Messages endpoint with the diff of each uncovered file and asks for suggested test cases. It requests temperature 0 and repeats the call once without it for a model that refuses the parameter, such as Claude Opus 5.5 on Bedrock. The suggestions are appended to each finding's `detail`. The findings, their severity, and the gate verdict come from the rules alone. A gateway error or a timeout after 60 seconds leaves a fixed detail. The call passes through the platform's guardrails and appears in Langfuse under the run, so it is the pattern to copy for an engine of your own that calls a model.

## Running them

Both engines run in image mode, where the gate pod pulls a published image and runs the harness inside it.

```yaml
# config/<env>/gate_engine.yaml
config_version: 1
engines:
  - name: security-policy
    enabled: true
    mode: image
    output_format: sarif
    review_entrypoint: bin/scan
  - name: test-policy
    enabled: true
    mode: image
    output_format: review-json
    review_model: claude-opus-5-5
    review_entrypoint: bin/review
```

```yaml
# config/<env>/gates.yaml
gates:
  - name: security-policy
    enabled: true
    required: true
    params:
      threshold: high
  - name: test-policy
    enabled: true
    required: false
```

An image-mode entry without an `image` runs the image `GATE_ENGINE_IMAGES` names for it.

- On a workstation, start the local registry with `docker compose --profile registry up -d registry`, run `inv gate-engine-images`, set the `GATE_REGISTRY_TLS_VERIFY` and `GATE_ENGINE_IMAGES` lines it prints in `.env`, and run `inv k8s-reload`.
- In AWS, `inv publish-platform-images` and the `publish-images` workflow push both images to the `gate-engine-security-policy` and `gate-engine-test-policy` ECR repositories and write them to `eks_gate_engine_images` in `images.auto.tfvars.json`, and `inv eks-deploy` sets `GATE_ENGINE_IMAGES` on the activity worker.

## Writing your own

Copy a directory, keep the entrypoint arguments and the exit status, and write SARIF or review-json to the `--json-out` file. Install the tools the engine needs in its Dockerfile, because a gate pod in AWS cannot install packages at run time. Name the engine in `gate_engine.yaml` with `mode: image` and its published `image`, or add it to `GATE_ENGINES` in `scripts/publish_platform_images.py` with an ECR repository in `container-services` to publish it with the platform.
