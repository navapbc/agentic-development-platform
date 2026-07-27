# agentic-dev-platform

A local, Docker-based agentic SDLC platform (POC). Given a Jira ticket it plans, codes, tests, security-scans, then opens a PR against an allowlisted repository, with human-in-the-loop (HITL) approvals, full audit, and observability. Parallel multi-agent execution is an explicit goal.

> The canonical design is `.agent-context/agentic_plan_v2.md` (v2). Where v1 (`agentic_plan.md`) and v2 conflict, v2 wins.

## Non-negotiable constraints

- FedRAMP Medium, `us-east-1`. Amazon Bedrock is the only inference path. Reachability is a contract, not a fixed route: platform code depends only on a network path to `bedrock-runtime`, an IAM principal with `bedrock:InvokeModel`, and an injected endpoint URL plus region and credential chain. A pluggable connectivity provider satisfies it (default `jumphost` for current OSRE testing via `components/osre-opis-jumphost/terraform` in the sibling `infrastructure` repo). US-only models: Claude primary, Nova fallback.
- AWS profile `mpse-legacy`; secrets naming `mpsm/<environment>/opis/<secret>`.
- VPC endpoints exist for S3 and Secrets Manager, not Bedrock.
- Jira is canonical (no git-copied tickets); a custom Jira adapter serves both Cloud and Data Center. rebar's ticket store and reconciler are not adopted.
- Python-native: `invoke` task runner, Python under `scripts/` (no `make` or `.sh` as the primary surface), Alembic and SQLAlchemy Core for migrations.
- Allowlists only: Jira `AGENTDEV`; repos `agentic-demo-service`, `agentic-demo-greenfield`. The `infrastructure` repo is dev-time read-only reference, never a runtime target.

## Locked stack

Docker Compose, Python 3.11+, invoke, PostgreSQL, Alembic and SQLAlchemy Core, Temporal, LangGraph, OpenHands, LiteLLM fronting Bedrock (single choke point), OPA, rebar (library: review kernel, attestation, and prompt/eval only), Zoekt plus a custom Repository Intelligence Service (codebase-memory-mcp under evaluation), Langfuse, and OTel.

## Quick start (Phase 1)

```bash
cp .env.example .env          # fill in placeholders locally (never commit .env)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,reasoning]"

inv tunnel-start              # open the jump-host path to Bedrock
inv connectivity-check        # validate the Bedrock path end-to-end
inv local-up                  # start the Phase-1 Compose stack
inv db-migrate                # alembic upgrade head (agent_platform only)
inv smoke-test                # DB reachable, services healthy, Bedrock path
```

Common tasks (`inv --list`): `tunnel-start`, `connectivity-check`, `local-up`, `local-down`, `db-migrate`, `db-reset`, `seed`, `smoke-test`, `test`, `logs`, `workflow-status`.

## Bedrock connectivity providers

`inv tunnel-start` and `inv connectivity-check` dispatch to the provider named by `BEDROCK_CONNECTIVITY_PROVIDER`. Each satisfies the same reachability contract, so the same platform code runs unchanged per project by swapping the provider and its config.

| Provider | Path | Required config |
| --- | --- | --- |
| `jumphost` (default) | SSM port-forward through an on-demand jump-host | `JUMPHOST_NAME`, `JUMPHOST_ENV`, `JUMPHOST_CONNECT_SCRIPT` |
| `ssh-bastion` | SSH local forward through a bastion | `SSH_BASTION_TARGET` |
| `direct` | No tunnel; the caller resolves and originates inside the VPC | `BEDROCK_ENDPOINT_URL` (optional) |
| `public` | Public Bedrock endpoint, synthetic-data dev only | `ALLOW_PUBLIC_BEDROCK_ENDPOINT=true` |

`BEDROCK_ENDPOINT_URL` is the injected endpoint surfaced by `Settings.bedrock_client_kwargs()` for boto3 and `BedrockConverseModel`; empty uses the default regional resolver. The public provider is refused unless `ALLOW_PUBLIC_BEDROCK_ENDPOINT` is set, keeping inference inside the ATO boundary by default. Projects with no existing interface endpoint can apply the optional `infra/terraform/bedrock-vpce` module to provision their own.

## Phase-1 services

`postgres`, `temporal` (plus UI), `workflow-worker`, `langgraph-service`, `litellm`, `opa`, `langfuse`, `otel-collector`. Databases: `agent_platform`, `temporal`, `temporal_visibility`, `langfuse`. Phase-1 exit: a workflow invokes Bedrock and persists state; Temporal resumes after a worker restart.

## Repository layout

```text
config/            services/          repository_indexes/  adapters/
schemas/           database/          observability/       openhands/
fixtures/          scripts/           tests/
```

Migrations are scoped to the platform's own `agent_platform` schema only; Temporal and Langfuse manage their own schemas.

## Delivery phases

1. Platform foundation
2. Repository intelligence
3. rebar review integration
4. Ticket and coordination
5. Enterprise adapters and coding
6. CI, review, approval
7. Observability and evaluation
8. AWS transition planning

## Open research (see `.agent-context/research-backlog.md`)

- R1: route rebar to Bedrock via LiteLLM (P1)
- R2: CBM vs custom repo-intel (P1, done: hybrid query-type routed)
- R4: rebar maintainer session
- R5: connectivity and sandbox
- R6: language coverage
- R7: project-agnostic Bedrock reachability (P2)
