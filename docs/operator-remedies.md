# Operator remedies

Every alert that stops or parks a run ends with a `Next step:` line that names what the reader does next. This note records how the platform establishes that the command in that line runs where the reader reads it, and the guard that holds the rule.

A rendered command appears in two places only, the alerts of a tenant without Slack and the alerts sent to the platform administrators. While a tenant's Slack channel is enabled, every tenant alert, in Slack, in the Jira comment, and in the email, names the buttons of the Slack post and no command, in either posture, and the Jira comment and the email name the Slack channel of the post. A tenant configuration fault names the file and key to change in the tenant's config repository.

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
| Restore the model path | `inv aws-login` for the `credentials` hop, `inv tunnel-start --detach` for the `bedrock_path` hop, and `inv k8s-reload` for the `litellm` hop | `kubectl -n agentic-dev-platform rollout restart deploy/litellm` |
| Resume a run parked on the model path | `inv resume-parked --issue <issue> --tenant <tenant>` | The same `kubectl exec`, running `python -m scripts.resume_parked --issue <issue>` |

The deployed rendering of a run action is the command an `inv` task runs for the host operator, addressed to the tenant's worker namespace. Both reach the same module in the activity worker, so both audiences resolve a run through the same code path and the same audit trail.

A step's kind crosses the Temporal payload boundary as a plain string, so a worker on an older build decodes a step it does not know and renders its prose rather than failing the workflow task. A step carries its kind, issue key, tenant, gate, and execution as separate fields, so a channel that supports interactive messages can present an action as a button rather than as a command, without parsing the rendered text. The Slack channel presents approve, reject, retry, abandon, and restart as buttons, as [slack-escalation.md](slack-escalation.md#alert-buttons) describes, and the tenant rendering of each such step names its button. A repair park after exhausted repairs offers retry and abandon. A park on a reached run limit offers Request increase and abandon, and a park on a spent tenant model budget offers abandon, because a retry at that budget parks again at once. The spent budget's period alert offers Request increase and Park all jobs, and a grant or the period reset resumes the parked runs. Abandon ends the run and leaves the ticket's intake record in place, so the remedy also names `inv restart` for an operator who wants intake to admit the ticket again.

The alert activity returns the rendered remedy and the channels it reached, and the workflow records both on the `HUMAN_INTERVENTION_ALERTED` audit event, in `audit_events.remedy` and `audit_events.alert_channels`. The audit record therefore holds the instruction the operator read.

Two alerts cover a run that stops outside a handled outcome. A draft pull request run that fails on an error no outcome handles, such as a child workflow whose model call exhausted its retries, records its execution as `FAILED`, writes a `RUN_FAILED` audit event with the stage and the root cause, and raises a `run_failed` alert whose remedy names `inv restart`. When a run's workflow is missing or closed while its execution is still non-terminal, the orphan reconciler marks the execution `CANCELLED`, raises an `orphan_reclaimed` alert through the same fan-out with a remedy that names `inv restart`, and records the rendered remedy and the channels it reached on an `EXECUTION_ORPHAN_RECLAIMED` audit event. A run that failed is already terminal, so the reconciler does not alert on it a second time.

Two alerts cover the model path, and both reach a tenant rather than a run. When the intake probe names a failing hop, intake defers the ticket it picked up and raises a `model_path_unavailable` alert. The model path is a platform fault, so the tenant receives the effect, the statement that work resumes on its own, and how to validate, with Acknowledge, Run validation, and Fix done, and the administrators receive the error and the remedy that restores the hop, rendered for the deployment posture. When a later probe answers, or Fix done finds the path healthy, the outage resolves, the alert posts are edited to resolved, and the recovery is posted in their threads and sent once to the administrators. Each alert goes to the tenant's escalation channels once per transition, and its Jira or rebar comment lands on the ticket whose pickup observed the transition, so a tenant whose only channel is the ticket comment still receives it. The probe runs only while `MODEL_PATH_PROBE` is on, which the EKS overlay turns off, so the deployed rendering of the restore step is not reached in a deployment that follows the overlay.

A run whose plan stage fails with `ModelUnavailable` parks rather than fails, behind the `model-path-park` patch. It records its execution at `AWAITING_HUMAN`, a `model_unavailable` suspension row whose detail is the hop, and a `MODEL_PATH_PARKED` audit event, and it raises a `model_unavailable` alert. The tenant receives the effect and the statement that work resumes on its own, and the administrators receive the remedy, which restores the hop and names `inv resume-parked --issue <issue>`. The alert carries no park key, so it offers no decision buttons. Every intake pass that finds a parked run for the tenant probes the path even when it picked up no ticket, and the first fresh probe that answers resumes the tenant's parked runs and raises the `model_path_recovered` alert. A resumed run writes a `MODEL_PATH_RESUMED` audit event with the actor that resumed it and repeats only the plan stage, keeping its gathered context and base commit. A second failure parks the run again under a new park key, and a resume naming the earlier key is refused. A run that started before the patch fails as `run_failed` instead.

The gate pod remedies in `services/orchestration/gates/pod_runner.py` name `kubectl` commands against the gate pod's namespace. They read the same in both postures, because both audiences reach the cluster with `kubectl`.

## Deployment posture

`DEPLOYMENT_POSTURE` selects the rendering. It is `host` by default, and the EKS overlay in `deploy/kubernetes/aws` sets it to `deployed` in the `agentic-platform-eks` ConfigMap that every tenant's activity worker reads. No existing setting carries the posture. `PLATFORM_IN_CONTAINER` is true under the local stack as well, and `BEDROCK_CONNECTIVITY_PROVIDER` describes how the platform reaches Bedrock.

## Tenant

The draft-PR workflow carries no tenant. The alert activity runs in the activity worker of the run's tenant, so the delivery layer binds that worker's `TENANT` into every action that names none. `inv restart`, `inv repair`, `inv gate`, and `inv resume-parked --issue` take `--tenant`, which addresses that tenant's activity worker, and with `--local` runs the host command for that tenant. They also take `--context`, which selects the kubectl context of the worker's cluster in place of `K8S_CONTEXT`, so an operator with a checkout reaches an EKS cluster through the context `inv eks-api` writes. A host rendering is therefore correct as written for every tenant.

A deployed command runs in the activity worker container, where the worker's own process holds the tenant's active bundle and a new process holds none. `scripts.restart`, `scripts.intake`, `scripts.rebar_poll`, and `scripts.import_rebar_tickets` read tenant-only files such as `tickets.yaml`, so each activates the tenant's latest published bundle from the config bucket before it reads them. A bundle that fails validation stops the command with the validation error.

A decision acts on the runs of one tenant only. Every tenant's workflows share one Temporal namespace, so an issue key two tenants both track names a parked run of each. `scripts.restart`, `scripts.repair`, and `scripts.gate_decision` read the parked runs from the executions table filtered on the `TENANT` of the worker they run in, which `--tenant` selects, and signal only those. `scripts.resume_parked --issue` and the intake resume read the model path parks through the same filter. A worker without a tenant acts on the executions recorded without one, whose tenant is NULL or empty.

## Parks that no decision reaches

`inv restart`, `inv repair`, and `inv gate` signal only the runs the executions table records at `AWAITING_HUMAN`. A run that parks on another suspension keeps its state and its running workflow, so `inv restart` signals nothing and then refuses to restart because the workflow is still running. A remedy raised while a run parks on such a suspension therefore names no decision command.

The code-defect park is the case this covers. A push that fails on a defect in ADP records an `awaiting_fix` suspension that resumes on a newer build, and the run resumes there on its own. Its remedy names the action that works, fixing the defect and deploying a newer build, and states that the run parks again if the defect persists. The park does not end on an operator command. Ending it on the signal `inv restart` sends would need the restart store to select runs parked for a fix and the workflow to accept the signal during that park, a change to the command sequence of runs already parked. A run that must not wait for the fix is cancelled from the Temporal UI, after which `inv restart` admits the ticket again.

The model path park records `AWAITING_HUMAN`, so `inv restart`, `inv repair`, and `inv gate` find the run, but the park accepts no gate or repair decision. The signal they send leaves the run parked, and `inv restart` then refuses because the workflow is still running. Its remedy therefore names `inv resume-parked --issue <issue>`, which resumes the park, and no decision command.

## Directions considered

- Assert at build time that a remedy names only commands reachable where it claims they run. A remedy authored as text makes no claim about its audience, so the assertion has nothing to check a command against. The guard below performs this check over the structured rendering instead.
- Replace remedy text with a structured action that the delivery layer renders per audience. Chosen, as described above.
- Make remedies executable through a callback into the platform, so validity holds by construction. A Jira comment and an email cannot carry a callback, so every channel would still need a rendered command. The structured action carries the fields such a callback needs, and the Slack channel offers its actions as buttons, which the tenant rendering names in place of a command.

## Guard

`tests/unit/test_operator_remedy_contract.py` renders every remedy the module exports for both postures, with and without a tenant, and fails when any of the following holds.

- A host command is neither an `inv` task in `tasks.py` nor a `kubectl` or `aws` command, or passes a flag the task does not take.
- A deployed command uses a tool other than `kubectl` or `aws`, or runs a module that `docker/Dockerfile` does not copy into the platform image, that has no `__main__` entry, or that does not accept the flags the command passes.
- A deployed rendering names a settings file, `tasks.py`, a `config/` path, an `inv` command, or a Python source path.
- A config key is absent from the committed template, or a setting is absent from `Settings`.
- An alert site in the draft-PR workflow supplies no remedy, or a remedy other than the code-defect remedy names no action.
- An alert raised while a run parks on a suspension other than `AWAITING_HUMAN` names a restart, repair, or gate decision, which no such park receives.
