---
title: Temporal Server Upgrade - Plan
type: feat
date: 2026-10-30
origin: .agent-context/roadmap-jira.md
ticket: ADP-138
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: roadmap
execution: local-stack
---

# Temporal Server Upgrade - Plan

## Goal Capsule

Objective. The local stack runs Temporal server 1.32.1 and Temporal UI 2.57.0 on images that Temporal still publishes, and an existing 1.29.7 database moves to 1.32.1 without losing an execution.

Means. Replace `temporalio/auto-setup` with `temporalio/server` behind a thin image that adds the static loopback probe, apply the schemas and the `default` namespace through a one-shot `temporalio/admin-tools` service, and add `inv temporal-upgrade`, which backs up the two Temporal databases and walks them through each minor release.

Product authority. ADP-138 in Jira, from the v0.3.0 section of `.agent-context/roadmap-jira.md`. ADP-97 reuses the server digest and the admin-tools schema approach for the AWS control plane.

Stop conditions. `inv local-up` on an empty volume reaches 1.32.1 with both schemas and the `default` namespace. `inv temporal-upgrade` takes a 1.29.7 database to 1.32.1, and an execution started before the upgrade completes after it. `inv workflow-status` and `inv durability-proof` pass. The Compose health check reports the server healthy without `tctl`. `inv ci` passes.

## Product Contract

### Summary

Compose runs `temporalio/auto-setup:1.29.7`, which applies the schemas and registers the `default` namespace inside the server container on every start. Temporal stopped publishing `auto-setup` after 1.29.7, and the 1.29 line receives no further patches. The `temporalio/server` image carries only the server binary, busybox `wget`, and `nc`. It ships no `tctl`, no `temporal` CLI, no `bash`, and no `curl`, and it refuses to start without a dynamic configuration file.

### Upgrade constraints

Temporal supports an upgrade only between consecutive minor versions, each from the highest patch of the current minor, and the server must run at each step. The schema versions each release requires, read from `schema/postgresql/v12/version.go` at each tag, are below.

| Release | Core schema | Visibility schema |
|---|---|---|
| 1.29.7 | 1.18 | 1.9 |
| 1.30.8 | 1.18 | 1.13 |
| 1.31.4 | 1.19 | 1.14 |
| 1.32.1 | 1.19 | 1.14 |

The schema change is one-way. Restoring a 1.29.7 server after the upgrade requires restoring both databases from the backup.

### Requirements

- R1. `temporal` runs `temporalio/server:1.32.1` pinned by digest, bound to `0.0.0.0`, with a dynamic configuration file the repository tracks.
- R2. A `temporal-admin-tools` service under the `tasks` profile runs `temporal-sql-tool` setup and update against `temporal` and `temporal_visibility`, and registers the `default` namespace with a 24-hour retention, matching what `auto-setup` registered.
- R3. `inv local-up` applies the schemas before the server starts and registers the namespace before the workers start.
- R4. `inv local-up` refuses a database whose schema is older than 1.31, so the minor-version path cannot be skipped.
- R5. The server health check is the static loopback probe the `opa`, `otel-collector`, and `tempo` images use.
- R6. `inv temporal-upgrade` backs up both databases, then runs each remaining minor release in turn, applying its schema with the matching admin-tools image and waiting for its server to report healthy.
- R7. `temporal-ui` runs 2.57.0 pinned by digest.
- R8. The `temporalio` SDK pin moves only if server 1.32 requires it.
- R9. The CHANGELOG breaking changes and the README document the upgrade path, the one-way schema change, the backup, and the restore.

### Scope Boundaries

- The AWS control plane is ADP-97. This ticket changes the local stack only.
- Worker Deployment routing on 1.32.1 needs pull request #58. That acceptance criterion closes after #58 merges.
- Server 1.32.2 is not yet published. The digest moves to 1.32.2 in a later change.

### Sources

- `temporalio/temporal` tags v1.29.7, v1.30.8, v1.31.4, v1.32.1, the embedded configuration template `common/config/config_template_embedded.yaml`, and the schema versions in `schema/postgresql/v12/version.go`.
- `temporalio/samples-server` `compose/docker-compose-postgres.yml`, `compose/scripts/setup-postgres.sh`, and `compose/scripts/create-namespace.sh`, which replace `auto-setup` with the same server and admin-tools split.

## Planning Contract

### Key Technical Decisions

- The server image is built from `docker/temporal.Dockerfile`, which takes the upstream image as the `TEMPORAL_SERVER_IMAGE` build argument with the 1.32.1 digest as its default and adds `healthprobe`. Compose passes the argument through from the environment, so `inv temporal-upgrade` builds each intermediate release from the same Dockerfile.
- The probe requests `/api/v1/namespaces/temporal-system` on the frontend HTTP port 7243. The server answers it only after the frontend is serving and the namespace registry has read the system namespace from the database.
- `temporal-admin-tools` reads its connection from the `SQL_*` variables `temporal-sql-tool` accepts, so the AWS task in ADP-97 supplies TLS through `SQL_TLS*` without a script change. The image comes from `TEMPORAL_ADMIN_TOOLS_IMAGE`, which defaults to the 1.32.1 digest.
- `setup-schema -v 0.0` skips an existing schema and `update-schema` is a no-op at the latest version, so the schema action is safe on every `inv local-up`.
- `inv temporal-upgrade` identifies the current release from the `schema_version` rows. A 1.31 and a 1.32 database share a schema, and moving either to 1.32.1 is a supported single step, so the task never runs an older server against a newer database.
- The backup is `pg_dump -Fc` of both databases to `~/temporal-backup/<UTC timestamp>`, taken before any schema change. `--skip-backup` exists for a disposable stack.
- The dynamic configuration file is `config/platform/temporal-dynamicconfig.yaml` and holds no overrides.

### Sequencing

U1, then U2 and U3, then U4, then U5.

## Implementation Units

### U1. Server and admin-tools services

Add `docker/temporal.Dockerfile` and `docker/temporal-admin-tools.sh`. Replace the `temporal` service, add `temporal-admin-tools`, add the dynamic configuration file, and move `temporal-ui` to 2.57.0.

### U2. Local stack startup

Change `inv local-up` to start Postgres, refuse a pre-1.31 schema, run the schema action, start and wait for `temporal`, register the namespace, then start the stack.

### U3. Upgrade task

Add `inv temporal-upgrade` with the release table, the backup, the per-step schema update and server start, and the final namespace registration. Cover the step selection and the command sequence with unit tests.

### U4. Documentation

Add the breaking change to the CHANGELOG, update the README services section, and document the upgrade and restore.

### U5. Verification

Run the acceptance criteria against the local stack, then `inv ci`.

## Verification Record

Run on 2026-10-30 against the local stack.

- Upgrade. The existing stack ran `temporalio/auto-setup:1.29.7` with core schema 1.18 and visibility schema 1.9. A `CountingWorkflow` with a target of 30 was started, and the three workers were stopped at count 3 to park it. `inv temporal-upgrade` wrote `temporal.dump` and `temporal_visibility.dump`, ran 1.30.8, 1.31.4, and 1.32.1 with each server reporting healthy, and finished with core 1.19 and visibility 1.14 in 66 seconds. After the workers started, the parked execution completed with result 30. The executions recorded before the upgrade were still listed.
- Empty volume. In a separate Compose project with new volumes, `inv local-up` exited 0 with every service healthy, server 1.32.1, core schema 1.19, visibility schema 1.14, and the `default` namespace at a 24-hour retention. A second `inv local-up` left the schemas unchanged and reported the namespace as registered.
- Guard. Restoring the 1.29.7 backup into that project made `inv local-up` exit 1 with the `inv temporal-upgrade` message before any Temporal change. `inv temporal-upgrade --skip-backup` then reached 1.32.1, and `inv local-up` exited 0.
- `inv workflow-status` listed the executions, and `inv durability-proof` passed on both stacks.
- The `temporal` container reports healthy through `/usr/local/bin/healthprobe http://127.0.0.1:7243/api/v1/namespaces/temporal-system`.
- `inv ci` passed with 1824 tests.
- The Worker Deployment criterion stays open until pull request #58 merges.
