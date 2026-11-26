"""invoke task runner for agentic-dev-platform."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from invoke import Collection, Exit, task
from scripts.build_id import recorded_build_id, resolve_build_id
from scripts.env_file import PLATFORM_ENV_FILE
from services import process

COMPOSE = f"docker compose --env-file {PLATFORM_ENV_FILE.as_posix()}"

# A task that runs on the host must use the interpreter running invoke. A bare `python` resolves
# to whatever the operator's PATH offers, which on a machine with a system or pyenv Python has
# none of the project's dependencies and fails on an import rather than on a missing interpreter.
HOST_PYTHON = shlex.quote(sys.executable)


def require_pinned_python(
    version_file: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".python-version"),
    running: tuple[int, int] = (sys.version_info.major, sys.version_info.minor),
) -> None:
    """Refuse to run on an interpreter older than the one .python-version names."""
    try:
        with open(version_file, encoding="utf-8") as handle:
            pinned = handle.read().strip()
    except FileNotFoundError:
        return
    required = tuple(int(part) for part in pinned.split(".")[:2])
    if running < required:
        raise SystemExit(
            f"inv runs on Python {running[0]}.{running[1]}, and .python-version requires "
            f"{pinned}. Run `uv sync --locked --all-extras` without --python, then rerun."
        )


require_pinned_python()


def _env_file_value(name: str, path: str = PLATFORM_ENV_FILE.as_posix()) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == name:
                return value.strip().strip("\"'")
    return ""


_DEV_MOUNTS_COMPOSE = "compose.dev-mounts.yaml"
_DEV_MOUNTS_FLAG = "COMPOSE_DEV_MOUNTS"


def _compose_overlays() -> list[str]:
    """Return the overlays applied on top of compose.yaml, in Compose's merge order.

    The workflow worker runs workflow code from the image the last build produced, unless
    COMPOSE_DEV_MOUNTS opts in to running it from the working tree.
    """
    dev_mounts = (os.environ.get(_DEV_MOUNTS_FLAG) or _env_file_value(_DEV_MOUNTS_FLAG)).lower()
    overlays = ["compose.override.yaml"]
    if dev_mounts in ("1", "true", "yes"):
        overlays.append(_DEV_MOUNTS_COMPOSE)
    overlays.append(_REPOSITORIES_COMPOSE)
    return [overlay for overlay in overlays if os.path.exists(overlay)]


def _compose_file_list() -> list[str]:
    configured = os.environ.get("COMPOSE_FILE") or _env_file_value("COMPOSE_FILE")
    if configured:
        return configured.split(os.environ.get("COMPOSE_PATH_SEPARATOR", os.pathsep))
    return ["compose.yaml", *_compose_overlays()]


def _compose_files() -> str:
    """Build the -f argument list, deferring to COMPOSE_FILE when the operator sets it.

    Explicit -f flags override COMPOSE_FILE, so emitting them unconditionally would make
    every task target a different projection of the stack than the one Compose itself
    resolves. Compose reads COMPOSE_FILE from the environment and from its env file,
    config/local/platform.env, so both are checked here.
    """
    if os.environ.get("COMPOSE_FILE") or _env_file_value("COMPOSE_FILE"):
        return ""
    files = ["-f", "compose.yaml"]
    for overlay in _compose_overlays():
        files += ["-f", overlay]
    return " ".join(files)


_REPOSITORIES_COMPOSE = "compose.repositories.yaml"


@task
def repos_compose(c):
    """Write compose.repositories.yaml, the registry, tenant bundle, and checkout mounts."""
    c.run(f"{HOST_PYTHON} -m scripts.repositories_compose", pty=True)


def _on_host(command: str) -> str:
    """Point a ``python ...`` command at the interpreter running invoke."""
    if command == "python" or command.startswith("python "):
        return HOST_PYTHON + command[len("python") :]
    return command


def _tenant(requested: str = "") -> str:
    """Return the tenant a host task acts on, the one --tenant names or else the one local tenant.

    With two or more local tenants, a task without --tenant is refused.
    """
    from scripts.local_tenants import TenantLayoutError, select_tenant

    try:
        return select_tenant(requested)
    except TenantLayoutError as exc:
        raise Exit(str(exc)) from exc


def _tenant_env(requested: str = "") -> dict[str, str]:
    """Return the TENANT that makes a host command load the tenant's settings file.

    The command reads config/local/platform.env and then config/local/tenants/<tenant>.env.
    """
    return {"TENANT": _tenant(requested)}


_TENANT_HELP = "Tenant to act for. It may be omitted only when one local tenant exists."


def _run_scripted(c, command: str, *, local: bool, tenant: str = "") -> None:
    """Run a python -m command in the tenant's activity worker, or on the host with local=True.

    The host command runs with TENANT set to the tenant, so it reads the tenant's settings file.
    """
    env = _tenant_env(tenant)
    if local:
        c.run(_on_host(command), pty=True, env=env)
        return
    from scripts.config import get_settings
    from scripts.k8s_cluster import exec_argv

    settings = get_settings().model_copy(update={"tenant": env["TENANT"]})
    c.run(shlex.join(exec_argv(settings, shlex.split(command), tty=False)), pty=True)


def _compose_file_flags() -> str:
    """Name the compose files the cluster scripts read the published ports from."""
    tokens = shlex.split(_compose_files())
    names = [tokens[index + 1] for index, part in enumerate(tokens) if part == "-f"]
    return "".join(f" --compose-file {shlex.quote(name)}" for name in names)


def _k8s(
    c, action: str, build_id: str = "", extra: str = "", env: dict[str, str] | None = None
) -> None:
    flags = f" --build-id {shlex.quote(build_id)}" if build_id else ""
    c.run(
        f"{HOST_PYTHON} -m scripts.k8s_cluster {action}{flags}{_compose_file_flags()}{extra}",
        pty=True,
        env=env or {},
    )


@task(help={"detach": "Run the tunnel in the background."})
def tunnel_start(c, detach=False):
    """Start the jump-host tunnel to Bedrock."""
    c.run(f"{HOST_PYTHON} -m scripts.tunnel start {'--detach' if detach else ''}".strip(), pty=True)


@task
def tunnel_stop(c):
    """Stop the backgrounded Bedrock connectivity tunnel."""
    c.run(f"{HOST_PYTHON} -m scripts.tunnel stop", pty=True)


@task
def tunnel_status(c):
    """Report whether the backgrounded Bedrock connectivity tunnel is running."""
    c.run(f"{HOST_PYTHON} -m scripts.tunnel status", pty=True)


@task
def connectivity_check(c):
    """Validate the Bedrock path and local service reachability."""
    c.run(f"{HOST_PYTHON} -m scripts.connectivity", pty=True)


@task
def config_check(c):
    """Report each config file version against the version this release expects."""
    c.run(f"{HOST_PYTHON} -m scripts.config_check", pty=True)


def _compose_build(c, files: str, build_id: str) -> None:
    _use_hardened_base()
    # BuildKit's default provenance attestation records the build time, so a rebuild of an
    # unchanged image is a new image index under the containerd image store, and `up` would
    # recreate Temporal and the other built services under the running workers. The compose
    # file's `provenance` key does not reach the builder, so the flag goes on the command.
    c.run(
        f"{COMPOSE} {files} build --provenance=false "
        f"--build-arg AGENT_BUILD_ID={shlex.quote(build_id)}"
    )


@task(
    help={
        "force": "Replace the workers even when running executions are pinned to the "
        "outgoing build."
    }
)
def local_up(c, force=False):
    """Build the images, apply the schemas, then start the stack and wait for it to be healthy."""
    outgoing = recorded_build_id()
    build_id = resolve_build_id()
    if build_id != outgoing and not force:
        _refuse_to_strand(outgoing)
    print(f"[local-up] build id {build_id}")
    os.environ["AGENT_BUILD_ID"] = build_id
    repos_compose(c)
    files = _compose_files()
    _compose_build(c, files, build_id)
    db_migrate(c)
    _ensure_litellm_database(c)
    _require_temporal_schema_at_target(c)
    _temporal_admin(c, "schema", build=True)
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 temporal")
    _temporal_admin(c, "namespace")
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 --remove-orphans")
    _k8s(c, "reload", build_id)
    aws_login(c)
    for tenant in _local_tenants():
        repos_sync(c, tenant=tenant)
    _report_exceptions_setup()


@task(
    help={
        "force": "Replace the workers even when running executions are pinned to the "
        "outgoing build."
    }
)
def k8s_reload(c, force=False):
    """Rebuild the platform images for the current sources and roll compose and the cluster.

    The compose override is regenerated first, so repository intelligence serves the tenant
    folders as they are now. The compose workflow worker and the cluster workers register as one
    Worker Deployment version, so both move to the build these sources produce.
    """
    outgoing = recorded_build_id()
    build_id = resolve_build_id()
    if build_id != outgoing and not force:
        _refuse_to_strand(outgoing)
    os.environ["AGENT_BUILD_ID"] = build_id
    repos_compose(c)
    files = _compose_files()
    _compose_build(c, files, build_id)
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 --remove-orphans")
    _ensure_litellm_database(c)
    _k8s(c, "reload", build_id)


@task
def aws_login(c):
    """Sign in when needed and write the AWS credentials to the cluster Secret."""
    c.run(f"{HOST_PYTHON} -m scripts.aws_login", pty=True)


@task(
    help={
        "watch": "Check every interval until stopped, as the launchd agent does.",
        "interval": "Seconds between checks under --watch, 60 by default.",
    }
)
def local_renew(c, watch=False, interval=60):
    """Renew a static-key AWS session near expiry and start the Bedrock tunnel when it stopped."""
    if watch:
        c.run(f"{HOST_PYTHON} -m scripts.local_renew watch --interval {int(interval)}")
    else:
        c.run(f"{HOST_PYTHON} -m scripts.local_renew run")


@task(help={"interval": "Seconds between checks of the agent, 60 by default."})
def local_renew_install(c, interval=60):
    """Install a macOS launchd agent that keeps inv local-renew --watch running."""
    c.run(f"{HOST_PYTHON} -m scripts.local_renew install --interval {int(interval)}")


@task
def local_renew_uninstall(c):
    """Unload and remove the launchd agent that runs inv local-renew --watch."""
    c.run(f"{HOST_PYTHON} -m scripts.local_renew uninstall")


def _local_tenants() -> list[str]:
    from scripts.local_tenants import TenantLayoutError, local_tenant_names

    try:
        return local_tenant_names()
    except TenantLayoutError as exc:
        raise Exit(str(exc)) from exc


@task(help={"repo": "Sync only this repository or gate engine.", "tenant": _TENANT_HELP})
def repos_sync(c, repo="", tenant=""):
    """Copy the tenant's local-backend repositories and local gate engines into its worker."""
    flag = f" --repo {shlex.quote(repo)}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.repos_sync{flag}", pty=True, env=_tenant_env(tenant))


@task(
    help={
        "registry": "Registry the host pushes to (default localhost:5001).",
        "pull_registry": "The same registry as the gate pods address it.",
    }
)
def gate_engine_images(c, registry="", pull_registry=""):
    """Build the example gate engines, push them to the local registry, and write their digests."""
    flags = f" --registry {shlex.quote(registry)}" if registry else ""
    flags += f" --pull-registry {shlex.quote(pull_registry)}" if pull_registry else ""
    _start_local_registry(c)
    c.run(f"{HOST_PYTHON} -m scripts.gate_engine_images{flags}", pty=True)


def _start_local_registry(c) -> None:
    c.run(f"{COMPOSE} {_compose_files()} --profile registry up -d --wait registry")


@task
def runtime_images(c):
    """Build the catalog, builder, and scanner images for the local cluster and write digests."""
    _start_local_registry(c)
    c.run(f"{HOST_PYTHON} -m scripts.runtime_images", pty=True)


def _tenant_config_flags(tenant: str, bundle_dir: str, extra: dict[str, str]) -> str:
    flags = f" --tenant {shlex.quote(tenant)}" if tenant else ""
    flags += f" --dir {shlex.quote(bundle_dir)}" if bundle_dir else ""
    return flags + _optional_flags(extra)


def _optional_flags(pairs: Mapping[str, str]) -> str:
    return "".join(f" --{name} {shlex.quote(value)}" for name, value in pairs.items() if value)


@task(
    help={
        "tenant": "Tenant name. It may be omitted only when one local tenant exists.",
        "bundle_dir": "Bundle directory (default config/local/tenants/<tenant>).",
        "bucket": "Tenant config bucket the claims probe lists (default TENANT_CONFIG_BUCKET).",
        "platform_dir": "Platform config directory whose replaceable files the bundle may omit.",
    }
)
def tenant_config_validate(c, tenant="", bundle_dir="", bucket="", platform_dir=""):
    """Validate a tenant config bundle against the platform config."""
    from scripts.config import get_settings

    flags = _tenant_config_flags(
        tenant or _tenant(), bundle_dir, {"bucket": bucket, "platform-dir": platform_dir}
    )
    c.run(
        f"{HOST_PYTHON} -m scripts.tenant_config validate{flags}",
        pty=True,
        env=_aws_env(get_settings()),
    )


@task(
    help={
        "tenant": "Tenant name. It may be omitted only when one local tenant exists.",
        "bundle_dir": "Bundle directory (default config/local/tenants/<tenant>).",
        "bucket": "Tenant config bucket (default TENANT_CONFIG_BUCKET).",
        "platform_dir": "Platform config directory whose replaceable files the bundle may omit.",
    }
)
def tenant_config_publish(c, tenant="", bundle_dir="", bucket="", platform_dir=""):
    """Validate a tenant config bundle and publish it to the tenant config bucket."""
    from scripts.config import get_settings

    flags = _tenant_config_flags(
        tenant or _tenant(), bundle_dir, {"bucket": bucket, "platform-dir": platform_dir}
    )
    c.run(
        f"{HOST_PYTHON} -m scripts.tenant_config publish{flags}",
        pty=True,
        env=_aws_env(get_settings()),
    )


@task(
    help={
        "command": "backfill, report, release, or sweep.",
        "tenant": "Tenant whose claim release removes, for release.",
        "kind": (
            "Claim kind, slack-channel, slack-own-channel, slack-app, jira-project, or "
            "registry-username."
        ),
        "value": "Claim value, a Slack ID, or a Jira site, project key, and intake label.",
        "bucket": "Tenant config bucket (default TENANT_CONFIG_BUCKET).",
        "platform_dir": "Platform config directory whose replaceable files the bundles may omit.",
    }
)
def tenant_config_claims(c, command, tenant="", kind="", value="", bucket="", platform_dir=""):
    """Backfill, report, release, or sweep the claims of published tenant bundles, as an admin."""
    from scripts.config import get_settings

    if command not in ("backfill", "report", "release", "sweep"):
        raise SystemExit("command must be backfill, report, release, or sweep")
    flags = _tenant_config_flags(
        tenant,
        "",
        {"kind": kind, "value": value, "bucket": bucket, "platform-dir": platform_dir},
    )
    c.run(
        f"{HOST_PYTHON} -m scripts.tenant_config claims-{command}{flags}",
        pty=True,
        env=_aws_env(get_settings()),
    )


@task(help={"repo": "Fetch only this repository.", "tenant": _TENANT_HELP})
def fetch_results(c, repo="", tenant=""):
    """Fetch the agent branches and their results refs from the cluster into the host checkouts."""
    flag = f" --repo {shlex.quote(repo)}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.fetch_results{flag}", pty=True, env=_tenant_env(tenant))


@dataclass(frozen=True)
class TemporalRelease:
    version: str
    schema: tuple[int, int]
    visibility: tuple[int, int]
    server_image: str = ""
    admin_tools_image: str = ""

    def compose_override(self) -> str:
        """Return a Compose file that runs this release's upstream images in place of the builds.

        The target release returns none, because Compose builds it from the Dockerfiles. An
        upstream server image has no health probe, so the override checks the same endpoint with
        its busybox wget.
        """
        if not self.server_image:
            return ""
        health = "http://127.0.0.1:7243/api/v1/namespaces/temporal-system"
        return (
            "services:\n"
            "  temporal:\n"
            f"    image: {self.server_image}\n"
            "    build: !reset null\n"
            "    healthcheck:\n"
            f'      test: ["CMD", "wget", "-q", "-O", "/dev/null", "{health}"]\n'
            "      interval: 15s\n"
            "      timeout: 10s\n"
            "      start_period: 30s\n"
            "      retries: 15\n"
            "  temporal-admin-tools:\n"
            f"    image: {self.admin_tools_image}\n"
            "    build: !reset null\n"
        )


# Temporal upgrades only between consecutive minor releases, from the highest patch of each.
# The schema versions are those in schema/postgresql/v12/version.go at each release tag.
TEMPORAL_UPGRADE_PATH = (
    TemporalRelease("1.29.7", (1, 18), (1, 9)),
    TemporalRelease(
        "1.30.8",
        (1, 18),
        (1, 13),
        "docker.io/temporalio/server:1.30.8@sha256:54c59485b185ad787061f9a2ed72bd31c8efeae2d4144c61f22873df52c3c781",
        "docker.io/temporalio/admin-tools:1.30.8@sha256:22d417fd0d541da6b18486912a546b3203f516f0bf9f8b2f27611506fca2507c",
    ),
    TemporalRelease(
        "1.31.4",
        (1, 19),
        (1, 14),
        "docker.io/temporalio/server:1.31.4@sha256:1986d4554cc0da8cf09ddcaa10bc348f22f192972b4c2ec1f768f7307c5892e6",
        "docker.io/temporalio/admin-tools:1.31.4@sha256:c286fe515148587228d692806bc6f7652be5febc384a73dfd16379aed69f7d87",
    ),
)
TEMPORAL_TARGET = TemporalRelease("1.32.2", (1, 19), (1, 14))
TEMPORAL_DATABASES = ("temporal", "temporal_visibility")
LITELLM_DATABASE_EXISTS = "select 1 from pg_database where datname = 'litellm'"
CREATE_LITELLM_DATABASE = "create database litellm"


def _report_exceptions_setup() -> None:
    from scripts.config import get_settings
    from services.integration.image_exceptions import exceptions_setup_problems

    for problem in exceptions_setup_problems(get_settings()):
        print(f"[local-up] warning: {problem}")


def _use_hardened_base() -> None:
    """Build the hardened base when it is absent and point the Compose builds at it.

    The reference is also written to config/local/platform.env, so a
    `docker compose --env-file config/local/platform.env build` run outside the tasks starts from
    the same base instead of the minimal image, which has no cis-harden.
    """
    from scripts.al2023_base import Base
    from scripts.env_file import set_values
    from scripts.k8s_cluster import local_arch
    from scripts.publish_platform_images import REPO_ROOT, build_context

    with build_context(REPO_ROOT) as context:
        base = Base(local_arch()).reference(context)
    os.environ["AL2023_IMAGE"] = base
    set_values({"AL2023_IMAGE": base})


def _temporal_admin(c, action: str, env: dict[str, str] | None = None, build: bool = False) -> None:
    files = "" if env else f" {_compose_files()}"
    flag = " --build" if build else ""
    c.run(
        f"{COMPOSE}{files} run --rm{flag} temporal-admin-tools {action}",
        pty=True,
        env=env or {},
    )


@contextlib.contextmanager
def _release_compose_env(release: TemporalRelease) -> Iterator[dict[str, str]]:
    """Yield the environment that points Compose at the release's images, empty for the target."""
    override = release.compose_override()
    if not override:
        yield {}
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", prefix=f"temporal-{release.version}-", delete=False
    ) as handle:
        handle.write(override)
    try:
        separator = os.environ.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
        yield {"COMPOSE_FILE": separator.join([*_compose_file_list(), handle.name])}
    finally:
        os.unlink(handle.name)


def _psql(c, database: str, query: str) -> str:
    sql = shlex.quote(f'psql -U "$POSTGRES_USER" -d {database} -Atc "{query}"')
    return c.run(
        f"{COMPOSE} {_compose_files()} exec -T postgres sh -c {sql}", hide=True
    ).stdout.strip()


def _ensure_litellm_database(c) -> None:
    """Create the LiteLLM database on a Postgres volume that predates it.

    The compose init script creates the service databases only on a fresh volume.
    """
    if not _psql(c, "postgres", LITELLM_DATABASE_EXISTS):
        _psql(c, "postgres", CREATE_LITELLM_DATABASE)


def _temporal_schema_version(c, database: str) -> tuple[int, int] | None:
    if not _psql(c, database, "select to_regclass('schema_version')"):
        return None
    major, _, minor = _psql(c, database, "select curr_version from schema_version").partition(".")
    return int(major), int(minor)


def _require_temporal_schema_at_target(c) -> None:
    targets = (TEMPORAL_TARGET.schema, TEMPORAL_TARGET.visibility)
    for database, target in zip(TEMPORAL_DATABASES, targets, strict=True):
        current = _temporal_schema_version(c, database)
        if current is None or current == target:
            continue
        version = ".".join(map(str, current))
        if current < target:
            raise Exit(
                f"The {database} database is at schema {version}, older than server "
                f"{TEMPORAL_TARGET.version} upgrades from. Run inv temporal-upgrade."
            )
        raise Exit(
            f"The {database} database is at schema {version}, newer than server "
            f"{TEMPORAL_TARGET.version} supports. Check out the release that wrote it."
        )


def _temporal_upgrade_steps(
    schema: tuple[int, int], visibility: tuple[int, int]
) -> list[TemporalRelease]:
    for index, release in enumerate(TEMPORAL_UPGRADE_PATH):
        if (release.schema, release.visibility) == (schema, visibility):
            return [*TEMPORAL_UPGRADE_PATH[index + 1 :], TEMPORAL_TARGET]
    raise Exit(
        f"No upgrade path from schema {schema[0]}.{schema[1]} and visibility schema "
        f"{visibility[0]}.{visibility[1]}. "
        f"The path starts at server {TEMPORAL_UPGRADE_PATH[0].version}."
    )


def _backup_temporal_databases(c, directory: str) -> None:
    os.makedirs(directory, exist_ok=True)
    for database in TEMPORAL_DATABASES:
        dump = shlex.quote(f'pg_dump -U "$POSTGRES_USER" -Fc {database}')
        target = shlex.quote(os.path.join(directory, f"{database}.dump"))
        c.run(f"{COMPOSE} {_compose_files()} exec -T postgres sh -c {dump} > {target}")
    print(f"[temporal-upgrade] backed up {', '.join(TEMPORAL_DATABASES)} to {directory}")


@task(
    help={
        "backup_dir": "Directory for the pg_dump backups (default ~/temporal-backup/<UTC time>).",
        "skip_backup": "Skip the backup, for a stack whose Temporal data is disposable.",
    }
)
def temporal_upgrade(c, backup_dir="", skip_backup=False):
    """Back up the Temporal databases and upgrade them one minor release at a time."""
    files = _compose_files()
    c.run(f"{COMPOSE} {files} up -d --wait postgres")
    schema, visibility = (_temporal_schema_version(c, database) for database in TEMPORAL_DATABASES)
    if schema is None or visibility is None:
        raise Exit("No Temporal schema to upgrade. Run inv local-up to set up a new database.")
    steps = _temporal_upgrade_steps(schema, visibility)
    _use_hardened_base()
    if not skip_backup:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        _backup_temporal_databases(
            c, backup_dir or os.path.join(os.path.expanduser("~"), "temporal-backup", stamp)
        )
    for release in steps:
        print(f"[temporal-upgrade] applying the schema and starting server {release.version}")
        with _release_compose_env(release) as env:
            _temporal_admin(c, "schema", env, build=not env)
            step_files = "" if env else f" {files}"
            c.run(
                f"{COMPOSE}{step_files} up -d --build --no-deps --wait --wait-timeout 600 temporal",
                env=env,
            )
    _temporal_admin(c, "namespace")


def _refuse_to_strand(outgoing: str) -> None:
    """Abort when replacing the workers would leave running executions with nowhere to run.

    A pinned execution only makes progress while workers on its own version poll, and this build
    replaces them. Reaching the server is best effort, because the stack is down on a first run
    and an unreachable server reports no executions to strand either way.
    """
    from scripts.config import get_settings, to_host_hostport

    settings = get_settings()
    if not settings.worker_versioning_enabled:
        return

    async def _stranded() -> list[str]:
        from services import temporal_client
        from services.orchestration.worker_versioning import pinned_executions

        client = await temporal_client.connect(
            to_host_hostport(settings.temporal_host, settings), settings
        )
        return await pinned_executions(client, settings.worker_deployment_name, outgoing)

    try:
        stranded = asyncio.run(_stranded())
    except Exception as exc:
        print(f"[local-up] could not list executions pinned to build {outgoing}: {exc}")
        return
    if not stranded:
        return
    listed = "\n".join(f"  {workflow_id}" for workflow_id in stranded)
    raise Exit(
        f"{len(stranded)} running execution(s) are pinned to build {outgoing}, whose workers this "
        f"build replaces. Each one stops until a worker on that build polls again.\n{listed}\n"
        f"Let them finish, or resume the parked ones with `inv resume-parked`, which repins them "
        f"to the current build. Run `inv local-up --force` to replace the workers anyway."
    )


@task
def local_down(c):
    """Stop the local stack and scale the cluster workloads to zero (volumes preserved)."""
    _k8s(c, "stop")
    c.run(f"{COMPOSE} {_compose_files()} down")


@task
def local_stop(c):
    """Stop the compose containers without removing them and scale the cluster workloads to zero."""
    _k8s(c, "stop")
    c.run(f"{COMPOSE} {_compose_files()} stop")


@task
def local_destroy(c):
    """Remove the stack and its volumes, including the cluster workspace volume."""
    _k8s(c, "delete", recorded_build_id(), " --volumes")
    c.run(f"{COMPOSE} {_compose_files()} down --volumes --remove-orphans")


@task(help={"match": "Print only records whose body holds this text."})
def platform_logs(c, match=""):
    """Print the platform log records the local collector stored, which survive a redeploy."""
    flag = f" --match {shlex.quote(match)}" if match else ""
    c.run(f"{HOST_PYTHON} -m scripts.platform_logs{flag}", pty=True)


@task(help={"service": "Optional single service to tail.", "tenant": _TENANT_HELP})
def logs(c, service="", tenant=""):
    """Follow logs for the compose stack, or for one compose service or cluster deployment.

    The activity worker, the MCP gateway, and the review worker run per tenant, and --tenant
    selects whose to follow.
    """
    from scripts.k8s_cluster import DEPLOYMENTS, GATEWAY, REVIEW_WORKER
    from scripts.tenant_render import WORKER

    if service in DEPLOYMENTS or service in (WORKER, GATEWAY, REVIEW_WORKER):
        from scripts.config import get_settings
        from scripts.k8s_cluster import in_review_namespace, in_worker_namespace, namespaced

        settings = get_settings()
        if service in DEPLOYMENTS:
            address = namespaced
        else:
            address = in_review_namespace if service == REVIEW_WORKER else in_worker_namespace
            settings = settings.model_copy(update={"tenant": _tenant(tenant)})
        argv = address(settings, "logs", "-f", f"deployment/{service}", "--all-containers")
        c.run(shlex.join(argv), pty=True)
        return
    c.run(f"{COMPOSE} {_compose_files()} logs -f {service}".strip(), pty=True)


@task
def db_migrate(c):
    """Apply migrations by running the db-migrate container."""
    c.run(f"{COMPOSE} {_compose_files()} run --rm --build db-migrate", pty=True)


@task
def schedules_install(c):
    """Remove the untenanted reap and intake schedules, which each tenant's worker installs."""
    c.run(f"{COMPOSE} {_compose_files()} run --rm --build --no-deps schedule-bootstrap", pty=True)


@task
def db_reset(c):
    """Drop and recreate the ``agent_platform`` schema, then re-migrate.

    The LiteLLM database, with its virtual keys and spend, is left in place.
    """
    c.run(
        f"{COMPOSE} {_compose_files()} run --rm --build --entrypoint python "
        "db-migrate -m scripts.db_reset",
        pty=True,
    )
    db_migrate(c)


@task
def seed(c):
    """Load local seed/fixture data (allowlisted AGENTDEV + demo repos)."""
    c.run(f"{HOST_PYTHON} -m scripts.seed", pty=True)


@task(
    help={
        "xlsx": "Path to the Jira xlsx export to import.",
        "root": "Optional store path (defaults to the tickets backend repo_root).",
        "status": "Comma-separated Jira statuses to import (default: all).",
        "limit": "Import at most N tickets (0 means no limit).",
        "reset": "Wipe the store and re-init empty before importing.",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def import_rebar_tickets(
    c, xlsx="", root="", status="", limit=0, reset=False, local=False, tenant=""
):
    """Import a Jira xlsx export into the local rebar ticket store."""
    flags = xlsx
    if root:
        flags += f" --root {root}"
    if status:
        flags += f' --status "{status}"'
    if limit:
        flags += f" --limit {limit}"
    if reset:
        flags += " --reset"
    _run_scripted(
        c, f"python -m scripts.import_rebar_tickets {flags}".strip(), local=local, tenant=tenant
    )


@task(
    help={
        "new": "Report new tickets.",
        "field-changes": "Report field changes.",
        "status-changes": "Report status changes.",
        "local": "Run on the host instead of in the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def rebar_changes(c, new=False, field_changes=False, status_changes=False, local=False, tenant=""):
    """Report rebar ticket changes since the last intake pass without starting work."""
    flags = []
    if new:
        flags.append("--new")
    if field_changes:
        flags.append("--field-changes")
    if status_changes:
        flags.append("--status-changes")
    _run_scripted(
        c, f"python -m scripts.rebar_poll {' '.join(flags)}".strip(), local=local, tenant=tenant
    )


@task(
    help={"local": "Run on the host instead of in the activity worker pod.", "tenant": _TENANT_HELP}
)
def rebar_store_remote(c, local=False, tenant=""):
    """Point the rebar ticket store at its S3 bucket as the reader for fetch and writer for push."""
    command = "python -m scripts.rebar_store_remote"
    env = _tenant_env(tenant)
    if local:
        c.run(_on_host(command), pty=True, env=env)
        return
    # A one-off pod, because the activity worker refuses to start while the remote is wrong.
    _k8s(c, "one-off", extra=f" -- {command}", env=env)


def _rebar_gate_env() -> dict[str, str]:
    """Name the tier config for rebar's LLM gates.

    A gate reads its config from an attested snapshot materialised with `git checkout-index`,
    which contains only tracked files. The root `rebar.toml` is untracked by mandate, so rebar's
    own discovery finds no `[llm]` table there and falls back to its built-in direct-Anthropic
    class defaults, which cannot authenticate. `compose.yaml` names the same file for the review
    worker.

    The path is derived from this file rather than the working directory, because `invoke`
    locates `tasks.py` by walking up from the caller and then runs the command in the caller's
    directory.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    return {"REBAR_LLM_CONFIG_FILE": os.path.join(root, "config", "platform", "rebar-review.toml")}


@task(
    help={
        "ticket": "The rebar ticket id to review.",
        "gate-ref": "Git ref the gate grounds against, for a ticket stacked on an open branch.",
    }
)
def rebar_review_plan(c, ticket, gate_ref=""):
    """Run rebar's plan-review gate against the platform tier config."""
    env = _rebar_gate_env()
    if gate_ref:
        env["REBAR_GATE_REF"] = gate_ref
    c.run(f"rebar review-plan {ticket}", pty=True, env=env)


@task(
    help={
        "base": "Git ref the change branched from.",
        "head": "Git ref holding the change.",
    }
)
def rebar_review_code(c, base, head):
    """Run rebar's code-review gate against the platform tier config."""
    c.run(f"rebar review-code --base {base} --head {head}", pty=True, env=_rebar_gate_env())


def _host_source_control_env() -> dict[str, str]:
    """Rewrite the container workspace root in the local artifacts setting to its host directory.

    operator-done runs on the host against the same artifacts the workers write, so the
    container-relative SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT of the settings must resolve to the host
    directory that backs the workspace mount. Outside the platform image, the repository
    registry already locates each local repository at its host path.
    """
    from scripts.config import get_settings

    settings = get_settings()
    workspaces = os.path.join(os.path.abspath(os.getcwd()), ".agent-workspaces")
    artifacts_root = settings.local_artifacts_root()
    if artifacts_root == "/workspaces" or artifacts_root.startswith("/workspaces/"):
        artifacts_root = workspaces + artifacts_root[len("/workspaces") :]
    return {"SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT": os.path.abspath(artifacts_root)}


@task(
    help={
        "workflow-id": "The draft-PR workflow id to complete.",
        "repo": "The repository of the promoted branch.",
        "branch": "The promoted local branch.",
        "completion-signal-id": "Expected completion-signal id to verify before signaling.",
        "closed-unmerged": "Complete as closed-unmerged rather than merged.",
        "tenant": _TENANT_HELP,
    }
)
def operator_done(
    c, workflow_id, repo, branch, completion_signal_id="", closed_unmerged=False, tenant=""
):
    """Complete a local-backend run by verifying artifacts and signaling the workflow."""
    flags = f"--workflow-id {workflow_id} --repo {repo} --branch {branch}"
    if completion_signal_id:
        flags += f" --completion-signal-id {completion_signal_id}"
    if closed_unmerged:
        flags += " --closed-unmerged"
    env = _tenant_env(tenant)
    fetch_results(c, repo=repo, tenant=env["TENANT"])
    c.run(
        f"{HOST_PYTHON} -m scripts.operator_done {flags}",
        pty=True,
        env={**env, **_host_source_control_env()},
    )


@task
def smoke_test(c):
    """Run the smoke test: DB reachable, services healthy, Bedrock path."""
    c.run(f"{HOST_PYTHON} -m scripts.smoke_test", pty=True)


@task
def prompts_seed(c):
    """Publish the platform prompt library to Langfuse."""
    c.run(f"{HOST_PYTHON} -m scripts.seed_prompts", pty=True)


@task
def evidence_bucket_bootstrap(c):
    """Create the evidence object-lock bucket for local development."""
    c.run(f"{HOST_PYTHON} -m scripts.evidence_bucket_bootstrap", pty=True)


@task(
    help={
        "function_name": "The role_bootstrap_function_name Terraform output.",
        "region": "Function region. Defaults to AWS_REGION.",
    }
)
def db_bootstrap_roles(c, function_name="", region=""):
    """Create the per-service database roles and databases in a deployed Aurora cluster."""
    if not function_name:
        raise Exit("provide --function-name")
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --function-name {shlex.quote(function_name)}"
    flags += f" --region {shlex.quote(region)}" if region else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.db_bootstrap_roles{flags}", pty=True, env=env)


@task(help={"offline": "Use the deterministic offline fixture instead of the rebar kernel."})
def review_eval(c, offline=False, out=""):
    """Score the rebar review kernel against the keyword baseline on the gold set."""
    flags = " --offline" if offline else ""
    flags += f" --out {out}" if out else ""
    c.run(f"{HOST_PYTHON} -m scripts.review_eval{flags}", pty=True)


@task(help={"out": "Optional path to write the consolidated scorecard."})
def context_eval(c, out=""):
    """Reproduce the consolidated repository-context accuracy scorecard on the demo corpora."""
    flags = f" --out {out}" if out else ""
    c.run(f"{HOST_PYTHON} -m services.repository_intelligence.eval.consolidate{flags}", pty=True)


def _usage_flags(execution_id: str, tenant: str) -> str:
    flags = f" --execution-id {shlex.quote(execution_id)}" if execution_id else ""
    return flags + (f" --tenant {shlex.quote(tenant)}" if tenant else "")


@task(
    help={
        "execution-id": "The execution id to roll up. Omit it to roll up every execution.",
        "tenant": "Roll up only the executions recorded under this tenant.",
    }
)
def cost_rollup(c, execution_id="", tenant=""):
    """Print the cost, token, and budget rollup for one execution or per tenant."""
    c.run(f"{HOST_PYTHON} -m scripts.cost_rollup{_usage_flags(execution_id, tenant)}", pty=True)


@task(
    help={
        "execution-id": "The execution id to reconcile. Omit it to reconcile every execution.",
        "tenant": "Reconcile only the executions recorded under this tenant.",
    }
)
def reconcile_usage(c, execution_id="", tenant=""):
    """Reconcile Langfuse generations against usage metrics per execution and tenant."""
    c.run(f"{HOST_PYTHON} -m scripts.reconcile_usage{_usage_flags(execution_id, tenant)}", pty=True)


@task
def test(c):
    """Run the pytest suite."""
    c.run(f"{HOST_PYTHON} -m pytest", pty=True)


@task
def ci(c):
    """Run the portable CI checks: lint, format, the hermetic tests, and the evaluation gate."""
    c.run(f"{HOST_PYTHON} -m ruff check", pty=True)
    c.run(f"{HOST_PYTHON} -m ruff format --check", pty=True)
    c.run(f"{HOST_PYTHON} -m pytest -q tests/unit tests/end_to_end tests/integration", pty=True)
    c.run(f"{HOST_PYTHON} -m scripts.eval_gate", pty=True)


_GO_SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules", "vendor", ".terraform"}


def _go_modules(root: str) -> list[str]:
    modules = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in _GO_SKIPPED_DIRECTORIES]
        if "go.mod" in files:
            modules.append(directory)
    return sorted(modules)


@task
def go_test(c):
    """Check formatting, vet, and test every Go module under docker/."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    if not c.run("go version", hide=True, warn=True).ok:
        raise Exit("go is not on PATH. Install the Go version .github/workflows/ci.yml pins.")
    for module in _go_modules(os.path.join(repo_root, "docker")):
        unformatted = c.run(f"gofmt -l {shlex.quote(module)}", hide=True).stdout.strip()
        if unformatted:
            raise Exit(f"gofmt would rewrite these files. Run gofmt -w on them.\n{unformatted}")
        c.run(f"cd {shlex.quote(module)} && go vet ./... && go test ./...", pty=True)


def _terraform_version_remedy(pinned: str) -> str:
    override = os.environ.get("TFENV_TERRAFORM_VERSION")
    if override:
        return f"Unset TFENV_TERRAFORM_VERSION, which selects {override}."
    return (
        f"Run tfenv install {pinned} and put the tfenv shim ahead of any other terraform on PATH."
    )


def _terraform_modules(root: str) -> list[str]:
    modules = []
    for directory, subdirectories, _ in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name != ".terraform"]
        if "tests" in subdirectories:
            modules.append(directory)
    return sorted(modules)


@task
def terraform_test(c):
    """Run terraform test in each module under infra/terraform that has a tests directory."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(repo_root, ".terraform-version"), encoding="utf-8") as handle:
        pinned = handle.read().strip()
    result = c.run("terraform version -json", hide=True, warn=True)
    if not result.ok:
        raise Exit(
            f"terraform version -json exited {result.exited}. {result.stderr.strip()} "
            f"Install Terraform {pinned} with tfenv install {pinned}, and put the tfenv shim "
            "first on PATH."
        )
    installed = json.loads(result.stdout)["terraform_version"]
    if installed != pinned:
        raise Exit(
            f"Terraform {installed} is on PATH and .terraform-version pins {pinned}. "
            f"{_terraform_version_remedy(pinned)}"
        )

    for module in _terraform_modules(os.path.join(repo_root, "infra", "terraform")):
        c.run(f"terraform -chdir={shlex.quote(module)} init -backend=false -input=false", pty=True)
        c.run(f"terraform -chdir={shlex.quote(module)} test", pty=True)


@task
def sast(c):
    """Run the SAST, secret, misconfiguration, and dependency scans against the register."""
    c.run(f"{HOST_PYTHON} -m scripts.sast_scan", pty=True)


@task
def workflow_status(c):
    """Show recent Temporal workflow executions."""
    c.run(f"{HOST_PYTHON} -m scripts.workflow_status", pty=True)


@task(
    help={
        "issue": "Resume the run of this issue parked on the model path, for example ADSER-24.",
        "workflow_id": "Resume the run of this workflow ID parked on the model path.",
        "local": "With --issue or --workflow-id, run on the host instead of the activity worker.",
        "tenant": _TENANT_HELP,
    }
)
def resume_parked(c, issue="", workflow_id="", local=False, tenant=""):
    """Resume the parked workflows a newer build can retry, or one run parked on the model path."""
    if issue and workflow_id:
        raise Exit("pass at most one of --issue or --workflow-id")
    if not (issue or workflow_id):
        c.run(f"{HOST_PYTHON} -m scripts.resume_parked", pty=True)
        return
    target = (
        f"--issue {shlex.quote(issue)}" if issue else f"--workflow-id {shlex.quote(workflow_id)}"
    )
    _run_scripted(c, f"python -m scripts.resume_parked {target}", local=local, tenant=tenant)


@task
def prune_worker_versions(c):
    """Delete the drained versions of the Temporal worker deployment."""
    c.run(f"{HOST_PYTHON} -m scripts.prune_worker_versions", pty=True)


@task
def durability_proof(c):
    """Prove a workflow survives a worker restart."""
    c.run(f"{HOST_PYTHON} -m scripts.durability_proof", pty=True)


@task(
    help={
        "source": "Existing CBM source checkout to build from (default clones upstream).",
        "ref": "Upstream tag, branch, or commit to clone (default v0.9.0).",
        "dest": "Output directory for the binary (default vendor/bin).",
    }
)
def cbm_build(c, source="", ref="", dest="vendor/bin"):
    """Build and vendor the codebase-memory-mcp binary from upstream or a local checkout."""
    flags = f" --source {source}" if source else ""
    flags += f" --ref {ref}" if ref else ""
    c.run(f"{HOST_PYTHON} -m scripts.build_cbm --dest {dest}{flags}", pty=True)


@task(
    help={
        "tag": "Image tag to build and push, one the repository does not hold yet. Required.",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "repo": "ECR repository name (default agentic-platform/jumphost-base).",
        "region": "AWS region (empty uses AWS_REGION).",
        "exceptions": "image_exceptions.yaml whose entries the pre-push scan accepts.",
        "builder": "Image that runs notation for the signing (default the published buildah).",
        "unsigned": "Publish without signing, for a deployment with no platform signing profile.",
    }
)
def publish_jumphost_image(
    c, tag, arch="arm64", repo="", region="", exceptions="", builder="", unsigned=False
):
    """Build the Fargate jumphost-base image, scan it, push it to ECR, and sign it."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --tag {shlex.quote(tag)} --arch {shlex.quote(arch)}"
    flags += f" --repository {shlex.quote(repo)}" if repo else ""
    flags += f" --region {shlex.quote(region)}" if region else ""
    flags += f" --exceptions {shlex.quote(exceptions)}" if exceptions else ""
    flags += f" --builder {shlex.quote(builder)}" if builder else ""
    flags += " --unsigned" if unsigned else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.publish_jumphost_image{flags}", pty=True, env=env)


@task(
    help={
        "name_prefix": "Repository prefix (default agentic-platform).",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "region": "AWS region (empty uses AWS_REGION).",
        "exceptions": "image_exceptions.yaml whose entries the pre-push scan accepts.",
    }
)
def publish_temporal_images(c, name_prefix="", arch="arm64", region="", exceptions=""):
    """Build the Temporal server, admin-tools, and UI images, scan them, and push them to ECR."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --arch {shlex.quote(arch)}"
    flags += f" --name-prefix {shlex.quote(name_prefix)}" if name_prefix else ""
    flags += f" --region {shlex.quote(region)}" if region else ""
    flags += f" --exceptions {shlex.quote(exceptions)}" if exceptions else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.publish_temporal_images{flags}", pty=True, env=env)


@task(
    help={
        "name_prefix": "Repository prefix (default agentic-platform).",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "region": "AWS region (empty uses AWS_REGION).",
        "manifest": "Also write each image's repository, digest, and whether this run pushed it.",
        "exceptions": "image_exceptions.yaml whose entries the pre-push scan accepts.",
        "unsigned": "Publish without signing, for a deployment that does not verify signatures.",
        "stage": "all, or base, group, or finalize for the parallel jobs of the CI workflow.",
        "group": "The image group a group stage publishes.",
        "partial": "Where a group stage writes its references.",
        "partials": "The directory a finalize stage merges the group references from.",
    }
)
def publish_platform_images(
    c,
    name_prefix="",
    arch="arm64",
    region="",
    manifest="",
    exceptions="",
    unsigned=False,
    stage="",
    group="",
    partial="",
    partials="",
):
    """Build the platform and third-party component images, scan each, push them to ECR,
    and write their references to images.auto.tfvars.json in the composition root."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --arch {shlex.quote(arch)}"
    flags += f" --name-prefix {shlex.quote(name_prefix)}" if name_prefix else ""
    flags += f" --region {shlex.quote(region)}" if region else ""
    flags += f" --manifest {shlex.quote(manifest)}" if manifest else ""
    flags += f" --exceptions {shlex.quote(exceptions)}" if exceptions else ""
    flags += " --unsigned" if unsigned else ""
    flags += _optional_flags(
        {"stage": stage, "group": group, "partial": partial, "partials": partials}
    )
    c.run(
        f"{HOST_PYTHON} -m scripts.publish_platform_images{flags}", pty=True, env=_aws_env(settings)
    )


@task(
    help={
        "image": "Image to scan, repeatable, as the local Docker names it.",
        "arch": "Platform of the image (default arm64).",
        "exceptions": "An image_exceptions.yaml whose entries accept a finding.",
    },
    iterable=["image"],
)
def image_scan(c, image, arch="arm64", exceptions=""):
    """Scan local images with the Amazon Inspector SBOM generator and Scan API, pushing nothing."""
    from scripts.config import get_settings

    flags = "".join(f" --image {shlex.quote(name)}" for name in image)
    flags += f" --arch {shlex.quote(arch)}"
    flags += f" --exceptions {shlex.quote(exceptions)}" if exceptions else ""
    c.run(f"{HOST_PYTHON} -m scripts.image_scan{flags}", pty=True, env=_aws_env(get_settings()))


def _aws_env(settings) -> dict[str, str]:
    """Sign with the configured profile, unless the environment already holds credentials.

    A CI job receives role credentials in the environment, and naming a profile the runner
    does not define would make the SDK refuse to start.
    """
    env = {"AWS_REGION": settings.aws_region}
    if not any(os.environ.get(name) for name in _AWS_CREDENTIAL_VARIABLES):
        env["AWS_PROFILE"] = settings.aws_profile
    return env


_AWS_CREDENTIAL_VARIABLES = ("AWS_ACCESS_KEY_ID", "AWS_WEB_IDENTITY_TOKEN_FILE")


def _profile_env(settings) -> dict[str, str]:
    return {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}


@task(
    help={
        "var_file": "Var file in the composition root (default greenfield.tfvars).",
        "delete_images": "Also delete the ECR repositories and their images.",
        "delete_stores": "Also delete the rebar ticket stores and the tickets they hold.",
        "plan_only": "Show what the destroy removes and keeps, then stop.",
    }
)
def aws_destroy(
    c, var_file="greenfield.tfvars", delete_images=False, delete_stores=False, plan_only=False
):
    """Destroy the composition root, keeping bootstrap, images, and ticket stores."""
    from scripts.config import get_settings

    flags = f" --var-file {shlex.quote(var_file)}"
    flags += " --delete-images" if delete_images else ""
    flags += " --delete-stores" if delete_stores else ""
    flags += " --plan-only" if plan_only else ""
    c.run(f"{HOST_PYTHON} -m scripts.aws_destroy{flags}", pty=True, env=_aws_env(get_settings()))


@task(
    help={
        "var_file": "Var file in the composition root (default greenfield.tfvars).",
        "region": "AWS region for the deploy (empty uses AWS_REGION).",
        "yes": "Apply the plan without asking.",
    }
)
def aws_release(c, var_file="greenfield.tfvars", region="", yes=False):
    """Plan and apply the composition root with the published images, then deploy ECS and EKS."""
    from scripts.config import get_settings

    flags = f" --var-file {shlex.quote(var_file)}"
    flags += f" --region {shlex.quote(region)}" if region else ""
    flags += " --yes" if yes else ""
    c.run(f"{HOST_PYTHON} -m scripts.aws_release{flags}", pty=True, env=_aws_env(get_settings()))


@task
def langfuse_ui(c):
    """Forward the local port of the Langfuse public URL to Langfuse through the jump-host."""
    c.run(f"{HOST_PYTHON} -m scripts.langfuse_ui", pty=True)


@task(help={"region": "AWS region (empty uses AWS_REGION)."})
def temporal_deploy(c, region=""):
    """Apply the Temporal schema, roll the server, and register the namespace."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --region {shlex.quote(region)}" if region else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.temporal_deploy{flags}", pty=True, env=env)


@task(help={"region": "AWS region (empty uses AWS_REGION)."})
def platform_deploy(c, region=""):
    """Migrate the database, roll the platform services, register the schedules, and prune."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --region {shlex.quote(region)}" if region else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.platform_deploy{flags}", pty=True, env=env)


@task(
    help={
        "command": "deploy (default), check-policies, or restart followed by deployment names, "
        "namespace/name, tenant-workers, tenant-review-workers, or mcp-gateway.",
        "region": "AWS region (empty uses AWS_REGION).",
    }
)
def eks_deploy(c, command="deploy", region=""):
    """Deploy the EKS workloads through the eks-deployer task inside the VPC."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --region {shlex.quote(region)}" if region else ""
    words = " ".join(shlex.quote(word) for word in command.split())
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.eks_deploy{flags} {words}", pty=True, env=env)


@task(
    help={
        "platform": "Run the platform checks only.",
        "all_tenants": "Run the platform checks and the validation of every tenant.",
        "tenant": "Run the platform checks and the validation of this tenant.",
        "post": "Post each validated tenant's report to its Slack channel.",
        "context": "kubectl context of the cluster (empty uses the current context).",
    }
)
def deployment_validate(c, platform=False, all_tenants=False, tenant="", post=False, context=""):
    """Run the deployment validation as a Job in the cluster and print its report."""
    if [bool(platform), bool(all_tenants), bool(tenant)].count(True) != 1:
        raise Exit("Name exactly one of --platform, --all-tenants, or --tenant <tenant>.")
    if platform:
        flags = " --platform"
    elif all_tenants:
        flags = " --all-tenants"
    else:
        flags = f" --tenant {shlex.quote(tenant)}"
    flags += " --post" if post else ""
    flags += f" --context {shlex.quote(context)}" if context else ""
    c.run(f"{HOST_PYTHON} -m scripts.validation_job{flags}", pty=True)


@task(
    help={
        "context": "kubectl context of the EKS cluster (empty uses the current context).",
        "outputs": "File holding `terraform output -json` (empty reads the composition root).",
        "region": "AWS region (empty uses the eks output's region).",
        "admin_role_arn": "Role that must read no tenant secret (default role/platform-admin).",
        "repo": "TENANT=REPOSITORY to probe, repeatable (default the first indexed one).",
        "only": "Comma-separated groups of iam, env, ri, mounts, review, claims, slack, registry "
        "(default all).",
    },
    iterable=["repo"],
)
def tenant_verify_aws(c, context="", outputs="", region="", admin_role_arn="", repo=None, only=""):
    """Check the AWS tenant boundary with read-only calls and print a pass or fail table."""
    from scripts.config import get_settings

    flags = _optional_flags(
        {
            "context": context,
            "outputs": outputs,
            "region": region,
            "admin-role-arn": admin_role_arn,
            "only": only,
        }
    )
    flags += "".join(f" --repo {shlex.quote(pair)}" for pair in repo or [])
    c.run(
        f"{HOST_PYTHON} -m scripts.tenant_verify_aws{flags}", pty=True, env=_aws_env(get_settings())
    )


@task(help={"port": "Local port forwarded to the UI load balancer (default 8443)."})
def temporal_ui(c, port=8443):
    """Forward a local port to the Temporal UI load balancer through the jump-host."""
    c.run(f"{HOST_PYTHON} -m scripts.temporal_ui --port {int(port)}", pty=True)


@task(
    help={
        "action": "generate to issue self-signed certificates, or put to store a directory.",
        "out": "generate: directory outside the repository to write the certificates to.",
        "dir": "put: directory holding ca.pem and the internode and frontend pairs.",
        "frontend_name": "generate: frontend SAN, comma-separated. The first is the CN.",
        "internode_name": "generate: internode SAN, comma-separated. The first is the CN.",
        "ui_name": "generate: also issue a UI load balancer certificate with this SAN list.",
        "internal_name": "generate: also issue an internal load balancer certificate, SAN list.",
        "days": "generate: leaf certificate validity in days (default 397).",
        "no_frontend": "generate: skip the frontend pair, for a frontend served from ACM.",
        "force": "generate: replace certificates already in the directory.",
        "import_to_acm": "put: also import the internode certificate into ACM to track its expiry.",
    }
)
def temporal_certs(
    c,
    action,
    out="",
    dir="",  # noqa: A002
    frontend_name="",
    internode_name="",
    ui_name="",
    internal_name="",
    days=397,
    no_frontend=False,
    force=False,
    import_to_acm=False,
):
    """Generate the Temporal TLS certificates, or check and store a certificate directory."""
    from scripts.config import get_settings

    if action == "generate":
        if not out:
            raise Exit("provide --out")
        flags = f" --out {shlex.quote(out)} --days {int(days)}"
        for name in filter(None, frontend_name.split(",")):
            flags += f" --frontend-name {shlex.quote(name.strip())}"
        for name in filter(None, internode_name.split(",")):
            flags += f" --internode-name {shlex.quote(name.strip())}"
        for name in filter(None, ui_name.split(",")):
            flags += f" --ui-name {shlex.quote(name.strip())}"
        for name in filter(None, internal_name.split(",")):
            flags += f" --internal-name {shlex.quote(name.strip())}"
        flags += " --no-frontend" if no_frontend else ""
        flags += " --force" if force else ""
        c.run(f"{HOST_PYTHON} -m scripts.temporal_certs generate{flags}", pty=True)
        return
    if action != "put":
        raise Exit("action must be generate or put")
    if not dir:
        raise Exit("provide --dir")
    settings = get_settings()
    flags = f" --dir {shlex.quote(dir)}"
    flags += " --import-to-acm" if import_to_acm else ""
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.temporal_certs put{flags}", pty=True, env=env)


@task(
    help={
        "action": "issue to sign the certificate with an existing CA, or put to store it.",
        "dir": "Directory holding ca.pem and ca-key.pem from temporal-certs --internal-name.",
        "days": "issue: validity in days (default 397).",
        "force": "issue: replace a posting service certificate already in the directory.",
    }
)
def slack_poster_certs(c, action, dir, days=397, force=False):  # noqa: A002
    """Issue the Slack posting service certificate, or check it and store it in its secret."""
    from scripts.config import get_settings

    if action == "issue":
        flags = f" --dir {shlex.quote(dir)} --days {int(days)}"
        flags += " --force" if force else ""
        c.run(f"{HOST_PYTHON} -m scripts.slack_poster_certs issue{flags}", pty=True)
        return
    if action != "put":
        raise Exit("action must be issue or put")
    settings = get_settings()
    env = _profile_env(settings)
    c.run(
        f"{HOST_PYTHON} -m scripts.slack_poster_certs put --dir {shlex.quote(dir)}",
        pty=True,
        env=env,
    )


@task(
    help={
        "action": "issue to sign the certificate with an existing CA, or put to store it.",
        "dir": "Directory holding ca.pem and ca-key.pem from temporal-certs --internal-name.",
        "name": "issue: the ClickHouse name (default clickhouse.data.platform.internal).",
        "days": "issue: validity in days (default 397).",
        "force": "issue: replace a ClickHouse certificate already in the directory.",
    }
)
def clickhouse_certs(c, action, dir, name="", days=397, force=False):  # noqa: A002
    """Issue the ClickHouse server certificate, or check it and store it in its secret."""
    from scripts.config import get_settings

    if action == "issue":
        flags = f" --dir {shlex.quote(dir)} --days {int(days)}"
        flags += f" --name {shlex.quote(name)}" if name else ""
        flags += " --force" if force else ""
        c.run(f"{HOST_PYTHON} -m scripts.clickhouse_certs issue{flags}", pty=True)
        return
    if action != "put":
        raise Exit("action must be issue or put")
    settings = get_settings()
    env = _profile_env(settings)
    c.run(
        f"{HOST_PYTHON} -m scripts.clickhouse_certs put --dir {shlex.quote(dir)}", pty=True, env=env
    )


@task(
    help={
        "action": "issue to write a self-signed certificate, or import to import it into ACM.",
        "dir": "Directory outside the repository for webhook.pem and webhook-key.pem.",
        "name": "issue: the host name (default *.<AWS_REGION>.elb.amazonaws.com).",
        "days": "issue: validity in days (default 397).",
        "force": "issue: replace a certificate already in the directory.",
    }
)
def webhook_certs(c, action, dir, name="", days=397, force=False):  # noqa: A002
    """Issue the self-signed inbound webhook certificate, or import it into ACM."""
    from scripts.config import get_settings

    if action not in ("issue", "import"):
        raise Exit("action must be issue or import")
    flags = f" --dir {shlex.quote(dir)}"
    if action == "issue":
        flags += f" --days {int(days)}" + (f" --name {shlex.quote(name)}" if name else "")
        flags += " --force" if force else ""
    settings = get_settings()
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.webhook_certs {action}{flags}", pty=True, env=env)


@task(
    help={
        "action": "issue to sign the certificate with an existing CA, or import to import it.",
        "dir": "Directory holding ca.pem and ca-key.pem from temporal-certs --internal-name.",
        "domain": "issue: eks_mcp_gateway_domain (default the eks output's mcp_gateway_domain).",
        "days": "issue: validity in days (default 397).",
        "force": "issue: replace an MCP gateway certificate already in the directory.",
    }
)
def mcp_gateway_certs(c, action, dir, domain="", days=397, force=False):  # noqa: A002
    """Issue the MCP gateway certificate from the platform CA, or import it into ACM."""
    from scripts.config import get_settings

    if action not in ("issue", "import"):
        raise Exit("action must be issue or import")
    flags = f" --dir {shlex.quote(dir)}"
    if action == "issue":
        flags += f" --days {int(days)}" + (f" --domain {shlex.quote(domain)}" if domain else "")
        flags += " --force" if force else ""
    settings = get_settings()
    env = _profile_env(settings)
    c.run(f"{HOST_PYTHON} -m scripts.mcp_gateway_certs {action}{flags}", pty=True, env=env)


@task(help={"apply": "Perform real deletions instead of a dry-run."})
def reap(c, apply=False):
    """Reap expired platform orphan artifacts (dry-run by default)."""
    c.run(f"{HOST_PYTHON} -m scripts.reap {'--apply' if apply else ''}".strip(), pty=True)


@task(
    help={
        "root": "Workspace root to scrub (empty uses EXECUTION_WORKSPACE_ROOT).",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def scrub_workspaces(c, root="", local=False, tenant=""):
    """Clear the git credential earlier releases persisted into execution workspaces."""
    root_flag = f" --root {root}" if root else ""
    _run_scripted(c, f"python -m scripts.scrub_workspaces{root_flag}", local=local, tenant=tenant)


@task(
    help={
        "source": "Ticket source, jira or rebar. Defaults to TICKET_BACKEND and must match it.",
        "schedule": "Install the Temporal intake schedule for the source.",
        "retry": "Clear the intake dedup for a failed ticket so the next pass relaunches it.",
        "repo": "Scope a --retry to one repository.",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def intake(c, source="", schedule=False, retry="", repo="", local=False, tenant=""):
    """Reconcile ticket intake from Jira or rebar into draft PR executions (one pass by default)."""
    flags = [f"--source {source}"] if source else []
    if retry:
        flags.append(f"--retry {retry}")
        if repo:
            flags.append(f"--repo {repo}")
    elif schedule:
        flags.append("--schedule")
    _run_scripted(
        c, f"python -m scripts.intake {' '.join(flags)}".strip(), local=local, tenant=tenant
    )


@task(
    help={
        "repo": "Allowlisted repository name to pre-index.",
        "commit": "Commit to index (empty uses the head).",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def index_repo(c, repo, commit="", local=False, tenant=""):
    """Pre-index a repository in repository intelligence before intake."""
    flags = f"--repo {repo}" + (f" --commit {commit}" if commit else "")
    _run_scripted(c, f"python -m scripts.index_repo {flags}", local=local, tenant=tenant)


@task(
    help={
        "issue": "Jira issue key parked for human repair, for example ADSER-24.",
        "retry": "Re-run CI and repair on the current branch head.",
        "abandon": (
            "End the workflow. Run `inv restart --issue X` to have intake admit the ticket again."
        ),
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def repair(c, issue, retry=False, abandon=False, local=False, tenant=""):
    """Release the AWAITING_HUMAN pause on a workflow parked after CI repair exhaustion."""
    if retry == abandon:
        raise Exit("pass exactly one of --retry or --abandon")
    decision = "--retry" if retry else "--abandon"
    _run_scripted(c, f"python -m scripts.repair {issue} {decision}", local=local, tenant=tenant)


@task(
    help={
        "issue": "Jira issue key parked at a human gate, for example ADSER-24.",
        "gate": "Name of the human gate to resolve, for example plan.",
        "approve": "Approve the gate and continue the run.",
        "reject": "Reject the gate and cancel the run.",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def gate(c, issue, gate, approve=False, reject=False, local=False, tenant=""):
    """Release the AWAITING_HUMAN pause on a workflow parked at a configured human gate."""
    if approve == reject:
        raise Exit("pass exactly one of --approve or --reject")
    decision = "--approve" if approve else "--reject"
    _run_scripted(
        c,
        f"python -m scripts.gate_decision {issue} --gate {gate} {decision}",
        local=local,
        tenant=tenant,
    )


@task(
    help={
        "issue": "Jira issue key to restart, for example ADSER-24.",
        "repo": "Scope the intake retry to one repository when the issue fanned out to several.",
        "local": "Run on the host instead of the activity worker pod.",
        "tenant": _TENANT_HELP,
    }
)
def restart(c, issue, repo="", local=False, tenant=""):
    """Abandon a parked workflow and re-trigger intake for a fresh execution."""
    repo_flag = f" --repo {repo}" if repo else ""
    _run_scripted(c, f"python -m scripts.restart {issue}{repo_flag}", local=local, tenant=tenant)


@task(
    help={
        "repo": "Demo repo to ground the run with a briefing pack of its code.",
        "focus": "Center the briefing pack on a function or file (default TaskStore).",
        "work_seconds": "Length of the long-running task you interrupt with the restart.",
        "auto_restart": "Restart the worker automatically mid-task instead of by hand.",
        "show_context": "Print the full briefing pack. Act 2 shows this, leave off for Act 1.",
    }
)
def demo_durable_task(
    c, repo="", focus="TaskStore", work_seconds=15, auto_restart=False, show_context=False
):
    """Demo: a durable, briefing-pack-grounded workflow survives a worker restart (Acts 1 and 2)."""
    cmd = [sys.executable, "-m", "scripts.demo_durable_task"]
    if repo:
        cmd += ["--repo", repo]
    if focus:
        cmd += ["--focus", focus]
    cmd += ["--work-seconds", str(work_seconds)]
    if auto_restart:
        cmd.append("--auto-restart")
    if show_context:
        cmd.append("--show-context")
    process.replace_process(cmd, os.environ)


@task(help={"target": "How high to count before finishing (default 50)."})
def demo_durable_counter(c, target=50):
    """Demo: a durable workflow counts through a hand-run worker restart."""
    process.replace_process(
        [sys.executable, "-m", "scripts.demo_durable_counter", "--target", str(target)], os.environ
    )


@task(
    help={
        "task": "Real Jira ticket to snapshot for the run (defaults to ADSER-13).",
        "repo": "Repo whose head commit pins the run (defaults to agentic-demo-service).",
    }
)
def demo_coordination(c, task="ADSER-13", repo="agentic-demo-service"):
    """Demo: the coordination stack blocks double-work and duplicate side effects (Act 3)."""
    c.run(f"{HOST_PYTHON} -m scripts.demo_coordination --task {task} --repo {repo}", pty=True)


@task(
    help={
        "repo": "Repo to query (defaults to agentic-demo-service).",
        "commit": "Commit to pin (empty uses the indexed head).",
        "focus": "Center the briefing pack on a function or file (default TaskStore).",
    }
)
def demo_repo_intelligence(c, repo="agentic-demo-service", commit="", focus="TaskStore"):
    """Demo: build a briefing pack of a repo's most relevant code (Act 2)."""
    flags = f"--repo {repo}"
    flags += f" --commit {commit}" if commit else ""
    flags += f' --focus "{focus}"' if focus else ""
    c.run(f"{HOST_PYTHON} -m scripts.demo_repo_intelligence {flags}".strip(), pty=True)


@task(
    help={
        "repo": "Allowlisted repo to open the draft PR against.",
        "base": "Base branch (empty uses the configured default).",
        "task": "Jira-style task id used in the PR title and linkback.",
        "stub_agent": "Author the change with the stand-in step instead of the sandbox agent.",
        "litellm_url": "LiteLLM base URL reachable from the sandbox pod (empty uses the default).",
        "in_process": "Drive the demo in-process instead of the workflow on Temporal.",
        "full_pipeline": "Run CI, reviews, scan, policy, and the approval pause on Temporal.",
        "ci_job": "CI job for the full pipeline (empty uses <repo>-ci).",
        "await_pr_close": "After promotion, park for the PR-closed signal and finish on close.",
        "tenant": _TENANT_HELP,
    }
)
def demo_draft_pr(
    c,
    repo="agentic-demo-service",
    base="",
    task="AGENTDEV-1",
    stub_agent=False,
    litellm_url="",
    in_process=False,
    full_pipeline=False,
    ci_job="",
    await_pr_close=False,
    tenant="",
):
    """Demo: open a real draft PR on GitHub through the full coordination stack (Act 4)."""
    flags = f"--repo {repo} --task {task}"
    flags += f" --base {base}" if base else ""
    flags += " --no-live-agent" if stub_agent else ""
    flags += f" --litellm-url {litellm_url}" if litellm_url else ""
    flags += " --no-via-temporal" if in_process else ""
    flags += " --full-pipeline" if full_pipeline else ""
    flags += f" --ci-job {ci_job}" if ci_job else ""
    flags += " --await-pr-close" if await_pr_close else ""
    c.run(
        f"{HOST_PYTHON} -m scripts.demo_draft_pr {flags}".strip(),
        pty=True,
        env=_tenant_env(tenant),
    )


@task(
    help={
        "workflow_id": "Workflow id parked for approval (printed when the pipeline starts).",
        "reject": "Reject the pull request instead of approving it.",
        "pr_closed": "Send the PR-close signal for the AWAITING_PR_CLOSE stage.",
        "merged": "With pr_closed, mark the pull request merged rather than closed unmerged.",
    }
)
def approve_pr(c, workflow_id, reject=False, pr_closed=False, merged=False):
    """Release the approval pause on a running draft-PR workflow."""
    flags = f"--workflow-id {workflow_id}"
    flags += " --reject" if reject else ""
    flags += " --pr-closed" if pr_closed else ""
    flags += " --merged" if merged else ""
    c.run(f"{HOST_PYTHON} -m scripts.approve_pr {flags}".strip(), pty=True)


@task(help={"build": "Build and load the images of the current sources before deploying."})
def k8s_deploy(c, build=False):
    """Apply the cluster manifests for the last build and roll every deployment."""
    if build:
        k8s_reload(c)
        return
    _ensure_litellm_database(c)
    _k8s(c, "deploy", recorded_build_id())


@task(help={"volumes": "Also delete the workspace volume and everything on it."})
def k8s_delete(c, volumes=False):
    """Remove the cluster workloads, keeping the namespaces and, unless asked, the volume."""
    _k8s(c, "delete", recorded_build_id(), " --volumes" if volumes else "")


@task(
    help={
        "repo": "Repository name under GITHUB_OWNER to forward events from.",
        "url": "Local inbound-webhook endpoint that receives the deliveries.",
    }
)
def webhook_setup(c, repo="", url="http://localhost:8140/webhook/pr"):
    """Forward GitHub pull-request events to the local inbound-webhook via gh webhook forward."""
    repo_flag = f"--repo {repo}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.webhook_forward {repo_flag} --url {url}".strip(), pty=True)


@task(help={"repo": "Repository name under GITHUB_OWNER to clean dev hooks from."})
def webhook_teardown(c, repo=""):
    """Delete the gh webhook forward dev hooks left on the repository."""
    repo_flag = f"--repo {repo}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.webhook_forward {repo_flag} --remove".strip(), pty=True)


@task(
    help={
        "scope": "platform for config/local/image_exceptions.yaml, tenant for the tenant's file.",
        "image": "The image as repository@sha256:digest.",
        "finding": "The finding ID, such as a CVE.",
        "reason": "Why the finding is accepted.",
        "expires": "The last day the entry applies, YYYY-MM-DD, within 30 days.",
        "tenant": "Tenant for the tenant scope. It may be omitted when one local tenant exists.",
        "requester": "Who requests the entry. Defaults to the operating system user.",
        "approver": "Who approves a platform entry.",
    }
)
def image_exceptions_add(
    c, scope, image, finding, reason, expires, tenant="", requester="", approver=""
):
    """Add or renew a local image finding exception after validating its fields and scope."""
    flags = f" --scope {shlex.quote(scope)} --image {shlex.quote(image)}"
    flags += f" --finding {shlex.quote(finding)} --reason {shlex.quote(reason)}"
    flags += f" --expires {shlex.quote(expires)}"
    if scope == "tenant":
        tenant = _tenant(tenant)
    flags += _optional_flags({"tenant": tenant, "requester": requester, "approver": approver})
    c.run(f"{HOST_PYTHON} -m scripts.image_exceptions add --local{flags}", pty=True)


@task(
    help={
        "scope": "platform for config/local/image_exceptions.yaml, tenant for the tenant's file.",
        "tenant": "Tenant for the tenant scope. It may be omitted when one local tenant exists.",
    }
)
def image_exceptions_prune(c, scope, tenant=""):
    """Remove the expired entries from a local image exceptions file."""
    if scope == "tenant":
        tenant = _tenant(tenant)
    flags = f" --scope {shlex.quote(scope)}"
    flags += f" --tenant {shlex.quote(tenant)}" if tenant else ""
    c.run(f"{HOST_PYTHON} -m scripts.image_exceptions prune --local{flags}", pty=True)


@task(
    help={
        "path": "Directory to create the worktree in.",
        "branch": "Branch to check out in the worktree.",
        "create": "Create the branch instead of checking out an existing one.",
    }
)
def worktree_new(c, path, branch, create=False):
    """Add a git worktree and give it its own virtualenv."""
    create_flag = " --create" if create else ""
    c.run(
        f"{HOST_PYTHON} -m scripts.worktree new {shlex.quote(path)} "
        f"{shlex.quote(branch)}{create_flag}",
        pty=True,
    )


@task(help={"repair": "Provision a virtualenv for each worktree that shares or lacks one."})
def worktree_check(c, repair=False):
    """Report worktrees whose virtualenv is shared with, or points at, another worktree."""
    repair_flag = " --repair" if repair else ""
    c.run(f"{HOST_PYTHON} -m scripts.worktree check{repair_flag}", pty=True)


ns = Collection()
for _t in (
    tunnel_start,
    tunnel_stop,
    tunnel_status,
    connectivity_check,
    config_check,
    repos_compose,
    local_up,
    temporal_upgrade,
    local_down,
    local_stop,
    local_destroy,
    logs,
    platform_logs,
    db_migrate,
    schedules_install,
    db_reset,
    db_bootstrap_roles,
    seed,
    import_rebar_tickets,
    rebar_changes,
    rebar_store_remote,
    rebar_review_plan,
    rebar_review_code,
    operator_done,
    smoke_test,
    prompts_seed,
    evidence_bucket_bootstrap,
    review_eval,
    context_eval,
    cost_rollup,
    reconcile_usage,
    test,
    ci,
    workflow_status,
    resume_parked,
    durability_proof,
    prune_worker_versions,
    sast,
    terraform_test,
    go_test,
    cbm_build,
    publish_jumphost_image,
    publish_temporal_images,
    publish_platform_images,
    image_scan,
    aws_release,
    aws_destroy,
    temporal_certs,
    clickhouse_certs,
    webhook_certs,
    slack_poster_certs,
    mcp_gateway_certs,
    eks_deploy,
    deployment_validate,
    tenant_verify_aws,
    langfuse_ui,
    temporal_deploy,
    platform_deploy,
    temporal_ui,
    reap,
    scrub_workspaces,
    intake,
    index_repo,
    repair,
    gate,
    restart,
    demo_durable_task,
    demo_durable_counter,
    demo_coordination,
    demo_repo_intelligence,
    demo_draft_pr,
    approve_pr,
    k8s_deploy,
    k8s_delete,
    k8s_reload,
    aws_login,
    local_renew,
    local_renew_install,
    local_renew_uninstall,
    repos_sync,
    gate_engine_images,
    runtime_images,
    tenant_config_validate,
    tenant_config_publish,
    tenant_config_claims,
    fetch_results,
    webhook_setup,
    webhook_teardown,
    image_exceptions_add,
    image_exceptions_prune,
    worktree_new,
    worktree_check,
):
    ns.add_task(_t)
