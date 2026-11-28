# Tenant config

Each tenant of a deployment owns the config files that describe its work and its credentials. It publishes them as a bundle, and the workers load the bundle at run time without a deploy. This document lists the files a tenant owns, which files are the platform's and which a tenant may replace or extend, what a bundle must contain, the default each file and key takes when it is absent, and how a run pins one version of the bundle. [tenants](../infra/terraform/examples/private-bedrock/README.md#tenants) in the `private-bedrock` README covers the AWS resources each tenant receives.

## Where a bundle comes from

`TENANT` names the tenant a process serves. Every process serves at least one tenant, and an activity worker refuses to start without `TENANT`, everywhere, because there is no default tenant. Locally, the tenants are the folders of `config/local/tenants/` with their settings files, and `inv k8s-reload` gives each tenant's pods their `TENANT` and every pod the list as `TENANTS`. In AWS the deploy sets `TENANT` on each tenant's worker and `TENANTS` on the platform tasks from the Terraform `tenants` map.

Each tenant's activity worker polls its own task queue, `<TEMPORAL_TASK_QUEUE>-<tenant>`, and the workflow worker polls the base queue and the queue of every tenant in `TENANTS`. A run starts on the queue of the tenant whose worker started it, so its activities run in that tenant's worker. When a tenant's worker starts, it installs its intake schedule, `<source>-intake-<tenant>`, and its reap schedule, `<REAP_SCHEDULE_ID>-<tenant>`, on its queue, and removes the untenanted schedules of the same name. When the ticket source cannot authenticate with the worker's credentials, for example a Jira `token_ref` without a value or a Jira Cloud site that refuses the account email and token, the intake schedule installs paused with the reason as its note, and the next worker start after the gap is fixed installs it running. In AWS, `inv eks-deploy --command "restart tenant-workers"` restarts every tenant worker after a secret is set. Each execution row records its tenant, and a worker counts, admits, and reconciles only its tenant's executions. The reap schedule reaps orphaned branches and pull requests only in the tenant's repositories. It also deletes each pod in the tenant's sandbox namespace whose `agentic-dev-platform/execution-id` label names an execution that has no record or has finished, and records a `SANDBOX_POD_REAPING` audit event for each such pod before it deletes the pod. It leaves a pod without that label, such as a gate pod, and a pod whose execution belongs to another tenant. A pod that fails to reap does not stop the pass, and the reap activity fails at the end of the pass so that Temporal retries it. Every sandbox pod also carries `activeDeadlineSeconds`, the sum of the 600-second ready wait, `OPENHANDS_MAX_WALL_CLOCK_SECONDS`, the 900-second patch extraction timeout, and a 900-second buffer for the copy-in and the overrun of the wall-clock cap, so the kubelet stops the containers of a pod that outlives its run whether or not a reap runs.

| Setting | Source of the bundle |
|---|---|
| `TENANT_CONFIG_BUCKET` set | The objects under `tenants/<TENANT>/` in that bucket, which the [tenant-config-store](../infra/terraform/tenant-config-store) module creates and the tenant's publisher role writes |
| `TENANT_CONFIG_BUCKET` empty | The directory `tenants/<TENANT>/` of the platform config directory, `config/local/tenants/<TENANT>/` in a checkout |

A bundle is flat. It holds files named `<name>.yaml` at the top of its directory or prefix. A tenant worker whose bucket prefix holds no published bundle refuses to start and names the prefix.

## Platform and tenant files

The platform's config sits in `config/local/` in a checkout, and in the directory `PLATFORM_ENV_CONFIG_DIR` names in a deployed task, which the platform image fills from the files the operator writes into `container_config_dir`. Each tenant's config sits in its bundle, `config/local/tenants/<tenant>/` locally and `tenants/<tenant>/` of the tenant config bucket in AWS. No tenant is a home tenant, so no tenant reads another tenant's files or a platform copy of a tenant-only file. Each file belongs to one of the classes below.

| Class | Files | Where a process reads it |
|---|---|---|
| Tenant only | `repositories.yaml`, `checks.yaml`, `gate_engine.yaml`, `github.yaml`, `jira.yaml`, `profiles.yaml`, `escalation.yaml`, `tickets.yaml` without its `isolation` section, the tenant `image_exceptions.yaml`, and the authoring key | The tenant's bundle alone, and the authoring key from the tenant's settings. A copy in the platform directory is refused by `inv k8s-reload`, `inv config-check`, and `inv tenant-config-validate`, which name the tenant's folder as its location, and in AWS by the Terraform plan over `container_config_dir` |
| Platform, replaceable by the tenant | `gates.yaml`, `integrations.yaml` | The tenant's bundle when it holds the file, and the platform directory otherwise. A tenant copy replaces the whole platform file, so a tenant `gates.yaml` that disables a core gate runs without it, and a tenant without one runs the platform's |
| Platform, extended by the tenant | `registries.yaml` | The platform file, with the tenant's entries merged over it by key, as [Registries](#registries) describes |
| Platform only | `image_policy.yaml`, `review-trust-root.yaml`, `slack-shared-app.yaml`, `rebar-store-aws-config`, `tickets_isolation.yaml`, the platform `image_exceptions.yaml`, and the tracked files under `config/platform/` | The platform directory alone. A bundle that holds one is refused |

`tickets_isolation.yaml` names the evidence and audit buckets and writer roles that no tenant's rebar ticket store may reuse. Validation compares each tenant's `tickets.yaml` with it and refuses a `tickets.yaml` that sets `isolation`. In AWS the operator writes `tickets_isolation.yaml` into `container_config_dir` beside the other platform files.

`platform-escalation.yaml` names the platform administrators. On a host it sits in `config/platform/`, and a copy in `config/local/` takes precedence. `inv k8s-reload` projects it into `/app/config/local` of the activity and review workers, the inbound webhook, the Slack posting service, and the `deployment-validate` CronJob. In AWS the operator writes it into `container_config_dir`, Terraform carries it in `environment_config` to the `platform-config` ConfigMap, and each pod reads it from `PLATFORM_ENV_CONFIG_DIR`. A change to it changes the `agentic-dev-platform/config-hash` annotation, so the next apply rolls the pods that read it.

The files a bundle may hold are listed below.

| File | What it holds |
|---|---|
| `repositories.yaml` | The tenant's repositories, their backend, default branch, and indexing |
| `checks.yaml` | Each repository's checks image, command, and fix command |
| `gates.yaml` | The gate bundle and the per-gate overrides a run evaluates, replacing the platform's |
| `integrations.yaml` | The delivery mode of each inbound integration, replacing the platform's |
| `gate_engine.yaml` | The external gate engines and their source or image |
| `registries.yaml` | The tenant's mirror and package entries on the platform's hosts, and its registry identity |
| `github.yaml` | The GitHub hosts, the default owner, and the `token_ref` of each credential |
| `jira.yaml` | The Jira sites, projects, boards, flows, and the `token_ref` of each credential |
| `tickets.yaml` | The rebar ticket store, its status flow, its poll identity, and its S3 store |
| `escalation.yaml` | The tenant's alert channels and Slack settings |
| `profiles.yaml` | The tenant's own runtime profiles, beside the catalog's |
| `image_exceptions.yaml` | The scan findings the tenant accepts in its own images |

Because a replaceable or extended file that the bundle lacks is read from the platform directory, one platform file can serve every tenant, such as the aws-mode `registries.yaml` that Terraform generates. The required-content rules below apply to the effective config, the bundle together with those platform files.

Locally, the platform templates are the `.example` files of `config/local/`, and the tenant templates are the folder `config/local/tenants/tenant.example/` and the settings file `config/local/tenants/tenant.example.env`. `cp -R config/local/tenants/tenant.example config/local/tenants/<tenant>` and `cp config/local/tenants/tenant.example.env config/local/tenants/<tenant>.env` create a tenant. A tenant template whose name ends in `.example` is optional, and the tenant uses it by removing the suffix.

### Registries

`registries.yaml` splits by key between the platform file and the tenant's file.

- `mode` is platform only, because it is bound to the ECR caches, the worker IAM, and DNS Firewall. A tenant file that sets `mode` is refused, and so is a platform file that sets `credential`.
- A tenant's `mirrors` and `packages` entries merge over the platform's entries by key, and only on a host the platform file names. A tenant entry on another host is refused with the host named.
- The tenant file names the tenant's own `credential.username`, which the tenant template `registries.yaml.example` holds alone. The username is a `registry-username` claim, so two tenants with one username fail the local render, and in AWS the second publish fails.
- The token is always `REGISTRY_TOKEN`, from the tenant's settings file locally and from the tenant's `registry_token` secret in AWS. A tenant file that sets `credential.token_env` is refused.
- In `external` mode, a tenant without `credential.username` is refused at validation, and the render and `inv tenant-verify-aws` refuse a tenant whose `REGISTRY_TOKEN` is empty. `inv tenant-verify-aws` sends each tenant's credential to `https://<mirror host>/v2/` and to each package index from the tenant's activity worker, and expects 200.

### Onboarding a tenant in AWS

In AWS the Terraform `tenants` map of the `private-bedrock` root is the tenant list, because an entry creates the tenant's IAM roles, KMS key, secrets, and isolation resources. Adding an entry and applying the root is the onboarding step, and no platform file names the tenant. The tenant then fills its secrets, including `<name_prefix>/tenants/<tenant>/github-webhook-secret`, `jira-webhook-secret`, and `slack-signing-secret`, which the inbound webhook reads for every tenant, and publishes its first bundle before its worker first starts, because a worker whose prefix holds no published bundle refuses to start and names the prefix. [Onboarding a tenant](../infra/terraform/examples/private-bedrock/README.md#onboarding-a-tenant) lists each step.

## Validation

A worker validates a bundle before it activates it, and `inv tenant-config-validate` runs the same validation on a directory. A bundle is refused when any of the following holds.

- It holds a file that is neither a tenant-only file nor a file the tenant may replace or extend.
- A file's `config_version` is older than the release expects.
- A file fails the loader the platform uses for it, for example an unknown gate, a malformed mirror, or a repository on the local backend without a path.
- `repositories.yaml` is absent from the effective config, or names no repository.
- `gates.yaml` is absent from the effective config.
- `TICKET_BACKEND=rebar` and `tickets.yaml` is absent.
- `TICKET_BACKEND=jira` and `jira.yaml` is absent.
- A repository uses the `github` backend and `github.yaml` is absent.
- A repository names a runtime profile that neither the catalog nor `profiles.yaml` defines, a profile reuses a catalog name, a profile's catalog image is not published, or a profile's checks or gates do not parse or enable a gate that does not register.
- An image the effective config names, a profile's sandbox or checks image, a checks image, or a gate engine image, lacks an `@sha256:` digest or comes from a repository the platform's `image_policy.yaml` does not admit for the tenant. `image_policy.yaml` and `image_exceptions.yaml` are platform files. See [image governance](image-governance.md).
- The bundle names a value another tenant claims, as [Claims](#claims) describes. `inv tenant-config-validate` checks every local tenant directory, and lists the bucket's claims when a bucket and AWS credentials are present.

The workers check the source every `TENANT_CONFIG_REFRESH_SECONDS`, 60 by default. A new bundle that passes validation becomes active for the runs that start after it. A new bundle that fails stays refused until its content changes. The last valid bundle stays active, and the workers alert over the Slack webhook and the alert email with every reason. A worker whose first bundle fails validation refuses to start.

## Defaults

A file the effective config lacks, and a key a file omits, take the defaults below.

### repositories.yaml

Required. `defaults.backend` is `github`, `defaults.default_branch` is `main`, `defaults.index` is `false`, and `defaults.profile` is unset. Each repository inherits the defaults and may override them. A repository on the `local` backend needs a `path`. A repository without a `profile` runs in the platform's sandbox image with its `checks.yaml` entry and `gates.yaml` alone.

### checks.yaml

Optional. Absent, no repository has checks of its own, and a repository whose runtime profile has checks runs those. A repository on the `github` backend without an entry is held at intake, because the GitHub backend requires checks. A repository on the `local` backend without an entry skips the checks gate. An entry's `working_dir` is `/workspace`, its `timeout_minutes` is 15, and it has no `fix_command` unless it sets one. `lock_command` names the command that regenerates the lockfiles from the manifests with package scripts disabled, `lock_manifests` the manifests it reads, and `lockfiles` the files it writes. An entry that names no `lock_command` takes its profile's, as the [dependency step](runtime-profiles.md#the-dependency-step) describes.

### gates.yaml

Required. `profile` selects a shipped bundle, `private` or `government`. Both run `code-review`, `security-review`, `acceptance`, `semgrep`, and `trivy` as required gates, except that `private` runs `semgrep` advisory. `fail_fast` is off for the review and scanner stages. An entry under `gates` overrides one gate by name and leaves the rest of the bundle as it is.

### gate_engine.yaml

Optional. Absent, only the catalog engines `security-policy` and `test-policy` register, where `GATE_ENGINE_IMAGES` names their images, and a tenant entry may not reuse their names. An entry's `enabled` is `false`, `mode` is `local`, `output_format` is `review-json`, `tool` is `claude`, `on_unavailable` is `abort`, and `max_output_tokens` is 8192. An enabled entry must set `review_entrypoint`, and `classifier_entrypoint` is empty, which runs no classifier. An `image`-mode entry without an `image` runs the image `GATE_ENGINE_IMAGES` names for it. [gate-engine.md](gate-engine.md) describes every field.

### registries.yaml

Optional for a tenant. Absent from the platform directory, the gate, checks, and scanner pods pull images as written and install packages from their public indexes, which works on a workstation and fails in AWS, where DNS Firewall resolves no public registry. A tenant without its own file runs the platform's mirrors and packages, and in `external` mode is refused for want of `credential.username`. [Registries](#registries) describes the split, and [gate-engine.md](gate-engine.md#registries) the `aws` and `external` modes.

### github.yaml

Required when a repository uses the `github` backend. `default.host` names one of `hosts`, and `default.token_ref` names the variable holding the token, `GITHUB_TOKEN` in the template. A repository in `repositories.yaml` may override the host, owner, or `token_ref`.

### jira.yaml

Required with `TICKET_BACKEND=jira`. `allowlist` names the projects intake reads. `default` is the identity of any allowlisted project without a `projects` entry. `intake_label` unset admits every issue in a project that is not Done. `dependency_link_type` unset turns off dependency recording for the project. `acceptance_field` empty reads no acceptance criteria field.

### tickets.yaml

Required with `TICKET_BACKEND=rebar`. `backend` names the store's `repo_root`, `project`, `default_repo`, and `status_flow`. `store`, `roles`, and `provenance` are optional together, and `isolation` is refused, because the platform's `tickets_isolation.yaml` holds it. Without `store`, the ticket store stays local to `repo_root` and syncs to no bucket. `write_back.allowed_scopes` defaults to every write scope, `status`, `comment`, `approval`, and `link`.

### escalation.yaml

Optional. Absent, an alert reaches every channel whose settings are present, the Jira comment, the email, and Slack through `SLACK_WEBHOOK_URL`. `channels` selects the channels, and a `slack` block names the tenant's Slack app, its transport, its channel, its allowlist, and its content level. Validation refuses a `slack.profile` other than the `gates.yaml` profile, and a `government` tenant whose Slack host is outside `approved_hosts` or whose content is `full`. [slack-escalation.md](slack-escalation.md) describes every field.

### profiles.yaml

Optional. Absent, the tenant uses the catalog profiles alone. A profile's `sandbox_image` empty runs the platform's sandbox image, `checks` absent leaves a repository without a `checks.yaml` entry with no checks, and `gates` absent applies no override. [runtime-profiles.md](runtime-profiles.md) describes the catalog and every field.

## Pinning a run

A run's first activity, `record_manifest`, records the tenant and the identity of the active bundle in the run's determinism manifest. The identity is the version of each file, the S3 version ID from the bucket or a content hash from a directory. Every later activity of the run reads that identity's copy, which the worker keeps in a local store under `$TMPDIR/tenant-config/<tenant>/`, holding the 20 most recent bundles, and fetches again from the bucket's version history when it has been pruned. A bundle published while a run is in flight therefore changes nothing the run reads. An activity that runs before the manifest, such as intake, reads the bundle active when it starts. An activity of a run whose manifest names another tenant fails without a retry.

A directory source keeps no history. A run pinned to a directory bundle that has since changed reads its copy from the worker's store, and fails when the copy has been pruned.

## Publishing

```bash
inv tenant-config-validate --tenant adp --bundle-dir path/to/bundle --bucket <config bucket>
inv tenant-config-publish --tenant adp --bundle-dir path/to/bundle --bucket <config bucket>
```

`inv tenant-config-publish` validates the bundle, claims its values, uploads every file with the `claims-protocol=1` tag, deletes the files under the tenant's prefix the bundle no longer holds, releases the claims the bundle no longer names, and prints each file's new version. A publish refused on a claim uploads nothing. In a tenant's CI, the workflow assumes the tenant's publisher role through GitHub OIDC and passes `--platform-dir` at a copy of the deployment's platform files, the contents of `container_config_dir`, so validation reads the same replaceable and extended files the workers use. The workers validate the bundle again before they activate it.

## Claims

Some values may belong to only one tenant. Each is a claim, which the tenant's publish records in the config bucket.

| Kind | Value | Contends with |
|---|---|---|
| `slack-channel` | The channel of a tenant on the shared Slack app | Every claim on that channel ID, of either Slack channel kind |
| `slack-own-channel` | The channel of a tenant on its own Slack app | `slack-channel` claims on that ID. Two tenants on their own apps may share a channel |
| `slack-app` | The app ID of a tenant's own Slack app | Every claim on that app ID |
| `jira-project` | `https://<site host>/<project key>/<intake label>`, for a tenant on the Jira backend | Every claim on that value, so two tenants share a project only under different intake labels |
| `registry-username` | The `credential.username` of the tenant's `registries.yaml` | Every claim on that username, so no two tenants share a registry identity |

A claim is the empty object `claims/<kind>/<digest>/<tenant>`, where the digest is SHA-256 over the kind and the value, and each tenant's publisher keeps an index of its claims at `claims/_owners/<tenant>.json`. The publish writes the index with the new claims, creates each new marker with `If-None-Match`, and lists the value's digest prefix. The marker with the earliest `LastModified` holds the value. A publisher whose marker is not the earliest deletes it and refuses the publish, naming the kind, the value, and the holder. The holder publishes and warns that the other tenant also claims the value. Markers with the same `LastModified` refuse both. A failed publish leaves extra claims and never fewer.

The bucket policy lets a tenant's publisher create and delete only markers ending in its own tenant name, and admits a claim listing only of one digest prefix ending in `/`. A principal therefore learns which tenant holds a value only when it already knows the value. At runtime, the inbound webhook and the Slack posting service serve the tenant holding the earliest marker on a channel, so a marker written outside the publish does not take a channel from its holder.

Locally, `inv k8s-reload` and `inv tenant-config-validate` refuse a value that two tenant directories name before anything deploys.

### Retained channels

A publish that drops a `slack-channel` claim keeps the marker and records the channel in the tenant's index under `retained`, with a release time of the publish time plus the `token_ttl_hours` of the `escalation.yaml` it replaces, 72 when that file sets none, plus twice `TENANT_CONFIG_REFRESH_SECONDS`. Until that time, every other tenant's publish of the channel is refused, and a click on a button in it is decided by the tenant that left it. A later publish that names the channel again returns it to a current claim with its first marker and precedence. The tenant's first publish after the release time deletes the marker and the index entry, and so does `inv tenant-config-claims sweep`, which an admin runs for a tenant that does not publish again. Other claim kinds are released at once.

`inv tenant-config-claims report` lists a retained channel inside its window with its release time and no finding, and reports one past its release time as a finding that names `sweep`. `inv tenant-config-claims sweep` releases every retained channel past its release time, whichever tenant retains it. `inv tenant-config-claims release` removes one retained channel and its index entry before its release time.

### Pull request CI

A tenant's `config_repository` with `pull_request_subject` set gets a claims probe role, the tenant's `claims_probe_role_arn`, which trusts the repository's pull request OIDC subject and may only list one digest prefix at a time. A pull request job assumes it and runs `inv tenant-config-validate --bucket <config bucket> --platform-dir <platform files>`, which reports a value another tenant holds without writing. The probe role also reads `platform/claims-protocol.json`. Without a bucket or AWS credentials, validation prints that it skipped the claims probe and the claims-protocol check. Run each tenant's publish in one CI concurrency group, such as `tenant-config-<tenant>`, so two publishes of one tenant do not interleave their claim steps.

### Backfill and enforcement

Bundles published by a release before claims hold no markers, and a value with no marker belongs to no tenant. A tenant on its own app whose channel has no marker therefore loses that channel at runtime to a tenant on the shared app that publishes the same channel ID, because the shared tenant's marker is then the earliest. Run the backfill once, after the first deploy of this release and before any tenant publishes `app: shared`, so every published value is claimed by the tenant that already named it.

With `tenant_config_enforce_claims_protocol` set in the private-bedrock example, the bucket refuses a bundle upload that lacks the `claims-protocol=1` tag, which stops a publisher from a release before claims. Turn it on after the backfill.

`tenant_config_claims_backfilled` writes `platform/claims-protocol.json`, and Terraform refuses it without `tenant_config_enforce_claims_protocol`. A new deployment sets both on its first apply, and an existing one sets them once `backfill` exits 0. While the object is absent, `inv tenant-config-validate --bucket` and `inv tenant-config-publish` refuse a bundle that names `app: shared` before any marker or file is written, and the inbound webhook and the Slack posting service refuse every tenant on the shared app and keep serving tenants on their own apps. Each reader has only `s3:GetObject` on the key, so S3 answers `AccessDenied` for an absent object, and the refusal names the object as absent or not readable by the role. A failed read other than those keeps the last answer, and before the first successful read the shared tenants are refused.

```bash
inv tenant-config-claims backfill --bucket <config bucket>
inv tenant-config-claims report --bucket <config bucket>
inv tenant-config-claims release --tenant beta --kind slack-channel --value C0123456789 --bucket <config bucket>
inv tenant-config-claims sweep --bucket <config bucket>
```

`backfill` claims every value of each published bundle and exits non-zero when two bundles already name one value, which it leaves unclaimed and reports. `report` lists the values two tenants claim, with the tenant whose marker holds each, and the claims no published bundle names. `release` removes one tenant's claim on a value. `sweep` releases the retained channels past their release time. All four run under an admin principal.

### A contested value

A claim goes to the first tenant that publishes it, so a tenant that publishes another tenant's channel or project first holds it, and the other tenant's publish is refused with a message that names the holder. Per-tenant action keys keep the holder from deciding the other tenant's runs, so the cost is that the other tenant's alerts cannot use the value. To resolve it:

1. Run `inv tenant-config-claims report --bucket <config bucket>`, which names the tenant whose marker holds the value.
2. Confirm which tenant owns the value. For a Slack channel, ask a workspace admin or a manager of the channel. For a Jira project, ask the project's admin.
3. If the holder does not own the value, have the holder's team remove it from its bundle and publish, or run `inv tenant-config-claims release --tenant <holder> --kind <kind> --value <value> --bucket <config bucket>`.
4. Publish the owner's bundle, which then claims the value.

### Removing or renaming a tenant

Delete the tenant's bundle under `tenants/<tenant>/`, run `inv tenant-config-claims report`, which then lists the tenant's markers as claims no published bundle names and its retained channels, and release each with `inv tenant-config-claims release`, retained channels included. A renamed tenant is a new tenant, so release the old name's claims before the new name's first publish.

An admin restore of an earlier object version under `tenants/` bypasses the claims, and with enforcement on, the restored object must carry the `claims-protocol=1` tag. Republish through the publisher instead.

## Upgrading a v0.2.x database to v0.3.0

A v0.2.x database records executions, orphan artifacts, work claims, sequence gates, and evidence artifacts without a tenant. The migrations `0012_execution_tenant` and `0013_orphan_tenant` add the tenant column, leave `executions.tenant` NULL on every existing row, and set `orphan_artifacts.tenant` to the empty string on every existing row. The migration `0014_claim_tenant` adds a non-null tenant column to `work_claims` and `sequence_gate`. It copies the tenant of the owning execution onto each work claim, and a claim whose execution has no tenant or no longer exists receives the empty string. It copies each sequence gate row to every tenant that holds a work claim of the gate's task, because a workflow sequences from the version of its claim and only the tenant that keeps the claim continues above the gate. A gate row whose task has no claim receives the empty string, and a tenant that holds no claim of the task starts its gate from zero. The claim unique constraint becomes `(tenant, task_id, work_unit)` and the gate primary key becomes `(tenant, task_id)`. The migration `0015_evidence_execution_scope` adds a non-null tenant column to `evidence_artifacts`, copies the tenant of the owning execution onto each row, and gives a row whose execution has no tenant or no longer exists the empty string. It replaces the unique constraint on `content_hash` with one on `(execution_id, content_hash)`, so each execution holds its own row for evidence whose content another execution already recorded. The object store keys each evidence object written after the upgrade as `evidence/<execution_id>/<content_hash>`, and a row recorded before the upgrade keeps the object location it stored. The upgrade assigns none of these rows to a named tenant, because every execution it reads has a NULL tenant.

### Before the upgrade

1. Pause intake. Pause the intake schedule, which `INTAKE_SCHEDULE_ID` names and which is `jira-intake` by default, in the Temporal UI, at `localhost:3001` locally or through `inv temporal-ui` in AWS. Do not run `inv intake` until the upgrade is complete.
2. Let every in-flight run finish, or cancel its workflow in the Temporal UI. `inv workflow-status` lists the recent workflow executions.
3. Run `inv reap` to list the orphaned branches and pull requests whose TTL has passed, then `inv reap --apply` to delete them. The reaper deletes only orphans whose TTL has passed, so close any remaining agent branch or pull request in the repository host before the upgrade.

### After the upgrade

The rows recorded before the upgrade belong to no tenant. Every tenant-scoped query matches its tenant exactly, so these executions do not count toward a tenant's `MAX_CONCURRENT_RUNS` limit, the reaper does not reclaim these orphan artifacts, and the MCP gateway evidence tools do not return the evidence of these executions. A pull request event for one of these executions resolves to no workflow, and the inbound webhook answers it as it answers any event without a workflow.

Work claims and sequence gates recorded before the upgrade carry the empty tenant. A tenant's run claims and sequences on rows of its own, so these rows block no tenant's run, and `inv intake --retry` on a tenant's worker neither releases nor resets them.

The intake idempotency records in `agent_platform.side_effects` name an issue and a repository without a tenant, and the intake of a tenant matches only records that name the tenant. Intake therefore treats a ticket it started before the upgrade as not started. Intake selects every ticket that carries the intake label and is not in a Done status, so before resuming intake, move each ticket whose run finished or was cancelled to a Done status or remove its intake label. An unroutable ticket that was alerted before the upgrade alerts once more for each tenant.

### Optional backfill

A one-time backfill assigns the rows recorded before the upgrade to one tenant. It consists of the following three statements. It leaves `work_claims` and `sequence_gate` unchanged, because no run of a named tenant reads the rows with the empty tenant.

```sql
UPDATE agent_platform.executions SET tenant = '<tenant>' WHERE tenant IS NULL;
UPDATE agent_platform.orphan_artifacts SET tenant = '<tenant>' WHERE tenant = '';
UPDATE agent_platform.evidence_artifacts SET tenant = '<tenant>' WHERE tenant = '';
```

No `inv` task runs SQL against the platform database. Locally, run the statements in the Compose `postgres` service, the database `inv db-migrate` migrates.

```bash
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d agent_platform' <<'EOF'
BEGIN;
UPDATE agent_platform.executions SET tenant = '<tenant>' WHERE tenant IS NULL;
UPDATE agent_platform.orphan_artifacts SET tenant = '<tenant>' WHERE tenant = '';
UPDATE agent_platform.evidence_artifacts SET tenant = '<tenant>' WHERE tenant = '';
COMMIT;
EOF
```

In AWS, run the statements from the activity worker of the tenant, which holds the platform `DATABASE_URL`, through the same `kubectl exec` path the `inv` worker tasks use.

```bash
kubectl -n tenant-<tenant> exec -i deploy/activity-worker -c activity-worker -- python - <<'EOF'
import asyncio

from database.engine import create_platform_engine
from scripts.config import get_settings
from sqlalchemy import text


async def backfill() -> None:
    engine = create_platform_engine(get_settings().database_url)
    async with engine.begin() as conn:
        executions = await conn.execute(text("UPDATE agent_platform.executions SET tenant = '<tenant>' WHERE tenant IS NULL"))
        orphans = await conn.execute(text("UPDATE agent_platform.orphan_artifacts SET tenant = '<tenant>' WHERE tenant = ''"))
        evidence = await conn.execute(text("UPDATE agent_platform.evidence_artifacts SET tenant = '<tenant>' WHERE tenant = ''"))
    await engine.dispose()
    print(f"executions {executions.rowcount}, orphan artifacts {orphans.rowcount}, evidence artifacts {evidence.rowcount}")


asyncio.run(backfill())
EOF
```

The backfill does not rewrite the intake idempotency records, so after it a ticket started before the upgrade still reads as not started to the tenant's intake. Pausing intake and finishing or cancelling every run before the upgrade is what keeps intake from starting a second run of such a ticket.
