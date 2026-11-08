"""invoke task runner for agentic-dev-platform."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from invoke import Collection, Exit, task
from scripts.build_id import recorded_build_id, resolve_build_id
from services import process

COMPOSE = "docker compose"

# A task that runs on the host must use the interpreter running invoke. A bare `python` resolves
# to whatever the operator's PATH offers, which on a machine with a system or pyenv Python has
# none of the project's dependencies and fails on an import rather than on a missing interpreter.
HOST_PYTHON = shlex.quote(sys.executable)


def _env_file_value(name: str, path: str = ".env") -> str:
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


def _compose_files() -> str:
    """Build the -f argument list, deferring to COMPOSE_FILE when the operator sets it.

    Explicit -f flags override COMPOSE_FILE, so emitting them unconditionally would make
    every task target a different projection of the stack than the one Compose itself
    resolves. Compose reads COMPOSE_FILE from the environment and from the project .env,
    so both are checked here.
    """
    if os.environ.get("COMPOSE_FILE") or _env_file_value("COMPOSE_FILE"):
        return ""
    files = ["-f", "compose.yaml"]
    for overlay in ("compose.override.yaml", _REPOSITORIES_COMPOSE):
        if os.path.exists(overlay):
            files += ["-f", overlay]
    return " ".join(files)


_REPOSITORIES_COMPOSE = "compose.repositories.yaml"


@task
def repos_compose(c):
    """Write compose.repositories.yaml, which mounts each repository in repositories.yaml."""
    c.run(f"{HOST_PYTHON} -m scripts.repositories_compose", pty=True)


def _on_host(command: str) -> str:
    """Point a ``python ...`` command at the interpreter running invoke."""
    if command == "python" or command.startswith("python "):
        return HOST_PYTHON + command[len("python") :]
    return command


def _run_scripted(c, command: str, *, local: bool) -> None:
    """Run a python -m command in the in-cluster activity worker, or on the host with local=True."""
    if local:
        c.run(_on_host(command), pty=True)
        return
    from scripts.config import get_settings
    from scripts.k8s_cluster import exec_argv

    c.run(shlex.join(exec_argv(get_settings(), shlex.split(command), tty=False)), pty=True)


def _compose_file_flags() -> str:
    """Name the compose files the cluster scripts read the published ports from."""
    tokens = shlex.split(_compose_files())
    names = [tokens[index + 1] for index, part in enumerate(tokens) if part == "-f"]
    return "".join(f" --compose-file {shlex.quote(name)}" for name in names)


def _k8s(c, action: str, build_id: str = "", extra: str = "") -> None:
    flags = f" --build-id {shlex.quote(build_id)}" if build_id else ""
    c.run(
        f"{HOST_PYTHON} -m scripts.k8s_cluster {action}{flags}{_compose_file_flags()}{extra}",
        pty=True,
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
    """Report each per-environment config file version against the version this release expects."""
    c.run(f"{HOST_PYTHON} -m scripts.config_check", pty=True)


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
    c.run(f"{COMPOSE} {files} build --build-arg AGENT_BUILD_ID={shlex.quote(build_id)}")
    db_migrate(c)
    _require_temporal_schema_at_target(c)
    _temporal_admin(c, "schema")
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 temporal")
    _temporal_admin(c, "namespace")
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 --remove-orphans")
    _k8s(c, "reload", build_id)
    aws_login(c)
    repos_sync(c)


@task(
    help={
        "force": "Replace the workers even when running executions are pinned to the "
        "outgoing build."
    }
)
def k8s_reload(c, force=False):
    """Rebuild the platform images for the current sources and roll compose and the cluster.

    The compose workflow worker and the cluster workers register as one Worker Deployment
    version, so both move to the build these sources produce.
    """
    outgoing = recorded_build_id()
    build_id = resolve_build_id()
    if build_id != outgoing and not force:
        _refuse_to_strand(outgoing)
    os.environ["AGENT_BUILD_ID"] = build_id
    files = _compose_files()
    c.run(f"{COMPOSE} {files} build --build-arg AGENT_BUILD_ID={shlex.quote(build_id)}")
    c.run(f"{COMPOSE} {files} up -d --wait --wait-timeout 600 --remove-orphans")
    _k8s(c, "reload", build_id)


@task
def aws_login(c):
    """Sign in when needed and write the AWS credentials to the cluster Secret."""
    c.run(f"{HOST_PYTHON} -m scripts.aws_login", pty=True)


@task(help={"repo": "Sync only this repository or gate engine."})
def repos_sync(c, repo=""):
    """Copy the local-backend repositories and local gate engines into the cluster volume."""
    flag = f" --repo {shlex.quote(repo)}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.repos_sync{flag}", pty=True)


@task(help={"repo": "Fetch only this repository."})
def fetch_results(c, repo=""):
    """Fetch the agent branches and their results refs from the cluster into the host checkouts."""
    flag = f" --repo {shlex.quote(repo)}" if repo else ""
    c.run(f"{HOST_PYTHON} -m scripts.fetch_results{flag}", pty=True)


@dataclass(frozen=True)
class TemporalRelease:
    version: str
    schema: tuple[int, int]
    visibility: tuple[int, int]
    server_image: str = ""
    admin_tools_image: str = ""

    def image_env(self) -> dict[str, str]:
        if not self.server_image:
            return {}
        return {
            "TEMPORAL_SERVER_IMAGE": self.server_image,
            "TEMPORAL_ADMIN_TOOLS_IMAGE": self.admin_tools_image,
        }


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
TEMPORAL_TARGET = TemporalRelease("1.32.1", (1, 19), (1, 14))
TEMPORAL_DATABASES = ("temporal", "temporal_visibility")


def _temporal_admin(c, action: str, env: dict[str, str] | None = None) -> None:
    c.run(
        f"{COMPOSE} {_compose_files()} run --rm temporal-admin-tools {action}",
        pty=True,
        env=env or {},
    )


def _psql(c, database: str, query: str) -> str:
    sql = shlex.quote(f'psql -U "$POSTGRES_USER" -d {database} -Atc "{query}"')
    return c.run(
        f"{COMPOSE} {_compose_files()} exec -T postgres sh -c {sql}", hide=True
    ).stdout.strip()


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
    if not skip_backup:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        _backup_temporal_databases(
            c, backup_dir or os.path.join(os.path.expanduser("~"), "temporal-backup", stamp)
        )
    for release in steps:
        print(f"[temporal-upgrade] applying the schema and starting server {release.version}")
        env = release.image_env()
        _temporal_admin(c, "schema", env)
        c.run(
            f"{COMPOSE} {files} up -d --build --no-deps --wait --wait-timeout 600 temporal",
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


@task(help={"service": "Optional single service to tail."})
def logs(c, service=""):
    """Follow logs for the compose stack, or for one compose service or cluster deployment."""
    from scripts.k8s_cluster import DEPLOYMENTS

    if service in DEPLOYMENTS:
        from scripts.config import get_settings
        from scripts.k8s_cluster import namespaced

        argv = namespaced(get_settings(), "logs", "-f", f"deployment/{service}", "--all-containers")
        c.run(shlex.join(argv), pty=True)
        return
    c.run(f"{COMPOSE} {_compose_files()} logs -f {service}".strip(), pty=True)


@task
def db_migrate(c):
    """Apply migrations by running the db-migrate container."""
    c.run(f"{COMPOSE} {_compose_files()} run --rm --build db-migrate", pty=True)


@task
def schedules_install(c):
    """Register the Temporal reap and intake schedules by running the schedule-bootstrap task."""
    c.run(f"{COMPOSE} {_compose_files()} run --rm --build schedule-bootstrap", pty=True)


@task
def db_reset(c):
    """Drop and recreate the ``agent_platform`` schema, then re-migrate."""
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
    }
)
def import_rebar_tickets(c, xlsx="", root="", status="", limit=0, reset=False, local=False):
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
    _run_scripted(c, f"python -m scripts.import_rebar_tickets {flags}".strip(), local=local)


@task(
    help={
        "new": "Report new tickets.",
        "field-changes": "Report field changes.",
        "status-changes": "Report status changes.",
        "local": "Run on the host instead of in the activity worker pod.",
    }
)
def rebar_changes(c, new=False, field_changes=False, status_changes=False, local=False):
    """Report rebar ticket changes since the last intake pass without starting work."""
    flags = []
    if new:
        flags.append("--new")
    if field_changes:
        flags.append("--field-changes")
    if status_changes:
        flags.append("--status-changes")
    _run_scripted(c, f"python -m scripts.rebar_poll {' '.join(flags)}".strip(), local=local)


@task(help={"local": "Run on the host instead of in the activity worker pod."})
def rebar_store_remote(c, local=False):
    """Point the rebar ticket store at its S3 bucket as the reader for fetch and writer for push."""
    command = "python -m scripts.rebar_store_remote"
    if local:
        c.run(_on_host(command), pty=True)
        return
    # A one-off pod, because the activity worker refuses to start while the remote is wrong.
    _k8s(c, "one-off", extra=f" -- {command}")


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
    container-relative SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT from .env must resolve to the host
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
    }
)
def operator_done(c, workflow_id, repo, branch, completion_signal_id="", closed_unmerged=False):
    """Complete a local-backend run by verifying artifacts and signaling the workflow."""
    flags = f"--workflow-id {workflow_id} --repo {repo} --branch {branch}"
    if completion_signal_id:
        flags += f" --completion-signal-id {completion_signal_id}"
    if closed_unmerged:
        flags += " --closed-unmerged"
    fetch_results(c, repo=repo)
    c.run(
        f"{HOST_PYTHON} -m scripts.operator_done {flags}",
        pty=True,
        env=_host_source_control_env(),
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
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
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


@task(help={"execution-id": "The execution id to roll up."})
def cost_rollup(c, execution_id=""):
    """Print the per-stage cost, token, and budget rollup for one execution."""
    if not execution_id:
        raise Exit("provide --execution-id")
    c.run(f"{HOST_PYTHON} -m scripts.cost_rollup --execution-id {execution_id}", pty=True)


@task(help={"execution-id": "The execution id to reconcile."})
def reconcile_usage(c, execution_id=""):
    """Reconcile an execution's Langfuse generations against its usage metrics."""
    if not execution_id:
        raise Exit("provide --execution-id")
    c.run(f"{HOST_PYTHON} -m scripts.reconcile_usage --execution-id {execution_id}", pty=True)


@task
def test(c):
    """Run the pytest suite."""
    c.run(f"{HOST_PYTHON} -m pytest", pty=True)


@task
def ci(c):
    """Run the portable CI checks: lint, the hermetic test tiers, and the evaluation gate."""
    c.run(f"{HOST_PYTHON} -m ruff check", pty=True)
    c.run(f"{HOST_PYTHON} -m pytest -q tests/unit tests/end_to_end", pty=True)
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


@task
def resume_parked(c):
    """Resume parked workflows a newer agent build can retry."""
    c.run(f"{HOST_PYTHON} -m scripts.resume_parked", pty=True)


@task
def prune_worker_versions(c):
    """Delete the drained versions of the Temporal worker deployment."""
    c.run(f"{HOST_PYTHON} -m scripts.prune_worker_versions", pty=True)


@task
def durability_proof(c):
    """Prove a workflow survives a worker restart."""
    c.run(f"{HOST_PYTHON} -m scripts.durability_proof", pty=True)


@task(help={"dest": "Output path for the vendored binary (default vendor/bin/acli)."})
def fetch_acli(c, dest="vendor/bin/acli"):
    """Fetch and vendor the Atlassian acli binary (configurable URL, pinned sha256)."""
    c.run(f"{HOST_PYTHON} -m scripts.fetch_acli --dest {dest}", pty=True)


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
        "tag": "Image tag to build and push (default v1). Bump to ship a new image.",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "base_image": "Override the Dockerfile base image, for an approved registry.",
        "repo": "ECR repository name (default agentic-platform/jumphost-base).",
        "region": "AWS region (empty uses AWS_REGION).",
    }
)
def publish_jumphost_image(c, tag="v1", arch="arm64", base_image="", repo="", region=""):
    """Build and push the Fargate jumphost-base image to the account ECR repository."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f"-t {tag} -a {arch}"
    flags += f" -b {base_image}" if base_image else ""
    flags += f" -r {repo}" if repo else ""
    flags += f" -R {region}" if region else ""
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(f"bash infra/bin/publish_jumphost_image.sh {flags}".strip(), pty=True, env=env)


@task(
    help={
        "name_prefix": "Repository prefix (default agentic-platform).",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "region": "AWS region (empty uses AWS_REGION).",
    }
)
def publish_temporal_images(c, name_prefix="", arch="arm64", region=""):
    """Build and push the Temporal server and admin-tools images to the account ECR repositories."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --arch {shlex.quote(arch)}"
    flags += f" --name-prefix {shlex.quote(name_prefix)}" if name_prefix else ""
    flags += f" --region {shlex.quote(region)}" if region else ""
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(f"{HOST_PYTHON} -m scripts.publish_temporal_images{flags}", pty=True, env=env)


@task(
    help={
        "name_prefix": "Repository prefix (default agentic-platform).",
        "arch": "Target architecture, arm64 or amd64 (default arm64).",
        "region": "AWS region (empty uses AWS_REGION).",
        "install_acli": "Install acli in the platform image, as INSTALL_ACLI does locally.",
        "acli_version": "acli version (empty uses the version scripts/fetch_acli.py pins).",
        "acli_sha256": "SHA-256 of that acli version, required with --acli-version.",
        "manifest": "Also write each image's repository, digest, and whether this run pushed it.",
    }
)
def publish_platform_images(
    c,
    name_prefix="",
    arch="arm64",
    region="",
    install_acli=False,
    acli_version="",
    acli_sha256="",
    manifest="",
):
    """Build the platform images, mirror Langfuse and ClickHouse, push them all to ECR, and
    write their references to images.auto.tfvars.json in the composition root."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --arch {shlex.quote(arch)}"
    flags += f" --name-prefix {shlex.quote(name_prefix)}" if name_prefix else ""
    flags += f" --region {shlex.quote(region)}" if region else ""
    flags += " --install-acli" if install_acli else ""
    flags += f" --acli-version {shlex.quote(acli_version)}" if acli_version else ""
    flags += f" --acli-sha256 {shlex.quote(acli_sha256)}" if acli_sha256 else ""
    flags += f" --manifest {shlex.quote(manifest)}" if manifest else ""
    c.run(
        f"{HOST_PYTHON} -m scripts.publish_platform_images{flags}", pty=True, env=_aws_env(settings)
    )


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


@task(
    help={
        "var_file": "Var file in the composition root (default greenfield.tfvars).",
        "delete_images": "Also delete the ECR repositories and their images.",
        "plan_only": "Show what the destroy removes and keeps, then stop.",
    }
)
def aws_destroy(c, var_file="greenfield.tfvars", delete_images=False, plan_only=False):
    """Destroy the composition root after the account ID is typed, keeping bootstrap and images."""
    from scripts.config import get_settings

    flags = f" --var-file {shlex.quote(var_file)}"
    flags += " --delete-images" if delete_images else ""
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
    """Plan and apply the composition root with the published images, then run platform-deploy."""
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
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(f"{HOST_PYTHON} -m scripts.temporal_deploy{flags}", pty=True, env=env)


@task(help={"region": "AWS region (empty uses AWS_REGION)."})
def platform_deploy(c, region=""):
    """Migrate the database, roll the platform services, register the schedules, and prune."""
    from scripts.config import get_settings

    settings = get_settings()
    flags = f" --region {shlex.quote(region)}" if region else ""
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(f"{HOST_PYTHON} -m scripts.platform_deploy{flags}", pty=True, env=env)


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
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(f"{HOST_PYTHON} -m scripts.temporal_certs put{flags}", pty=True, env=env)


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
    env = {"AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    c.run(
        f"{HOST_PYTHON} -m scripts.clickhouse_certs put --dir {shlex.quote(dir)}", pty=True, env=env
    )


@task(help={"apply": "Perform real deletions instead of a dry-run."})
def reap(c, apply=False):
    """Reap expired platform orphan artifacts (dry-run by default)."""
    c.run(f"{HOST_PYTHON} -m scripts.reap {'--apply' if apply else ''}".strip(), pty=True)


@task(
    help={
        "root": "Workspace root to scrub (empty uses EXECUTION_WORKSPACE_ROOT).",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def scrub_workspaces(c, root="", local=False):
    """Clear the git credential earlier releases persisted into execution workspaces."""
    root_flag = f" --root {root}" if root else ""
    _run_scripted(c, f"python -m scripts.scrub_workspaces{root_flag}", local=local)


@task(
    help={
        "source": "Ticket source, jira or rebar. Defaults to TICKET_BACKEND and must match it.",
        "schedule": "Install the Temporal intake schedule for the source.",
        "retry": "Clear the intake dedup for a failed ticket so the next pass relaunches it.",
        "repo": "Scope a --retry to one repository.",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def intake(c, source="", schedule=False, retry="", repo="", local=False):
    """Reconcile ticket intake from Jira or rebar into draft PR executions (one pass by default)."""
    flags = [f"--source {source}"] if source else []
    if retry:
        flags.append(f"--retry {retry}")
        if repo:
            flags.append(f"--repo {repo}")
    elif schedule:
        flags.append("--schedule")
    _run_scripted(c, f"python -m scripts.intake {' '.join(flags)}".strip(), local=local)


@task(
    help={
        "repo": "Allowlisted repository name to pre-index.",
        "commit": "Commit to index (empty uses the head).",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def index_repo(c, repo, commit="", local=False):
    """Pre-index a repository in repository intelligence before intake."""
    flags = f"--repo {repo}" + (f" --commit {commit}" if commit else "")
    _run_scripted(c, f"python -m scripts.index_repo {flags}", local=local)


@task(
    help={
        "issue": "Jira issue key parked for human repair, for example ADSER-24.",
        "retry": "Re-run CI and repair on the current branch head.",
        "abandon": "Abandon the workflow so intake can relaunch a fresh execution.",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def repair(c, issue, retry=False, abandon=False, local=False):
    """Release the AWAITING_HUMAN pause on a workflow parked after CI repair exhaustion."""
    if retry == abandon:
        raise Exit("pass exactly one of --retry or --abandon")
    decision = "--retry" if retry else "--abandon"
    _run_scripted(c, f"python -m scripts.repair {issue} {decision}", local=local)


@task(
    help={
        "issue": "Jira issue key parked at a human gate, for example ADSER-24.",
        "gate": "Name of the human gate to resolve, for example plan.",
        "approve": "Approve the gate and continue the run.",
        "reject": "Reject the gate and cancel the run.",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def gate(c, issue, gate, approve=False, reject=False, local=False):
    """Release the AWAITING_HUMAN pause on a workflow parked at a configured human gate."""
    if approve == reject:
        raise Exit("pass exactly one of --approve or --reject")
    decision = "--approve" if approve else "--reject"
    _run_scripted(
        c, f"python -m scripts.gate_decision {issue} --gate {gate} {decision}", local=local
    )


@task(
    help={
        "issue": "Jira issue key to restart, for example ADSER-24.",
        "repo": "Scope the intake retry to one repository when the issue fanned out to several.",
        "local": "Run on the host instead of the activity worker pod.",
    }
)
def restart(c, issue, repo="", local=False):
    """Abandon a parked workflow and re-trigger intake for a fresh execution."""
    repo_flag = f" --repo {repo}" if repo else ""
    _run_scripted(c, f"python -m scripts.restart {issue}{repo_flag}", local=local)


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
    c.run(f"{HOST_PYTHON} -m scripts.demo_draft_pr {flags}".strip(), pty=True)


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
    durability_proof,
    prune_worker_versions,
    fetch_acli,
    sast,
    terraform_test,
    go_test,
    cbm_build,
    publish_jumphost_image,
    publish_temporal_images,
    publish_platform_images,
    aws_release,
    aws_destroy,
    temporal_certs,
    clickhouse_certs,
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
    repos_sync,
    fetch_results,
    webhook_setup,
    webhook_teardown,
):
    ns.add_task(_t)
