# agentic-dev-platform

A Docker-based agentic software delivery platform. Given a ticket it plans, writes code, runs tests, scans for security issues, and delivers a change against an allowlisted repository, with human approvals, a full audit trail, and observability. Parallel multi-agent execution is a design goal.

The ticket source and the source-control target are pluggable and selected independently. The platform reads work from a Jira project or a git-backed rebar ticket store, and it delivers to GitHub as a draft pull request or to a local git folder as a promoted branch with a summary and patch. See [Ticket and source-control backends](#ticket-and-source-control-backends).

The end goal is a hosted deployment on AWS. The Docker Compose stack in this repository is the development environment that stands the platform up on a workstation. Components are built to move to AWS without changing platform code. Inference targets Amazon Bedrock through a swappable connectivity provider, so the same code path runs in development and in the hosted environment.

This README gets a new developer from a fresh clone to a running stack. Contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## What you will be running

The stack runs under Docker Compose and talks to Amazon Bedrock over a connectivity provider you choose. The pieces are described in [System components](#system-components) below. Inference always passes through the LiteLLM container, which is the single path to Bedrock.

## Prerequisites

Install these on the host before you start.

| Requirement | Why | Notes |
| --- | --- | --- |
| Docker Desktop with Compose v2 | Runs the whole stack | The `docker compose` command must work |
| Python 3.12 or newer | Task runner, migrations, local scripts | The container images use 3.12 |
| uv 0.13.3 or newer | Installs the locked Python dependencies | Install it from [docs.astral.sh/uv](https://docs.astral.sh/uv/). `pyproject.toml` refuses an older uv |
| git | Clone this repo and its siblings | |
| An AWS profile with Bedrock access | Inference | An IAM principal with `bedrock:InvokeModel` and credentials under `~/.aws`. Set the profile and region in `.env` |
| AWS CLI v2 | The jumphost tunnel, the jumphost image push, and the secret values | The default `jumphost` connectivity provider calls it |
| AWS Session Manager plugin | The jumphost tunnel | Install `session-manager-plugin` from the [AWS documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) |
| jq | The jumphost tunnel | The connect script parses the ECS and SSM responses with it |

Deploying the infrastructure to AWS needs these as well. See [Deploying to AWS](#deploying-to-aws).

| Requirement | Why | Notes |
| --- | --- | --- |
| Terraform 1.11 or newer | Applies the infrastructure roots under `infra/terraform` | Every module requires it, because the secrets are written through write-only arguments from ephemeral resources. `.terraform-version` pins 1.11.4, which tfenv selects inside this repository whatever its global version is |
| An AWS profile for the target account | Creates the network, IAM roles, KMS keys, data tier, and secrets | The examples use the `agentic-platform` profile. Confirm it with `aws sts get-caller-identity --profile agentic-platform` |
| Docker Buildx | Builds the arm64 jumphost image | Docker Desktop includes it |

The review path installs `rebar` (distribution `nava-rebar`) from PyPI through the `review` extra, so no local checkout of the library is required.

The Repository Intelligence profile and the demo tasks mount a set of test repositories read-only from the same parent directory. Check out the ones you plan to exercise, or remove the mounts you do not need from `compose.override.yaml`.

| Test repository | Used by |
| --- | --- |
| `agentic-demo-service` | Repository Intelligence and the demo tasks |
| `agentic-demo-greenfield`, `agentic-demo-ts`, `agentic-demo-tf`, `agentic-demo-java`, `agentic-demo-php`, `agentic-demo-ruby` | The Repository Intelligence profile, mounted read-only |

Two host tools are optional and only needed for specific paths.

| Optional tool | Needed for |
| --- | --- |
| kubeconfig under `~/.kube/config` | The OpenHands sandbox agent, used by live-agent demo runs |
| Vendored `acli` binary | Jira Cloud writes. The worker image build installs it when `INSTALL_ACLI=true`. See [Vendored binaries](#vendored-binaries) |

## First-time setup

Run these once from the repository root.

1. Copy the environment file with `cp .env.example .env`, then fill in the placeholders. See [Configuration](#configuration). Never commit `.env`.
2. Copy the integration source configs with `cp config/local/github.yaml.example config/local/github.yaml`, `cp config/local/jira.yaml.example config/local/jira.yaml`, and `cp config/local/integrations.yaml.example config/local/integrations.yaml`, then edit the copies. See [Integration credentials](#integration-credentials). Never commit the `config/local/*.yaml` operator files. If you set `TICKET_BACKEND=rebar`, also copy `config/local/tickets.yaml.example` to `config/local/tickets.yaml`. See [Ticket and source-control backends](#ticket-and-source-control-backends).
3. Copy the gate selection config with `cp config/local/gates.yaml.example config/local/gates.yaml`. It is required for any run that evaluates gates. Copying `config/local/gate_engine.yaml.example` to `config/local/gate_engine.yaml` is optional and adds an external gate engine. See [Configurable gates](#configurable-gates).
4. Provision review-verdict signing, which is on by default. Generate a key and trust root and set the signing variables in `.env` by following the local setup in [docs/review-verdict-signing.md](docs/review-verdict-signing.md). Without this the review worker fails closed at startup. To skip signing, set `REVIEW_SIGNING_ENABLED=false`.
5. Install the locked dependencies into `.venv` with `uv sync --locked --all-extras`, then activate it with `source .venv/bin/activate`. `uv.lock` pins every package with its hashes, and `--locked` refuses a lock that no longer matches `pyproject.toml`. See [CONTRIBUTING.md](CONTRIBUTING.md) for changing a dependency.
6. Confirm the toolchain with `inv ci`, which runs the lint, the hermetic tests, and the evaluation gate.
7. Choose a Bedrock connectivity provider in `.env` and open the path with `inv tunnel-start`. See [Bedrock connectivity](#bedrock-connectivity).
8. Validate the Bedrock path and local reachability with `inv connectivity-check`.
9. Build the images, apply database migrations, and start the stack with `inv local-up`.
10. Register the Temporal reap and intake schedules with `inv schedules-install`. They persist in Temporal's database, so run it again only after the volumes are wiped or a schedule setting changes.
11. Publish the prompt library to Langfuse with `inv prompts-seed`. Without it each stage falls back to the static text in `services/prompts/library.py` and logs a prompt fetch warning on every run.
12. Confirm everything is healthy with `inv smoke-test`.
13. Load allowlisted seed data with `inv seed` when you need demo fixtures.

## Deploying to AWS

These steps stand up the AWS infrastructure for one account from a workstation: the tiered network, the private Bedrock path, the Fargate jumphost, the data tier, and the secret store. Run them from the repository root unless a step changes directory, and pass the `agentic-platform` profile, or your own, to every AWS command. The platform services do not run in AWS yet. The container-platform services feature on the [roadmap](ROADMAP.md) adds them, together with the deployment steps that migrate the database and start the services.

1. Create the state bucket and the access-log bucket once per account. Run `cd infra/terraform/bootstrap`, copy `terraform.tfvars.example` to `terraform.tfvars` only to override a default, then run `terraform init` and `AWS_PROFILE=agentic-platform terraform apply`. This root keeps its own state locally. See the [bootstrap README](infra/terraform/bootstrap/README.md).
2. Write the backend settings for the composition root with `terraform output -raw backend_hcl > ../examples/private-bedrock/backend.hcl`, and record the access-log bucket name from `terraform output -raw access_log_bucket`. The file belongs in `infra/terraform/examples/private-bedrock`, next to the committed `backend.hcl.example` that shows its shape, so you can also copy the example and fill in the bucket by hand. Never commit `backend.hcl`.
3. Choose the network shape. Run `cd ../examples/private-bedrock`, then copy `greenfield.tfvars.example` to `greenfield.tfvars` to create the VPC, or `existing-network.tfvars.example` to `existing-network.tfvars` to reuse one. Set `access_log_bucket`, `region`, and `name_prefix`. Never commit the tfvars files.
4. Choose the services in the same tfvars file by setting each toggle to `true`. `create_aurora`, `create_cache`, `create_clickhouse_storage`, and `create_langfuse_bucket` add the data tier, and `create_secret_store` adds the secret store with the per-service database credentials and their rotation. `create_rebar_ticket_store` adds the bucket the rebar ticket store syncs to, with its reader and writer roles. Set `rebar_ticket_store_trusted_principals` to the principals that assume those roles and `rebar_ticket_store_admin_principals` to the operator that seeds and empties the bucket. The data tier and the secret store together cost a few dollars a day while they run. See the [composition root README](infra/terraform/examples/private-bedrock/README.md) for every toggle.
5. Initialize the remote state with `terraform init -backend-config=backend.hcl`. A working directory that already holds a local `terraform.tfstate` from an earlier apply moves it into the bucket once with `terraform init -migrate-state -backend-config=backend.hcl`. Confirm with a plan that no changes are pending, then delete the local `terraform.tfstate` and `terraform.tfstate.backup`.
6. Review the change set with `AWS_PROFILE=agentic-platform terraform plan -var-file=greenfield.tfvars`, then apply it with `AWS_PROFILE=agentic-platform terraform apply -var-file=greenfield.tfvars`. Use `existing-network.tfvars` in both commands for an existing network. With every service enabled the apply creates about 220 resources and takes 15 to 20 minutes, most of it Aurora and ElastiCache. A second plan reports no changes.
7. Return to the repository root and push the jumphost image to the repository in the `jumphost_ecr_repository_url` output with `inv publish-jumphost-image --tag v1`. The tag must match `jumphost_image_tag`.
8. Record the `bedrock_invoke_policy_arn` output. It grants `bedrock:InvokeModel` and is attached to the role of each platform service that calls Bedrock once those services run in AWS. A workstation keeps signing with the profile from the prerequisites, which already needs `bedrock:InvokeModel`, so nothing is attached at this step for local use.
9. With the secret store and Aurora enabled, and with `.venv` active, create the per-service database roles and databases before anything connects to the cluster with `inv db-bootstrap-roles --function-name "$(AWS_PROFILE=agentic-platform terraform -chdir=infra/terraform/examples/private-bedrock output -raw role_bootstrap_function_name)"`. The task signs with `AWS_PROFILE` from `.env`. The first scheduled rotation fails until the roles exist.
10. With the secret store enabled, set each external secret, for example `aws secretsmanager put-secret-value --profile agentic-platform --secret-id agentic-platform/external/github-token --secret-string file://github-token.txt`. The [secret-store README](infra/terraform/secret-store/README.md) lists every secret, which ones Terraform generates, and how each one rotates.
11. With the ticket store enabled, set `store.bucket`, `roles.read_only`, and `roles.write` in `config/local/tickets.yaml` from the `rebar_ticket_store_bucket`, `rebar_ticket_store_reader_role_arn`, and `rebar_ticket_store_writer_role_arn` outputs. Add the `rebar-tickets-reader` and `rebar-tickets-writer` profiles to `~/.aws/config` with those role ARNs, as the [rebar-ticket-store README](infra/terraform/rebar-ticket-store/README.md) shows. Point the store at the bucket with `inv rebar-store-remote`, adding `--local` when the store lives on this host, then seed it once with `git -C <store> push origin tickets`. Set `AWS_USE_FIPS_ENDPOINT=true` for that push when FIPS endpoints are required.
12. Point the local stack at the deployed jumphost. Set `BEDROCK_CONNECTIVITY_PROVIDER=jumphost`, `JUMPHOST_NAME` to the `jumphost_name` variable, which defaults to `bedrock-jumphost`, and `AWS_PROFILE` and `AWS_REGION` to the target account in `.env`.
13. Open the tunnel with `inv tunnel-start --detach` and confirm the private Bedrock path with `inv connectivity-check`.

For a later change, run the plan and apply from step 6 again. Read the breaking changes in [CHANGELOG.md](CHANGELOG.md) before applying a new release, because some releases need a `terraform init -upgrade` or a one-time state step.

### Tearing down the AWS infrastructure

The composition root protects its data by default, so a destroy needs a preparation apply first. The bootstrap root stays, because it holds the state of every other root.

1. Stop the tunnel with `inv tunnel-stop`, then stop any jumphost task that is still running with `aws ecs stop-task --cluster agentic-platform-bedrock-jumphost --task <task arn>`. A running task holds a network interface that blocks the subnet delete.
2. Empty the Langfuse bucket and the rebar ticket store bucket, including every object version, when anything has written to them. Empty the ticket store bucket as a principal in `rebar_ticket_store_admin_principals`, because its policy denies object access to everyone else. The bucket is versioned and refuses to delete while it holds objects.
3. Set `aurora_deletion_protection = false` in the tfvars file. In a disposable environment also set `aurora_skip_final_snapshot = true` to delete the cluster without a final snapshot, and `secret_recovery_window_days = 0` so the secret names can be created again at once. Otherwise each name stays reserved for 30 days.
4. Apply the change with `AWS_PROFILE=agentic-platform terraform apply -var-file=greenfield.tfvars`. Terraform reads these settings from state when it destroys, so this apply must come first.
5. Destroy with `AWS_PROFILE=agentic-platform terraform destroy -var-file=greenfield.tfvars`.

The destroy removes the jumphost image and the external secret values, so a later deployment repeats steps 7 and 10. The KMS keys wait 30 days before deletion and can be restored with `aws kms cancel-key-deletion` during that time. A final snapshot, when one is taken, and the ClickHouse file system backups in the `aws/efs/automatic-backup-vault` vault remain after the destroy and are deleted separately.

## Vendored binaries

Two binaries live under `vendor/bin` and are provisioned outside the Python install.

`acli` is the Atlassian CLI used for Jira Cloud writes, so only a Jira Cloud ticket source needs it. The worker images install it when `INSTALL_ACLI=true` is set in `.env`, and `inv local-up` then runs `scripts/fetch_acli.py` during the build. On Linux the script downloads the versioned Debian package from Atlassian's apt pool, verifies it against the SHA-256 committed in the script, and extracts the binary to `/app/vendor/bin/acli`. The pinned release is 1.3.39. Another `ACLI_VERSION`, or an `ACLI_DOWNLOAD_URL` mirror, needs `ACLI_SHA256`, and the build refuses a Linux download without a digest. Without acli, the Jira Cloud path fails with a message that names `INSTALL_ACLI`. To place the binary on the host for host-side runs, use `inv fetch-acli`, which writes `vendor/bin/acli`. Atlassian publishes the macOS binary only as `latest`, so a macOS fetch is verified only when `ACLI_SHA256` is set.

`codebase-memory-mcp` is an optional Repository Intelligence provider that supplies file-level reference and caller candidates. It has no release binary and is built from its upstream MIT-licensed source at `https://github.com/DeusData/codebase-memory-mcp`. Run `inv cbm-build`, which clones the upstream repository at the pinned tag and builds the binary into `vendor/bin/codebase-memory-mcp`. Pass `--source <path>` to build from an existing local checkout for a closed environment, or `--ref <tag>` to build a different upstream version. The Repository Intelligence services mount that path read-only and drive the binary as a per-call subprocess, so CBM engages automatically once the binary is present. When the binary is absent the provider is skipped and the stack runs on its other providers.

## Configuration

`.env` is grouped by concern. The defaults in `.env.example` work for a local run except where a value is specific to your account or environment.

| Group | Keys | What to set |
| --- | --- | --- |
| Postgres | `POSTGRES_*`, `DATABASE_URL`, `ALEMBIC_DATABASE_URL` | Defaults work locally. Both URLs target the `postgres` service, and a host-run task rewrites the address to the published port |
| Temporal | `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` | Defaults work locally |
| Bedrock | `AWS_PROFILE`, `PLATFORM_AWS_PROFILE`, `AWS_REGION`, `BEDROCK_CONNECTIVITY_PROVIDER`, `BEDROCK_ENDPOINT_URL`, provider keys | Set your profile and provider. See [Bedrock connectivity](#bedrock-connectivity) |
| LiteLLM | `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY` | Set a local master key |
| Review (rebar) | `REBAR_LLM_*`, `REVIEW_ATTESTATION_SECRET` | Point at LiteLLM, set an attestation secret for the HMAC fallback |
| Review signing | `REVIEW_SIGNING_ENABLED`, `REVIEW_SIGNING_KEY_PATH` or `REVIEW_SIGNING_KEY`, `REVIEW_TRUST_ROOT_PATH` | On by default. Provision a signing key and trust root, or set enabled to false. See [docs/review-verdict-signing.md](docs/review-verdict-signing.md) |
| Authorship signing | `AUTHORSHIP_SIGNING_ENABLED`, `AUTHORSHIP_IDENTITY_ID`, `AUTHORSHIP_SIGNING_KEY_PATH` or `AUTHORSHIP_SIGNING_KEY` | On by default, active only on the rebar backend with an identity set. Jira does not participate. See [docs/authored-identity-signing.md](docs/authored-identity-signing.md) |
| Backends | `TICKET_BACKEND`, `SOURCE_CONTROL_BACKEND`, `SOURCE_CONTROL_LOCAL_*` | Select the ticket and source-control backends. Defaults are Jira and GitHub. See [Ticket and source-control backends](#ticket-and-source-control-backends) |
| Admission | `MAX_CONCURRENT_RUNS`, `EXECUTION_ORPHAN_GRACE_SECONDS` | Cap on runs in flight and the grace window before a dead run's slot is reclaimed. Defaults work locally |
| Dependency discovery | `DEPENDENCY_DETECTION_ENABLED`, `DEPENDENCY_REPLAN_ATTEMPTS` | Enable cross-ticket dependency discovery and set the re-plan attempt cap. Defaults to off. See [Cross-ticket dependency discovery](#cross-ticket-dependency-discovery) |
| Integration tokens | `GITHUB_TOKEN`, `JIRA_API_TOKEN`, `JIRA_PAT` | The secrets named by `token_ref` in `github.yaml` and `jira.yaml`. See [Integration credentials](#integration-credentials) |
| Jira tooling | `JIRA_ACLI_PATH`, `INSTALL_ACLI`, `ACLI_VERSION`, `ACLI_DOWNLOAD_URL`, `ACLI_SHA256` | Path and provisioning for the vendored acli binary |
| Policy and observability | `OPA_URL`, `LANGFUSE_*`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Defaults work locally |

The non-secret integration settings, including the repository and project allowlists, live in the `config/local` YAML files rather than `.env`. See [Integration credentials](#integration-credentials). The gate selection and the optional AI-CLI engine live in `config/local/gates.yaml` and `config/local/gate_engine.yaml`. See [Configurable gates](#configurable-gates).

The tracked files under `config/platform` hold configuration the platform owns and every environment shares, so they carry no `.example` template and are not edited per operator. `litellm-config.yaml` and `litellm_redaction.py` configure the LiteLLM gateway, and `rebar-review.toml` holds the rebar review model classes. The per-environment directories such as `config/local` hold only operator-edited files, which are git-ignored and shipped as `.example` templates.

### Integration credentials

Non-secret integration config lives in the git-ignored YAML files under `config/local`, and the secret tokens live in `.env`. The YAML never holds a secret. It names one with `token_ref`, and the platform reads the value from the environment variable of that name locally and from Secrets Manager in AWS.

For example, `github.yaml` sets `token_ref: GITHUB_TOKEN` and `.env` sets `GITHUB_TOKEN=<your token>`. Do not paste a token into the YAML. Set it in `.env` under the key the config names. Every `token_ref` you use must have a matching key in `.env`. The repository and project allowlists also live in these YAML files under `allowlist`, so `.env` holds only the secrets.

### GitHub source configuration

`config/local/github.yaml` maps each repository to the host, owner, and token used to reach it. Copy the committed template with `cp config/local/github.yaml.example config/local/github.yaml` and edit the copy. The file is git-ignored, so your edits never conflict with a repository update.

`allowlist` lists the repositories the platform may reach and is the authorization boundary. `hosts` names a connection once, its REST API base and its git base, and reuses it under a short key. `default` sets the host, owner, and token reference applied to any allowlisted repository you do not list under `repositories`. A `repositories` entry overrides the host, owner, or token for one repository, and anything it leaves unset inherits the default. A github.com repository that uses the default identity needs no entry.

```yaml
allowlist:
  - agentic-demo-service
  - agentic-demo-greenfield

hosts:
  github-com:
    api_url: https://api.github.com
    git_base_url: https://github.com
  enterprise:
    api_url: https://ghe.example.com/api/v3
    git_base_url: https://ghe.example.com

default:
  host: github-com
  owner: agentic-demo
  token_ref: GITHUB_TOKEN

repositories:
  billing-svc:
    token_ref: BILLING_TOKEN
  legacy-core:
    host: enterprise
    owner: platform
```

### Pull request base branch

Every execution opens its pull request against `github_default_base_branch`, which defaults to `main`. A ticket overrides that for itself with a `base:<branch>` label, so a change that only makes sense on top of an unmerged pull request can target that pull request's branch instead. Rebar tags and Jira labels both feed this, so the convention is the same on either backend.

```
base:alice/PLAT-482
```

The setting applies to every ticket that carries no `base:` label, so existing tickets are unaffected. Repeating one value is a duplicate and resolves to it.

A declaration that cannot be honoured holds the ticket and raises the same unroutable alert as a ticket that cannot be routed to a repository. That covers two cases, a declared branch that does not exist on the remote, and two `base:` labels naming different branches. Neither falls back to the default branch, because an execution rooted at the wrong branch would produce a pull request whose diff is not the one the ticket asked for.

### Jira source configuration

`config/local/jira.yaml` maps each project to the site, account, token, and board used to reach it. Copy the committed template with `cp config/local/jira.yaml.example config/local/jira.yaml` and edit the copy. The file is git-ignored, so your edits never conflict with a repository update.

`allowlist` lists the projects the platform may reach and is the authorization boundary. `acceptance_field` names the Jira custom field that holds acceptance criteria, and an empty value skips it. `sites` names a connection once, its site url and an `auth_mode` of `cloud` or `datacenter`, and reuses it under a short key. `default` sets the site, account fields, token reference, and board applied to any allowlisted project you do not list under `projects`. A `projects` entry overrides the site, account, token, or board for one project, and anything it leaves unset inherits the default. A cloud identity carries an `email` and a `token_ref` for the API token, and a datacenter identity carries a `token_ref` for the PAT. `default_repo` names the repository the Jira intake reconciler targets when a ticket names none, and it must be in the GitHub allowlist. `intake_label` gates intake so only issues carrying that Jira label enter the platform, and an empty value admits every not-Done issue in the project. It resolves per project with the default as the fallback, the same as the board and `default_repo`.

`statuses` lists the board's available status names, and `flow` maps the two moves the platform drives, `work_started` on the work claim and `ready_for_review` on the draft pull request, onto concrete statuses. Every status in `flow` must appear in `statuses` or the config fails closed at load. Leave a `flow` stage unset to skip it. A board with no review status leaves `ready_for_review` unset, keeps the ticket in its `work_started` status, and relies on the pull request linkback as the review signal. The platform never invents a status and never advances a ticket to a terminal status such as Done.

```yaml
allowlist:
  - AGENTDEV

acceptance_field: ""

sites:
  cloud:
    url: https://your-org.atlassian.net
    auth_mode: cloud
  datacenter:
    url: https://jira.example.com
    auth_mode: datacenter

default:
  site: cloud
  email: agent@example.com
  token_ref: JIRA_API_TOKEN
  board: AGENTDEV
  default_repo: agentic-demo-service
  statuses: [Backlog, Selected for Development, In Progress, In Review, Done]
  flow:
    work_started: In Progress
    ready_for_review: In Review

projects:
  PLATFORM:
    site: datacenter
    token_ref: JIRA_PAT
    board: PLATFORM
    statuses: [Backlog, Selected for Development, In Progress, Done]
    flow:
      work_started: In Progress
```

### Integration delivery modes

`config/local/integrations.yaml` sets how each inbound integration is delivered. Copy the committed template with `cp config/local/integrations.yaml.example config/local/integrations.yaml` and edit the copy. `modes` names the three integrations `pr_closed`, `jira_intake`, and `ci_status`, and gives each a mode of `polling`, `webhook`, or `hybrid`. `polling` reconciles on the level-triggered poll only, `webhook` accepts inbound deliveries only, and `hybrid` runs both with the poll as the fallback. `config_version` records the schema version of the file. Every inbound path is duplicate-safe through the coordination idempotency ledger, so a webhook and a poll for the same event converge on one outcome.

### Ticket and source-control backends

The platform reads work from a ticket backend and delivers changes to a source-control backend, and each is selected independently in `.env`. `TICKET_BACKEND` is `jira` (default) or `rebar`, and `SOURCE_CONTROL_BACKEND` is `github` (default) or `local`. Leaving both unset behaves exactly as a Jira plus GitHub deployment, so [GitHub source configuration](#github-source-configuration) and [Jira source configuration](#jira-source-configuration) apply unchanged. The two axes are orthogonal, so a rebar store can target GitHub and a Jira project can target a local folder.

`MAX_CONCURRENT_RUNS` caps the runs in flight at once across every ticket source, and excess candidates are deferred to a later intake pass. At the start of each pass the reconciler reclaims the slot of any non-terminal execution whose workflow is missing or closed, once the execution is older than `EXECUTION_ORPHAN_GRACE_SECONDS`, so a crashed run does not hold a slot forever.

#### Rebar ticket backend

Set `TICKET_BACKEND=rebar` to track work in a git-backed rebar ticket store instead of Jira. Copy the committed template with `cp config/local/tickets.yaml.example config/local/tickets.yaml` and edit the copy. The file is git-ignored and is required whenever the rebar backend is selected. `inv config-check` reports whether it is current.

`backend.repo_root` points at the rebar store working tree. `backend.project` labels the ticket source, and `backend.default_repo` names the repository a ticket targets when it names none. `backend.status_flow` maps the platform's lifecycle stages `work_started`, `ready_for_review`, and `terminal` onto the store's own statuses, drawn from rebar's vocabulary of `idea`, `open`, `in_progress`, `closed`, `blocked`, `cancelled`, and `deleted`. The `poll` section runs change detection under an authenticated reader identity restricted to approved roles and redacts any field outside `allowed_fields`. The `store`, `roles`, `provenance`, and `isolation` sections keep the ticket store's object-store location and credentials separate from the evidence and audit stores, and the loader fails closed on any overlap. See [docs/tickets-store-credentials.md](docs/tickets-store-credentials.md) for the credential fields and [docs/rebar-ticket-api.md](docs/rebar-ticket-api.md) for the store API the backend calls.

Seed the store from an operator-provided Excel export with `inv import-rebar-tickets --xlsx <path>`, which imports through rebar's own capability. Add `--reset` to wipe and re-initialize the store first, `--status` to import only selected statuses, and `--limit` to cap the count.

The `rebar-intake` schedule starts a draft-PR execution for each ready ticket in the store. Install it with `inv intake --schedule` or `inv schedules-install`, or run one pass with `inv intake`. `inv rebar-changes` reports new tickets, field changes, and status changes since the last intake pass. It reads the intake cursor without saving it, so a report never hides a ticket from the next intake pass.

The store can sync to S3 through the `git-remote-s3` helper, which the `review` extra installs. The `store` section of `tickets.yaml` names the bucket, the prefix, and two AWS profiles. `inv rebar-store-remote` points the store's git remote at the bucket so that it fetches under `reader_profile` and pushes under `writer_profile`. When the remote is an S3 URL, the activity worker validates the isolation rules in `tickets.yaml` and the remote at startup and refuses to start on a mismatch. A store with no remote stays local-only and skips the check.

Locally the store syncs to the `rebar-tickets` bucket in MinIO. Copy `config/local/rebar-store-aws-config.example` to `config/local/rebar-store-aws-config`, which defines both profiles against MinIO with the `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` values, then run `inv rebar-store-remote` and push the store once with `docker compose exec activity-worker git -C <repo_root> push origin tickets`. Both local profiles use the MinIO root credentials, so the read and write split is enforced only in AWS.

#### Local source-control backend

Set `SOURCE_CONTROL_BACKEND=local` to deliver changes to a git folder on disk instead of GitHub. `SOURCE_CONTROL_LOCAL_ROOT` is the parent directory that holds the target repositories, and `SOURCE_CONTROL_LOCAL_REPOS` is an optional comma-separated allowlist where an empty value discovers every git repository under the root. The local backend commits the change to an `agent/<task_id>` branch in the repository, performs no push, and opens no pull request. It writes a `SUMMARY.md` and a `change.patch` per run under `SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT`, along with a done ledger, and an empty artifacts root defaults to `<root>/.agent-publish`.

A local-backend run has no merge webhook, so an operator completes it. When the run reaches the promoted branch it parks at `AWAITING_PR_CLOSE`. Run `inv operator-done --workflow-id ID --repo REPO --branch agent/<task_id>` to verify the published artifacts and signal completion, which advances the ticket to its terminal status. Add `--closed-unmerged` to complete as closed-unmerged rather than merged. `SOURCE_CONTROL_LOCAL_OPERATORS` is the comma-separated allowlist of operators authorized to run the command, matched against the operating system account, and the workflow id prints when the pipeline starts.

`inv operator-done` runs on the host while the workers run in containers, so it rewrites the container mount roots in `SOURCE_CONTROL_LOCAL_ROOT` and `SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT` to the host directories that back the compose mounts before it verifies artifacts. The `/repos` mount resolves to the parent directory of the platform checkout and the `/workspaces` mount resolves to `.agent-workspaces` inside it, matching `compose.override.yaml`. A value already set to a host path is left unchanged.

### Cross-ticket dependency discovery

Two tickets admitted together can turn out to depend on each other even when neither ticket declares it. Set `DEPENDENCY_DETECTION_ENABLED=true` to have a run detect during planning whether its change depends on another in-flight run, through file-impact overlap and unresolved-symbol references against the runs still in flight. On a hit the run records the dependency on the ticket store, pauses on a dependency checkpoint without holding compute, and resumes only once the blocking ticket reaches certified terminal completion. On resume it re-captures its base and re-plans against the updated tree, then re-verifies. A dependency-cycle guard fails a mutual dependency rather than looping, and `DEPENDENCY_REPLAN_ATTEMPTS` bounds the re-plan attempts before a run whose dependency stays unresolved fails with a recorded reason.

Detection is off by default, so a deployment that does not set the flag behaves exactly as before. Each run's predicted touch set is held in the platform database, so overlap detection is backend-neutral. Recording a dependency, reading a ticket's blockers, and reading its dependents are ticket-graph operations each backend supplies natively. The rebar backend uses its native links and is always graph-active. A Jira deployment uses issue links and is graph-active only for a project that configures `dependency_link_type` with the name of an existing Jira link type, for example `Blocks`, so the blocking issue blocks the dependent issue. A project that leaves it unset publishes its touch set but records nothing and pauses on nothing. Projects that share a repository must use the same link type.

### Rebar review gate models

`pyproject.toml` carries the `[tool.rebar.llm]` table that maps rebar's three model classes onto the same Bedrock Claude tiers as `config/platform/litellm-config.yaml`. No class declares a fallback leg, so a tier that cannot reach Claude fails once rebar exhausts its retry budget rather than answering from a different model family. The table widens that budget past rebar's default because there is no leg to fall to. `[tool.rebar.verify]` sits alongside it and carries the close and claim gate flags.

Both tables live in `pyproject.toml` because that is a tracked file rebar discovers on its own. A gate reads its config from an attested snapshot materialised with `git checkout-index`, which holds only tracked files, so an untracked project `rebar.toml` is invisible to it and rebar falls back to its built-in model-class defaults. Those point at direct Anthropic and cannot authenticate against this deployment, which surfaces as an authentication error for a provider the project never configured and leaves the gate returning `INDETERMINATE` rather than a verdict. A local `rebar.toml` also takes precedence over `pyproject.toml` in rebar's discovery walk, so one present in a working tree shadows these tables entirely. Do not create one.

`config/platform/rebar-review.toml` holds the same model classes for the two callers that name a config file by path rather than relying on discovery. `compose.yaml` names it for the review worker through `REBAR_LLM_CONFIG_FILE`, and `inv rebar-review-plan` and `inv rebar-review-code` name it for host runs. It layers over the discovered table with identical values, so the merge changes nothing. `tests/unit/test_rebar_review_config.py` asserts the two declarations stay equal.

Naming the file is now a convenience rather than a requirement, so `rebar review-plan`, `rebar review-code` and `rebar transition ... closed` all resolve the Bedrock classes when run directly.

## Bedrock connectivity

`inv tunnel-start` and `inv connectivity-check` dispatch to the provider named by `BEDROCK_CONNECTIVITY_PROVIDER`. Every provider satisfies the same reachability contract, so the platform code runs unchanged when you swap providers. Every path terminates at a private endpoint, so inference stays inside the compliance boundary. There is no public-endpoint provider.

| Provider | Path | Required config |
| --- | --- | --- |
| `jumphost` (default) | SSM port-forward through an on-demand jump-host | `JUMPHOST_NAME`, `JUMPHOST_ENV`, `JUMPHOST_CONNECT_SCRIPT`, `JUMPHOST_SESSION_DURATION_SECONDS`, `JUMPHOST_KEEPALIVE_SECONDS` |
| `ssh-bastion` | SSH local forward through a bastion | `SSH_BASTION_TARGET` |
| `direct` | No tunnel. The caller originates inside the VPC | `BEDROCK_ENDPOINT_URL` optional |

`BEDROCK_ENDPOINT_URL` is the injected endpoint used by boto3 and the Bedrock model client. An empty value uses the default regional resolver. A project with no interface endpoint can apply the `infra/terraform/vpc-endpoints` module to provision one, or apply the `infra/terraform/examples/private-bedrock` composition to stand up the whole private path at once.

Set `AWS_USE_FIPS_ENDPOINT=true` in a deployed environment to select FIPS-validated service endpoints on every AWS SDK path, the native review Bedrock client, the LiteLLM gateway, the evidence object store, and the rebar ticket sync. The variable is wired into the worker and LiteLLM containers and read by botocore, so it covers all of them at once. Leave it false locally, because the tunnel and MinIO use custom endpoints that have no FIPS variant.

### Credentials in the containers

The LiteLLM gateway and the review worker mount `~/.aws` read-only and sign with `AWS_PROFILE`. They carry no AWS CLI, so a profile that resolves through `credential_process` or another helper binary fails there with an error naming the missing program, even though the same profile works on the host. Compose also reads a shell export ahead of `.env`, so a profile exported by your shell startup files reaches the containers regardless of what `.env` says.

Set `PLATFORM_AWS_PROFILE` to the profile the containers should use when it differs from the one your shell exports. Name a profile the mounted `~/.aws` resolves on its own, such as one holding static keys or an SSO profile with a cached token. The host keeps its own `AWS_PROFILE` unchanged. Leave `PLATFORM_AWS_PROFILE` empty to pass `AWS_PROFILE` through.

Host-side tooling keeps reading `AWS_PROFILE`, so `inv tunnel-start` and `inv connectivity-check` report the host profile while the containers sign with the one you set here. The two names differ by design whenever the override is in use.

### Reaching the private endpoint

The `vpc-endpoints` module provisions a `bedrock-runtime` interface endpoint that admits 443 only from the in-VPC subnet CIDRs you pass, with private DNS on. That means `bedrock-runtime.<region>.amazonaws.com` resolves to the endpoint's private IPs for any caller inside the VPC and is unreachable from outside it.

- From in-VPC compute (an EKS pod or an EC2 host in an allowed subnet): set `BEDROCK_CONNECTIVITY_PROVIDER=direct` and leave `BEDROCK_ENDPOINT_URL` empty. Private DNS routes the call over the endpoint. Attach the `bedrock-iam` module's `bedrock:InvokeModel` policy to the workload role.
- From a developer laptop: the private endpoint is not reachable directly, so forward through an in-VPC hop. Set `BEDROCK_CONNECTIVITY_PROVIDER=jumphost` for an SSM port-forward through a host in an allowed subnet, or `ssh-bastion` for an SSH local forward through a bastion, then run `inv tunnel-start` followed by `inv connectivity-check`. The hop resolves and originates the request inside the VPC.
- `inv connectivity-check` runs a TLS handshake to `bedrock-runtime` over the selected provider and reports the result, so use it to confirm any path.

### Provisioning modules

The AWS infrastructure is split into reusable Terraform modules under `infra/terraform`. `vpc-endpoints` owns the shared interface and gateway endpoints, `ecr` owns the jumphost image registry, `cloudtrail` owns the audit trail, `bedrock-iam` owns the `bedrock:InvokeModel` policy, `bedrock-jumphost-fargate` provisions an ephemeral ECS Exec hop to the private endpoint, and `evidence-store` provisions the S3 bucket for the evidence and audit object store under Object Lock. `egress` provides the outbound path through a NAT instance or a NAT Gateway, and `dns-firewall` limits the VPC to an allowlist of domains. The data tier is `aurora-postgresql` for the platform database, `elasticache` for the Valkey cache, `clickhouse-storage` for the ClickHouse file system, and `langfuse-blob-store` for the Langfuse upload bucket. `secret-store` holds the platform secrets in Secrets Manager, creates the per-service database roles through `inv db-bootstrap-roles`, and rotates the database and cache credentials. `tf-state` provisions the encrypted, versioned state bucket, and `access-logs` provisions the bucket that receives S3 server access logs from every other bucket. The `infra/terraform/bootstrap` root creates both once per account, and each composition root takes the access-log bucket name as its required `access_log_bucket`. Each is toggleable so an account reuses infrastructure it already has. The `infra/terraform/examples/private-bedrock` composition root wires them into one apply over a tiered network with public, application, management, and data subnets, keeps its state in the S3 backend, and works from zero or against an existing VPC through tfvars. After the apply, build and push the jumphost image with `inv publish-jumphost-image --tag v1` and attach the emitted `bedrock:InvokeModel` policy to the workload role. See [infra/terraform/examples/private-bedrock/README.md](infra/terraform/examples/private-bedrock/README.md).

### Evidence and audit object store

The review evidence and the audit events persist to Postgres by default. Setting `EVIDENCE_OBJECT_STORE_ENABLED=true` adds a write-once-read-many tier that persists each evidence and audit object to an S3-compatible store under Object Lock, so a record cannot be altered or deleted within its retention window, and the Postgres row records the object location. Provision the deployed bucket with the [evidence-store](infra/terraform/evidence-store) module, and set `EVIDENCE_OBJECT_STORE_ENDPOINT_URL`, `EVIDENCE_OBJECT_STORE_BUCKET`, the region, and the credentials in `.env`. In a deployed environment the credentials come from the workload role, so the access and secret keys stay empty. For a local MinIO opt-in, run `inv evidence-bucket-bootstrap` once to create the Object-Lock bucket with a default retention. The activity and review workers run a startup preflight that refuses to start when the store is enabled but the bucket is missing or unlocked.

`EVIDENCE_RETENTION_DAYS` sets how long every evidence and audit record is retained, in days. It defaults to 2555 days, which is seven years, satisfying the organization-defined AU-11 audit retention and exceeding the OMB M-21-31 floor. Adjust it in `.env` to your records-retention schedule. The value is written onto every stored record and applied as the S3 Object Lock retain-until window, and it is consumed by the evidence object-store sink, the audit object writer, and the `evidence-bucket-bootstrap` task. It applies to records written after it is set, since a stored object is immutable for its window.

Each record carries an evidence classification that selects its retention window. The recognized classifications are `standard`, `security`, and `audit`. Plan-review, code-review, and acceptance-verification verdicts and engine gate results are `standard`. Security-review verdicts and scanner findings are `security`. Audit events are `audit`. The classification is assigned by the producer and validated when the record is constructed, so an unknown value fails closed. `EVIDENCE_RETENTION_CLASSIFICATION_DAYS` overrides the baseline per classification through a comma-separated list of `classification:days` pairs, for example `security:3650,audit:3650`. Retention resolves to the record's own numeric retention when set, then the classification override, then `EVIDENCE_RETENTION_DAYS`, so a per-classification window overrides the baseline while an explicit numeric retention overrides both.

## Services and ports

`inv local-up` builds the images, runs the `db-migrate` task, starts the services below, and waits up to ten minutes for every service to report healthy. It fails and names the service when one does not. Each long-running service declares a health check. The HTTP services answer `/health` or their own readiness endpoint, the Temporal workers and `repo-index-worker` refresh a heartbeat file that `python -m services.healthcheck heartbeat` checks for freshness, and `opa`, `otel-collector`, and `tempo` build from thin images that add a static loopback probe because their upstream images carry no shell or HTTP client. Run `docker compose ps` to see each service's health. The four backing databases `agent_platform`, `temporal`, `temporal_visibility`, and `langfuse` are created in the one Postgres container.

| Service | Host port | Role |
| --- | --- | --- |
| `postgres` | 5432 | Platform, Temporal, and Langfuse databases |
| `temporal` | 7233 | Durable workflow engine |
| `temporal-ui` | 3001 | Workflow inspection UI |
| `workflow-worker`, `activity-worker` | none | Temporal workers for workflows and activities |
| `coordination-service` | 8130 | Claims, ordering, exactly-once, orphan reaping |
| `repo-intelligence` | 8120 | Context packages built from indexed repos |
| `repo-index-worker` | none | Reindexes each repo when its commit moves |
| `langgraph-service` | 8100 | Bounded reasoning stages |
| `litellm` | 4000 | Single gateway to Bedrock |
| `opa` | 8181 | Policy decisions |
| `langfuse-web` | 3000 | LLM tracing and prompt UI |
| `langfuse-worker` | none | Langfuse ingestion and background jobs |
| `clickhouse` | none | Langfuse analytics store |
| `redis` | none | Valkey cache and Langfuse ingestion queue |
| `minio` | 9090, 9091 | Shared S3-compatible object store |
| `otel-collector` | 4317, 4318 | Telemetry pipeline |

The one-shot services run under the `tasks` profile, so `up` never creates them. The `inv` tasks run them with `docker compose run --rm`, and Docker removes each container when its command exits.

| Service | Run with | Role |
| --- | --- | --- |
| `db-migrate` | `inv db-migrate`, `inv db-reset`, `inv local-up` | Applies the migrations |
| `schedule-bootstrap` | `inv schedules-install` | Registers the reap schedule and the intake schedule for the configured ticket source |

The workers and `coordination-service` check the schema revision at startup. While the database is behind the migrations their image carries, they wait and log `Run inv db-migrate` every 30 seconds. When the database holds a revision their image does not carry, as after a newer checkout migrated it, they exit.

The `registry` service runs under the `registry` profile. It is the local stand-in for ECR that gate pods pull the engine image from in image mode. Start it with `docker compose --profile registry up -d registry`.

Migrations only touch the platform's own `agent_platform` schema. Temporal and Langfuse manage their own schemas. Langfuse v4 runs as `langfuse-web` and `langfuse-worker` backed by ClickHouse, Valkey, and the shared MinIO object store, reusing the `langfuse` Postgres database.

### Langfuse backing services

`compose.yaml` pins Langfuse 4.59.0, ClickHouse 26.9.19.9, and Valkey 9.1.2 by digest. The three versions move together. ClickHouse supports a stable release line for about three months, so each ClickHouse bump needs a Langfuse release that writes to it. Langfuse 4.15.0 fails every trace insert on ClickHouse 26.8 and 26.9, and Langfuse 4.59.0 is verified against 26.9. Valkey 9.1 is the ElastiCache engine the AWS data tier runs.

The cache service keeps the name `redis` and the volume `langfuse-redis-data`, so Langfuse's `REDIS_HOST` and the queued jobs carry over. Valkey runs with the default user disabled and one RBAC user, `REDIS_USERNAME`, which defaults to `langfuse`, with the password in `REDIS_AUTH` and the access string `on ~* +@all` that Langfuse requires. `maxmemory-policy noeviction` keeps queued jobs from being evicted. The local cache does not use TLS. In AWS, ElastiCache requires TLS, and Langfuse sets `REDIS_TLS_ENABLED=true`. The RBAC user, the access string, and the eviction policy are the same in both.

Moving an existing checkout to these versions takes a backup, a pull, a one-time cleanup, and an `inv local-up`. The first start rewrites the cache snapshot in the Valkey format, which Redis cannot read, upgrades the ClickHouse data in place, and applies the Langfuse Postgres and ClickHouse migrations. None of these steps can be reversed by changing the image tags back, so take the backup first. Stopping the cache writes its snapshot to disk before the copy:

```
mkdir -p "$HOME/langfuse-backup" && docker compose stop langfuse-web langfuse-worker redis clickhouse && docker run --rm -v agentic-dev-platform_langfuse-redis-data:/cache:ro -v agentic-dev-platform_langfuse-clickhouse-data:/clickhouse:ro -v "$HOME/langfuse-backup":/dst alpine:3.24 sh -c 'cp -p /cache/dump.rdb /dst/cache-dump.rdb && tar -czf /dst/clickhouse-data.tgz -C /clickhouse .' && docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc langfuse' > "$HOME/langfuse-backup/langfuse.dump"
```

Then pull, remove the exited one-shot containers an earlier `up` left behind, and start the stack:

```
git pull && docker compose rm -f db-migrate schedule-bootstrap && inv local-up
```

No `.env` change is needed, because `REDIS_USERNAME` defaults in `compose.yaml` and `REDIS_AUTH` already exists. Valkey loads the Redis snapshot through `--rdb-version-check relaxed`, which stays in place through v0.3.x and is removed in v0.4.0.

Restoring means checking out the previous version, restoring the cache snapshot, recreating the ClickHouse volume from the archive, and restoring the `langfuse` database, then starting the stack. Run these from the previous checkout:

```
docker compose stop langfuse-web langfuse-worker redis clickhouse && docker run --rm -v agentic-dev-platform_langfuse-redis-data:/data -v "$HOME/langfuse-backup":/src:ro alpine:3.24 cp -p /src/cache-dump.rdb /data/dump.rdb
```

```
docker compose rm -f clickhouse && docker volume rm agentic-dev-platform_langfuse-clickhouse-data && docker volume create agentic-dev-platform_langfuse-clickhouse-data && docker run --rm -v agentic-dev-platform_langfuse-clickhouse-data:/dst -v "$HOME/langfuse-backup":/src:ro alpine:3.24 sh -c 'tar -xzf /src/clickhouse-data.tgz -C /dst && chown -R 101:101 /dst'
```

```
docker compose up -d --wait postgres && docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" --clean --if-exists -d langfuse' < "$HOME/langfuse-backup/langfuse.dump" && docker compose up -d
```

Without a cache backup, delete `dump.rdb` from the `langfuse-redis-data` volume instead. Redis then starts empty and Langfuse loses only the ingestion jobs queued at that moment.

The local cache refuses password-only clients. Connect with `docker compose exec redis sh -c 'valkey-cli --no-auth-warning --user "$REDIS_USERNAME" --pass "$REDIS_AUTH"'`, which reads both values from the container environment.

Migrations are applied only through the `db-migrate` task, which `inv db-migrate`, `inv db-reset`, and `inv local-up` run. The container mounts the working-tree migrations, so the applied scripts always match the checkout without an image rebuild. Running `alembic upgrade`, `downgrade`, or `stamp` directly on the host is refused, because a host run can advance the database past the revisions the container carries and leave the two out of sync. The guard covers the alembic command line. Authoring commands such as `alembic revision --autogenerate` still run on the host.

A deployed environment has no working-tree mount, so its `db-migrate` image carries the migrations baked in at build time. Rebuild or repull the `db-migrate` image whenever `database/migrations` changes, so the deployed container applies the current scripts.

## Inbound webhooks and CI

The inbound front door receives Jira, pull-request, and CI deliveries. It runs on the local Kubernetes cluster alongside the MCP gateway. Deploy both with `inv k8s-deploy`, which builds the image when passed `--build` and rolls out the `inbound-webhook` and `mcp-gateway` deployments. The front door listens on `localhost:8140` and serves `/webhook/jira`, `/webhook/pr`, and `/webhook/ci`. Forward GitHub pull-request events to it during a demo with `inv webhook-setup`, and remove the forwards with `inv webhook-teardown`.

Local CI runs under the Compose `ci` profile, which `inv local-up` does not start. Bring up Jenkins with `docker compose --profile ci up -d jenkins`, reachable at `localhost:8080`. The job checks out the target branch, runs the repository Jenkinsfile, and notifies `/webhook/ci` on completion. A parked CI wait resumes on that notification to the same result the poll would produce, and the poller stays the fallback.

## Security scanning

`inv sast` stages the files git tracks or would track, so ignored files such as `.env` stay out of every report, and scans them with Semgrep, Bandit, Trivy, and gitleaks. gitleaks also scans every commit reachable from the checked-out commit, so local refs that CI does not clone, such as stashes or agent checkpoints, stay out of the scan, and pip-audit checks every package version and hash that `uv.lock` pins, which is the set CI and both images install, against OSV, including the OpenSSF malicious-package records. The scan also lists every locked release younger than seven days with its PyPI provenance attestation, for the reviewer, without failing on it. The scan needs Docker and uv. Bandit scans the shipped code and leaves out `tests/`. Semgrep, Trivy, and gitleaks run from the digest-pinned images the platform gates use. Reports land in `.sast/`, and the CI `sast` job uploads them as an artifact.

The scan fails on any finding that [security/sast-accepted.yaml](security/sast-accepted.yaml) does not accept, and on any accepted entry that no longer matches a finding. Fix a finding in the code first. Add a register entry only when no code change can clear it, and record why in its `justification`. An entry matches on the tool, the rule, the path, and the normalized source line, so it survives a line move but not an edit to the flagged line. A secret finding matches on a digest of the line, or on the commit fingerprint for a history finding, so the register never holds the matched value.

## Verifying the stack

`inv smoke-test` checks the database and the `agent_platform` schema, the LiteLLM, OPA, and Langfuse health endpoints, and a Bedrock inference round trip through LiteLLM. `inv durability-proof` shows a workflow surviving a worker restart. `inv workflow-status` lists recent Temporal executions.

## Worker builds

`inv local-up` derives a build identifier of the form `<release version>.<UTC timestamp>`, for example `0.3.0.20261028093000`, from a digest of the sources the worker image is built from, and passes it to `docker compose build` as the `AGENT_BUILD_ID` build argument. The Dockerfile bakes it into the image as an environment variable, so every environment that runs the image reports the build it was built as, including a Kubernetes or ECS deployment that never runs `inv local-up`. A build whose sources have not changed finds its identifier in `.build-id.json` and reuses it, so a rebuild of unchanged sources consumes no new version. An `AGENT_BUILD_ID` line in `.env` overrides the baked identifier, so remove one left from an earlier checkout.

Host tasks read `.build-id.json` for the identifier the last build recorded, so a parked execution records the build that parked it and `inv resume-parked` compares against the build now deployed. `services/orchestration/suspension.py` orders two identifiers by the digit runs it reads out of them, so the timestamp suffix is what makes a later build rank above an earlier one. A commit SHA would not.

The workers register as a Temporal Worker Deployment named by `WORKER_DEPLOYMENT_NAME`, with the default versioning behaviour `PINNED`. Each execution stays on the deployment version that started it for its whole life, so a deploy mid-run replays that execution on its own build in place of failing it on a non-determinism error. A pinned execution therefore only makes progress while workers on its own version keep polling. Stop them, and its next workflow task has nowhere to run and the execution stalls until they return or something repins it. A deployment version spans every task queue its workers poll, so a pinned execution runs its review activities on the review worker of its own version rather than the newest one. Once a worker polls, it makes its version the current one, which routes executions that start afterwards to it. A worker restarted on a superseded build leaves the newer version current and goes on draining the executions pinned to its own.

`inv resume-parked` repins each execution it resumes to the current deployment version before it signals, because a signal alone leaves the execution on the build that parked it, which is the build it parked to escape. Set `WORKER_VERSIONING_ENABLED=false` and the repin is skipped along with the rest of versioning.

Replacing the workers is what strands a pinned execution, and `inv local-up` does exactly that whenever the sources have changed. A draft pull request run waits on plan approval, pull request approval, and pull request close, so it can sit open for days across several rebuilds. `inv local-up` therefore lists the running executions pinned to the outgoing build and refuses to replace the workers while any remain. Let them finish, resume the parked ones, or pass `--force` to replace the workers and leave them waiting for a worker on their own build. Reaching Temporal for that check is best effort, so a first run against a stopped stack reports what it could not read and carries on.

Worker Deployments replace Worker Versioning V1, the build-id compatibility sets that Temporal server 1.28.0 deprecates, 1.31.0 sunsets, and 1.33 removes. They need no dynamic config, so the Compose Temporal service mounts none.

A version the server keeps counts against the per-deployment version ceiling. Run `inv prune-worker-versions` to delete every version the server reports as drained, which is every version with no execution left pinned to it. The server refuses to delete a version whose task queues still have pollers, so a version whose workers are still running stays for the next pass, which the task reports. A worker that cannot make its version current exits with the server's reason.

Versioning pins the executions that start under it and nothing earlier. Drain the executions already in flight once, before the first deploy that runs versioned workers. Set `WORKER_VERSIONING_ENABLED=false` to run unversioned.

## Everyday commands

Run `inv --list` for the full set. The common tasks are below.

| Task | Purpose |
| --- | --- |
| `inv local-up`, `inv local-down` | Build, migrate, and start the stack, or stop it with volumes preserved |
| `inv logs [--service NAME]` | Follow logs for the stack or one service |
| `inv db-migrate`, `inv db-reset` | Apply migrations, or rebuild the schema then migrate |
| `inv schedules-install` | Register the reap schedule and the intake schedule for the configured ticket source |
| `inv seed` | Load allowlisted seed data |
| `inv smoke-test`, `inv connectivity-check` | Health and Bedrock path checks |
| `inv test` | Run the pytest suite |
| `inv ci` | Run lint, the hermetic test tiers, and the evaluation gate as CI does |
| `inv sast` | Run the SAST, secret, misconfiguration, and dependency scans against the accepted-findings register. Needs Docker |
| `inv terraform-test` | Run `terraform test` in each Terraform module that has a `tests` directory, as CI does. Needs Terraform 1.11 or later |
| `inv tunnel-start`, `inv tunnel-stop`, `inv tunnel-status` | Manage the Bedrock connectivity tunnel |
| `inv fetch-acli` | Vendor the Atlassian `acli` binary |
| `inv cbm-build [--source PATH] [--ref TAG]` | Build and vendor the `codebase-memory-mcp` binary, cloning upstream by default |
| `inv reap [--apply]` | Reap expired orphan artifacts, dry-run by default |
| `inv resume-parked` | Signal every execution parked for a newer agent build that this build can retry it |
| `inv prune-worker-versions` | Delete the drained versions of the Temporal worker deployment |
| `inv intake [--source jira\|rebar] [--schedule]` | Reconcile ticket intake into runs, single pass by default. `--source` defaults to `TICKET_BACKEND` and must match it. Add `--local` to run on the host instead of the worker container |
| `inv rebar-changes [--new] [--field-changes] [--status-changes]` | Report rebar ticket changes since the last intake pass without starting work or moving the intake cursor |
| `inv import-rebar-tickets --xlsx PATH [--reset] [--status S] [--limit N]` | Seed the rebar ticket store from a Jira xlsx export |
| `inv rebar-review-plan --ticket ID [--gate-ref REF]` | Run rebar's plan-review gate against the platform tier config |
| `inv rebar-review-code --base REF --head REF` | Run rebar's code-review gate against the platform tier config |
| `inv operator-done --workflow-id ID --repo REPO --branch BRANCH [--closed-unmerged]` | Complete a local source-control run by verifying artifacts and signaling the workflow |
| `inv publish-jumphost-image [--tag TAG]` | Build and push the jumphost base image to the account ECR repository |
| `inv approve-pr --workflow-id ID [--reject] [--pr-closed] [--merged]` | Release an approval or PR-close pause on a running workflow |
| `inv gate --issue KEY --gate NAME [--approve\|--reject]` | Resolve a configured human-intervention gate parked at `AWAITING_HUMAN` |

See [Recovering a stuck intake](#recovering-a-stuck-intake) for the retry, repair, and restart commands that drive a ticket after a failure.

## Recovering a stuck intake

The Jira intake reconciler turns a labeled ticket into a draft-PR execution on each pass, and the rebar reconciler does the same for a ready ticket in the store. `inv intake` runs one pass from the source `TICKET_BACKEND` names, and `inv intake --schedule` installs its recurring Temporal schedule, `jira-intake` or `rebar-intake`. Installing one removes the other source's schedule, so only one intake schedule runs. `INTAKE_SCHEDULE_ID` overrides the schedule name. When an execution fails, or a ticket needs another attempt, the commands below drive it without signalling Temporal by hand.

`inv intake --retry TICKET [--repo REPO]` clears the intake dedup for a failed ticket, marks its stale executions failed, and resets its sequence gate, so the next poll relaunches the ticket from a clean state. Add `--repo` to scope the retry to one repository when the ticket fanned out to several. It refuses while a workflow for the ticket is still running.

The two backends track admission differently, and the retry clears both cursors. Jira intake lists every open issue on each pass, so the side-effect ledger decides on its own whether a ticket is admitted again. The rebar poll instead compares each ticket against a per-ticket revision in `<repo_root>/.rebar-poll-watermark.json`, and a ticket whose revision is already recorded is reported as `rebar_poll_noop` rather than offered to intake. A retry or a restart therefore also releases the ticket from that file. The retry runs in a different process from the poll and against a shared mount whose advisory locks cannot be relied on, so it does not edit the watermark. It writes a marker file naming the ticket under `<repo_root>/.rebar-poll-releases/`, and the next pass drops the marked tickets as it loads the cursor. Each release writes its own uniquely named marker and a pass deletes only the markers it read, so a release that lands while a pass is in flight survives that pass and takes effect on the following one. Exclude `.rebar-poll-watermark.json`, `.rebar-poll-watermark.json.*.tmp`, and `.rebar-poll-releases/` from the ticket repository's version control.

Both commands read `backend.repo_root` from `config/<env>/tickets.yaml`, which must be an absolute path. `inv restart` resolves it on the host and the poll resolves it inside the worker, so a relative path names a different store in each. The config load refuses a relative path for every consumer rather than leaving the mismatch to surface during an incident.

A release reports what it could not do rather than withholding the restart. It warns when the watermark file does not exist at the configured path, when the cursor no longer holds the ticket, when the rebar store cannot be read to check readiness, and when the store does not list the ticket as ready, because the next pass would then report the ticket blocked rather than start it. Clear its blockers or move it to an open status, then run the command again. The readiness check reads the rebar store through the same throttled fetch the store uses elsewhere, so it can refresh the tickets branch on the host.

When automated CI repair exhausts its attempts, the draft-PR workflow parks at the `AWAITING_HUMAN` state and posts a comment on the ticket. Read the failing Security, Test, or Lint stage in Jenkins to see what to change, then resolve the pause with one of the following.

`inv repair ISSUE --retry` re-runs CI and the automated repair loop on the current branch head. Use it after you push a fix to the branch, or when the failure was transient. It does not re-read the ticket or re-run the coding agent.

`inv repair ISSUE --abandon` ends the parked execution as human-abandoned. It leaves the draft PR open and re-runs nothing.

`inv restart ISSUE [--repo REPO]` abandons the parked execution and re-triggers intake in one step, so a corrected ticket relaunches a fresh execution with a new coding-agent implementation. It waits for the abandoned workflow to finalize before it clears the intake state. This is the usual command after you edit a ticket that failed.

Every decision records the operator identity in the audit trail. The identity is read from the operating system account that runs the command, not a flag, so it cannot be recorded under another name.

When the review and approval gates are enabled, a workflow instead pauses at `AWAITING_PR_APPROVAL` and `AWAITING_PR_CLOSE`. Release those with `inv approve-pr --workflow-id ID`, add `--reject` to reject the pull request, and pass `--pr-closed` with `--merged` to stand in for the GitHub PR-closed webhook. The workflow id prints when the pipeline starts. On the local source-control backend a run parks at `AWAITING_PR_CLOSE` on every run rather than only under the approval gate, because there is no merge webhook, and `inv operator-done` is what releases it. See [Local source-control backend](#local-source-control-backend).

## Configurable gates

The review and scan stages run a set of gates chosen from config, not a fixed sequence. `config/<env>/gates.yaml` names a shipped bundle with `profile` (`government` or `private`) and overrides individual gates by name. Each gate entry sets `enabled`, `required`, an `after` list, an `on_block` response, and a `params` map, and `fail_fast` is set per stage. A bundle names the working default, and an explicit entry overrides one gate without restating the rest. The promotion policy is data-driven over the resulting gate verdicts, so enabling or disabling a gate changes the run without a policy edit. The policy's repository and project allowlists and its continuous-integration requirement follow the configured ticket and source-control backends, so a local source-control run that runs no continuous integration is not denied for a GitHub or Jira-specific check.

`gates.yaml` is required for any run that evaluates gates. Copy it from `gates.yaml.example` during setup. A run with reviews or policy enabled fails with a config-missing error until the file exists, and `inv config-check` reports it as not created. `gate_engine.yaml` is optional. With no `gate_engine.yaml`, no engine gate registers and the built-in rebar `security-review` gate stays the reviewer. Both operator files are git-ignored, and the repository ships only their `.example` templates.

The gate set is extended entirely through config, with one uniform mechanism for every external tool. A tool is never installed into the platform, it is named in config and run as a subprocess against a config-selected source.

- External gates. One or more external tools drive their own gates and the advisory test classifier through an invocation and output contract, configured as a list in `config/<env>/gate_engine.yaml`. Each entry selects a source (`local`, `tag`, or `zip`) and an `output_format` of `sarif` or `review-json`, so a SARIF-emitting scanner such as Semgrep, Snyk, or SonarQube and an AI-CLI reviewer such as `ai-common-workflows` are added the same way, with no packaging and no code change. See [docs/gate-engine.md](docs/gate-engine.md). The path is inert until configured, so the built-in rebar `security-review` gate is the default reviewer.

A gate entry set to `on_block: pause_for_human` parks the run at `AWAITING_HUMAN` on a blocking verdict rather than failing closed, and a promotion condition the policy classifies as human review parks the same way. The reserved `plan` gate is a config-only human checkpoint on the reviewed plan, off in both bundles. Resolve any parked human gate with `inv gate --issue KEY --gate NAME --approve` or `--reject`. Approve continues the run, reject cancels it, and the operator identity is read from the operating system account.

## Demos

The demo tasks walk the platform in four acts. Each is an `inv` task with its own flags, listed under `inv --list`.

| Task | Act |
| --- | --- |
| `inv demo-durable-task`, `inv demo-durable-counter` | Durability through a worker restart |
| `inv demo-repo-intelligence` | A briefing pack of a repo's most relevant code |
| `inv demo-coordination` | Blocking double-work and duplicate side effects |
| `inv demo-draft-pr` | Opening a draft PR through the full stack |

## System components

| Component | Backed by | Built here |
| --- | --- | --- |
| Repository Intelligence | SCIP, Zoekt, LSP, ast-grep, coverage and build-metadata readers | The router, index worker, per-repo registry, and context package |
| Orchestration | Temporal, LangGraph | The workflows, budget caps, suspend and resume, and the write pipeline |
| Coordination | PostgreSQL | The whole component, claims, exactly-once effects, ordering, and reaping |
| Integration | Jira or rebar, GitHub or a local git folder, Jenkins, OPA | The adapters, backend selection, snapshot, recheck, and linkback |
| Review | rebar as a library | The kernel wrapper, verdict and attestation logic, and the gate |
| Agent runtime | OpenHands, LiteLLM to Bedrock | The spend and time caps, request tagging, and connectivity options |

## Repository layout

```text
config/            services/          repository_indexes/  adapters/
schemas/           database/          observability/       openhands/
fixtures/          scripts/           tests/               docker/
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `inv local-up` fails on a missing mount | A sibling demo repo is not checked out next to this one. Clone it, or remove its mount from `compose.override.yaml` |
| `inv connectivity-check` fails | The Bedrock path is not open. Confirm `BEDROCK_CONNECTIVITY_PROVIDER` and its keys, then rerun `inv tunnel-start` |
| Smoke test reports a database failure | Migrations have not run. Apply them with `inv db-migrate` |
| Container build fails resolving `nava-rebar` | The PyPI index is unreachable. Confirm network access to install the `review` extra |
| Live-agent demos cannot reach the cluster | No kubeconfig at `~/.kube/config`. Provide one or skip the live-agent flag |
| A ticket is stuck at `AWAITING_HUMAN` | CI repair exhausted and the workflow parked. Fix the branch and run `inv repair ISSUE --retry`, or edit the ticket and run `inv restart ISSUE` |

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the license text and [NOTICE](NOTICE) for the copyright notice and third-party components.
