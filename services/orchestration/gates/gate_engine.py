"""Drive an external review engine as a scanner-family gate backend."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from adapters.interfaces.security_scanner import (
    CRITICAL,
    HIGH,
    LOW,
    MEDIUM,
    UNKNOWN,
    ScanFinding,
    ScanTarget,
)

from services.orchestration.gates.engine_source import (
    CommandResult,
    GateEngineConfig,
    ResolvedEngineSource,
    resolve_engine_source,
)

_SEVERITY = {"CRITICAL": CRITICAL, "HIGH": HIGH, "MEDIUM": MEDIUM, "LOW": LOW}
_KIND = "ai-review"

EngineRunner = Callable[[Sequence[str], str, dict[str, str]], Awaitable[CommandResult]]


class GateEngineError(Exception):
    """The AI-CLI engine invocation failed, so the gate fails closed."""


def map_review_findings(review: dict, *, scanner: str) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for comment in review.get("comments", []):
        if not isinstance(comment, dict):
            continue
        severity = _SEVERITY.get(str(comment.get("severity", "")).upper(), UNKNOWN)
        path = str(comment.get("path", ""))
        line = comment.get("line", "")
        location = f"{path}:{line}" if path else ""
        findings.append(
            ScanFinding(
                finding_id=uuid.uuid4().hex,
                scanner=scanner,
                kind=_KIND,
                severity=severity,
                title=str(comment.get("title") or comment.get("message") or "AI review finding"),
                detail=str(comment.get("detail") or comment.get("body") or ""),
                location=location,
                rule_id=str(comment.get("perspective") or comment.get("category") or ""),
            )
        )
    return findings


async def _default_runner(args: Sequence[str], cwd: str, env: dict[str, str]) -> CommandResult:
    import asyncio
    import os

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd or None,
        env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return CommandResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


async def _run_engine_json(
    *,
    name: str,
    entrypoint: Path,
    target: ScanTarget,
    extra_args: list[str],
    gateway_base_url: str,
    gateway_api_key: str,
    tool: str,
    model: str,
    max_output_tokens: int,
    runner: EngineRunner,
    marker: str,
) -> dict:
    if not entrypoint.is_file():
        raise GateEngineError(f"engine entrypoint missing at {entrypoint}")
    if not gateway_base_url:
        raise GateEngineError(
            f"engine {name!r} has no LiteLLM gateway base URL; refusing to run so no model "
            "call bypasses the platform gateway"
        )
    if not model:
        raise GateEngineError(
            f"engine {name!r} has no model configured; a LiteLLM model name is required so the "
            "gateway routes the call to Bedrock"
        )

    json_out = Path(target.repo_root) / f".ai-{marker}-{uuid.uuid4().hex}.json"
    args = [str(entrypoint), "--against", target.ref, "--json-out", str(json_out), *extra_args]
    env = {
        "AI_REVIEW_SCM": "github",
        "AI_REVIEW_PROVIDER": "api",
        "AI_REVIEW_TOOL": tool,
        "AI_REVIEW_MODEL": model,
        "ANTHROPIC_BASE_URL": gateway_base_url,
        "OPENAI_BASE_URL": gateway_base_url,
        "ANTHROPIC_API_KEY": gateway_api_key,
        "OPENAI_API_KEY": gateway_api_key,
    }
    if tool == "claude" and max_output_tokens > 0:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)

    try:
        result = await runner(args, target.repo_root, env)
        if result.returncode not in (0, 1):
            raise GateEngineError(
                f"engine {name!r} exited {result.returncode}: {result.stderr.strip()[:400]}"
            )
        if not json_out.is_file():
            raise GateEngineError(f"engine {name!r} produced no JSON output")
        return json.loads(json_out.read_text(encoding="utf-8"))
    finally:
        json_out.unlink(missing_ok=True)


class AiReviewScanner:
    def __init__(
        self,
        *,
        name: str,
        config: GateEngineConfig,
        profile: str = "",
        litellm_base_url: str = "",
        litellm_api_key: str = "",
        runner: EngineRunner | None = None,
        source: ResolvedEngineSource | None = None,
    ) -> None:
        self._name = name
        self._config = config
        self._profile = profile
        self._litellm_base_url = litellm_base_url
        self._litellm_api_key = litellm_api_key
        self._runner = runner or _default_runner
        self._source = source

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self) -> GateEngineConfig:
        return self._config

    async def _resolve(self) -> ResolvedEngineSource:
        if self._source is not None:
            return self._source
        resolved = await resolve_engine_source(self._config)
        if resolved is None:
            raise GateEngineError(f"gate {self._name!r} engine is not enabled")
        return resolved

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        source = await self._resolve()
        extra = ["--profile", self._profile] if self._profile else []
        review = await _run_engine_json(
            name=self._name,
            entrypoint=Path(source.path).joinpath(*self._config.review_entrypoint.split("/")),
            target=target,
            extra_args=extra,
            gateway_base_url=self._litellm_base_url,
            gateway_api_key=self._litellm_api_key,
            tool=self._config.tool,
            model=self._config.review_model,
            max_output_tokens=self._config.max_output_tokens,
            runner=self._runner,
            marker="review",
        )
        return map_review_findings(review, scanner=self._name)


APPLICATION_BUG = "APPLICATION_BUG"
TEST_BUG = "TEST_BUG"
FLAKY_FAILURE = "FLAKY_FAILURE"
ENVIRONMENT_ISSUE = "ENVIRONMENT_ISSUE"
_TEST_VERDICTS = frozenset({APPLICATION_BUG, TEST_BUG, FLAKY_FAILURE, ENVIRONMENT_ISSUE})


@dataclass(frozen=True)
class FailureClassification:
    test: str
    path: str
    line: str
    verdict: str
    category: str
    confidence: str
    in_scope: bool
    rationale: str


@dataclass(frozen=True)
class FailureTriage:
    mode: str
    summary: str
    classifications: list[FailureClassification]
    should_patch: bool
    flaky_only: bool
    has_environment_issue: bool


_PATCHABLE = frozenset({APPLICATION_BUG, TEST_BUG})

PATCH = "patch"
RERUN = "rerun"
ESCALATE = "escalate"


def triage_action(triage: FailureTriage) -> str:
    if triage.should_patch:
        return PATCH
    if triage.has_environment_issue:
        return ESCALATE
    if triage.flaky_only:
        return RERUN
    return ESCALATE


def map_classifications(payload: dict) -> FailureTriage:
    classifications: list[FailureClassification] = []
    for entry in payload.get("classifications", []):
        if not isinstance(entry, dict):
            continue
        verdict = str(entry.get("verdict", "")).upper()
        classifications.append(
            FailureClassification(
                test=str(entry.get("test", "")),
                path=str(entry.get("path", "")),
                line=str(entry.get("line", "")),
                verdict=verdict if verdict in _TEST_VERDICTS else "",
                category=str(entry.get("category", "")),
                confidence=str(entry.get("confidence", "")),
                in_scope=bool(entry.get("in_scope", False)),
                rationale=str(entry.get("rationale", "")),
            )
        )
    should_patch = any(item.verdict in _PATCHABLE and item.in_scope for item in classifications)
    has_environment_issue = any(
        item.verdict == ENVIRONMENT_ISSUE for item in classifications
    )
    non_patchable = [item for item in classifications if item.verdict not in _PATCHABLE]
    flaky_only = (
        not should_patch
        and bool(non_patchable)
        and all(item.verdict == FLAKY_FAILURE for item in non_patchable)
    )
    return FailureTriage(
        mode=str(payload.get("mode", "")),
        summary=str(payload.get("summary", "")),
        classifications=classifications,
        should_patch=should_patch,
        flaky_only=flaky_only,
        has_environment_issue=has_environment_issue,
    )


class AiTestClassifier:
    def __init__(
        self,
        *,
        name: str,
        config: GateEngineConfig,
        litellm_base_url: str = "",
        litellm_api_key: str = "",
        runner: EngineRunner | None = None,
        source: ResolvedEngineSource | None = None,
    ) -> None:
        self._name = name
        self._config = config
        self._litellm_base_url = litellm_base_url
        self._litellm_api_key = litellm_api_key
        self._runner = runner or _default_runner
        self._source = source

    @property
    def name(self) -> str:
        return self._name

    async def _resolve(self) -> ResolvedEngineSource:
        if self._source is not None:
            return self._source
        resolved = await resolve_engine_source(self._config)
        if resolved is None:
            raise GateEngineError(f"gate {self._name!r} engine is not enabled")
        return resolved

    async def classify(self, target: ScanTarget) -> FailureTriage:
        source = await self._resolve()
        payload = await _run_engine_json(
            name=self._name,
            entrypoint=Path(source.path).joinpath(*self._config.classifier_entrypoint.split("/")),
            target=target,
            extra_args=[],
            gateway_base_url=self._litellm_base_url,
            gateway_api_key=self._litellm_api_key,
            tool=self._config.tool,
            model=self._config.triage_model,
            max_output_tokens=self._config.max_output_tokens,
            runner=self._runner,
            marker="classify",
        )
        return map_classifications(payload)
