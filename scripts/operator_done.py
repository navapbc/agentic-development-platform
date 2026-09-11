"""Operator done command for the local source-control backend.

Completes a run that targets the local git-folder backend, which has no remote
pull request and no webhook. The command authenticates an authorized operator,
verifies the promoted branch and artifact identifiers, emits an audit event, is
idempotent on repeat, and sends the provider-neutral completion signal that the
draft-PR workflow consumes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from adapters.interfaces.git_host import GitHost, PublishResult

COMPLETION_SIGNAL = "submit_completion"


class OperatorAuthorizer(Protocol):
    def is_authorized(self, actor: str) -> bool: ...


class AuditSink(Protocol):
    def record(self, event: dict[str, object]) -> None: ...


class DoneLedger(Protocol):
    def seen(self, key: str) -> bool: ...

    def mark(self, key: str) -> None: ...


CompletionSignaler = Callable[[str, bool], Awaitable[None]]


@dataclass(frozen=True)
class OperatorDoneResult:
    authorized: bool
    verified: bool
    signaled: bool
    replayed: bool
    reason: str
    completion_signal_id: str = ""


@dataclass(frozen=True)
class AllowlistAuthorizer:
    operators: frozenset[str]

    def is_authorized(self, actor: str) -> bool:
        return bool(actor) and actor in self.operators


async def run_operator_done(
    *,
    workflow_id: str,
    repo: str,
    branch: str,
    actor: str,
    host: GitHost,
    authorizer: OperatorAuthorizer,
    audit: AuditSink,
    signaler: CompletionSignaler,
    ledger: DoneLedger,
    merged: bool = True,
    expected_completion_signal_id: str = "",
) -> OperatorDoneResult:
    audit.record(
        {"event": "operator_done_invoked", "actor": actor, "workflow_id": workflow_id, "repo": repo}
    )
    if not authorizer.is_authorized(actor):
        audit.record({"event": "operator_done_denied", "actor": actor, "workflow_id": workflow_id})
        return OperatorDoneResult(
            authorized=False, verified=False, signaled=False, replayed=False, reason="unauthorized"
        )

    publish: PublishResult | None = await host.find_published_branch(repo, branch)
    if publish is None:
        audit.record(
            {"event": "operator_done_no_publish", "workflow_id": workflow_id, "branch": branch}
        )
        return OperatorDoneResult(
            authorized=True, verified=False, signaled=False, replayed=False, reason="no_publish"
        )
    mismatch = (
        expected_completion_signal_id
        and publish.completion_signal_id != expected_completion_signal_id
    )
    if mismatch:
        audit.record(
            {
                "event": "operator_done_identifier_mismatch",
                "workflow_id": workflow_id,
                "expected": expected_completion_signal_id,
                "actual": publish.completion_signal_id,
            }
        )
        return OperatorDoneResult(
            authorized=True,
            verified=False,
            signaled=False,
            replayed=False,
            reason="identifier_mismatch",
            completion_signal_id=publish.completion_signal_id,
        )

    key = f"{workflow_id}:{branch}:{publish.completion_signal_id}"
    if ledger.seen(key):
        audit.record({"event": "operator_done_replayed", "workflow_id": workflow_id})
        return OperatorDoneResult(
            authorized=True,
            verified=True,
            signaled=False,
            replayed=True,
            reason="idempotent_replay",
            completion_signal_id=publish.completion_signal_id,
        )

    audit.record(
        {
            "event": "operator_done_authorized",
            "actor": actor,
            "workflow_id": workflow_id,
            "branch": branch,
            "completion_signal_id": publish.completion_signal_id,
        }
    )
    await signaler(workflow_id, merged)
    ledger.mark(key)
    audit.record(
        {"event": "operator_done_signaled", "workflow_id": workflow_id, "merged": merged}
    )
    return OperatorDoneResult(
        authorized=True,
        verified=True,
        signaled=True,
        replayed=False,
        reason="signaled",
        completion_signal_id=publish.completion_signal_id,
    )


class StructlogAuditSink:
    def __init__(self) -> None:
        import structlog

        self._log = structlog.get_logger("operator_done")

    def record(self, event: dict[str, object]) -> None:
        payload = dict(event)
        name = str(payload.pop("event", "operator_done"))
        self._log.info(name, **payload)


class FileDoneLedger:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> set[str]:
        if not self._path.exists():
            return set()
        return set(json.loads(self._path.read_text(encoding="utf-8")))

    def seen(self, key: str) -> bool:
        return key in self._load()

    def mark(self, key: str) -> None:
        keys = self._load()
        keys.add(key)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from services.integration.github.factory import build_git_host
    from temporalio.client import Client

    from scripts.config import get_settings, to_host_hostport
    from scripts.operator_identity import local_operator

    parser = argparse.ArgumentParser(description="Complete a local-backend run.")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--completion-signal-id", default="")
    parser.add_argument(
        "--closed-unmerged",
        action="store_true",
        help="Complete as closed-unmerged rather than merged.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    operators = frozenset(
        name.strip() for name in settings.source_control_local_operators.split(",") if name.strip()
    )
    actor = local_operator()
    host = build_git_host(settings)
    artifacts_root = settings.source_control_local_artifacts_root or (
        f"{settings.source_control_local_root}/.agent-publish"
    )
    ledger = FileDoneLedger(Path(artifacts_root) / ".operator-done.json")

    async def _signaler(workflow_id: str, merged: bool) -> None:
        address = to_host_hostport(settings.temporal_host)
        client = await Client.connect(address, namespace=settings.temporal_namespace)
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(COMPLETION_SIGNAL, args=[merged, actor])

    result = asyncio.run(
        run_operator_done(
            workflow_id=args.workflow_id,
            repo=args.repo,
            branch=args.branch,
            actor=actor,
            host=host,
            authorizer=AllowlistAuthorizer(operators),
            audit=StructlogAuditSink(),
            signaler=_signaler,
            ledger=ledger,
            merged=not args.closed_unmerged,
            expected_completion_signal_id=args.completion_signal_id,
        )
    )
    print(f"[operator-done] {result.reason} (signaled={result.signaled})")
    return 0 if result.authorized and (result.signaled or result.replayed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
