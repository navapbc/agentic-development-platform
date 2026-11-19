# Release verification

This runbook checks a release on a workstation two ways, as a developer who upgrades an existing v0.2.1 stack and as one who installs v0.3.0 fresh. Each run ends by resolving parked runs from Slack. Run the upgrade first and the fresh install last, so the workstation ends on a clean v0.3.0 stack. Check each step's expected result before the next step.

Both versions use the Compose project `agentic-dev-platform` and the kind cluster of the same name, so the runs cannot overlap. Several steps are one-way. The Temporal schema upgrade, the Valkey snapshot rewrite, the ClickHouse data upgrade, and the platform migrations cannot be reversed by checking out an older version, so the backup comes first.

## Prerequisites

- The `v0.2.1` tag. Create it once with `git tag v0.2.1 772496f5 && git push origin v0.2.1`.
- A Slack workspace where you can create and install an app, a channel for the alerts, a second Slack user who is not on the allowlist, and an app created from [`config/platform/slack-app-manifest.yaml.example`](../config/platform/slack-app-manifest.yaml.example) with Socket Mode on, its bot token, and an app-level token with `connections:write`. [slack-escalation.md](slack-escalation.md#creating-the-app) lists the steps to create the app.
- A ticket that parks at a gate and one that parks for repair, prepared for each run as the Slack flows below describe.

## 1. Back up the current stack

| Step | Command | Expected result |
|---|---|---|
| 1.1 | `export BACKUP="$HOME/adp-release-backup/$(date -u +%Y%m%dT%H%M%SZ)" && mkdir -p "$BACKUP"` | The directory exists |
| 1.2 | `cp .env "$BACKUP/" && cp -R config/local "$BACKUP/config-local" && cp .build-id.json "$BACKUP/" 2>/dev/null; ls "$BACKUP"` | `.env` and `config-local` are listed |
| 1.3 | `docker compose exec -T postgres sh -c 'pg_dumpall -U "$POSTGRES_USER"' > "$BACKUP/postgres.sql"` | A non-empty dump holding `agent_platform`, `temporal`, `temporal_visibility`, `langfuse`, and `litellm` |
| 1.4 | The Langfuse, Valkey, and ClickHouse backup command in the README section on moving a checkout to the current Langfuse versions, with `"$HOME/langfuse-backup"` replaced by `"$BACKUP/langfuse"` | `cache-dump.rdb`, `clickhouse-data.tgz`, and `langfuse.dump` in `$BACKUP/langfuse` |

Restoring is manual. Recreate each database from `postgres.sql`, restore the Langfuse files as the README describes, and copy `.env` and `config-local` back.

## 2. Upgrade from v0.2.1

| Step | Command or action | Expected result |
|---|---|---|
| 2.1 | From the v0.3.0 checkout, `inv local-destroy`, then `rm -f .build-id.json` | No `agentic-dev-platform` container, volume, or kind cluster remains |
| 2.2 | `git worktree add --detach ../adp-v021 v0.2.1`, then follow the v0.2.1 README first-time setup in that worktree, with a Python virtual environment of its own | `inv smoke-test` passes on v0.2.1 |
| 2.3 | On v0.2.1, run one ticket to a finished run and one to a resolved gate park with `inv gate` | Both executions are recorded, and `inv workflow-status` shows both closed |
| 2.4 | Drain as `docs/tenant-config.md` describes under Before the upgrade. Then `docker compose exec -T postgres psql -U "$POSTGRES_USER" -d agent_platform -tAc "select count(*) from agent_platform.executions where state = 'AWAITING_HUMAN'"` | `0`, and `inv workflow-status` lists no running workflow |
| 2.5 | Move each v0.2.1 ticket whose run finished to Done, or remove its intake label, as `docs/tenant-config.md` describes under After the upgrade | Intake will select only the tickets meant to run again |
| 2.6 | `docker compose stop`, then copy `../adp-v021/.env`, `../adp-v021/config/local`, and `../adp-v021/.build-id.json` into the v0.3.0 checkout | The v0.3.0 checkout holds the v0.2.1 settings |
| 2.7 | Apply every entry under Breaking changes in the 0.3.0 section of the [changelog](../CHANGELOG.md) to `.env` and `config/local`. Set `TENANT` and `TENANTS`, copy `config/local/image_policy.yaml.example`, add the Slack settings and `config/local/escalation.yaml` as section 4 describes, and run `uv sync --locked --all-extras` | `inv config-check` reports every file current |
| 2.8 | Run the one-time Langfuse cleanup the README names, then `inv runtime-images`, `inv gate-engine-images`, and `inv temporal-upgrade` | The images are written to `.env`, and Temporal reports 1.32.2 |
| 2.9 | `inv local-up` | Every service starts, and the tenant's activity worker logs that it installed its intake schedule |
| 2.10 | `docker compose exec -T postgres psql -U "$POSTGRES_USER" -d agent_platform -tAc "select version_num from agent_platform.alembic_version"` | `0017_audit_event_id_length` |
| 2.11 | `inv config-check` and `inv smoke-test` | Both pass |
| 2.12 | `docker compose exec -T postgres psql -U "$POSTGRES_USER" -d agent_platform -tAc "select count(*) from agent_platform.executions where tenant is null"` | The two v0.2.1 executions, with no tenant |
| 2.13 | Watch one intake cycle | Intake admits only the tickets meant to run, and none of the v0.2.1 tickets moved to Done |
| 2.14 | Run the Slack flows in section 4 | Every flow gives its expected result |

## 3. Fresh v0.3.0 install

| Step | Command or action | Expected result |
|---|---|---|
| 3.1 | `inv local-destroy` and `rm -f .build-id.json` | No `agentic-dev-platform` container, volume, or kind cluster remains, the cluster workspace volume included |
| 3.2 | `mv .env .env.upgrade && mv config/local config-local.upgrade && git checkout -- config/local` | Only the committed examples remain in `config/local`, with no tenant files |
| 3.3 | Follow the README first-time setup from `.env.example` and the `config/local` examples, then add the Slack settings and `config/local/escalation.yaml` as section 4 describes | `inv config-check` reports every file current |
| 3.4 | `inv local-up` | Every service starts |
| 3.5 | Repeat 2.10 and 2.11 | `0017_audit_event_id_length`, and both checks pass |
| 3.6 | Run the Slack flows in section 4 | Every flow gives its expected result |

After the fresh run, remove `.env.upgrade`, `config-local.upgrade`, and the `../adp-v021` worktree with `git worktree remove ../adp-v021`.

## 4. Slack flows

Set up Slack for the home tenant before the first start.

1. In `.env`, set `SLACK_BOT_TOKEN` to the app's bot token, `SLACK_APP_TOKEN` to its app-level token, and `ESCALATION_ACTION_KEY` to the output of `openssl rand -hex 32`.
2. Copy `config/local/escalation.yaml.example` to `config/local/escalation.yaml`. Set `transport: socket`, the channel ID, the organization ID, the app ID, and an allowlist naming your Slack user for both `gate` and `repair`.
3. To observe email, start Mailpit with `docker compose --profile verify up -d mailpit`, set `SMTP_HOST=host.docker.internal`, `SMTP_PORT=1025`, `ALERT_EMAIL_FROM`, and `ALERT_EMAIL_TO` in `.env`, and read the messages at `http://localhost:8025`.
4. For a gate park, enable the `plan` gate in `config/local/gates.yaml`. For a repair park, give the ticket's repository a `checks.yaml` command that exits non-zero.

| Flow | Action | Expected result |
|---|---|---|
| Gate approve | Admit a ticket, wait for its plan gate park, and click Approve | The message shows Approved and your actor with no buttons, the run continues, and the `HUMAN_INTERVENTION_DECISION` audit event has `actor_type` `slack` |
| Gate reject | Park a second ticket at the plan gate and click Reject | The message shows Rejected, and the run stops |
| Repair retry | Admit the repair ticket, wait for its repair park, and click Retry repair | The run repairs again and parks again with a new message |
| Stale click | Click Retry repair on the first repair message | An ephemeral reply says the message belongs to an earlier park, and a `SLACK_ACTION_REFUSED` event records `earlier_park` |
| Repair abandon | Click Abandon on the newest repair message | The message shows Abandoned, and the execution ends `CANCELLED` |
| Outside the allowlist | As the second Slack user, click a button on a parked run | An ephemeral reply refuses it, and a `SLACK_ACTION_REFUSED` event records `not_allowed` |
| Double click | Click Approve twice in quick succession on a gate park | One decision is recorded, and both clicks report it |
| Slack outage | Set `SLACK_BOT_TOKEN` to an invalid value, run `inv k8s-reload`, and park a run | The Jira comment and the email in Mailpit arrive, the alert's `alert_channels` omits `slack`, and the run is parked. Restore the token and run `inv k8s-reload` |

During the gate approve and the outside the allowlist flows, `RUN_LIVE_SLACK=1 LIVE_SLACK_EXECUTION_ID=<execution> pytest -q tests/integration/test_slack_live.py -k <flow>` asserts the audit records while you click, with `approved_from_slack` or `outside_the_allowlist` as the flow. The test waits up to five minutes for the click.

Record the Socket Mode hosts during both runs. Run `curl -s -X POST -H "Authorization: Bearer $SLACK_APP_TOKEN" https://slack.com/api/apps.connections.open` several times, note every distinct host of the `wss://` URL it returns, and add them to the DNS Firewall hosts in the [private-bedrock example README](../infra/terraform/examples/private-bedrock/README.md). An unused URL expires on its own.
