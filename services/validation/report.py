"""Check results and the reports that render them for a tenant, an operator, or an alert."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from services.operator_remedy import DeploymentPosture, Remedy, render


class Status(StrEnum):
    PASSED = "pass"
    FAIL = "fail"
    SKIP = "skip"


class Scope(StrEnum):
    PLATFORM = "platform"
    TENANT = "tenant"


MODEL_PATH = "Model path"
TEMPORAL = "Temporal"
SCHEMA = "Database schema"
DEPENDENCIES = (MODEL_PATH, TEMPORAL, SCHEMA)


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome.

    ``status`` and ``scope`` cross the Temporal payload boundary as plain strings. ``fix`` renders
    for both postures unless ``fix_deployed`` holds steps, which then render for the deployed
    posture. ``dependency`` names the platform dependency a tenant sees this result as.
    """

    name: str
    status: str
    scope: str = Scope.TENANT
    cause: str = ""
    fix: Remedy = field(default_factory=Remedy)
    fix_deployed: Remedy = field(default_factory=Remedy)
    confirm: str = ""
    dependency: str = ""

    @property
    def failed(self) -> bool:
        return self.status == Status.FAIL

    def fix_for(self, posture: DeploymentPosture) -> str:
        chosen = self.fix
        if posture is DeploymentPosture.DEPLOYED and self.fix_deployed.steps:
            chosen = self.fix_deployed
        return render(chosen, posture)


def passed(
    name: str, cause: str = "", *, scope: str = Scope.TENANT, dependency: str = ""
) -> CheckResult:
    return CheckResult(name, Status.PASSED, scope=scope, cause=cause, dependency=dependency)


def skipped(
    name: str, cause: str, *, scope: str = Scope.TENANT, dependency: str = ""
) -> CheckResult:
    return CheckResult(name, Status.SKIP, scope=scope, cause=cause, dependency=dependency)


def failed(
    name: str,
    cause: str,
    fix: Remedy,
    *,
    confirm: str,
    fix_deployed: Remedy | None = None,
    scope: str = Scope.TENANT,
    dependency: str = "",
) -> CheckResult:
    return CheckResult(
        name,
        Status.FAIL,
        scope=scope,
        cause=cause,
        fix=fix,
        fix_deployed=fix_deployed or Remedy(),
        confirm=confirm,
        dependency=dependency,
    )


@dataclass(frozen=True)
class Dependency:
    """A platform dependency as a tenant sees it, which carries no platform detail."""

    name: str
    ok: bool


@dataclass(frozen=True)
class TenantReport:
    tenant: str
    checks: list[CheckResult] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)

    @property
    def failing(self) -> list[str]:
        names = [check.name for check in self.checks if check.failed]
        return names + [dependency.name for dependency in self.dependencies if not dependency.ok]

    @property
    def failed(self) -> bool:
        return bool(self.failing)


@dataclass(frozen=True)
class PlatformReport:
    checks: list[CheckResult] = field(default_factory=list)
    tenants: list[TenantReport] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(check.failed for check in self.checks) or any(t.failed for t in self.tenants)


def dependencies(
    results: Iterable[CheckResult], platform_failures: Iterable[str] = ()
) -> list[Dependency]:
    """Fold platform results into one line per dependency, failing what the platform failed."""
    failing = set(platform_failures)
    seen: dict[str, bool] = {}
    for result in results:
        if result.dependency:
            seen[result.dependency] = seen.get(result.dependency, True) and not result.failed
    return [
        Dependency(name, seen.get(name, True) and name not in failing)
        for name in DEPENDENCIES
        if name in seen or name in failing
    ]


def _summary(results: Sequence[CheckResult], dependencies: Sequence[Dependency] = ()) -> str:
    counts = {status: 0 for status in Status}
    for result in results:
        counts[Status(result.status)] += 1
    fails = counts[Status.FAIL] + sum(not d.ok for d in dependencies)
    passes = counts[Status.PASSED] + sum(d.ok for d in dependencies)
    return f"Result: {fails} failed, {passes} passed, {counts[Status.SKIP]} skipped"


def _check_lines(result: CheckResult, posture: DeploymentPosture) -> list[str]:
    lines = [f"{Status(result.status).value.upper()} {result.name}"]
    if result.cause:
        label = "Detail" if result.status == Status.PASSED else "Cause"
        lines.append(f"  {label}: {result.cause}")
    if result.failed:
        fix = result.fix_for(posture)
        if fix:
            lines.append(f"  Fix: {fix}")
        if result.confirm:
            lines.append(f"  Confirm: {result.confirm}")
    return lines


def _ordered(results: Iterable[CheckResult]) -> list[CheckResult]:
    order = {Status.FAIL: 0, Status.SKIP: 1, Status.PASSED: 2}
    return sorted(results, key=lambda result: order[Status(result.status)])


_DEPENDENCY_FIX = (
    "  Fix: The platform administrator restores each failing dependency. Runs that need it "
    "wait or fail until it passes."
)


def _dependency_lines(dependencies: Sequence[Dependency]) -> list[str]:
    if not dependencies:
        return []
    lines = ["Platform dependencies:"]
    lines.extend(f"{'PASS' if d.ok else 'FAIL'} {d.name}" for d in dependencies)
    if any(not d.ok for d in dependencies):
        lines.append(_DEPENDENCY_FIX)
    return lines


def _own(report: TenantReport) -> list[CheckResult]:
    return [check for check in report.checks if check.scope == Scope.TENANT]


def render_tenant_report(report: TenantReport, posture: DeploymentPosture) -> str:
    """Render a tenant's report as plain lines with backticked commands, for Slack or a terminal.

    Only the tenant's own checks carry a cause and a fix. Each platform dependency is one pass
    or fail line, because its detail belongs to the platform administrator.
    """
    own = _own(report)
    lines = [
        f"Deployment validation of tenant {report.tenant}",
        _summary(own, report.dependencies),
    ]
    for check in _ordered(own):
        lines.extend(_check_lines(check, posture))
    lines.extend(_dependency_lines(report.dependencies))
    return "\n".join(lines)


def render_platform_report(report: PlatformReport, posture: DeploymentPosture) -> str:
    """Render the platform checks in full, then each tenant's report as the tenant sees it."""
    sections = []
    if report.checks:
        lines = ["Deployment validation of the platform", _summary(report.checks)]
        for check in _ordered(report.checks):
            lines.extend(_check_lines(check, posture))
        sections.append("\n".join(lines))
    sections.extend(render_tenant_report(tenant, posture) for tenant in report.tenants)
    sections.append(f"Deployment validation {'failed' if report.failed else 'passed'}.")
    return "\n\n".join(sections)


def platform_failure_alert(
    report: PlatformReport, posture: DeploymentPosture
) -> tuple[str, str] | None:
    """Return the subject and body of the administrator alert for the failing platform checks,
    each with its cause and fix, or None when every platform check passed."""
    failing = [check for check in _ordered(report.checks) if check.failed]
    if not failing:
        return None
    subject = "Deployment validation failed for the platform"
    lines = [
        subject,
        f"Failing checks: {', '.join(check.name for check in failing)}",
        "Effect: Every tenant whose runs need a failing check waits or fails until it passes.",
    ]
    for check in failing:
        lines.extend(_check_lines(check, posture))
    return subject, "\n".join(lines)


def failure_alert(report: TenantReport, posture: DeploymentPosture) -> tuple[str, str]:
    """Return the subject and body of the alert a scheduled validation sends for its failures."""
    subject = f"Deployment validation failed for tenant {report.tenant}"
    lines = [
        subject,
        f"Failing checks: {', '.join(report.failing)}",
        "Effect: Runs that need a failing check wait or fail until it passes.",
    ]
    for check in _own(report):
        if check.failed:
            lines.extend(_check_lines(check, posture))
    lines.extend(_dependency_lines([d for d in report.dependencies if not d.ok]))
    return subject, "\n".join(lines)
