"""Renew the workstation's AWS session and Bedrock tunnel when either needs it.

`run` renews the session through the `inv aws-login` code path when the stored credentials are
missing, expired, or within AWS_CREDENTIALS_WARN_SECONDS of expiry, and only when the source signs
in without a person, an AWS_LOGIN_COMMAND or a static access key. A source that needs an interactive
sign-in is reported and skipped. It then starts the tunnel in the background when the connectivity
provider forwards through one and no tunnel is running. It reports each renewal, tunnel restart,
and failure it cannot fix on the open model path episode of every tenant, whose worker posts the
report in the alert's threads at its next intake pass.

`watch` runs `run` every interval until it is stopped. `install` writes a launchd LaunchAgent that
keeps `uv run inv local-renew --watch` running, and `uninstall` removes it.
"""

from __future__ import annotations

import asyncio
import enum
import os
import plistlib
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services import process
from services.aws_credentials import expiration_stamp, expiry_of

from scripts.config import Settings, get_settings, to_host_url
from scripts.env_file import CHECKOUT_ROOT

LOG = "[local-renew]"
LABEL = "com.navapbc.agentic-dev-platform.local-renew"
DEFAULT_INTERVAL_SECONDS = 60
# An episode that opens while the report text is unchanged receives the text on the next refresh.
REPORT_REFRESH_CHECKS = 10
MANUAL_FIX = (
    "Follow How to fix in this alert, press Run validation, and press Fix done once validation "
    "passes."
)
REQUIRED_TOOLS = ("uv", "kubectl")
OPTIONAL_TOOLS = ("aws", "docker", "session-manager-plugin")
BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
STATIC_KEY_METHODS = frozenset(
    {"env", "explicit", "shared-credentials-file", "config-file", "boto-config"}
)


class Step(enum.Enum):
    RENEW = "renew"
    SKIP = "skip"
    START_TUNNEL = "start-tunnel"
    NONE = "none"


@dataclass(frozen=True)
class Action:
    step: Step
    reason: str


@dataclass(frozen=True)
class Session:
    stored: bool
    expiry: datetime | None
    renewal_source: str
    blocker: str = ""


@dataclass(frozen=True)
class Tunnel:
    provider: str
    required: bool
    running: bool


def session_action(session: Session, *, now: datetime, warn_seconds: int) -> Action:
    if not session.stored:
        why = "the aws-credentials Secret is missing"
    elif session.expiry is None:
        return Action(Step.NONE, "the credentials carry no expiry, so no renewal is needed")
    else:
        remaining = (session.expiry - now).total_seconds()
        stamp = expiration_stamp(session.expiry)
        if remaining > warn_seconds:
            return Action(Step.NONE, f"the credentials expire at {stamp}, so no renewal is needed")
        if remaining <= 0:
            why = f"the credentials expired at {stamp}"
        else:
            why = f"the credentials expire at {stamp}, in {int(remaining // 60)} minutes"
    if not session.renewal_source:
        blocker = session.blocker
        return Action(
            Step.SKIP,
            f"skipping the renewal because {why}. {blocker[:1].upper()}{blocker[1:]}. "
            "Run inv aws-login at a terminal",
        )
    return Action(Step.RENEW, f"renewing the session from {session.renewal_source} because {why}")


def tunnel_action(tunnel: Tunnel) -> Action:
    if not tunnel.required:
        return Action(
            Step.NONE,
            f"the {tunnel.provider} connectivity provider forwards through no tunnel",
        )
    if tunnel.running:
        return Action(Step.NONE, f"the {tunnel.provider} tunnel is running")
    return Action(
        Step.START_TUNNEL,
        f"starting the {tunnel.provider} tunnel because no background tunnel is running",
    )


def decide(session: Session, tunnel: Tunnel, *, now: datetime, warn_seconds: int) -> list[Action]:
    """Return the session action, then the tunnel action, each with the reason for it."""
    return [session_action(session, now=now, warn_seconds=warn_seconds), tunnel_action(tunnel)]


def _profile_session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile)


def renewal_source(
    settings: Settings,
    environ: Mapping[str, str],
    session_factory: Callable[[str], Any] = _profile_session,
) -> tuple[str, str]:
    """Return the source that renews without a person, or an empty source and why it cannot."""
    from scripts.aws_login import profile_name

    if settings.aws_login_command:
        return "AWS_LOGIN_COMMAND", ""
    profile = profile_name(settings, environ)
    try:
        credentials = session_factory(profile).get_credentials()
    except Exception as exc:  # noqa: BLE001
        return "", f"the AWS profile {profile} cannot be read ({exc})"
    if credentials is None:
        return "", f"the AWS profile {profile} resolves no credentials"
    method = getattr(credentials, "method", "")
    if method == "sso":
        return "", f"the AWS profile {profile} signs in through SSO, which needs a browser"
    if method not in STATIC_KEY_METHODS:
        return "", f"the AWS profile {profile} resolves its credentials through {method}"
    if credentials.get_frozen_credentials().token:
        return "", f"the AWS profile {profile} holds a session token that it cannot renew"
    return f"the static access key of the AWS profile {profile}", ""


def _renew() -> bool:
    from scripts import aws_login

    try:
        return aws_login.main([]) == 0
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG} aws-login raised {type(exc).__name__}, {exc}", file=sys.stderr)
        return False


def _start_tunnel() -> bool:
    from scripts import tunnel

    try:
        return tunnel.start(detach=True) == 0
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG} the tunnel start raised {type(exc).__name__}, {exc}", file=sys.stderr)
        return False


PERFORM: dict[Step, tuple[Callable[[], bool], str]] = {
    Step.RENEW: (_renew, "the session renewal failed"),
    Step.START_TUNNEL: (_start_tunnel, "the tunnel did not start"),
}


def action_report(
    action: Action, succeeded: bool | None, *, provider: str, now: datetime, blocker: str = ""
) -> tuple[str, bool]:
    """Return the episode report of ``action`` and whether it is a failure the agent cannot fix.

    ``succeeded`` is None for an action the agent did not perform. A report of a success names
    its time, so each success posts, and a failure names none, so a failure repeated each pass
    posts once.
    """
    at = now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if action.step is Step.RENEW:
        if succeeded:
            return f"The host agent renewed the AWS session at {at}.", False
        return "The host agent could not renew the AWS session, because the renewal failed.", True
    if action.step is Step.START_TUNNEL:
        if succeeded:
            return f"The host agent restarted the {provider} tunnel at {at}.", False
        return f"The host agent could not restart the {provider} tunnel.", True
    if action.step is Step.SKIP:
        return f"The host agent cannot renew the AWS session, because {blocker}.", True
    return "", False


def compose_report(lines: list[tuple[str, bool]]) -> str:
    texts = [text for text, _ in lines if text]
    if any(failure for _, failure in lines):
        texts.append(MANUAL_FIX)
    return " ".join(texts)


def report_host_action(text: str) -> int:
    """Write ``text`` on the open model path episodes, and return the number written."""
    from database.engine import create_platform_engine
    from services.orchestration.model_path import record_host_action

    async def _write() -> int:
        engine = create_platform_engine(to_host_url(get_settings().database_url))
        try:
            return await record_host_action(text, engine=engine)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_write())
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG} the report was not recorded, {type(exc).__name__}, {exc}", file=sys.stderr)
        return 0


def changed_reports(
    record: Callable[[str], int], refresh_after: int = REPORT_REFRESH_CHECKS
) -> Callable[[str], int]:
    last, skipped = "", 0

    def report(text: str) -> int:
        nonlocal last, skipped
        if text == last and skipped < refresh_after:
            skipped += 1
            return 0
        written = record(text)
        last, skipped = (text if written else ""), 0
        return written

    return report


def run(write_report: Callable[[str], int] | None = None) -> int:
    from scripts import aws_login
    from scripts.connectivity_providers import tunnel_running
    from scripts.k8s_render import RELAY_PROVIDERS

    settings = get_settings()
    provider = settings.bedrock_connectivity_provider
    required = provider in RELAY_PROVIDERS
    tunnel = Tunnel(provider, required, required and tunnel_running())
    failed = False
    now = datetime.now(UTC)
    lines: list[tuple[str, bool]] = []
    blocker = ""
    try:
        record = aws_login.stored_record(settings)
    except (aws_login.LoginError, ValueError, OSError) as exc:
        print(f"{LOG} the aws-credentials Secret could not be read, {exc}", file=sys.stderr)
        failed = True
        lines.append(("The host agent could not read the aws-credentials Secret.", True))
        actions = [tunnel_action(tunnel)]
    else:
        source, blocker = renewal_source(settings, aws_login.login_environ())
        expiry = expiry_of(record) if record is not None else None
        actions = decide(
            Session(record is not None, expiry, source, blocker),
            tunnel,
            now=now,
            warn_seconds=settings.aws_credentials_warn_seconds,
        )
    for action in actions:
        print(f"{LOG} {action.reason}")
        succeeded = None
        if action.step in PERFORM:
            perform, failure = PERFORM[action.step]
            succeeded = perform()
            if not succeeded:
                print(f"{LOG} {failure}", file=sys.stderr)
                failed = True
        lines.append(action_report(action, succeeded, provider=provider, now=now, blocker=blocker))
    report = compose_report(lines)
    if report:
        written = (write_report or report_host_action)(report)
        print(f"{LOG} reported on {written} open model path episodes")
    return 1 if failed else 0


def watch(
    interval: int,
    *,
    once: Callable[[], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if interval <= 0:
        print(f"{LOG} the interval must be a positive number of seconds", file=sys.stderr)
        return 2
    write_report = changed_reports(report_host_action)
    check = once or (lambda: run(write_report))
    while True:
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            print(f"{LOG} the check raised {type(exc).__name__}, {exc}", file=sys.stderr)
        sys.stdout.flush()
        sleep(interval)


def agent_path(tools: Mapping[str, str]) -> str:
    """Return a PATH holding the directory of each resolved tool ahead of the system directories."""
    directories: list[str] = []
    for binary in tools.values():
        directory = str(Path(binary).parent)
        if directory not in directories:
            directories.append(directory)
    directories += [entry for entry in BASE_PATH if entry not in directories]
    return os.pathsep.join(directories)


def launch_agent(
    *, tools: Mapping[str, str], interval: int, root: Path, log: Path, temp_dir: str
) -> dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            tools["uv"],
            "run",
            "inv",
            "local-renew",
            "--watch",
            "--interval",
            str(interval),
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        # launchd kills the processes left in the job's process group when the agent exits, and
        # the tunnel the agent starts must outlive a restart of the agent.
        "AbandonProcessGroup": True,
        # The tunnel pidfile lives in the temporary directory, so the agent reads the TMPDIR of the
        # shell that installed the job, where a tunnel that shell started records its pid.
        "EnvironmentVariables": {"PATH": agent_path(tools), "TMPDIR": temp_dir},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "agentic-dev-platform-local-renew.log"


def _refuse_other_platforms(platform: str) -> bool:
    if platform == "darwin":
        return False
    print(
        f"{LOG} the launchd job runs on macOS only. On another host, add the cron line in the "
        "README section Renewing the session on a schedule",
        file=sys.stderr,
    )
    return True


def install(
    interval: int,
    *,
    home: Path | None = None,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    run_command: Callable[..., Any] = process.run,
    uid: int | None = None,
) -> int:
    if _refuse_other_platforms(platform):
        return 2
    if interval <= 0:
        print(f"{LOG} the interval must be a positive number of seconds", file=sys.stderr)
        return 2
    resolved = {tool: which(tool) for tool in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS)}
    missing = [tool for tool in REQUIRED_TOOLS if not resolved[tool]]
    if missing:
        print(f"{LOG} {', '.join(missing)} is not on PATH, so the job cannot run", file=sys.stderr)
        return 1
    tools = {tool: path for tool, path in resolved.items() if path}
    home = home or Path.home()
    target, log = plist_path(home), log_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    agent = launch_agent(
        tools=tools,
        interval=interval,
        root=CHECKOUT_ROOT,
        log=log,
        temp_dir=tempfile.gettempdir(),
    )
    with target.open("wb") as handle:
        plistlib.dump(agent, handle)
    print(f"{LOG} wrote {target}, checking every {interval} seconds")
    domain = f"gui/{os.getuid() if uid is None else uid}"
    run_command(
        ["launchctl", "bootout", f"{domain}/{LABEL}"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=False,
    )
    loaded = run_command(
        ["launchctl", "bootstrap", domain, str(target)],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=False,
    )
    if loaded.returncode != 0:
        detail = (loaded.stderr or "").strip() or f"exit {loaded.returncode}"
        print(f"{LOG} launchctl bootstrap {domain} failed with {detail}", file=sys.stderr)
        return 1
    print(f"{LOG} loaded {LABEL} into {domain}, logging to {log}")
    return 0


def uninstall(
    *,
    home: Path | None = None,
    platform: str = sys.platform,
    run_command: Callable[..., Any] = process.run,
    uid: int | None = None,
) -> int:
    if _refuse_other_platforms(platform):
        return 2
    target = plist_path(home or Path.home())
    domain = f"gui/{os.getuid() if uid is None else uid}"
    unloaded = run_command(
        ["launchctl", "bootout", f"{domain}/{LABEL}"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=False,
    )
    state = "unloaded" if unloaded.returncode == 0 else "found no loaded job"
    print(f"{LOG} {state} {LABEL} in {domain}")
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    else:
        print(f"{LOG} removed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    watcher = commands.add_parser("watch")
    watcher.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    installer = commands.add_parser("install")
    installer.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    commands.add_parser("uninstall")
    args = parser.parse_args(argv)
    if args.command == "install":
        return install(args.interval)
    if args.command == "uninstall":
        return uninstall()
    if args.command == "watch":
        return watch(args.interval)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
