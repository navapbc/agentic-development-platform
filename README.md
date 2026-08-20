# agentic-dev-platform

A Docker-based agentic software delivery platform. Given a Jira ticket it plans, writes code, runs tests, scans for security issues, and opens a pull request against an allowlisted repository, with human approvals, a full audit trail, and observability. Parallel multi-agent execution is a design goal.

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
2. Copy the integration source configs with `cp config/local/github.yaml.example config/local/github.yaml`, `cp config/local/jira.yaml.example config/local/jira.yaml`, and `cp config/local/integrations.yaml.example config/local/integrations.yaml`, then edit the copies. See [Integration credentials](#integration-credentials). Never commit the `config/local/*.yaml` operator files.
3. Copy the gate selection config with `cp config/local/gates.yaml.example config/local/gates.yaml`. It is required for any run that evaluates gates. Copying `config/local/gate_engine.yaml.example` to `config/local/gate_engine.yaml` is optional and adds an external gate engine. See [Configurable gates](#configurable-gates).
4. Create and activate a virtual environment with `python -m venv .venv && source .venv/bin/activate`.
5. Install the package with its development extras using `pip install -e ".[dev,reasoning]"`. Add `sandbox` for the OpenHands agent, and see [CONTRIBUTING.md](CONTRIBUTING.md) for the editable `rebar` install needed on the review path.
6. Choose a Bedrock connectivity provider in `.env` and open the path with `inv tunnel-start`. See [Bedrock connectivity](#bedrock-connectivity).
7. Validate the Bedrock path and local reachability with `inv connectivity-check`.
8. Start the stack with `inv local-up`.
9. Apply database migrations with `inv db-migrate`.
10. Confirm everything is healthy with `inv smoke-test`.
11. Load allowlisted seed data with `inv seed` when you need demo fixtures.

## Vendored binaries

Two binaries live under `vendor/bin` and are provisioned outside the Python install.

`acli` is the Atlassian CLI used for Jira Cloud writes. The worker image build runs `scripts/fetch_acli.py` during `inv local-up`, which downloads and checksum-verifies the binary into the image at `/app/vendor/bin/acli`. The download is driven by `ACLI_VERSION`, `ACLI_DOWNLOAD_URL`, and `ACLI_SHA256` in `.env`, where `ACLI_SHA256` is required and verifies the artifact whatever its source. To place the same binary on the host for host-side runs, use `inv fetch-acli`, which writes `vendor/bin/acli`.

`codebase-memory-mcp` is the optional CBM provider for Repository Intelligence, still under evaluation. It is not downloaded. Build it from a separate CBM source checkout with `docker build -f docker/cbm-build.Dockerfile --target export --output type=local,dest=vendor/bin <path-to-cbm-source>`, which writes `vendor/bin/codebase-memory-mcp`. The Repository Intelligence containers mount that path read-only, and the stack runs without it when the CBM provider is not in use.

## Configuration

`.env` is grouped by concern. The defaults in `.env.example` work for a local run except where a value is specific to your account or environment.

| Group | Keys | What to set |
| --- | --- | --- |
| Postgres | `POSTGRES_*`, `DATABASE_URL`, `ALEMBIC_DATABASE_URL` | Defaults work locally. The Alembic URL targets `localhost`, the app URL targets the `postgres` service |
| Temporal | `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` | Defaults work locally |
| Bedrock | `AWS_PROFILE`, `AWS_REGION`, `BEDROCK_CONNECTIVITY_PROVIDER`, `BEDROCK_ENDPOINT_URL`, provider keys | Set your profile and provider. See [Bedrock connectivity](#bedrock-connectivity) |
| LiteLLM | `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY` | Set a local master key |
| Review (rebar) | `REBAR_LLM_*`, `REVIEW_ATTESTATION_SECRET` | Point at LiteLLM, set an attestation secret |
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

## Bedrock connectivity

`inv tunnel-start` and `inv connectivity-check` dispatch to the provider named by `BEDROCK_CONNECTIVITY_PROVIDER`. Every provider satisfies the same reachability contract, so the platform code runs unchanged when you swap providers. Every path terminates at a private endpoint, so inference stays inside the compliance boundary. There is no public-endpoint provider.

| Provider | Path | Required config |
| --- | --- | --- |
| `jumphost` (default) | SSM port-forward through an on-demand jump-host | `JUMPHOST_NAME`, `JUMPHOST_ENV`, `JUMPHOST_CONNECT_SCRIPT` |
| `ssh-bastion` | SSH local forward through a bastion | `SSH_BASTION_TARGET` |
| `direct` | No tunnel. The caller originates inside the VPC | `BEDROCK_ENDPOINT_URL` optional |

`BEDROCK_ENDPOINT_URL` is the injected endpoint used by boto3 and the Bedrock model client. An empty value uses the default regional resolver. A project with no interface endpoint can apply the optional `infra/terraform/bedrock-vpce` module to provision one.

### Reaching the private endpoint

The `bedrock-vpce` module provisions an interface endpoint that admits 443 only from the in-VPC subnet CIDRs you pass, with private DNS on. That means `bedrock-runtime.<region>.amazonaws.com` resolves to the endpoint's private IPs for any caller inside the VPC and is unreachable from outside it.

- From in-VPC compute (an EKS pod or an EC2 host in an allowed subnet): set `BEDROCK_CONNECTIVITY_PROVIDER=direct` and leave `BEDROCK_ENDPOINT_URL` empty. Private DNS routes the call over the endpoint. Attach the module's `bedrock:InvokeModel` policy to the workload role.
- From a developer laptop: the private endpoint is not reachable directly, so forward through an in-VPC hop. Set `BEDROCK_CONNECTIVITY_PROVIDER=jumphost` for an SSM port-forward through a host in an allowed subnet, or `ssh-bastion` for an SSH local forward through a bastion, then run `inv tunnel-start` followed by `inv connectivity-check`. The hop resolves and originates the request inside the VPC.
- `inv connectivity-check` runs a TLS handshake to `bedrock-runtime` over the selected provider and reports the result, so use it to confirm any path.

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
| `langfuse` | 3000 | LLM tracing UI |
| `otel-collector` | 4317, 4318 | Telemetry pipeline |

Migrations only touch the platform's own `agent_platform` schema. Temporal and Langfuse manage their own schemas.

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
| `inv reap [--apply]` | Reap expired orphan artifacts, dry-run by default |
| `inv intake [--schedule]` | Reconcile Jira intake into draft PR executions, single pass by default |
| `inv approve-pr --workflow-id ID [--reject] [--pr-closed] [--merged]` | Release an approval or PR-close pause on a running workflow |
| `inv gate --issue KEY --gate NAME [--approve\|--reject]` | Resolve a configured human-intervention gate parked at `AWAITING_HUMAN` |

See [Recovering a stuck intake](#recovering-a-stuck-intake) for the retry, repair, and restart commands that drive a ticket after a failure.

## Recovering a stuck intake

The Jira intake reconciler turns a labeled ticket into a draft-PR execution on each pass. `inv intake` runs one pass and `inv intake --schedule` installs the recurring Temporal schedule. When an execution fails, or a ticket needs another attempt, the commands below drive it without signalling Temporal by hand.

`inv intake --retry ISSUE [--repo REPO]` clears the intake dedup for a failed issue, marks its stale executions failed, and resets its sequence gate, so the next poll relaunches the ticket from a clean state. Add `--repo` to scope the retry to one repository when the ticket fanned out to several. It refuses while a workflow for the ticket is still running.

When automated CI repair exhausts its attempts, the draft-PR workflow parks at the `AWAITING_HUMAN` state and posts a comment on the ticket. Read the failing Security, Test, or Lint stage in Jenkins to see what to change, then resolve the pause with one of the following.

`inv repair ISSUE --retry` re-runs CI and the automated repair loop on the current branch head. Use it after you push a fix to the branch, or when the failure was transient. It does not re-read the ticket or re-run the coding agent.

`inv repair ISSUE --abandon` ends the parked execution as human-abandoned. It leaves the draft PR open and re-runs nothing.

`inv restart ISSUE [--repo REPO]` abandons the parked execution and re-triggers intake in one step, so a corrected ticket relaunches a fresh execution with a new coding-agent implementation. It waits for the abandoned workflow to finalize before it clears the intake state. This is the usual command after you edit a ticket that failed.

Every decision records the operator identity in the audit trail. The identity is read from the operating system account that runs the command, not a flag, so it cannot be recorded under another name.

When the review and approval gates are enabled, a workflow instead pauses at `AWAITING_PR_APPROVAL` and `AWAITING_PR_CLOSE`. Release those with `inv approve-pr --workflow-id ID`, add `--reject` to reject the pull request, and pass `--pr-closed` with `--merged` to stand in for the GitHub PR-closed webhook. The workflow id prints when the pipeline starts.

## Configurable gates

The review and scan stages run a set of gates chosen from config, not a fixed sequence. `config/<env>/gates.yaml` names a shipped bundle with `profile` (`government` or `private`) and overrides individual gates by name. Each gate entry sets `enabled`, `required`, an `after` list, an `on_block` response, and a `params` map, and `fail_fast` is set per stage. A bundle names the working default, and an explicit entry overrides one gate without restating the rest. The promotion policy is data-driven over the resulting gate verdicts, so enabling or disabling a gate changes the run without a policy edit.

`gates.yaml` is required for any run that evaluates gates. Copy it from `gates.yaml.example` during setup. A run with reviews or policy enabled fails with a config-missing error until the file exists, and `inv config-check` reports it as not created. `gate_engine.yaml` is optional. With no `gate_engine.yaml`, no engine gate registers and the built-in rebar `security-review` gate stays the reviewer. Both operator files are git-ignored, and the repository ships only their `.example` templates.

The gate set is pluggable through two mechanisms, and each ships a worked example rather than a hard dependency.

- Scanner-tool plugins. A review or scanner gate can come from its own installable package through the `agentic_dev_platform.gates` entry-point group, bound only to the distribution a deployment names in config. `examples/snyk-gate` is the worked example. See [docs/gate-plugins.md](docs/gate-plugins.md).
- External AI-CLI review engines. One or more external engines drive their own gates and the advisory test classifier through an invocation and JSON contract, configured as a list in `config/<env>/gate_engine.yaml`. The adapter is engine-agnostic, and `ai-common-workflows` is the reference example beside which a wrapped tool such as SonarQube or a custom script can run. See [docs/gate-engine.md](docs/gate-engine.md). The path is inert until configured, so the built-in rebar `security-review` gate is the default reviewer.

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
| Integration | Jira, GitHub, Jenkins, OPA | The adapters, snapshot, recheck, and linkback |
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
