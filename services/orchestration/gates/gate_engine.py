"""Drive an external review engine as a scanner-family gate backend."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    SARIF,
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
                detail=str(
                    comment.get("detail")
                    or comment.get("description")
                    or comment.get("body")
                    or ""
                ),
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


def _engine_env(
    *,
    config: GateEngineConfig,
    profile: str,
    gateway_base_url: str,
    gateway_api_key: str,
    execution_id: str = "",
    generation_name: str = "",
    model: str = "",
) -> dict[str, str]:
    """The environment the engine harness reads, shared by the subprocess and pod paths.

    The compliance profile is passed as ``AI_REVIEW_PROFILE`` because the harness reads it from
    the environment, not a flag. Model routing env is added only for a model-driven engine, so a
    SARIF tool needs no gateway or model configuration. When an execution id is present, the
    ``langfuse_`` join headers are injected through the CLI's ``ANTHROPIC_CUSTOM_HEADERS`` so the
    engine's model calls group under the execution and its stage in Langfuse, the same headers the
    platform stamps on its own calls to the gateway.
    """
    env = {"AI_REVIEW_SCM": "github"}
    if profile:
        env["AI_REVIEW_PROFILE"] = profile
    if config.needs_model:
        env.update(
            {
                "AI_REVIEW_PROVIDER": "api",
                "AI_REVIEW_TOOL": config.tool,
                "AI_REVIEW_MODEL": model or config.review_model,
                "ANTHROPIC_BASE_URL": gateway_base_url,
                "OPENAI_BASE_URL": gateway_base_url,
                "ANTHROPIC_API_KEY": gateway_api_key,
                "OPENAI_API_KEY": gateway_api_key,
            }
        )
        if config.tool == "claude" and config.max_output_tokens > 0:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(config.max_output_tokens)
        if execution_id:
            headers = [f"langfuse_session_id: {execution_id}"]
            if generation_name:
                headers.append(f"langfuse_generation_name: {generation_name}")
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
    return env


async def _run_engine_json(
    *,
    name: str,
    entrypoint: Path,
    repo_root: str,
    base_ref: str,
    env: dict[str, str],
    gateway_base_url: str,
    model: str,
    runner: EngineRunner,
    marker: str,
    needs_model: bool = True,
) -> dict:
    if not entrypoint.is_file():
        raise GateEngineError(f"engine entrypoint missing at {entrypoint}")
    if needs_model and not gateway_base_url:
        raise GateEngineError(
            f"engine {name!r} has no LiteLLM gateway base URL; refusing to run so no model "
            "call bypasses the platform gateway"
        )
    if needs_model and not model:
        raise GateEngineError(
            f"engine {name!r} has no model configured; a LiteLLM model name is required so the "
            "gateway routes the call to Bedrock"
        )

    json_out = Path(repo_root) / f".ai-{marker}-{uuid.uuid4().hex}.json"
    args = [str(entrypoint), "--against", base_ref, "--json-out", str(json_out)]
    try:
        result = await runner(args, repo_root, env)
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
        pod_runner: Any | None = None,
        tls_verify: bool = True,
        review_model: str = "",
    ) -> None:
        self._name = name
        self._config = config
        self._profile = profile
        self._litellm_base_url = litellm_base_url
        self._litellm_api_key = litellm_api_key
        self._runner = runner or _default_runner
        self._source = source
        self._pod_runner = pod_runner
        self._tls_verify = tls_verify
        # The model actually sent to the gateway, resolved by the caller to a client-recognized id.
        # Falls back to the config value when the caller does not resolve.
        self._review_model = review_model or config.review_model

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
        env = _engine_env(
            config=self._config,
            profile=self._profile,
            gateway_base_url=self._litellm_base_url,
            gateway_api_key=self._litellm_api_key,
            execution_id=target.execution_id,
            generation_name=self._name,
            model=self._review_model,
        )
        against = target.base_ref or target.ref
        if self._pod_runner is not None:
            document = await self._run_in_pod(source, target, against, env)
        else:
            document = await _run_engine_json(
                name=self._name,
                entrypoint=Path(source.path).joinpath(
                    *self._config.review_entrypoint.split("/")
                ),
                repo_root=target.repo_root,
                base_ref=against,
                env=env,
                gateway_base_url=self._litellm_base_url,
                model=self._review_model,
                runner=self._runner,
                marker="review",
                needs_model=self._config.needs_model,
            )
        if self._config.output_format == SARIF:
            from services.orchestration.gates.sarif import parse_sarif

            return parse_sarif(document, scanner=self._name)
        return map_review_findings(document, scanner=self._name)

    async def _run_in_pod(
        self, source: ResolvedEngineSource, target: ScanTarget, against: str, env: dict[str, str]
    ) -> dict:
        from adapters.interfaces.security_scanner import ScannerUnavailableError

        from services.orchestration.gates.engine_source import IMAGE
        from services.orchestration.gates.pod_runner import PodEngineError, PodEngineSpec

        runner = self._pod_runner
        if runner is None:
            raise ScannerUnavailableError("pod runner is not configured")

        # config.image, when set, is a runtime override: the pod pulls it instead of building the
        # engine Dockerfile, while the source is still mounted at the engine path so the harness
        # stays current. In pure image mode the source path is empty and the image is the whole
        # tool. In local mode with an image set, the pull provides the runtime and the mount
        # provides the source.
        spec = PodEngineSpec(
            name=self._name,
            execution_id=target.execution_id,
            checkout_dir=target.repo_root,
            entrypoint=self._config.review_entrypoint,
            base_ref=against,
            source_path="" if source.mode == IMAGE else source.path,
            image=self._config.image,
            tls_verify=self._tls_verify,
            env=env,
        )
        try:
            return await runner.run_engine(spec)
        except PodEngineError as exc:
            raise ScannerUnavailableError(str(exc)) from exc


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
        env = _engine_env(
            config=self._config,
            profile="",
            gateway_base_url=self._litellm_base_url,
            gateway_api_key=self._litellm_api_key,
            execution_id=target.execution_id,
            generation_name=self._name,
        )
        payload = await _run_engine_json(
            name=self._name,
            entrypoint=Path(source.path).joinpath(*self._config.classifier_entrypoint.split("/")),
            repo_root=target.repo_root,
            base_ref=target.base_ref or target.ref,
            env=env,
            gateway_base_url=self._litellm_base_url,
            model=self._config.triage_model,
            runner=self._runner,
            marker="classify",
        )
        return map_classifications(payload)
