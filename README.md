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
| git | Clone this repo and its siblings | |
| An AWS profile with Bedrock access | Inference | An IAM principal with `bedrock:InvokeModel` and credentials under `~/.aws`. Set the profile and region in `.env` |

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
| Vendored `acli` binary | Jira Cloud writes. The worker image build provisions it. See [Vendored binaries](#vendored-binaries) |

## First-time setup

Run these once from the repository root.

1. Copy the environment file with `cp .env.example .env`, then fill in the placeholders. See [Configuration](#configuration). Never commit `.env`.
2. Copy the integration source configs with `cp config/local/github.yaml.example config/local/github.yaml`, `cp config/local/jira.yaml.example config/local/jira.yaml`, and `cp config/local/integrations.yaml.example config/local/integrations.yaml`, then edit the copies. See [Integration credentials](#integration-credentials). Never commit the `config/local/*.yaml` operator files. If you set `TICKET_BACKEND=rebar`, also copy `config/local/tickets.yaml.example` to `config/local/tickets.yaml`. See [Ticket and source-control backends](#ticket-and-source-control-backends).
3. Copy the gate selection config with `cp config/local/gates.yaml.example config/local/gates.yaml`. It is required for any run that evaluates gates. Copying `config/local/gate_engine.yaml.example` to `config/local/gate_engine.yaml` is optional and adds an external gate engine. See [Configurable gates](#configurable-gates).
4. Provision review-verdict signing, which is on by default. Generate a key and trust root and set the signing variables in `.env` by following the local setup in [docs/review-verdict-signing.md](docs/review-verdict-signing.md). Without this the review worker fails closed at startup. To skip signing, set `REVIEW_SIGNING_ENABLED=false`.
5. Create and activate a virtual environment with `python -m venv .venv && source .venv/bin/activate`.
6. Install the package with its development extras using `pip install -e ".[dev,reasoning]"`. Add `sandbox` for the OpenHands agent, and see [CONTRIBUTING.md](CONTRIBUTING.md) for the editable `rebar` install needed on the review path.
7. Choose a Bedrock connectivity provider in `.env` and open the path with `inv tunnel-start`. See [Bedrock connectivity](#bedrock-connectivity).
8. Validate the Bedrock path and local reachability with `inv connectivity-check`.
9. Start the stack with `inv local-up`.
10. Apply database migrations with `inv db-migrate`.
11. Confirm everything is healthy with `inv smoke-test`.
12. Load allowlisted seed data with `inv seed` when you need demo fixtures.

## Vendored binaries

Two binaries live under `vendor/bin` and are provisioned outside the Python install.

`acli` is the Atlassian CLI used for Jira Cloud writes. The worker image build runs `scripts/fetch_acli.py` during `inv local-up`, which downloads and checksum-verifies the binary into the image at `/app/vendor/bin/acli`. The download is driven by `ACLI_VERSION`, `ACLI_DOWNLOAD_URL`, and `ACLI_SHA256` in `.env`, where `ACLI_SHA256` is required and verifies the artifact whatever its source. To place the same binary on the host for host-side runs, use `inv fetch-acli`, which writes `vendor/bin/acli`.

`codebase-memory-mcp` is an optional Repository Intelligence provider that supplies file-level reference and caller candidates. It has no release binary and is built from its upstream MIT-licensed source at `https://github.com/DeusData/codebase-memory-mcp`. Run `inv cbm-build`, which clones the upstream repository at the pinned tag and builds the binary into `vendor/bin/codebase-memory-mcp`. Pass `--source <path>` to build from an existing local checkout for a closed environment, or `--ref <tag>` to build a different upstream version. The Repository Intelligence services mount that path read-only and drive the binary as a per-call subprocess, so CBM engages automatically once the binary is present. When the binary is absent the provider is skipped and the stack runs on its other providers.

## Configuration

`.env` is grouped by concern. The defaults in `.env.example` work for a local run except where a value is specific to your account or environment.

| Group | Keys | What to set |
| --- | --- | --- |
| Postgres | `POSTGRES_*`, `DATABASE_URL`, `ALEMBIC_DATABASE_URL` | Defaults work locally. The Alembic URL targets `localhost`, the app URL targets the `postgres` service |
| Temporal | `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` | Defaults work locally |
| Bedrock | `AWS_PROFILE`, `AWS_REGION`, `BEDROCK_CONNECTIVITY_PROVIDER`, `BEDROCK_ENDPOINT_URL`, provider keys | Set your profile and provider. See [Bedrock connectivity](#bedrock-connectivity) |
| LiteLLM | `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY` | Set a local master key |
| Review (rebar) | `REBAR_LLM_*`, `REVIEW_ATTESTATION_SECRET` | Point at LiteLLM, set an attestation secret for the HMAC fallback |
| Review signing | `REVIEW_SIGNING_ENABLED`, `REVIEW_SIGNING_KEY_PATH`, `REVIEW_TRUST_ROOT_PATH` | On by default. Provision a signing key and trust root, or set enabled to false. See [docs/review-verdict-signing.md](docs/review-verdict-signing.md) |
| Authorship signing | `AUTHORSHIP_SIGNING_ENABLED`, `AUTHORSHIP_IDENTITY_ID`, `AUTHORSHIP_SIGNING_KEY_PATH` | On by default, active only on the rebar backend with an identity set. Jira does not participate. See [docs/authored-identity-signing.md](docs/authored-identity-signing.md) |
| Backends | `TICKET_BACKEND`, `SOURCE_CONTROL_BACKEND`, `SOURCE_CONTROL_LOCAL_*` | Select the ticket and source-control backends. Defaults are Jira and GitHub. See [Ticket and source-control backends](#ticket-and-source-control-backends) |
| Admission | `MAX_CONCURRENT_RUNS`, `EXECUTION_ORPHAN_GRACE_SECONDS` | Cap on runs in flight and the grace window before a dead run's slot is reclaimed. Defaults work locally |
| Dependency discovery | `DEPENDENCY_DETECTION_ENABLED`, `DEPENDENCY_REPLAN_ATTEMPTS` | Enable cross-ticket dependency discovery and set the re-plan attempt cap. Defaults to off. See [Cross-ticket dependency discovery](#cross-ticket-dependency-discovery) |
| Integration tokens | `GITHUB_TOKEN`, `JIRA_API_TOKEN`, `JIRA_PAT` | The secrets named by `token_ref` in `github.yaml` and `jira.yaml`. See [Integration credentials](#integration-credentials) |
| Jira tooling | `JIRA_ACLI_PATH`, `ACLI_VERSION`, `ACLI_DOWNLOAD_URL`, `ACLI_SHA256` | Path and provisioning for the vendored acli binary |
| Policy and observability | `OPA_URL`, `LANGFUSE_*`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Defaults work locally |

The non-secret integration settings, including the repository and project allowlists, live in the `config/local` YAML files rather than `.env`. See [Integration credentials](#integration-credentials). The gate selection and the optional AI-CLI engine live in `config/local/gates.yaml` and `config/local/gate_engine.yaml`. See [Configurable gates](#configurable-gates).

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

#### Local source-control backend

Set `SOURCE_CONTROL_BACKEND=local` to deliver changes to a git folder on disk instead of GitHub. `SOURCE_CONTROL_LOCAL_ROOT` is the parent directory that holds the target repositories, and `SOURCE_CONTROL_LOCAL_REPOS` is an optional comma-separated allowlist where an empty value discovers every git repository under the root. The local backend commits the change to an `agent/<task_id>` branch in the repository, performs no push, and opens no pull request. It writes a `SUMMARY.md` and a `change.patch` per run under `SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT`, along with a done ledger, and an empty artifacts root defaults to `<root>/.agent-publish`.

A local-backend run has no merge webhook, so an operator completes it. When the run reaches the promoted branch it parks at `AWAITING_PR_CLOSE`. Run `inv operator-done --workflow-id ID --repo REPO --branch agent/<task_id>` to verify the published artifacts and signal completion, which advances the ticket to its terminal status. Add `--closed-unmerged` to complete as closed-unmerged rather than merged. `SOURCE_CONTROL_LOCAL_OPERATORS` is the comma-separated allowlist of operators authorized to run the command, matched against the operating system account, and the workflow id prints when the pipeline starts.

`inv operator-done` runs on the host while the workers run in containers, so it rewrites the container mount roots in `SOURCE_CONTROL_LOCAL_ROOT` and `SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT` to the host directories that back the compose mounts before it verifies artifacts. The `/repos` mount resolves to the parent directory of the platform checkout and the `/workspaces` mount resolves to `.agent-workspaces` inside it, matching `compose.override.yaml`. A value already set to a host path is left unchanged.

### Cross-ticket dependency discovery

Two tickets admitted together can turn out to depend on each other even when neither ticket declares it. Set `DEPENDENCY_DETECTION_ENABLED=true` to have a run detect during planning whether its change depends on another in-flight run, through file-impact overlap and unresolved-symbol references against the runs still in flight. On a hit the run records the dependency on the ticket store, pauses on a dependency checkpoint without holding compute, and resumes only once the blocking ticket reaches certified terminal completion. On resume it re-captures its base and re-plans against the updated tree, then re-verifies. A dependency-cycle guard fails a mutual dependency rather than looping, and `DEPENDENCY_REPLAN_ATTEMPTS` bounds the re-plan attempts before a run whose dependency stays unresolved fails with a recorded reason.

Detection is off by default, so a deployment that does not set the flag behaves exactly as before. Each run's predicted touch set is held in the platform database, so overlap detection is backend-neutral. Recording a dependency, reading a ticket's blockers, and reading its dependents are ticket-graph operations each backend supplies natively. The rebar backend uses its native links and is always graph-active. A Jira deployment uses issue links and is graph-active only for a project that configures `dependency_link_type` with the name of an existing Jira link type, for example `Blocks`, so the blocking issue blocks the dependent issue. A project that leaves it unset publishes its touch set but records nothing and pauses on nothing. Projects that share a repository must use the same link type.

## Bedrock connectivity

`inv tunnel-start` and `inv connectivity-check` dispatch to the provider named by `BEDROCK_CONNECTIVITY_PROVIDER`. Every provider satisfies the same reachability contract, so the platform code runs unchanged when you swap providers. Every path terminates at a private endpoint, so inference stays inside the compliance boundary. There is no public-endpoint provider.

| Provider | Path | Required config |
| --- | --- | --- |
| `jumphost` (default) | SSM port-forward through an on-demand jump-host | `JUMPHOST_NAME`, `JUMPHOST_ENV`, `JUMPHOST_CONNECT_SCRIPT`, `JUMPHOST_SESSION_DURATION_SECONDS`, `JUMPHOST_KEEPALIVE_SECONDS` |
| `ssh-bastion` | SSH local forward through a bastion | `SSH_BASTION_TARGET` |
| `direct` | No tunnel. The caller originates inside the VPC | `BEDROCK_ENDPOINT_URL` optional |

`BEDROCK_ENDPOINT_URL` is the injected endpoint used by boto3 and the Bedrock model client. An empty value uses the default regional resolver. A project with no interface endpoint can apply the `infra/terraform/vpc-endpoints` module to provision one, or apply the `infra/terraform/examples/private-bedrock` composition to stand up the whole private path at once.

Set `AWS_USE_FIPS_ENDPOINT=true` in a deployed environment to select FIPS-validated service endpoints on every AWS SDK path, the native review Bedrock client, the LiteLLM gateway, the evidence object store, and the rebar ticket sync. The variable is wired into the worker and LiteLLM containers and read by botocore, so it covers all of them at once. Leave it false locally, because the tunnel and MinIO use custom endpoints that have no FIPS variant.

### Reaching the private endpoint

The `vpc-endpoints` module provisions a `bedrock-runtime` interface endpoint that admits 443 only from the in-VPC subnet CIDRs you pass, with private DNS on. That means `bedrock-runtime.<region>.amazonaws.com` resolves to the endpoint's private IPs for any caller inside the VPC and is unreachable from outside it.

- From in-VPC compute (an EKS pod or an EC2 host in an allowed subnet): set `BEDROCK_CONNECTIVITY_PROVIDER=direct` and leave `BEDROCK_ENDPOINT_URL` empty. Private DNS routes the call over the endpoint. Attach the `bedrock-iam` module's `bedrock:InvokeModel` policy to the workload role.
- From a developer laptop: the private endpoint is not reachable directly, so forward through an in-VPC hop. Set `BEDROCK_CONNECTIVITY_PROVIDER=jumphost` for an SSM port-forward through a host in an allowed subnet, or `ssh-bastion` for an SSH local forward through a bastion, then run `inv tunnel-start` followed by `inv connectivity-check`. The hop resolves and originates the request inside the VPC.
- `inv connectivity-check` runs a TLS handshake to `bedrock-runtime` over the selected provider and reports the result, so use it to confirm any path.

### Provisioning modules

The AWS infrastructure is split into reusable Terraform modules under `infra/terraform`. `vpc-endpoints` owns the shared interface and gateway endpoints, `ecr` owns the jumphost image registry, `cloudtrail` owns the audit trail, `bedrock-iam` owns the `bedrock:InvokeModel` policy, `bedrock-jumphost-fargate` provisions an ephemeral ECS Exec hop to the private endpoint, and `evidence-store` provisions the S3 bucket for the evidence and audit object store under Object Lock. Each is toggleable so an account reuses infrastructure it already has. The `infra/terraform/examples/private-bedrock` composition root wires them into one apply and works from zero or against an existing VPC through tfvars. After the apply, build and push the jumphost image with `inv publish-jumphost-image --tag v1` and attach the emitted `bedrock:InvokeModel` policy to the workload role. See [infra/terraform/examples/private-bedrock/README.md](infra/terraform/examples/private-bedrock/README.md).

### Evidence and audit object store

The review evidence and the audit events persist to Postgres by default. Setting `EVIDENCE_OBJECT_STORE_ENABLED=true` adds a write-once-read-many tier that persists each evidence and audit object to an S3-compatible store under Object Lock, so a record cannot be altered or deleted within its retention window, and the Postgres row records the object location. Provision the deployed bucket with the [evidence-store](infra/terraform/evidence-store) module, and set `EVIDENCE_OBJECT_STORE_ENDPOINT_URL`, `EVIDENCE_OBJECT_STORE_BUCKET`, the region, and the credentials in `.env`. In a deployed environment the credentials come from the workload role, so the access and secret keys stay empty. For a local MinIO opt-in, run `inv evidence-bucket-bootstrap` once to create the Object-Lock bucket with a default retention. The activity and review workers run a startup preflight that refuses to start when the store is enabled but the bucket is missing or unlocked.

`EVIDENCE_RETENTION_DAYS` sets how long every evidence and audit record is retained, in days. It defaults to 2555 days, which is seven years, satisfying the organization-defined AU-11 audit retention and exceeding the OMB M-21-31 floor. Adjust it in `.env` to your records-retention schedule. The value is written onto every stored record and applied as the S3 Object Lock retain-until window, and it is consumed by the evidence object-store sink, the audit object writer, and the `evidence-bucket-bootstrap` task. It applies to records written after it is set, since a stored object is immutable for its window.

Each record carries an evidence classification that selects its retention window. The recognized classifications are `standard`, `security`, and `audit`. Plan-review, code-review, and acceptance-verification verdicts and engine gate results are `standard`. Security-review verdicts and scanner findings are `security`. Audit events are `audit`. The classification is assigned by the producer and validated when the record is constructed, so an unknown value fails closed. `EVIDENCE_RETENTION_CLASSIFICATION_DAYS` overrides the baseline per classification through a comma-separated list of `classification:days` pairs, for example `security:3650,audit:3650`. Retention resolves to the record's own numeric retention when set, then the classification override, then `EVIDENCE_RETENTION_DAYS`, so a per-classification window overrides the baseline while an explicit numeric retention overrides both.

## Services and ports

`inv local-up` starts the services below with the `repo-intelligence` profile. The four backing databases `agent_platform`, `temporal`, `temporal_visibility`, and `langfuse` are created in the one Postgres container.

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
| `redis` | none | Langfuse ingestion queue |
| `minio` | 9090, 9091 | Shared S3-compatible object store |
| `otel-collector` | 4317, 4318 | Telemetry pipeline |

Migrations only touch the platform's own `agent_platform` schema. Temporal and Langfuse manage their own schemas. Langfuse v4 runs as `langfuse-web` and `langfuse-worker` backed by ClickHouse, Redis, and the shared MinIO object store, reusing the `langfuse` Postgres database.

Migrations are applied only through the `db-migrate` container, which `inv db-migrate` and `inv db-reset` invoke. The container mounts the working-tree migrations, so the applied scripts always match the checkout without an image rebuild. Running `alembic upgrade`, `downgrade`, or `stamp` directly on the host is refused, because a host run can advance the database past the revisions the container carries and leave the two out of sync. The guard covers the alembic command line. Authoring commands such as `alembic revision --autogenerate` still run on the host.

A deployed environment has no working-tree mount, so its `db-migrate` image carries the migrations baked in at build time. Rebuild or repull the `db-migrate` image whenever `database/migrations` changes, so the deployed container applies the current scripts.

## Inbound webhooks and CI

The inbound front door receives Jira, pull-request, and CI deliveries. It runs on the local Kubernetes cluster alongside the MCP gateway. Deploy both with `inv k8s-deploy`, which builds the image when passed `--build` and rolls out the `inbound-webhook` and `mcp-gateway` deployments. The front door listens on `localhost:8140` and serves `/webhook/jira`, `/webhook/pr`, and `/webhook/ci`. Forward GitHub pull-request events to it during a demo with `inv webhook-setup`, and remove the forwards with `inv webhook-teardown`.

Local CI runs under the Compose `ci` profile, which `inv local-up` does not start. Bring up Jenkins with `docker compose --profile ci up -d jenkins`, reachable at `localhost:8080`. The job checks out the target branch, runs the repository Jenkinsfile, and notifies `/webhook/ci` on completion. A parked CI wait resumes on that notification to the same result the poll would produce, and the poller stays the fallback.

## Verifying the stack

`inv smoke-test` checks the database and the `agent_platform` schema, the LiteLLM, OPA, and Langfuse health endpoints, and a Bedrock inference round trip through LiteLLM. `inv durability-proof` shows a workflow surviving a worker restart. `inv workflow-status` lists recent Temporal executions.

## Everyday commands

Run `inv --list` for the full set. The common tasks are below.

| Task | Purpose |
| --- | --- |
| `inv local-up`, `inv local-down` | Start or stop the stack, volumes preserved |
| `inv logs [--service NAME]` | Follow logs for the stack or one service |
| `inv db-migrate`, `inv db-reset` | Apply migrations, or rebuild the schema then migrate |
| `inv seed` | Load allowlisted seed data |
| `inv smoke-test`, `inv connectivity-check` | Health and Bedrock path checks |
| `inv test` | Run the pytest suite |
| `inv tunnel-start`, `inv tunnel-stop`, `inv tunnel-status` | Manage the Bedrock connectivity tunnel |
| `inv fetch-acli` | Vendor the Atlassian `acli` binary |
| `inv cbm-build [--source PATH] [--ref TAG]` | Build and vendor the `codebase-memory-mcp` binary, cloning upstream by default |
| `inv reap [--apply]` | Reap expired orphan artifacts, dry-run by default |
| `inv intake [--schedule]` | Reconcile ticket intake into runs, single pass by default. Add `--local` to run on the host instead of the worker container |
| `inv import-rebar-tickets --xlsx PATH [--reset] [--status S] [--limit N]` | Seed the rebar ticket store from a Jira xlsx export |
| `inv operator-done --workflow-id ID --repo REPO --branch BRANCH [--closed-unmerged]` | Complete a local source-control run by verifying artifacts and signaling the workflow |
| `inv publish-jumphost-image [--tag TAG]` | Build and push the jumphost base image to the account ECR repository |
| `inv approve-pr --workflow-id ID [--reject] [--pr-closed] [--merged]` | Release an approval or PR-close pause on a running workflow |
| `inv gate --issue KEY --gate NAME [--approve\|--reject]` | Resolve a configured human-intervention gate parked at `AWAITING_HUMAN` |

See [Recovering a stuck intake](#recovering-a-stuck-intake) for the retry, repair, and restart commands that drive a ticket after a failure.

## Recovering a stuck intake

The Jira intake reconciler turns a labeled ticket into a draft-PR execution on each pass, and the rebar reconciler does the same for a ready ticket in the store. `inv intake` runs one pass and `inv intake --schedule` installs the recurring Temporal schedule. When an execution fails, or a ticket needs another attempt, the commands below drive it without signalling Temporal by hand.

`inv intake --retry ISSUE [--repo REPO]` clears the intake dedup for a failed issue, marks its stale executions failed, and resets its sequence gate, so the next poll relaunches the ticket from a clean state. Add `--repo` to scope the retry to one repository when the ticket fanned out to several. It refuses while a workflow for the ticket is still running.

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
