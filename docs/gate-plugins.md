# Writing a gate plugin

A gate plugin adds a review or scanner gate to the platform from its own installable package, with no change to this repository. The platform discovers plugins through the `agentic_dev_platform.gates` Python entry-point group and binds only to the distributions a deployment names in `config/<env>/gates.yaml`, so no unlisted third-party code loads.

## The two shapes a plugin can take

A plugin entry point resolves to a class or a factory the platform instantiates once per run. The instance satisfies one of two shapes.

1. A scanner. The instance exposes `name` and an async `scan(self, target) -> list[ScanFinding]`, the same `SecurityScanner` contract the built-in Semgrep and Trivy gates use. The platform wraps it as a scanner gate and derives the verdict from the findings against the configured severity threshold.
2. A full gate. The instance exposes `name`, `kind`, and an async `evaluate(self, context) -> GateResult`. This is the shape a review-kind plugin uses, since a review gate returns a verdict, findings, an attestation, and a summary rather than a raw finding list.

`ScanFinding`, `ScanTarget`, `GateContext`, and `GateResult` are imported from the platform, so the plugin depends on the platform contracts and nothing else.

## Registering the entry point

The plugin declares the entry point in its own `pyproject.toml` under the `agentic_dev_platform.gates` group, keyed by the entry-point name.

```toml
[project.entry-points."agentic_dev_platform.gates"]
snyk = "snyk_gate:SnykScanner"
```

## Enabling the plugin

A deployment enables the gate in `config/<env>/gates.yaml` and names the exact distribution and entry point, which is the trust boundary. The platform loads only that distribution's entry point and never scans for unlisted plugins.

```yaml
gates:
  - name: snyk
    enabled: true
    required: true
    plugin:
      distribution: snyk-gate
      entry_point: snyk
    params:
      threshold: high
```

## Fail-closed rule

A required gate whose plugin is absent, whose distribution does not match, or whose import raises stops the run with a clear reason. An advisory gate whose plugin is absent is skipped. This keeps a mandated scanner from silently dropping out of a run.

## Worked example

The `examples/snyk-gate` package is a complete Snyk dependency-scan gate. It shells the Snyk CLI, parses the JSON report, and maps each vulnerability onto the shared `ScanFinding` severity scale. Install it into the platform environment, add the gate entry above, and a high-severity finding blocks promotion when the gate is required.

The plugin process carries no source-control token, and a plugin that drives a model routes its calls through the platform LiteLLM gateway rather than a second provider path.
