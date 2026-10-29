# Jira sync setup

The `Reconcile Bridge` workflow (`.github/workflows/reconcile-bridge.yml`) synchronizes rebar tickets with Jira. This page lists the repository variables and secrets it reads, and the order to configure them in.

## Dormancy on a non-default branch

The workflow is dormant while it lives on a branch that is not the repository default. GitHub runs `schedule` only from the default branch, and a workflow must exist on the default branch before it can be dispatched. On `v0.3.0` neither trigger fires while `main` is the default. Run `rebar bridge run` locally until the default branch changes. No edit to the workflow is needed when it does.

## Repository variables

Set these under Settings, then Secrets and variables, then Actions, on the Variables tab. Create them with `gh variable set <NAME>`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JIRA_URL` | yes | none | Jira site base URL, for example `https://example.atlassian.net`. The preflight step fails when it is unset |
| `JIRA_USER` | yes | none | Account email used for acli authentication. The preflight step fails when it is unset |
| `JIRA_PROJECT` | yes | none | Jira project key the bridge reconciles against |
| `ACLI_VERSION` | yes | none | Pinned acli release, for example `1.3.39-stable`. The step fails when it is unset or set to `latest` |
| `ACLI_SHA256` | yes | none | sha256 of `acli_<version>_linux_amd64.tar.gz`. The step fails when it is unset |
| `TICKETS_S3_URL` | yes | none | Ticket store remote as `s3://bucket/prefix`. The preflight rejects any other shape |
| `AWS_BRIDGE_ROLE_ARN` | yes | none | Role the runner assumes through OIDC to reach the store. Needs the bucket prefix plus `kms:Decrypt` and `kms:GenerateDataKey` |
| `AWS_BRIDGE_REGION` | yes | none | Region holding the bucket and its key |
| `BRIDGE_BOT_NAME` | no | `rebar-bridge[bot]` | Git author name for commits the reconciler writes |
| `BRIDGE_BOT_EMAIL` | no | `rebar-bridge@users.noreply.github.com` | Git author email. The git-email fallback maps inline-signed events to a configured identity |
| `REBAR_ENV_ID` | no | `reconciler` | Environment identifier recorded on reconciler writes |
| `RECONCILE_CONTINUOUS` | no | `false` | Set to `true` to chain paced `workflow_dispatch` runs. Enabling it also requires restoring `actions: write` at both the workflow and job level, because this repository grants only `contents: write` |

## Repository secrets

| Secret | Required | Purpose |
|---|---|---|
| `JIRA_API_TOKEN` | yes | Jira API token for `JIRA_USER`. The preflight step fails when it is unset |
| `REBAR_BOT_SIGNING_KEY` | no | Event-signing key. Without it the reconciler still runs and its writes are unsigned, which identity verification treats as advisory |
| `GITHUB_TOKEN` | automatic | Supplied by Actions. The job grants it `contents: read` for the checkout only. Reconciler events go to the S3 store, so nothing is pushed back to GitHub |

## Ticket store access

The reconciler writes to the S3 ticket store rather than to a branch in this repository, so the job needs `id-token: write` to mint an OIDC token and assume `AWS_BRIDGE_ROLE_ARN`. No long-lived AWS key is stored as a secret. The role's trust policy must accept this repository through the account's GitHub OIDC provider, and its permissions policy must cover the store prefix plus the bucket's KMS key. See `docs/rebar-s3-ticket-store.md`.

## Pinning acli

`ACLI_SHA256` is mandatory and the archive is verified on every pass, including cache hits. The digest also forms part of the cache key, so repinning the version misses the old entry rather than reusing it. Compute the digest for the version you intend to pin.

```sh
curl -sSL "https://acli.atlassian.com/linux/1.3.39-stable/acli_1.3.39-stable_linux_amd64.tar.gz" -o acli.tar.gz && shasum -a 256 acli.tar.gz
```

Set both variables together.

```sh
gh variable set ACLI_VERSION --body "1.3.39-stable"
gh variable set ACLI_SHA256 --body "<digest from above>"
```

## Reconciler dependency

The workflow installs rebar from `.github/reconcile-bridge-requirements.txt`, a hash-pinned file compiled from `.github/reconcile-bridge-requirements.in`, and installs it with `pip install --require-hashes`. Bumping the pinned release means editing the `.in` file and recompiling.

```sh
uv pip compile --generate-hashes .github/reconcile-bridge-requirements.in -o .github/reconcile-bridge-requirements.txt
```

`--require-hashes` rejects any dependency that is not pinned with a hash, so the compiled file has to cover the full transitive closure. The reconciler does not need the `[agents]` extra.

## Verifying the configuration

Run the workflow manually once the default branch carries it, choosing the `dry-run` mode, which computes mutations and applies none. Locally, `rebar bridge preview` does the same against the configured project.
