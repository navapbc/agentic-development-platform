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

Check out the `rebar` repository next to this one, under the same parent directory. The container build and the review path both install it from `../rebar`.

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
2. Create and activate a virtual environment with `python -m venv .venv && source .venv/bin/activate`.
3. Install the package with its development extras using `pip install -e ".[dev,reasoning]"`. Add `sandbox` for the OpenHands agent, and see [CONTRIBUTING.md](CONTRIBUTING.md) for the editable `rebar` install needed on the review path.
4. Choose a Bedrock connectivity provider in `.env` and open the path with `inv tunnel-start`. See [Bedrock connectivity](#bedrock-connectivity).
5. Validate the Bedrock path and local reachability with `inv connectivity-check`.
6. Start the stack with `inv local-up`.
7. Apply database migrations with `inv db-migrate`.
8. Confirm everything is healthy with `inv smoke-test`.
9. Load allowlisted seed data with `inv seed` when you need demo fixtures.

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
| Jira | `JIRA_URL`, `JIRA_AUTH_MODE`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PAT`, `JIRA_ACLI_PATH` | Needed for ticket intake and linkback |
| GitHub | `GITHUB_API_URL`, `GITHUB_GIT_BASE_URL`, `GITHUB_OWNER`, `GITHUB_TOKEN` | Needed to open pull requests |
| Policy and observability | `OPA_URL`, `LANGFUSE_*`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Defaults work locally |
| Allowlists | `ALLOWLIST_JIRA_PROJECTS`, `ALLOWLIST_REPOS` | Leave at the defaults unless you are adding a target |

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
| Container build fails resolving `rebar` | The `../rebar` sibling is missing. Clone it next to this repo |
| Live-agent demos cannot reach the cluster | No kubeconfig at `~/.kube/config`. Provide one or skip the live-agent flag |
