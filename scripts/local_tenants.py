"""The tenants of this checkout, the folders of config/local/tenants/ with their settings files.

A tenant is a folder config/local/tenants/<tenant>/, which holds its config bundle, and the
settings file config/local/tenants/<tenant>.env beside it. A folder or settings file whose tenant
name ends in .example is a template, such as tenant.example/ and tenant.example.env. A folder
without its settings file, or a settings file without its folder, is refused, so a tenant that is
half onboarded or half offboarded stops every host command that reads the list. Onboarding or
offboarding a tenant changes no platform file.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from scripts import env_file

TEMPLATE_SUFFIX = ".example"
SETTINGS_SUFFIX = ".env"
TEMPLATE_FOLDER = f"tenant{TEMPLATE_SUFFIX}"
TEMPLATE_SETTINGS = f"{TEMPLATE_FOLDER}{SETTINGS_SUFFIX}"


class TenantLayoutError(ValueError):
    """Raised when the tenant folders and settings files do not name one set of tenants."""


def local_tenant_names(config_dir: Path | None = None) -> list[str]:
    """Return the tenants under config_dir/tenants/, by default this checkout's config/local/."""
    from services.orchestration.task_queues import TENANT_NAME

    directory = (
        config_dir / "tenants"
        if config_dir is not None
        else env_file.CHECKOUT_ROOT / env_file.TENANTS_DIRECTORY
    )
    if not directory.is_dir():
        return []
    folders: set[str] = set()
    settings: set[str] = set()
    for entry in directory.iterdir():
        tenant = entry.name.removesuffix(SETTINGS_SUFFIX) if entry.is_file() else entry.name
        if entry.name.startswith(".") or tenant.endswith(TEMPLATE_SUFFIX):
            continue
        if entry.is_dir():
            folders.add(entry.name)
        elif entry.is_file() and entry.name.endswith(SETTINGS_SUFFIX):
            settings.add(tenant)
    shown = _shown(directory)
    without_settings = sorted(folders - settings)
    if without_settings:
        tenant = without_settings[0]
        raise TenantLayoutError(
            f"{shown}/{tenant}/ has no settings file {shown}/{tenant}.env. Copy "
            f"{shown}/{TEMPLATE_SETTINGS} to {shown}/{tenant}.env, or remove {shown}/{tenant}/ to "
            "offboard the tenant"
        )
    without_folder = sorted(settings - folders)
    if without_folder:
        tenant = without_folder[0]
        raise TenantLayoutError(
            f"{shown}/{tenant}.env has no tenant folder {shown}/{tenant}/. Copy "
            f"{shown}/{TEMPLATE_FOLDER}/ to {shown}/{tenant}/, or remove {shown}/{tenant}.env to "
            "offboard the tenant"
        )
    invalid = sorted(name for name in folders if not TENANT_NAME.fullmatch(name))
    if invalid:
        raise TenantLayoutError(
            f"{shown}/{invalid[0]}/ is not a valid tenant name. A tenant name is 2 to 16 "
            "lower-case letters and digits that starts with a letter, and is not default"
        )
    return sorted(folders)


def select_tenant(requested: str = "", names: Sequence[str] | None = None) -> str:
    """Return the requested tenant, or the one local tenant when none is requested.

    A request may be omitted only when exactly one tenant exists, so a command never acts on a
    tenant the operator did not name.
    """
    tenants = local_tenant_names() if names is None else list(names)
    listed = ", ".join(tenants)
    if requested:
        if requested not in tenants:
            raise TenantLayoutError(
                f"{requested} is not a local tenant. The local tenants are {listed or 'none'}, "
                "the folders of config/local/tenants/ with their settings files"
            )
        return requested
    if len(tenants) == 1:
        return tenants[0]
    if not tenants:
        raise TenantLayoutError(
            f"no local tenant exists. Copy config/local/tenants/{TEMPLATE_FOLDER}/ to "
            f"config/local/tenants/<tenant>/ and config/local/tenants/{TEMPLATE_SETTINGS} to "
            "config/local/tenants/<tenant>.env"
        )
    raise TenantLayoutError(
        f"{len(tenants)} local tenants exist, {listed}. Name the tenant to act on with --tenant"
    )


def _shown(directory: Path) -> str:
    try:
        return directory.relative_to(env_file.CHECKOUT_ROOT).as_posix()
    except ValueError:
        return directory.as_posix()
