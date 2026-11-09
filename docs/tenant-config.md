# Tenant config

Each tenant of a deployment owns the config files that describe its work and its credentials. It publishes them as a bundle, and the workers load the bundle at run time without a deploy. This document lists the files a tenant owns, how a bundle layers over the environment's config, what a bundle must contain, the default each file and key takes when it is absent, and how a run pins one version of the bundle. [tenants](../infra/terraform/examples/private-bedrock/README.md#tenants) in the `private-bedrock` README covers the AWS resources each tenant receives.

## Where a bundle comes from

`TENANT` names the tenant a process serves. An activity worker refuses to start without it, everywhere, because there is no default tenant. Locally, set `TENANT` in `.env`.

| Setting | Source of the bundle |
|---|---|
| `TENANT_CONFIG_BUCKET` set | The objects under `tenants/<TENANT>/` in that bucket, which the [tenant-config-store](../infra/terraform/tenant-config-store) module creates and the tenant's publisher role writes |
| `TENANT_CONFIG_BUCKET` empty | The directory `config/<environment>/tenants/<TENANT>/` |

A bundle is flat. It holds files named `<name>.yaml` at the top of its directory or prefix.

## Layering

A bundle layers over `config/<environment>/`, or the directory `PLATFORM_ENV_CONFIG_DIR` names in a deployed task. For each tenant file, the bundle's copy wins, and a file the bundle lacks is read from the environment directory. A file that is not a tenant file is read only from the environment directory, so a bundle can never replace a platform file such as `review-trust-root.yaml` or `integrations.yaml`, and a bundle that holds one is refused.

The tenant files are listed below.

| File | What it holds |
|---|---|
| `repositories.yaml` | The tenant's repositories, their backend, default branch, and indexing |
| `checks.yaml` | Each repository's checks image, command, and fix command |
| `gates.yaml` | The gate bundle and the per-gate overrides a run evaluates |
| `gate_engine.yaml` | The external gate engines and their source or image |
| `registries.yaml` | The image and package mirrors the gate, checks, and scanner pods use |
| `github.yaml` | The GitHub hosts, the default owner, and the `token_ref` of each credential |
| `jira.yaml` | The Jira sites, projects, boards, flows, and the `token_ref` of each credential |
| `tickets.yaml` | The rebar ticket store, its status flow, its poll identity, and its S3 store |

Because the environment directory fills every gap, one environment-level file can serve every tenant, such as the aws-mode `registries.yaml` that Terraform generates. The required-content rules below still apply to the effective config, the bundle and the environment together.

## Validation

A worker validates a bundle before it activates it, and `inv tenant-config-validate` runs the same validation on a directory. A bundle is refused when any of the following holds.

- It holds a file that is not a tenant file.
- A file's `config_version` is older than the release expects.
- A file fails the loader the platform uses for it, for example an unknown gate, a malformed mirror, or a repository on the local backend without a path.
- `repositories.yaml` is absent from the effective config, or names no repository.
- `gates.yaml` is absent from the effective config.
- `TICKET_BACKEND=rebar` and `tickets.yaml` is absent.
- `TICKET_BACKEND=jira` and `jira.yaml` is absent.
- A repository uses the `github` backend and `github.yaml` is absent.

The workers check the source every `TENANT_CONFIG_REFRESH_SECONDS`, 60 by default. A new bundle that passes validation becomes active for the runs that start after it. A new bundle that fails stays refused until its content changes. The last valid bundle stays active, and the workers alert over the Slack webhook and the alert email with every reason. A worker whose first bundle fails validation refuses to start.

## Defaults

A file the effective config lacks, and a key a file omits, take the defaults below.

### repositories.yaml

Required. `defaults.backend` is `github`, `defaults.default_branch` is `main`, and `defaults.index` is `false`. Each repository inherits the defaults and may override them. A repository on the `local` backend needs a `path`.

### checks.yaml

Optional. Absent, no repository has checks. A repository on the `github` backend without an entry is held at intake, because the GitHub backend requires checks. A repository on the `local` backend without an entry skips the checks gate. An entry's `working_dir` is `/workspace`, its `timeout_minutes` is 15, and it has no `fix_command` unless it sets one.

### gates.yaml

Required. `profile` selects a shipped bundle, `private` or `government`. Both run `code-review`, `security-review`, `acceptance`, `semgrep`, and `trivy` as required gates, except that `private` runs `semgrep` advisory. `fail_fast` is off for the review and scanner stages. An entry under `gates` overrides one gate by name and leaves the rest of the bundle as it is.

### gate_engine.yaml

Optional. Absent, no engine gate registers and the built-in `security-review` gate stays the reviewer. An entry's `enabled` is `false`, `mode` is `local`, `output_format` is `review-json`, `tool` is `claude`, `on_unavailable` is `abort`, and `max_output_tokens` is 8192. An enabled entry must set `review_entrypoint`, and `classifier_entrypoint` is empty, which runs no classifier. An `image`-mode entry without an `image` runs the image `GATE_ENGINE_IMAGES` names for it. [gate-engine.md](gate-engine.md) describes every field.

### registries.yaml

Optional. Absent, the gate, checks, and scanner pods pull images as written and install packages from their public indexes, which works on a workstation and fails in AWS, where DNS Firewall resolves no public registry. [gate-engine.md](gate-engine.md#registries) describes the `aws` and `external` modes.

### github.yaml

Required when a repository uses the `github` backend. `default.host` names one of `hosts`, and `default.token_ref` names the variable holding the token, `GITHUB_TOKEN` in the template. A repository in `repositories.yaml` may override the host, owner, or `token_ref`.

### jira.yaml

Required with `TICKET_BACKEND=jira`. `allowlist` names the projects intake reads. `default` is the identity of any allowlisted project without a `projects` entry. `intake_label` unset admits every issue in a project that is not Done. `dependency_link_type` unset turns off dependency recording for the project. `acceptance_field` empty reads no acceptance criteria field.

### tickets.yaml

Required with `TICKET_BACKEND=rebar`. `backend` names the store's `repo_root`, `project`, `default_repo`, and `status_flow`. `store`, `roles`, `provenance`, and `isolation` are optional together. Without `store`, the ticket store stays local to `repo_root` and syncs to no bucket. `write_back.allowed_scopes` defaults to every write scope, `status`, `comment`, `approval`, and `link`.

## Pinning a run

A run's first activity, `record_manifest`, records the tenant and the identity of the active bundle in the run's determinism manifest. The identity is the version of each file, the S3 version ID from the bucket or a content hash from a directory. Every later activity of the run reads that identity's copy, which the worker keeps in a local store under `$TMPDIR/tenant-config/<tenant>/`, holding the 20 most recent bundles, and fetches again from the bucket's version history when it has been pruned. A bundle published while a run is in flight therefore changes nothing the run reads. An activity that runs before the manifest, such as intake, reads the bundle active when it starts. An activity of a run whose manifest names another tenant fails without a retry.

A directory source keeps no history. A run pinned to a directory bundle that has since changed reads its copy from the worker's store, and fails when the copy has been pruned.

## Publishing

```bash
inv tenant-config-validate --tenant adp --bundle-dir path/to/bundle
inv tenant-config-publish --tenant adp --bundle-dir path/to/bundle --bucket <config bucket>
```

`inv tenant-config-publish` validates the bundle, uploads every file, deletes the files under the tenant's prefix the bundle no longer holds, and prints each file's new version. In a tenant's CI, the workflow assumes the tenant's publisher role through GitHub OIDC and passes `--environment-dir` at a copy of the deployment's environment files, so validation layers over the same environment the workers use. The workers validate the bundle again before they activate it.
