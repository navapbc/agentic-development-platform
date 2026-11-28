"""Point the rebar ticket store at its S3 bucket and check the remote at worker startup.

The store syncs through the git-remote-s3 helper, which reads the AWS profile from the remote URL.
The fetch URL names the reader profile and the push URL names the writer profile, so git fetches
under the read-only role and pushes under the write role without a change to rebar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services import process
from services.integration.credentials import ConfigError

if TYPE_CHECKING:
    from scripts.config import Settings

    from services.integration.tickets.store_config import TicketsStoreConfig

_S3_SCHEMES = ("s3://", "s3+zip://")
_NO_SUCH_REMOTE = 2
_NO_MATCHING_REF = 2
TICKETS_WORKTREE = ".tickets-tracker"


@dataclass(frozen=True)
class StoreRemote:
    name: str
    fetch_url: str
    push_url: str

    @property
    def is_s3(self) -> bool:
        return self.fetch_url.startswith(_S3_SCHEMES) or self.push_url.startswith(_S3_SCHEMES)


def _git(repo_root: str, *args: str) -> process.CompletedProcess[str]:
    return process.run(
        ["git", "-C", repo_root, *args],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def store_remote_name(repo_root: str) -> str:
    from rebar.config import tickets_remote

    return tickets_remote(repo_root)


def read_store_remote(repo_root: str) -> StoreRemote | None:
    """Return the store's ticket remote, or None when the store is local-only."""
    name = store_remote_name(repo_root)
    fetch = _git(repo_root, "remote", "get-url", name)
    if fetch.returncode == _NO_SUCH_REMOTE:
        return None
    if fetch.returncode != 0:
        raise ConfigError(
            f"could not read the {name!r} remote of the rebar store at {repo_root}: "
            f"{fetch.stderr.strip()}"
        )
    push = _git(repo_root, "remote", "get-url", "--push", name)
    if push.returncode != 0:
        raise ConfigError(
            f"could not read the {name!r} push URL of the rebar store at {repo_root}: "
            f"{push.stderr.strip()}"
        )
    return StoreRemote(name=name, fetch_url=fetch.stdout.strip(), push_url=push.stdout.strip())


def expected_store_remote(name: str, config: TicketsStoreConfig) -> StoreRemote:
    store = config.store
    if not (store.reader_profile and store.writer_profile):
        raise ConfigError(
            "tickets store config store.reader_profile and store.writer_profile are required "
            "for an S3 ticket store"
        )
    return StoreRemote(
        name=name,
        fetch_url=store.remote_url(store.reader_profile),
        push_url=store.remote_url(store.writer_profile),
    )


def configure_store_remote(repo_root: str, config: TicketsStoreConfig) -> StoreRemote:
    """Set the store's ticket remote to fetch as the reader and push as the writer."""
    expected = expected_store_remote(store_remote_name(repo_root), config)
    current = read_store_remote(repo_root)
    verb = "add" if current is None else "set-url"
    for args in (
        ("remote", verb, expected.name, expected.fetch_url),
        ("remote", "set-url", "--push", expected.name, expected.push_url),
    ):
        result = _git(repo_root, *args)
        if result.returncode != 0:
            raise ConfigError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return expected


def store_initialized(repo_root: str) -> bool:
    """Return whether rebar init completed, which leaves the tickets worktree in the store."""
    return (Path(repo_root) / TICKETS_WORKTREE).is_dir()


def seed_store(repo_root: str, config: TicketsStoreConfig, identity_id: str = "") -> bool:
    """Create the store from its S3 remote when rebar init has not completed in repo_root.

    rebar init attaches to the remote's tickets branch when the remote is reachable, so a new
    volume starts with every ticket the bucket holds. A seed that stopped after git init is
    completed on the next start rather than left as a repository without the ticket system. The
    selected identity is store-local state the bucket does not carry, so the given identity is
    selected again. Returns whether it seeded the store.
    """
    if store_initialized(repo_root):
        return False
    root = Path(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        initialized = _git(repo_root, "init", "--quiet")
        if initialized.returncode != 0:
            raise ConfigError(
                f"git init of the rebar store at {repo_root} failed: {initialized.stderr}"
            )
    remote = configure_store_remote(repo_root, config)
    bucket_empty = _remote_has_no_tickets_branch(repo_root, remote.name)
    try:
        _rebar(repo_root, "init", "--silent")
    except ConfigError as exc:
        raise ConfigError(f"{exc} {_remote_probe_detail(repo_root, remote.name)}") from exc
    if identity_id:
        _rebar(repo_root, "identity", "use", identity_id)
    if bucket_empty:
        _push_new_store(repo_root, remote.name)
    return True


def _probe_remote_branch(repo_root: str, remote: str) -> process.CompletedProcess[str] | None:
    try:
        return process.run(
            ["git", "-C", repo_root, "ls-remote", "--exit-code", remote, "refs/heads/tickets"],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except process.TimeoutExpired:
        return None


def _remote_has_no_tickets_branch(repo_root: str, remote: str) -> bool:
    """Return whether the remote answers and holds no tickets branch, exit 2 of the probe."""
    probe = _probe_remote_branch(repo_root, remote)
    return probe is not None and probe.returncode == _NO_MATCHING_REF


def _push_new_store(repo_root: str, remote: str) -> None:
    """Push a store created against an empty bucket, so the bucket's first history is this one.

    rebar pushes only after a ticket write, so an unpushed seed keeps a history of its own, and
    a ticket or identity written to the bucket from anywhere else starts a second, unrelated
    history that the two copies never converge from.
    """
    pushed = _git(repo_root, "push", "--quiet", remote, "tickets")
    if pushed.returncode != 0:
        raise ConfigError(
            f"pushing the new rebar store at {repo_root} to the empty bucket of {remote} "
            f"failed: {pushed.stderr.strip()[-600:]}"
        )


def _remote_probe_detail(repo_root: str, remote: str) -> str:
    """Return git's own answer to the branch probe rebar init runs, which rebar reports only as
    unreachable."""
    probe = _probe_remote_branch(repo_root, remote)
    if probe is None:
        limit = process.SHORT_COMMAND_TIMEOUT_SECONDS
        return f"git ls-remote {remote} did not answer within {limit}s."
    stderr = probe.stderr.strip()[-600:]
    return f"git ls-remote {remote} exited {probe.returncode}: {stderr or 'no output'}"


def select_store_identity(repo_root: str, identity_id: str) -> None:
    """Select ``identity_id`` in the store when it is not the current identity.

    The selection is store-local state that the bucket does not carry, and a seed sets it only
    once, so a store seeded before ``AUTHORSHIP_IDENTITY_ID`` was set has no identity and the
    intake poll refuses it. ``rebar identity use`` fails when the store holds no such identity.
    """
    if not identity_id or not store_initialized(repo_root):
        return
    import rebar

    if rebar.resolve_current_identity(repo_root=repo_root) == identity_id:
        return
    _rebar(repo_root, "identity", "use", identity_id)


def _rebar(repo_root: str, *args: str) -> None:
    result = process.run(
        ["rebar", *args],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConfigError(
            f"rebar {' '.join(args)} in the store at {repo_root} failed: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def seed_absent_store(repo_root: str, settings: Settings) -> None:
    """Seed the store when the tickets config names a bucket, and leave it absent otherwise."""
    from services.integration.tickets.store_config import load_tickets_store_config

    try:
        config = load_tickets_store_config()
    except ConfigError:
        return
    if config.store.bucket:
        seed_store(repo_root, config, settings.authorship_identity_id)


def _profile_role_arn(profile: str) -> str:
    from botocore.exceptions import ProfileNotFound
    from botocore.session import Session

    try:
        scoped = Session(profile=profile).get_scoped_config()
    except ProfileNotFound as exc:
        raise ConfigError(
            f"AWS profile {profile!r} for the rebar ticket store is not defined"
        ) from exc
    return str(scoped.get("role_arn", "") or "")


def check_store_remote(remote: StoreRemote, config: TicketsStoreConfig) -> None:
    """Fail when the remote does not fetch as the reader and push as the writer of the store."""
    expected = expected_store_remote(remote.name, config)
    if remote.fetch_url != expected.fetch_url:
        raise ConfigError(
            f"the rebar store remote {remote.name!r} fetches from {remote.fetch_url}, "
            f"expected {expected.fetch_url}. Run inv rebar-store-remote."
        )
    if remote.push_url != expected.push_url:
        raise ConfigError(
            f"the rebar store remote {remote.name!r} pushes to {remote.push_url}, "
            f"expected {expected.push_url}. Run inv rebar-store-remote."
        )
    for profile, role in (
        (config.store.reader_profile, config.roles.read_only),
        (config.store.writer_profile, config.roles.write),
    ):
        role_arn = _profile_role_arn(profile)
        if role_arn and role_arn != role:
            raise ConfigError(
                f"AWS profile {profile!r} assumes {role_arn}, but the tickets store config "
                f"names {role}"
            )


def tickets_store_preflight(settings: Settings) -> None:
    """Validate the ticket store isolation and its remote when the store syncs to S3."""
    from services.integration.tickets.backend import REBAR
    from services.integration.tickets.factory import resolve_ticket_backend_name
    from services.integration.tickets.rebar_config import load_rebar_backend_config
    from services.integration.tickets.store_config import load_tickets_store_config

    if resolve_ticket_backend_name(settings) != REBAR:
        return
    repo_root = load_rebar_backend_config().repo_root
    if not store_initialized(repo_root):
        seed_absent_store(repo_root, settings)
    select_store_identity(repo_root, settings.authorship_identity_id)
    remote = read_store_remote(repo_root)
    if remote is None or not remote.is_s3:
        return
    check_store_remote(remote, load_tickets_store_config())
