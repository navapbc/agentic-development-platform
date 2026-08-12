"""Headline demo: open a real draft PR on a GitHub repo through the full coordination stack."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import subprocess
import uuid
from datetime import timedelta
from typing import Any

from adapters.interfaces.git_host import agent_branch_name
from adapters.real.git_host import RealGitHost
from services.coordination import (
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
)
from services.coordination.orphans import InMemoryOrphanRegistry
from services.integration.credentials import ConfigError
from services.integration.github.credentials import build_single_identity_resolver
from services.integration.github.mediator import GitHubMediator
from services.integration.jira import (
    CLOUD,
    DATACENTER,
    TaskSnapshot,
    ensure_acli_authenticated,
)
from services.integration.jira.adapter import build_jira_adapter_for_issue, project_of
from services.integration.jira.credentials import (
    StatusFlow,
    build_jira_credential_resolver,
    build_single_project_resolver,
)
from services.integration.jira.linkback import post_pr_linkback
from services.integration.jira.normalize import normalize_issue
from services.integration.jira.snapshot import SnapshotCandidate, snapshot_id_for
from services.orchestration.bounded_task_pipeline import (
    DRAFT_PR_OPEN,
    EXISTING_PR_REUSED,
    CodeResult,
    PipelineDeps,
    PipelineRequest,
    run_bounded_task,
)
from services.orchestration.branch_sync import GitBranchSynchronizer
from services.orchestration.materialization import MaterializedWorkspace, WorkspaceMaterializer
from services.orchestration.openhands_code_step import build_openhands_code_step

from scripts.config import Settings, get_settings, to_host_hostport
from scripts.connectivity import preflight as bedrock_preflight


def _authed_remote(settings: Settings, repo: str) -> str:
    base = settings.github_git_base_url.rstrip("/")
    if settings.github_token and base.startswith("https://"):
        base = f"https://x-access-token:{settings.github_token}@{base[len('https://') :]}"
    return f"{base}/{settings.github_owner}/{repo}.git"


def _demo_jira_adapter(settings: Settings, issue_key: str):
    project = project_of(issue_key)
    token = (
        settings.jira_pat
        if settings.jira_auth_mode.strip().lower() == DATACENTER
        else settings.jira_api_token
    )
    resolver = build_single_project_resolver(
        url=settings.jira_url,
        auth_mode=settings.jira_auth_mode,
        email=settings.jira_email,
        token=token,
        board=project,
        project=project,
    )
    return build_jira_adapter_for_issue(issue_key, settings, resolver=resolver)


def _demo_status_flow(settings: Settings, issue_key: str) -> StatusFlow:
    try:
        return build_jira_credential_resolver(settings).status_flow(project_of(issue_key))
    except ConfigError:
        return StatusFlow()


def _remote_head(remote: str, branch: str) -> str:
    proc = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


def _issue(task_id: str) -> dict:
    return {
        "key": task_id,
        "fields": {
            "project": {"key": task_id.split("-")[0]},
            "summary": "Demonstrate the automated draft PR",
            "description": "Append a demo marker and open a draft pull request.",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "labels": ["demo"],
        },
    }


class _SnapshotStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}

    async def persist(self, candidate: SnapshotCandidate) -> TaskSnapshot:
        history = self.by_execution.setdefault(candidate.execution_id, [])
        if history and history[-1].content_hash == candidate.content_hash:
            return history[-1]
        version = history[-1].version + 1 if history else 1
        snapshot = TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, version),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=version,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )
        history.append(snapshot)
        return snapshot


class _FakeReader:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.calls = 0

    def get_issue(self, issue_key: str) -> dict:
        self.calls += 1
        return dict(self.issue)


class _FakeLinkbackClient:
    def __init__(self, source: str) -> None:
        self.source = source
        self.comments: list[str] = []

    def list_comment_bodies(self, issue_key: str) -> tuple[str, ...]:
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append(body)


class _DemoCodeStep:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self.tasks: list[str] = []

    async def run(self, *, workspace: MaterializedWorkspace, task: str) -> CodeResult:
        self.tasks.append(task)
        readme = os.path.join(workspace.path, "README.md")
        line = f"\nAutomated draft demo marker for execution {self.execution_id}.\n"
        with open(readme, "a", encoding="utf-8") as handle:
            handle.write(line)
        return CodeResult(cost=0.2, tokens=150, llm_calls=2, terminal_reason="completed")


def _missing(settings: Settings) -> list[str]:
    required = {
        "GITHUB_API_URL": settings.github_api_url,
        "GITHUB_GIT_BASE_URL": settings.github_git_base_url,
        "GITHUB_OWNER": settings.github_owner,
        "GITHUB_TOKEN": settings.github_token,
    }
    return [name for name, value in required.items() if not value]


def _jira_configured(settings: Settings) -> bool:
    return (
        settings.jira_auth_mode.strip().lower() == CLOUD
        and bool(settings.jira_url)
        and bool(settings.jira_email)
        and bool(settings.jira_api_token)
    )


_DONE_STATUSES = frozenset(
    {"done", "closed", "resolved", "cancelled", "canceled", "won't do", "wont do"}
)


def _jira_task_actionable(adapter, task_id: str, parked_status: str = "") -> tuple[bool, str]:
    try:
        raw = adapter.get_issue(task_id)
    except Exception as exc:
        return False, f"could not read {task_id} from Jira ({exc})"
    status = ((raw.get("fields") or {}).get("status") or {}).get("name") or ""
    if status.strip().lower() in _DONE_STATUSES:
        return False, f"{task_id} is '{status}'; choose an actionable (not done) ticket"
    if parked_status and status.strip().lower() == parked_status.strip().lower():
        return False, f"{task_id} is parked ('{status}'); a newer build resumes it automatically"
    return True, status or "unknown status"


_PHASE_STEPS: tuple[tuple[str, str], ...] = (
    ("impl", "claims the work and writes the change"),
    ("sync", "syncs the branch onto the base"),
    ("commit", "commits and pushes the change"),
    ("pr", "opens the draft pull request"),
    ("linkback", "links the pull request back to Jira"),
)


async def _track_phases(handle: Any) -> None:
    from temporalio.api.enums.v1 import EventType

    labels = dict(_PHASE_STEPS)
    total = len(_PHASE_STEPS)
    order = {key: index + 1 for index, (key, _) in enumerate(_PHASE_STEPS)}
    announced: set[str] = set()

    def announce(key: str) -> None:
        if key in announced:
            return
        announced.add(key)
        print(f"[demo]   step {order[key]}/{total}: {labels[key]}")

    try:
        async for event in handle.fetch_history_events(wait_new_event=True):
            if event.event_type == EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
                started = event.child_workflow_execution_started_event_attributes
                child_id = started.workflow_execution.workflow_id
                if child_id.endswith("-impl"):
                    announce("impl")
                elif child_id.endswith("-sync"):
                    announce("sync")
                elif child_id.endswith("-pr"):
                    announce("pr")
            elif event.event_type == EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED:
                completed = event.child_workflow_execution_completed_event_attributes
                child_id = completed.workflow_execution.workflow_id
                if child_id.endswith("-pr"):
                    announce("linkback")
            elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                name = event.activity_task_scheduled_event_attributes.activity_type.name
                if name == "commit_and_push":
                    announce("commit")
                elif name == "link_pr_to_issue":
                    announce("linkback")
    except Exception as exc:  # noqa: BLE001
        print(f"[demo]   (phase tracking stopped: {exc})")


async def _run_via_temporal(
    settings: Settings,
    *,
    repo: str,
    base: str,
    base_sha: str,
    task_id: str,
    summary: str,
    raw_issue: dict,
    full_pipeline: bool = False,
    ci_job: str = "",
    await_pr_close: bool = False,
) -> int:
    from services.orchestration.temporal_workflows.draft_pull_request import (
        DRAFT_PR_OPEN,
        EXISTING_PR_REUSED,
        PR_MERGED,
        PR_PROMOTED,
        DraftPullRequestInput,
        DraftPullRequestWorkflow,
    )
    from temporalio.client import Client

    stored_issue = normalize_issue(
        raw_issue, acceptance_field=settings.jira_acceptance_field
    ).to_dict()
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    workflow_id = f"draft-pr-{task_id}-{execution_id}"
    address = to_host_hostport(settings.temporal_host)
    print(f"[demo] connecting to Temporal at {address}")
    client = await Client.connect(address, namespace=settings.temporal_namespace)
    print(f"[demo] starting {workflow_id} on task queue {settings.temporal_task_queue}")
    if full_pipeline:
        print(
            "[demo] full pipeline: the platform claims the work, writes the change in a "
            "sandbox pod, syncs, commits, opens the pull request, runs CI with the repair "
            "loop, runs the review gates, scans and evaluates policy, parks for approval, "
            "and promotes the pull request"
        )
        print(f"[demo] approve the pause with: inv approve-pr --workflow-id {workflow_id}")
    else:
        print(
            "[demo] the platform claims the work, writes the change, syncs the branch, commits, "
            "opens the pull request, and links it back to Jira"
        )
    handle = await client.start_workflow(
        DraftPullRequestWorkflow.run,
        DraftPullRequestInput(
            task_id=task_id,
            execution_id=execution_id,
            repo=repo,
            base=base,
            base_sha=base_sha,
            task=summary,
            issue_key=task_id,
            focus=summary,
            commit_message="Add automated draft demo marker",
            stored_issue=stored_issue,
            build_id=settings.agent_build_id,
            parked_status=settings.jira_parked_status,
            ci_job=ci_job,
            run_reviews=full_pipeline,
            run_policy=full_pipeline,
            run_approval=full_pipeline,
            await_pr_close=await_pr_close,
            jira_project=settings.jira_project or task_id.split("-")[0],
            plan_approved=full_pipeline,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(seconds=settings.draft_pr_execution_timeout_seconds),
    )
    tracker = asyncio.create_task(_track_phases(handle))
    try:
        result = await handle.result()
    finally:
        tracker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tracker

    print(f"\n[demo] workflow status: {result.status}")
    succeeded = {DRAFT_PR_OPEN, EXISTING_PR_REUSED, PR_PROMOTED, PR_MERGED}
    if result.status not in succeeded:
        print(f"[demo] stopped at terminal reason: {result.terminal_reason}")
        return 1
    if result.status == EXISTING_PR_REUSED:
        print(
            "[demo] this task already has an open pull request, so the platform reuses it "
            "instead of creating a duplicate"
        )
    print(f"  branch                {result.branch}")
    print(f"  pr number             {result.pr_number}")
    print(f"  pr url                {result.pr_url}")
    print(f"  jira linkback         {result.linkback}")
    if result.status in (PR_PROMOTED, PR_MERGED):
        print(f"  ci status             {result.ci_status}")
        print(f"  review status         {result.review_status}")
        print(f"  policy status         {result.policy_status}")
        print(f"  approval              {result.approval_status} by {result.approver}")
        print(f"  promoted              {result.promoted}")
    print("\nDRAFT PR DEMO: PASS")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("DEMO_REPO", "agentic-demo-service"),
        help="Allowlisted repo to open the draft PR against.",
    )
    parser.add_argument("--base", default=os.environ.get("DEMO_BASE", ""))
    parser.add_argument("--task", default=os.environ.get("DEMO_TASK", "AGENTDEV-1"))
    parser.add_argument("--live-agent", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--via-temporal", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--litellm-url", default=os.environ.get("DEMO_LITELLM_URL", ""))
    parser.add_argument(
        "--full-pipeline",
        action="store_true",
        help="Run CI, reviews, scan, policy, and the approval pause on Temporal (via-temporal).",
    )
    parser.add_argument(
        "--ci-job",
        default=os.environ.get("DEMO_CI_JOB", ""),
        help="CI job for the full pipeline. Defaults to <repo>-ci.",
    )
    parser.add_argument(
        "--await-pr-close",
        action="store_true",
        help="After promotion, park for the PR-closed webhook and finish on close or merge.",
    )
    return parser.parse_args()


async def main() -> int:
    settings = get_settings()
    missing = _missing(settings)
    if missing:
        print("Set these before running the demo (point them at github.com):")
        print("  GITHUB_API_URL=https://api.github.com")
        print("  GITHUB_GIT_BASE_URL=https://github.com")
        print("  GITHUB_OWNER=<your github user or org>")
        print("  GITHUB_TOKEN=<PAT with contents:write and pull-requests:write>")
        print(f"Missing: {', '.join(missing)}")
        return 2

    args = _parse_args()
    repo = args.repo
    base = args.base or settings.github_default_base_branch or "main"
    task_id = args.task
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"

    if args.live_agent or args.via_temporal:
        ok, detail = bedrock_preflight(settings)
        print(f"[demo] preflight inference path: [{'ok' if ok else 'FAIL'}] {detail}")
        if not ok:
            print("[demo] the live agent needs the Bedrock path; aborting before any work.")
            print("[demo] start it with `inv tunnel-start --detach`, confirm with")
            print("[demo]   `inv connectivity-check`, then retry.")
            return 2

    allowlist = sorted(settings.allowed_repos())
    if repo not in allowlist:
        print(f"Repo '{repo}' is not allowlisted. Choose one of: {', '.join(allowlist)}")
        return 2

    print(f"[demo] platform allowlist ({len(allowlist)} repos): {', '.join(allowlist)}")
    print(f"[demo] repo={settings.github_owner}/{repo} base={base} execution={execution_id}")

    def remote_url_for(name: str) -> str:
        return _authed_remote(settings, name)

    base_sha = _remote_head(remote_url_for(repo), base)
    if not base_sha:
        print(f"Could not read {base} on the remote. Confirm the repo exists and the token works.")
        return 2

    host = RealGitHost(
        build_single_identity_resolver(
            api_url=settings.github_api_url,
            owner=settings.github_owner,
            git_base_url=settings.github_git_base_url,
            token=settings.github_token,
            repos=set(allowlist),
        ),
        remote_url_for=remote_url_for,
    )
    ledger = InMemorySideEffectLedger()
    gate = InMemorySequenceGate()
    mediator = GitHubMediator(
        backend=host, ledger=ledger, gate=gate, allowed_repos=frozenset(allowlist)
    )
    materializer = WorkspaceMaterializer.from_settings(
        InMemoryOrphanRegistry(), remote_url_for=remote_url_for, settings=settings
    )
    synchronizer = GitBranchSynchronizer.from_settings(
        remote_url_for=remote_url_for, settings=settings
    )
    existing_pr = await mediator.find_open_pr(repo, task_id)
    if _jira_configured(settings):
        ensure_acli_authenticated(settings)
        adapter = _demo_jira_adapter(settings, task_id)
        ok, detail = _jira_task_actionable(adapter, task_id, settings.jira_parked_status)
        print(f"[demo] jira preflight {task_id}: [{'ok' if ok else 'FAIL'}] {detail}")
        if not ok:
            print("[demo] pick a real, not-done ticket with --task, then retry.")
            return 2
        if existing_pr is not None:
            print(f"[demo] {task_id} already has an open pull request, leaving Jira status as is")
        raw_issue = adapter.get_issue(task_id)
        summary = (raw_issue.get("fields") or {}).get("summary") or "Bounded agent change"
        description = normalize_issue(raw_issue).description or ""
        reader = adapter
        linkback_client = adapter
        jira_source = adapter.source
        print(f"[demo] jira live: read {task_id} ({summary})")
    else:
        raw_issue = _issue(task_id)
        summary = raw_issue["fields"].get("summary") or "Demonstrate the automated draft PR"
        description = raw_issue["fields"].get("description") or ""
        reader = _FakeReader(raw_issue)
        linkback_client = _FakeLinkbackClient(DATACENTER)
        jira_source = DATACENTER
        print("[demo] jira simulated (no Cloud credentials set); using a local stand-in issue")

    if args.via_temporal:
        rc = await _run_via_temporal(
            settings,
            repo=repo,
            base=base,
            base_sha=base_sha,
            task_id=task_id,
            summary=summary,
            raw_issue=dict(raw_issue),
            full_pipeline=args.full_pipeline,
            ci_job=args.ci_job or (f"{repo}-ci" if args.full_pipeline else ""),
            await_pr_close=args.await_pr_close,
        )
        return rc

    if args.live_agent:
        code_step = build_openhands_code_step(
            settings=settings,
            litellm_base_url=args.litellm_url or None,
            workspace_root=settings.execution_workspace_root,
            context=description,
        )
        print("[demo] live agent: OpenHands authors the change in a sandbox pod")
    else:
        code_step = _DemoCodeStep(execution_id)

    demo_flow = _demo_status_flow(settings, task_id) if _jira_configured(settings) else StatusFlow()
    deps = PipelineDeps(
        snapshot_store=_SnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=materializer,
        code_step=code_step,
        synchronizer=synchronizer,
        mediator=mediator,
        jira_reader=reader,
        linkback_client=linkback_client,
        settings=settings,
        status_flow=demo_flow,
    )
    request = PipelineRequest(
        task_id=task_id,
        execution_id=execution_id,
        repo=repo,
        base=base,
        base_sha=base_sha,
        task=summary,
        issue_key=task_id,
        raw_issue=raw_issue,
        jira_source=jira_source,
        commit_message="Add automated draft demo marker",
    )

    print("[demo] running the bounded task through intake, claim, materialize, sync, and PR...")

    def _progress(stage: str) -> None:
        print(f"[demo]   -> {stage}")

    result = await run_bounded_task(deps, request, on_progress=_progress)

    print(f"\n[demo] pipeline status: {result.status}")
    if result.status not in (DRAFT_PR_OPEN, EXISTING_PR_REUSED):
        print(f"[demo] stopped before the PR at terminal reason: {result.terminal_reason}")
        return 1

    if result.status == EXISTING_PR_REUSED:
        print("[demo] existing draft PR detected for this task; reused instead of duplicating")

    branch = agent_branch_name(task_id)
    print(f"  branch                {branch}")
    print(f"  draft pr number       {result.pr_number}")
    print(f"  draft pr url          {result.pr_url}")
    print(f"  jira linkback         {result.linkback}")
    print(f"  gated sequence        {result.sequence_used}")

    print("\n[demo] replaying the PR open to prove idempotency (no duplicate PR)...")
    replay = await mediator.open_draft_pr(
        repo, task_id, execution_id, branch, base, "ignored", "ignored", sequence=3
    )
    print(f"  replay pr number      {replay.number}")

    again = post_pr_linkback(
        linkback_client,
        task_id,
        execution_id=execution_id,
        pr_url=result.pr_url,
        pr_title="ignored",
        repo=repo,
        branch=branch,
    )
    print(f"  replay linkback       {again.action}")
    print("\nDRAFT PR DEMO: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
