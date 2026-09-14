"""Temporal activity that runs a project's declared checks in an isolated pod.

The checks run against a clean checkout of the branch at the commit, produced with ``git
archive`` so the coding agent's working tree, caches, and virtualenv never enter the pod. A repo
with no declared checks skips the gate. The activity returns the exit code and captured output,
and the workflow gates on the exit code and feeds the output into the repair loop.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass

from temporalio import activity

_IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"
)


@dataclass
class RunChecksRequest:
    repo: str
    execution_id: str
    workspace_path: str
    commit_sha: str


@dataclass
class RunChecksResult:
    ran: bool
    exit_code: int
    output: str
    skipped_reason: str = ""


def _clean_checkout(workspace_path: str, dest: str) -> None:
    """Copy the implementation working tree (base plus the change) into dest, isolated from the
    agent, without the .git directory, virtualenvs, or tool caches. The uncommitted working tree is
    the change under review, so the checks run against exactly what the pull request will carry.
    """
    shutil.copytree(workspace_path, dest, dirs_exist_ok=True, ignore=_IGNORED)


@activity.defn
async def run_checks(req: RunChecksRequest) -> RunChecksResult:
    from scripts.config import get_settings

    from services.orchestration.checks.config import checks_for_repo
    from services.orchestration.checks.runner import ChecksRunner, ChecksSpec
    from services.orchestration.gates.pod_runner import KubectlPodOps

    settings = get_settings()
    config = checks_for_repo(req.repo, settings)
    if config is None:
        return RunChecksResult(
            ran=False,
            exit_code=0,
            output="",
            skipped_reason=f"no checks configured for {req.repo}",
        )

    checkout = tempfile.mkdtemp(prefix="checks-")
    try:
        _clean_checkout(req.workspace_path, checkout)
        runner = ChecksRunner(
            KubectlPodOps(),
            namespace=settings.gate_engine_namespace,
            builder_image=settings.gate_engine_builder_image,
            privileged=settings.gate_engine_privileged,
        )
        result = await runner.run_checks(
            ChecksSpec(
                repo=req.repo,
                execution_id=req.execution_id,
                checkout_dir=checkout,
                command=config.command,
                image=config.image,
                build=config.build,
                working_dir=config.working_dir,
            )
        )
        if result.exit_code == 0:
            output = result.output
        else:
            from services.orchestration.langgraph.ci_repair import failure_summary

            output = failure_summary(result.output) or result.output
        return RunChecksResult(ran=True, exit_code=result.exit_code, output=output)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
