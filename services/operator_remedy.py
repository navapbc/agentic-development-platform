"""Operator remedies as structured actions, rendered for the audience that reads the alert.

A remedy is authored once, as prose interleaved with actions such as restarting a run or deciding
a gate. The delivery layer renders it for the deployment posture of the operator who reads the
alert. A host operator works in a platform checkout and runs ``inv`` tasks. A deployed operator
works against the deployment with ``kubectl`` and ``aws`` and has no checkout, so each action
renders as the command that reaches the tenant's activity worker directly.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import assert_never

NEXT_STEP_PREFIX = "Next step:"

WORKER_DEPLOYMENT = "activity-worker"
DEPLOYED_OPERATOR_TOOLS = frozenset({"aws", "kubectl"})


class DeploymentPosture(StrEnum):
    HOST = "host"
    DEPLOYED = "deployed"


class StepKind(StrEnum):
    TEXT = "text"
    RESTART_RUN = "restart_run"
    RETRY_REPAIR = "retry_repair"
    ABANDON_REPAIR = "abandon_repair"
    APPROVE_GATE = "approve_gate"
    REJECT_GATE = "reject_gate"
    ROLL_UP_COST = "roll_up_cost"
    CONFIG_KEY = "config_key"
    WORKER_SETTING = "worker_setting"


COMMAND_KINDS = frozenset(
    {
        StepKind.RESTART_RUN,
        StepKind.RETRY_REPAIR,
        StepKind.ABANDON_REPAIR,
        StepKind.APPROVE_GATE,
        StepKind.REJECT_GATE,
        StepKind.ROLL_UP_COST,
    }
)


@dataclass(frozen=True)
class RemedyStep:
    """One piece of a remedy, either prose or an action with the arguments it needs.

    ``kind`` crosses the Temporal payload boundary as a plain ``str``, because an enum-typed field
    raises in the data converter when a worker receives a member it does not know. The renderer
    narrows it, and an unknown kind renders as its ``text``.
    """

    kind: str
    text: str = ""
    issue_key: str = ""
    tenant: str = ""
    gate: str = ""
    execution_id: str = ""
    config: str = ""
    key: str = ""


@dataclass(frozen=True)
class Remedy:
    steps: tuple[RemedyStep, ...] = ()

    def for_tenant(self, tenant: str) -> Remedy:
        """Bind every action that names no tenant to ``tenant``, the tenant whose run alerted."""
        return Remedy(
            tuple(step if step.tenant else replace(step, tenant=tenant) for step in self.steps)
        )

    @property
    def actions(self) -> tuple[RemedyStep, ...]:
        """The steps an operator runs, which a chat renderer can present as buttons."""
        return tuple(step for step in self.steps if step.kind in COMMAND_KINDS)


def remedy(*parts: str | RemedyStep) -> Remedy:
    return Remedy(
        tuple(
            RemedyStep(StepKind.TEXT, text=part) if isinstance(part, str) else part
            for part in parts
        )
    )


def restart_run(issue_key: str) -> RemedyStep:
    return RemedyStep(StepKind.RESTART_RUN, issue_key=issue_key)


def retry_repair(issue_key: str) -> RemedyStep:
    return RemedyStep(StepKind.RETRY_REPAIR, issue_key=issue_key)


def abandon_repair(issue_key: str) -> RemedyStep:
    return RemedyStep(StepKind.ABANDON_REPAIR, issue_key=issue_key)


def approve_gate(issue_key: str, gate: str) -> RemedyStep:
    return RemedyStep(StepKind.APPROVE_GATE, issue_key=issue_key, gate=gate)


def reject_gate(issue_key: str, gate: str) -> RemedyStep:
    return RemedyStep(StepKind.REJECT_GATE, issue_key=issue_key, gate=gate)


def roll_up_cost(execution_id: str) -> RemedyStep:
    return RemedyStep(StepKind.ROLL_UP_COST, execution_id=execution_id)


def config_key(config: str, key: str) -> RemedyStep:
    """Name a key of a per-environment config file, located for the operator's posture.

    Both halves are checked against the committed template by the operator remedy contract tests,
    so a key renamed in the template fails the suite rather than sending an operator to a key that
    is not there.
    """
    return RemedyStep(StepKind.CONFIG_KEY, config=config, key=key)


def worker_setting(name: str) -> RemedyStep:
    return RemedyStep(StepKind.WORKER_SETTING, key=name)


def _value(value: str, placeholder: str) -> str:
    return shlex.quote(value) if value else placeholder


def _host_command(kind: StepKind, step: RemedyStep) -> str:
    issue = _value(step.issue_key, "<issue_key>")
    tenant = f" --tenant {shlex.quote(step.tenant)}" if step.tenant else ""
    match kind:
        case StepKind.RESTART_RUN:
            return f"inv restart --issue {issue}{tenant}"
        case StepKind.RETRY_REPAIR:
            return f"inv repair --issue {issue} --retry{tenant}"
        case StepKind.ABANDON_REPAIR:
            return f"inv repair --issue {issue} --abandon{tenant}"
        case StepKind.APPROVE_GATE | StepKind.REJECT_GATE:
            decision = "--approve" if kind is StepKind.APPROVE_GATE else "--reject"
            gate = _value(step.gate, "<gate>")
            return f"inv gate --issue {issue} --gate {gate} {decision}{tenant}"
        case StepKind.ROLL_UP_COST:
            return f"inv cost-rollup --execution-id {_value(step.execution_id, '<execution_id>')}"
        case StepKind.TEXT | StepKind.CONFIG_KEY | StepKind.WORKER_SETTING:
            return ""
        case _:
            assert_never(kind)


def _worker_module(kind: StepKind, step: RemedyStep) -> str:
    issue = _value(step.issue_key, "<issue_key>")
    match kind:
        case StepKind.RESTART_RUN:
            return f"scripts.restart {issue}"
        case StepKind.RETRY_REPAIR:
            return f"scripts.repair {issue} --retry"
        case StepKind.ABANDON_REPAIR:
            return f"scripts.repair {issue} --abandon"
        case StepKind.APPROVE_GATE | StepKind.REJECT_GATE:
            decision = "--approve" if kind is StepKind.APPROVE_GATE else "--reject"
            return f"scripts.gate_decision {issue} --gate {_value(step.gate, '<gate>')} {decision}"
        case StepKind.ROLL_UP_COST:
            execution = _value(step.execution_id, "<execution_id>")
            return f"scripts.cost_rollup --execution-id {execution}"
        case StepKind.TEXT | StepKind.CONFIG_KEY | StepKind.WORKER_SETTING:
            return ""
        case _:
            assert_never(kind)


def _deployed_command(kind: StepKind, step: RemedyStep) -> str:
    module = _worker_module(kind, step)
    if not module:
        return ""
    namespace = f"tenant-{_value(step.tenant, '<tenant>')}"
    return (
        f"kubectl -n {namespace} exec -i deploy/{WORKER_DEPLOYMENT} -c {WORKER_DEPLOYMENT} -- "
        f"python -m {module}"
    )


def _kind(step: RemedyStep) -> StepKind | None:
    try:
        return StepKind(step.kind)
    except ValueError:
        return None


def command(step: RemedyStep, posture: DeploymentPosture) -> str:
    """Return the shell command an action runs for ``posture``, or ``""`` for a prose step."""
    kind = _kind(step)
    if kind is None:
        return ""
    match posture:
        case DeploymentPosture.HOST:
            return _host_command(kind, step)
        case DeploymentPosture.DEPLOYED:
            return _deployed_command(kind, step)
        case _:
            assert_never(posture)


def _is_tenant_file(config: str) -> bool:
    from services.integration.tenant_config import TENANT_FILES

    return config in TENANT_FILES


def _config_location(step: RemedyStep, posture: DeploymentPosture, environment: str) -> str:
    file = f"{step.config}.yaml"
    tenant = step.tenant or "<tenant>"
    match posture:
        case DeploymentPosture.HOST:
            shared = f"config/{environment}/{file}"
            if not (step.tenant and _is_tenant_file(step.config)):
                return f"`{step.key}` in `{shared}`"
            return (
                f"`{step.key}` in `config/{environment}/tenants/{step.tenant}/{file}`, or in "
                f"`{shared}` when the tenant bundle does not hold `{file}`"
            )
        case DeploymentPosture.DEPLOYED:
            if not _is_tenant_file(step.config):
                return (
                    f"`{step.key}` in `{file}` of the environment config the platform reads from "
                    "`PLATFORM_ENV_CONFIG_DIR`"
                )
            return (
                f"`{step.key}` in `{file}` of the tenant config bundle under `tenants/{tenant}/` "
                "of the bucket `TENANT_CONFIG_BUCKET` names, published from the tenant's config "
                "repository"
            )
        case _:
            assert_never(posture)


def _setting_location(step: RemedyStep, posture: DeploymentPosture, environment: str) -> str:
    match posture:
        case DeploymentPosture.HOST:
            tenant_file = (
                f", or in `config/{environment}/tenants/{step.tenant}.env` when `{step.tenant}` "
                "is not the home tenant"
                if step.tenant
                else ""
            )
            return (
                f"`{step.key}` in `.env`{tenant_file}, applied to the workers with `inv k8s-reload`"
            )
        case DeploymentPosture.DEPLOYED:
            tenant = step.tenant or "<tenant>"
            return (
                f"`{step.key}` in the settings of tenant `{tenant}` in the deployment's `tenants` "
                "variable, applied by redeploying the tenant's activity worker"
            )
        case _:
            assert_never(posture)


def render_step(step: RemedyStep, posture: DeploymentPosture, *, environment: str) -> str:
    kind = _kind(step)
    if kind is None:
        return step.text
    match kind:
        case StepKind.TEXT:
            return step.text
        case StepKind.CONFIG_KEY:
            return _config_location(step, posture, environment)
        case StepKind.WORKER_SETTING:
            return _setting_location(step, posture, environment)
        case (
            StepKind.RESTART_RUN
            | StepKind.RETRY_REPAIR
            | StepKind.ABANDON_REPAIR
            | StepKind.APPROVE_GATE
            | StepKind.REJECT_GATE
            | StepKind.ROLL_UP_COST
        ):
            return f"`{command(step, posture)}`"
        case _:
            assert_never(kind)


def render(remedy: Remedy, posture: DeploymentPosture, *, environment: str) -> str:
    return "".join(render_step(step, posture, environment=environment) for step in remedy.steps)


def with_remedy(body: str, remedy_text: str) -> str:
    """Append the remedy to ``body`` as a ``Next step:`` line, or return ``body`` unchanged."""
    if not remedy_text:
        return body
    return f"{body}\n{NEXT_STEP_PREFIX} {remedy_text}"
