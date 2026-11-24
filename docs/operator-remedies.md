# Operator remedies

Every alert that stops or parks a run ends with a `Next step:` line that names what the operator does next. This note records how the platform establishes that the command in that line runs where the operator reads it, and the guard that holds the rule.

## The constraint

A remedy renders in the activity worker and fans out to a Jira comment, Slack, and email, so the operator acts somewhere the worker cannot observe. The platform image copies `services`, `adapters`, `schemas`, `database`, `scripts`, `observability`, `openhands`, and `config/platform`, and does not copy `tasks.py`. An `inv` command therefore runs only from a platform checkout. A deployed operator works against the deployment with `kubectl` and `aws` and has no checkout, no settings file, and no `config/local/` directory.

## Decision

A remedy is a structured action, and the delivery layer renders it for the audience that reads the alert.

`services/orchestration/remedies.py` authors each remedy as prose interleaved with steps from `services/operator_remedy.py`. A step is either prose or one of the following.

| Step | Host rendering | Deployed rendering |
|---|---|---|
| Restart a run | `inv restart --issue <issue> --tenant <tenant>` | `kubectl -n tenant-<tenant> exec -i deploy/activity-worker -c activity-worker -- python -m scripts.restart <issue>` |
| Retry a parked repair | `inv repair --issue <issue> --retry --tenant <tenant>` | The same `kubectl exec`, running `python -m scripts.repair <issue> --retry` |
| Abandon a parked repair | `inv repair --issue <issue> --abandon --tenant <tenant>` | The same `kubectl exec`, running `python -m scripts.repair <issue> --abandon` |
| Approve or reject a gate | `inv gate --issue <issue> --gate <gate> --approve --tenant <tenant>`, or `--reject` | The same `kubectl exec`, running `python -m scripts.gate_decision <issue> --gate <gate> --approve`, or `--reject` |
| Roll up a run's cost | `inv cost-rollup --execution-id <execution>` | The same `kubectl exec`, running `python -m scripts.cost_rollup --execution-id <execution>` |
| A config key | The key in `config/local/tenants/<tenant>/<file>.yaml` for a tenant-only file, in `config/local/<file>.yaml` for a platform file, and in the tenant's copy or else `config/local/<file>.yaml` for a file the tenant may replace or extend | The key in `<file>.yaml` of the tenant config bundle under `tenants/<tenant>/` of the bucket `TENANT_CONFIG_BUCKET` names. A platform file is located in the directory `PLATFORM_ENV_CONFIG_DIR` names |
| A worker setting | The setting in `config/local/tenants/<tenant>.env` for one tenant, or in `config/local/platform.env` for every tenant when the platform file may hold it, applied with `inv k8s-reload` | The setting in the environment of the tenant's activity worker, which the deployment's `tenants` variable sets, applied by redeploying the worker |

The deployed rendering of a run action is the command an `inv` task runs for the host operator, addressed to the tenant's worker namespace. Both reach the same module in the activity worker, so both audiences resolve a run through the same code path and the same audit trail.

A step's kind crosses the Temporal payload boundary as a plain string, so a worker on an older build decodes a step it does not know and renders its prose rather than failing the workflow task. A step carries its kind, issue key, tenant, gate, and execution as separate fields, so a channel that supports interactive messages can present an action as a button rather than as a command, without parsing the rendered text. The Slack channel presents approve, reject, retry, and abandon as buttons, as [slack-escalation.md](slack-escalation.md) describes. A repair park after exhausted repairs offers retry and abandon, and a repair park on a spent budget offers abandon, because a retry at that budget parks again at once. Abandon ends the run and leaves the ticket's intake record in place, so the remedy also names `inv restart` for an operator who wants intake to admit the ticket again.

The alert activity returns the rendered remedy and the channels it reached, and the workflow records both on the `HUMAN_INTERVENTION_ALERTED` audit event, in `audit_events.remedy` and `audit_events.alert_channels`. The audit record therefore holds the instruction the operator read.

The gate pod remedies in `services/orchestration/gates/pod_runner.py` name `kubectl` commands against the gate pod's namespace. They read the same in both postures, because both audiences reach the cluster with `kubectl`.

## Deployment posture

`DEPLOYMENT_POSTURE` selects the rendering. It is `host` by default, and the EKS overlay in `deploy/kubernetes/aws` sets it to `deployed` in the `agentic-platform-eks` ConfigMap that every tenant's activity worker reads. No existing setting carries the posture. `PLATFORM_IN_CONTAINER` is true under the local stack as well, and `BEDROCK_CONNECTIVITY_PROVIDER` describes how the platform reaches Bedrock.

## Tenant

The draft-PR workflow carries no tenant. The alert activity runs in the activity worker of the run's tenant, so the delivery layer binds that worker's `TENANT` into every action that names none. `inv restart`, `inv repair`, and `inv gate` take `--tenant`, which addresses that tenant's activity worker, and with `--local` runs the host command for that tenant. A host rendering is therefore correct as written for every tenant.

A decision acts on the runs of one tenant only. Every tenant's workflows share one Temporal namespace, so an issue key two tenants both track names a parked run of each. `scripts.restart`, `scripts.repair`, and `scripts.gate_decision` read the parked runs from the executions table filtered on the `TENANT` of the worker they run in, which `--tenant` selects, and signal only those. A worker without a tenant acts on the executions recorded without one, whose tenant is NULL or empty.

## Parks that no decision reaches

`inv restart`, `inv repair`, and `inv gate` signal only the runs the executions table records at `AWAITING_HUMAN`. A run that parks on another suspension keeps its state and its running workflow, so `inv restart` signals nothing and then refuses to restart because the workflow is still running. A remedy raised while a run parks on such a suspension therefore names no decision command.

The code-defect park is the case this covers. A push that fails on a defect in ADP records an `awaiting_fix` suspension that resumes on a newer build, and the run resumes there on its own. Its remedy names the action that works, fixing the defect and deploying a newer build, and states that the run parks again if the defect persists. The park does not end on an operator command. Ending it on the signal `inv restart` sends would need the restart store to select runs parked for a fix and the workflow to accept the signal during that park, a change to the command sequence of runs already parked. A run that must not wait for the fix is cancelled from the Temporal UI, after which `inv restart` admits the ticket again.

## Directions considered

- Assert at build time that a remedy names only commands reachable where it claims they run. A remedy authored as text makes no claim about its audience, so the assertion has nothing to check a command against. The guard below performs this check over the structured rendering instead.
- Replace remedy text with a structured action that the delivery layer renders per audience. Chosen, as described above.
- Make remedies executable through a callback into the platform, so validity holds by construction. A Jira comment and an email cannot carry a callback, so every channel would still need a rendered command. The structured action carries the fields such a callback needs, and the Slack channel offers its decisions as buttons beside the rendered command.

## Guard

`tests/unit/test_operator_remedy_contract.py` renders every remedy the module exports for both postures, with and without a tenant, and fails when any of the following holds.

- A host command is neither an `inv` task in `tasks.py` nor a `kubectl` or `aws` command, or passes a flag the task does not take.
- A deployed command uses a tool other than `kubectl` or `aws`, or runs a module that `docker/Dockerfile` does not copy into the platform image, that has no `__main__` entry, or that does not accept the flags the command passes.
- A deployed rendering names a settings file, `tasks.py`, a `config/` path, an `inv` command, or a Python source path.
- A config key is absent from the committed template, or a setting is absent from `Settings`.
- An alert site in the draft-PR workflow supplies no remedy, or a remedy other than the code-defect remedy names no action.
- An alert raised while a run parks on a suspension other than `AWAITING_HUMAN` names a restart, repair, or gate decision, which no such park receives.
