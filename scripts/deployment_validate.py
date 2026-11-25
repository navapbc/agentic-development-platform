"""Run the deployment validation inside the deployment, as the deployment-validate Job does.

Exactly one scope selects what runs. ``--platform`` runs the platform checks. ``--all-tenants``
also runs the validation of every tenant TENANTS names, and ``--tenant`` that of one tenant, each
on its tenant's task queue. ``--alert-failures``, which the daily CronJob passes, makes each
tenant send its failing checks to its own escalation channels and sends the failing platform
checks with their fixes to the administrator destination of platform-escalation.yaml.
``--alert-platform``, which the post-deploy validation passes, sends only the platform checks to
the administrators. ``--post`` makes each tenant post its report to its Slack channel. The report
goes to stdout, and the exit code is 1 when any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

LOG = "[deployment-validate]"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--platform", action="store_true", help="Run the platform checks only.")
    scope.add_argument(
        "--all-tenants", action="store_true", help="Also validate every tenant TENANTS names."
    )
    scope.add_argument("--tenant", default="", help="Also validate this tenant.")
    parser.add_argument(
        "--alert-failures",
        action="store_true",
        help="Send each tenant's failing checks to its escalation channels.",
    )
    parser.add_argument(
        "--alert-platform",
        action="store_true",
        help="Send the failing platform checks to the administrator destination.",
    )
    parser.add_argument(
        "--post", action="store_true", help="Post each tenant's report to its Slack channel."
    )
    return parser.parse_args(argv)


def scope_tenants(args: argparse.Namespace, known: Sequence[str]) -> list[str]:
    """Return the tenants whose validation runs, refusing a tenant TENANTS does not name."""
    if args.platform:
        return []
    if args.all_tenants:
        return list(known)
    if args.tenant not in known:
        names = ", ".join(known) or "no tenant"
        raise SystemExit(f"{LOG} TENANTS names {names}, not {args.tenant}")
    return [args.tenant]


async def _run(args: argparse.Namespace) -> int:
    from services import temporal_client
    from services.orchestration.task_queues import tenant_names
    from services.validation.platform import alert_platform_failures, validate_deployment
    from services.validation.report import render_platform_report
    from services.validation.tenant import TenantValidationRequest, posture

    from scripts.config import get_settings

    settings = get_settings()
    known = tenant_names(settings)
    tenants = scope_tenants(args, known)
    client, error = None, ""
    try:
        client = await temporal_client.connect(settings.temporal_host, settings)
    except Exception as exc:  # noqa: BLE001
        error = f"{settings.temporal_host} refused the connection ({type(exc).__name__}). {exc}"
    report = await validate_deployment(
        settings,
        tenants=tenants,
        queues=tenants or known,
        request=TenantValidationRequest(post=args.post, alert_failures=args.alert_failures),
        client=client,
        temporal_error=error,
    )
    if args.alert_failures or args.alert_platform:
        alert_platform_failures(report, settings)
    print(render_platform_report(report, posture(settings)))
    return 1 if report.failed else 0


def _log_errors_only() -> None:
    logging.basicConfig(level=logging.ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from scripts.aws_clients import configure_aws_fips

    _log_errors_only()
    configure_aws_fips()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
