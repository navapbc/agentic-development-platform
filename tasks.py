"""invoke task runner for agentic-dev-platform."""

from __future__ import annotations

import os
import sys

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
    c.run(f"python -m scripts.tunnel start {'--detach' if detach else ''}".strip(), pty=True)


@task
def tunnel_stop(c):
    """Stop the backgrounded Bedrock connectivity tunnel."""
    c.run("python -m scripts.tunnel stop", pty=True)


@task
def tunnel_status(c):
    """Report whether the backgrounded Bedrock connectivity tunnel is running."""
    c.run("python -m scripts.tunnel status", pty=True)


@task
def connectivity_check(c):
    """Validate the Bedrock path and local service reachability."""
    c.run("python -m scripts.connectivity", pty=True)


@task
def local_up(c):
    """Bring up the local Compose stack."""
    c.run(f"{COMPOSE} {_compose_files()} --profile repo-intelligence up -d --build")


@task
def local_down(c):
    """Stop the local stack (containers + network; volumes preserved)."""
    c.run(f"{COMPOSE} {_compose_files()} down")


@task
def local_stop(c):
    """Stop the local stack without removing containers (containers + volumes preserved)."""
    c.run(f"{COMPOSE} {_compose_files()} stop")


@task
def local_destroy(c):
    """Bring the local stack down and delete its volumes (destroys Postgres and index data)."""
    c.run(f"{COMPOSE} {_compose_files()} down --volumes --remove-orphans")


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
    """Run the smoke test: DB reachable, services healthy, Bedrock path."""
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
def resume_parked(c):
    """Resume parked workflows a newer agent build can retry."""
    c.run("python -m scripts.resume_parked", pty=True)


@task
def durability_proof(c):
    """Prove a workflow survives a worker restart."""
    c.run("python -m scripts.durability_proof", pty=True)


@task(help={"dest": "Output path for the vendored binary (default vendor/bin/acli)."})
def fetch_acli(c, dest="vendor/bin/acli"):
    """Fetch and vendor the Atlassian acli binary (configurable URL, pinned sha256)."""
    c.run(f"python -m scripts.fetch_acli --dest {dest}", pty=True)


@task(help={"apply": "Perform real deletions instead of a dry-run."})
def reap(c, apply=False):
    """Reap expired platform orphan artifacts (dry-run by default)."""
    c.run(f"python -m scripts.reap {'--apply' if apply else ''}".strip(), pty=True)


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
    os.execv(sys.executable, cmd)


@task(help={"target": "How high to count before finishing (default 50)."})
def demo_durable_counter(c, target=50):
    """Demo: a durable workflow counts through a hand-run worker restart."""
    os.execv(
        sys.executable,
        [sys.executable, "-m", "scripts.demo_durable_counter", "--target", str(target)],
    )


@task(
    help={
        "task": "Real Jira ticket to snapshot for the run (defaults to ADSER-13).",
        "repo": "Repo whose head commit pins the run (defaults to agentic-demo-service).",
    }
)
def demo_coordination(c, task="ADSER-13", repo="agentic-demo-service"):
    """Demo: the coordination stack blocks double-work and duplicate side effects (Act 3)."""
    c.run(f"python -m scripts.demo_coordination --task {task} --repo {repo}", pty=True)


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
    c.run(f"python -m scripts.demo_repo_intelligence {flags}".strip(), pty=True)


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
    c.run(f"python -m scripts.demo_draft_pr {flags}".strip(), pty=True)


@task(
    help={
        "workflow_id": "Workflow id parked for approval (printed when the pipeline starts).",
        "approver": "Approval identity recorded in the audit event.",
        "reject": "Reject the pull request instead of approving it.",
        "pr_closed": "Send the PR-close signal for the AWAITING_PR_CLOSE stage.",
        "merged": "With pr_closed, mark the pull request merged rather than closed unmerged.",
    }
)
def approve_pr(
    c, workflow_id, approver="release-manager", reject=False, pr_closed=False, merged=False
):
    """Release the approval pause on a running draft-PR workflow."""
    flags = f'--workflow-id {workflow_id} --approver "{approver}"'
    flags += " --reject" if reject else ""
    flags += " --pr-closed" if pr_closed else ""
    flags += " --merged" if merged else ""
    c.run(f"python -m scripts.approve_pr {flags}".strip(), pty=True)


_K8S_LOCAL = "deploy/kubernetes/local"
_K8S_RENDER = f"kubectl kustomize --load-restrictor LoadRestrictionsNone {_K8S_LOCAL}"


@task(
    help={
        "image": "Local image tag serving both the gateway and the webhook.",
        "build": "Build and tag the image from docker/Dockerfile before deploying.",
    }
)
def k8s_deploy(c, image="agentic-dev-platform-local:latest", build=False):
    """Deploy the sandbox namespace, mcp-gateway, and pr-webhook to the local cluster."""
    if build:
        c.run(
            f"docker build --build-context rebar=../rebar -f docker/Dockerfile -t {image} .",
            pty=True,
        )
    c.run(f"kubectl apply -f {_K8S_LOCAL}/namespaces.yaml", pty=True)
    c.run(f"{_K8S_RENDER} | kubectl apply -f -", pty=True)
    c.run(
        "kubectl rollout status deployment/mcp-gateway -n agentic-dev-platform --timeout=120s",
        pty=True,
    )
    c.run(
        "kubectl rollout status deployment/pr-webhook -n agentic-dev-platform --timeout=120s",
        pty=True,
    )


@task
def k8s_delete(c):
    """Remove the local mcp-gateway and pr-webhook deployment."""
    c.run(f"{_K8S_RENDER} | kubectl delete --ignore-not-found -f -", pty=True)


@task(
    help={
        "repo": "Repository name under GITHUB_OWNER to forward events from.",
        "url": "Local pr-webhook endpoint that receives the deliveries.",
    }
)
def webhook_setup(c, repo="", url="http://localhost:8140/webhook/pr"):
    """Forward GitHub pull-request events to the local pr-webhook via gh webhook forward."""
    repo_flag = f"--repo {repo}" if repo else ""
    c.run(f"python -m scripts.webhook_forward {repo_flag} --url {url}".strip(), pty=True)


@task(help={"repo": "Repository name under GITHUB_OWNER to clean dev hooks from."})
def webhook_teardown(c, repo=""):
    """Delete the gh webhook forward dev hooks left on the repository."""
    repo_flag = f"--repo {repo}" if repo else ""
    c.run(f"python -m scripts.webhook_forward {repo_flag} --remove".strip(), pty=True)


ns = Collection()
for _t in (
    tunnel_start,
    tunnel_stop,
    tunnel_status,
    connectivity_check,
    local_up,
    local_down,
    local_stop,
    local_destroy,
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
    demo_durable_task,
    demo_durable_counter,
    demo_coordination,
    demo_repo_intelligence,
    demo_draft_pr,
    approve_pr,
    k8s_deploy,
    k8s_delete,
    webhook_setup,
    webhook_teardown,
):
    ns.add_task(_t)
