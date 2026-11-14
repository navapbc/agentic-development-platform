"""Add or prune image finding exceptions with the validation every reader applies.

`add` writes one entry to the platform's or a tenant's image_exceptions.yaml after it checks the
entry's fields, its scope, and that its expiry falls within the window from today. With
--scan-log it also requires the finding to appear in that `inv image-scan` log, and with
--approvals it reads the approver from the GitHub approvals of the workflow run. `prune` removes
the expired entries. `check` reports a missing or unusable platform file.

Without TENANT_CONFIG_BUCKET both scopes write the local files. With it set the platform scope
writes platform/image_exceptions.yaml in the bucket through a conditional put, which only the
image-exception workflow does, and a tenant adds entries to its bundle and publishes it with
`inv tenant-config-publish`. --local refuses to run when TENANT_CONFIG_BUCKET is set.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

LOG = "[image-exceptions]"
ENVIRONMENT = "image-exception"
SCOPES = ("platform", "tenant")


def _settings(tenant: str = "") -> Any:
    from scripts.config import get_settings

    settings = get_settings()
    return settings.model_copy(update={"tenant": tenant or settings.tenant})


def _today() -> date:
    return datetime.now(UTC).date()


def approver_from(approvals: Any, environment: str = ENVIRONMENT) -> str:
    """Return the login of the reviewer who last approved the run for the environment."""
    from services.integration.credentials import ConfigError

    approved = [
        approval
        for approval in (approvals if isinstance(approvals, list) else [])
        if approval.get("state") == "approved"
        and any(e.get("name") == environment for e in approval.get("environments") or [])
    ]
    if not approved:
        raise ConfigError(f"no reviewer approved this run for the {environment} environment")
    return str((approved[-1].get("user") or {}).get("login") or "")


def require_active(finding: str, scan_log: str) -> None:
    """Refuse a finding the `inv image-scan` log does not report."""
    from services.integration.credentials import ConfigError

    from scripts.upstream_releases import parse_findings

    reported = parse_findings(scan_log)
    if finding not in reported:
        listed = ", ".join(reported) or "no finding"
        raise ConfigError(
            f"the scan of the image does not report {finding}, so it needs no exception. "
            f"The scan reports {listed}."
        )


def _target(settings: Any, scope: str) -> tuple[Any, Any]:
    """Return the file the scope writes and the check its entries must pass."""
    from services.integration.image_exceptions import (
        platform_file,
        scope_check,
        scope_policy,
        tenant_file,
        tenant_mirrors,
    )

    policy = scope_policy(settings)
    if scope == "platform":
        file = platform_file(settings)
        return file, scope_check(scope, policy, source=file.location)
    file = tenant_file(settings)
    mirrors = tenant_mirrors(file.path.parent)
    check = scope_check(
        scope, policy, tenant=settings.tenant, mirrors=mirrors, source=file.location
    )
    return file, check


def _refuse_bucket(settings: Any, args: argparse.Namespace) -> str:
    if not settings.tenant_config_bucket:
        return ""
    if args.local:
        return (
            "TENANT_CONFIG_BUCKET is set, so the local files are not the deployment's. "
            "The image-exception workflow writes the platform file, and a tenant publishes its "
            "file with its bundle through inv tenant-config-publish."
        )
    if args.scope == "tenant":
        return (
            "a tenant's entries live in its bundle. Add the entry to the bundle's "
            "image_exceptions.yaml and publish it with inv tenant-config-publish."
        )
    return ""


def add(args: argparse.Namespace) -> int:
    from services.integration.image_exceptions import update
    from services.integration.image_policy import parse_exception, require_new_entry

    settings = _settings(args.tenant)
    refusal = _refuse_bucket(settings, args)
    if refusal:
        print(f"{LOG} {refusal}", file=sys.stderr)
        return 2
    approver = args.approver
    if args.approvals:
        approver = approver_from(json.loads(Path(args.approvals).read_text(encoding="utf-8")))
    today = _today()
    entry = {
        "image": args.image,
        "finding": args.finding,
        "reason": args.reason,
        "created": today.isoformat(),
        "expires": args.expires,
        "requester": args.requester or getpass.getuser(),
        "approver": approver,
    }
    if args.scope == "tenant":
        entry.pop("approver")
    exception = parse_exception(entry, scope=args.scope, context="the new entry")
    require_new_entry(exception, today)
    file, check = _target(settings, args.scope)
    check((exception,))
    if args.scan_log:
        require_active(exception.finding, Path(args.scan_log).read_text(encoding="utf-8"))
    if args.dry_run:
        print(f"{LOG} the entry for {exception.describe()} is valid for {file.location}")
        return 0

    def replace(current: tuple[Any, ...]) -> tuple[Any, ...]:
        kept = [e for e in current if (e.image, e.finding) != (exception.image, exception.finding)]
        return (*kept, exception)

    result = update(file, replace, scope=args.scope, check=check)
    print(f"{LOG} {result.location} accepts {exception.describe()} until {exception.expires}")
    print(f"{LOG} version {result.version}")
    return 0


def prune(args: argparse.Namespace) -> int:
    from services.integration.image_exceptions import update

    settings = _settings(args.tenant)
    refusal = _refuse_bucket(settings, args)
    if refusal:
        print(f"{LOG} {refusal}", file=sys.stderr)
        return 2
    today = _today()
    file, check = _target(settings, args.scope)

    def unexpired(current: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(e for e in current if e.expires >= today)

    result = update(file, unexpired, scope=args.scope, check=check)
    removed = [e for e in result.before if e not in result.after]
    if not removed:
        print(f"{LOG} {result.location} holds no expired entry")
        return 0
    for exception in removed:
        print(f"{LOG} removed {exception.describe()}, expired on {exception.expires}")
    print(f"{LOG} {result.location} is now at version {result.version}")
    return 0


def check(_: argparse.Namespace) -> int:
    from services.integration.image_exceptions import exceptions_setup_problems

    problems = exceptions_setup_problems(_settings())
    for problem in problems:
        print(f"{LOG} {problem}", file=sys.stderr)
    if not problems:
        print(f"{LOG} the platform exceptions file is usable")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    from services.integration.credentials import ConfigError

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add", help="Add or renew one entry.")
    pruning = commands.add_parser("prune", help="Remove the expired entries.")
    commands.add_parser("check", help="Report a missing or unusable platform file.")
    for command in (adding, pruning):
        command.add_argument("--scope", choices=SCOPES, default="platform")
        command.add_argument("--tenant", default="", help="Tenant name. Defaults to TENANT.")
        command.add_argument(
            "--local", action="store_true", help="Refuse to run when TENANT_CONFIG_BUCKET is set."
        )
    adding.add_argument("--image", required=True, help="The image as repository@sha256:digest.")
    adding.add_argument("--finding", required=True, help="The finding ID, such as a CVE.")
    adding.add_argument("--reason", required=True, help="Why the finding is accepted.")
    adding.add_argument("--expires", required=True, help="The last day the entry applies.")
    adding.add_argument("--requester", default="", help="Defaults to the operating system user.")
    adding.add_argument("--approver", default="", help="The approver of a platform entry.")
    adding.add_argument(
        "--approvals", default="", help="A GitHub run approvals JSON to read the approver from."
    )
    adding.add_argument(
        "--scan-log", default="", help="An inv image-scan log that must report the finding."
    )
    adding.add_argument("--dry-run", action="store_true", help="Validate and write nothing.")
    args = parser.parse_args(argv)
    handlers = {"add": add, "prune": prune, "check": check}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
