"""Point the rebar ticket store at its S3 bucket and check the remote at worker startup.

The store syncs through the git-remote-s3 helper, which reads the AWS profile from the remote URL.
The fetch URL names the reader profile and the push URL names the writer profile, so git fetches
under the read-only role and pushes under the write role without a change to rebar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services import process
from services.integration.credentials import ConfigError

if TYPE_CHECKING:
    from scripts.config import Settings

    from services.integration.tickets.store_config import TicketsStoreConfig

_S3_SCHEMES = ("s3://", "s3+zip://")
_NO_SUCH_REMOTE = 2


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
    repo_root = load_rebar_backend_config(settings).repo_root
    remote = read_store_remote(repo_root)
    if remote is None or not remote.is_s3:
        return
    check_store_remote(remote, load_tickets_store_config(settings))
