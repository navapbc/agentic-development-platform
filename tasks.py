"""invoke task runner for agentic-dev-platform."""

from __future__ import annotations

import os

from invoke import Collection, task

COMPOSE = "docker compose"


def _compose_files() -> str:
    files = ["-f", "compose.yaml"]
    if os.path.exists("compose.override.yaml"):
        files += ["-f", "compose.override.yaml"]
    return " ".join(files)


@task(help={"detach": "Run the tunnel in the background."})
def tunnel_start(c, detach=False):
    """Start the jump-host tunnel to Bedrock."""
    c.run(f"python -m scripts.tunnel start {'--detach' if detach else ''}", pty=True)


@task
def connectivity_check(c):
    """Validate the Bedrock path and local service reachability."""
    c.run("python -m scripts.connectivity", pty=True)


@task
def local_up(c):
    """Bring up the Phase-1 Compose stack."""
    c.run(f"{COMPOSE} {_compose_files()} up -d --build")


@task
def local_down(c):
    """Stop the local stack (containers + network; volumes preserved)."""
    c.run(f"{COMPOSE} {_compose_files()} down")


@task(help={"service": "Optional single service to tail."})
def logs(c, service=""):
    """Follow logs for the stack or a single service."""
    c.run(f"{COMPOSE} {_compose_files()} logs -f {service}".strip(), pty=True)


@task
def db_migrate(c):
    """Apply migrations with alembic upgrade head."""
    c.run("alembic upgrade head", pty=True)


@task
def db_reset(c):
    """Drop and recreate the ``agent_platform`` schema, then re-migrate."""
    c.run("python -m scripts.db_reset", pty=True)
    db_migrate(c)


@task
def seed(c):
    """Load local seed/fixture data (allowlisted AGENTDEV + demo repos)."""
    c.run("python -m scripts.seed", pty=True)


@task
def smoke_test(c):
    """Run the Phase-1 smoke test: DB reachable, services healthy, Bedrock path."""
    c.run("python -m scripts.smoke_test", pty=True)


@task
def test(c):
    """Run the pytest suite."""
    c.run("pytest", pty=True)


@task
def workflow_status(c):
    """Show recent Temporal workflow executions."""
    c.run("python -m scripts.workflow_status", pty=True)


@task
def durability_proof(c):
    """Prove a workflow survives a worker restart (Phase-1 exit criterion)."""
    c.run("python -m scripts.durability_proof", pty=True)


@task(help={"dest": "Output path for the vendored binary (default vendor/bin/acli)."})
def fetch_acli(c, dest="vendor/bin/acli"):
    """Fetch and vendor the Atlassian acli binary (configurable URL, pinned sha256)."""
    c.run(f"python -m scripts.fetch_acli --dest {dest}", pty=True)


@task(help={"apply": "Perform real deletions instead of a dry-run."})
def reap(c, apply=False):
    """Reap expired platform orphan artifacts (dry-run by default)."""
    c.run(f"python -m scripts.reap {'--apply' if apply else ''}".strip(), pty=True)


ns = Collection()
for _t in (
    tunnel_start,
    connectivity_check,
    local_up,
    local_down,
    logs,
    db_migrate,
    db_reset,
    seed,
    smoke_test,
    test,
    workflow_status,
    durability_proof,
    fetch_acli,
    reap,
):
    ns.add_task(_t)
