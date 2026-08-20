"""Built-in review and scanner gates that satisfy the gate contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from services.orchestration.gates.contract import (
    BLOCK,
    PASS,
    REVIEW,
    SCANNER,
    Gate,
    GateContext,
    GateFinding,
    GateResult,
    gate_from_callable,
)

ReviewFn = Callable[[Any], Awaitable[Any]]
ScannerFactory = Callable[[], Any]


def review_gate(name: str, review_fn: ReviewFn) -> Gate:
    async def evaluate(context: GateContext) -> GateResult:
        from services.orchestration.temporal_activities.review import ReviewRequest

        outcome = await review_fn(
            ReviewRequest(
                task_id=context.task_id,
                execution_id=context.execution_id,
                subject=context.subject,
                context_ref=context.ref,
                context_summary=context.context_summary,
                repo_root=context.repo_root or None,
                ref=context.ref or None,
            )
        )
        findings = [GateFinding(severity=BLOCK, title=title) for title in outcome.findings]
        return GateResult(
            name=name,
            kind=REVIEW,
            verdict=BLOCK if outcome.verdict == BLOCK else PASS,
            findings=findings,
            summary=outcome.summary,
            attestation_hash=outcome.attestation_hash,
            metadata={
                "model_id": outcome.model_id,
                "prompt_version": outcome.prompt_version,
            },
        )

    return gate_from_callable(name, REVIEW, evaluate)


def scanner_gate(name: str, scanner_factory: ScannerFactory) -> Gate:
    async def evaluate(context: GateContext) -> GateResult:
        from adapters.interfaces.security_scanner import (
            DEFAULT_BLOCKING_THRESHOLD,
            ScanTarget,
            is_blocking,
        )

        scanner = scanner_factory()
        raw = await scanner.scan(
            ScanTarget(repo=context.repo, ref=context.ref, repo_root=context.repo_root)
        )
        threshold = str(context.params.get("threshold", DEFAULT_BLOCKING_THRESHOLD))
        findings = [
            GateFinding(
                severity=finding.severity,
                title=finding.title,
                detail=finding.detail,
                location=finding.location,
                rule_id=finding.rule_id,
            )
            for finding in raw
        ]
        blocking = any(is_blocking(finding.severity, threshold=threshold) for finding in raw)
        return GateResult(
            name=name,
            kind=SCANNER,
            verdict=BLOCK if blocking else PASS,
            findings=findings,
            summary=f"{len(findings)} findings over {len(raw)} scanned",
        )

    return gate_from_callable(name, SCANNER, evaluate)


def builtin_scanner_backends() -> dict[str, ScannerFactory]:
    from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner

    return {
        "semgrep": RealSemgrepScanner.from_settings,
        "trivy": RealTrivyScanner.from_settings,
    }


def engine_review_backend(config: Any, profile: str = "") -> Any:
    from scripts.config import get_settings

    from services.orchestration.gates.gate_engine import AiReviewScanner

    settings = get_settings()
    return AiReviewScanner(
        name=config.name,
        config=config,
        profile=profile,
        litellm_base_url=settings.litellm_base_url,
        litellm_api_key=settings.litellm_master_key,
    )


def engine_gate(config: Any) -> Gate:
    return scanner_gate(config.name, lambda: engine_review_backend(config))


def builtin_gates() -> dict[str, Callable[[], Gate]]:
    from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner

    from services.orchestration.temporal_activities.review import (
        review_code,
        review_plan,
        review_security,
        verify_acceptance,
    )

    return {
        "plan-review": lambda: review_gate("plan-review", review_plan),
        "code-review": lambda: review_gate("code-review", review_code),
        "security-review": lambda: review_gate("security-review", review_security),
        "acceptance": lambda: review_gate("acceptance", verify_acceptance),
        "semgrep": lambda: scanner_gate("semgrep", RealSemgrepScanner.from_settings),
        "trivy": lambda: scanner_gate("trivy", RealTrivyScanner.from_settings),
    }
