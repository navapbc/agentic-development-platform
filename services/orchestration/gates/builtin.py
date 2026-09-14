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
                repo=context.repo,
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
                "prompt_tokens": outcome.prompt_tokens,
                "completion_tokens": outcome.completion_tokens,
                "cache_read_tokens": outcome.cache_read_tokens,
                "cache_write_tokens": outcome.cache_write_tokens,
                "total_tokens": outcome.total_tokens,
                "llm_calls": outcome.llm_calls,
                "cost_usd": outcome.cost_usd,
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


def _resolve_gate_model(config: Any, settings: Any) -> str:
    """Resolve the gate's requested model to a client-recognized id that the gateway can route.

    Reads the available canonical-id model groups from the gateway config and maps the gate's
    ``review_model`` onto one. An exact or same-version match returns that model. A within-major
    substitution honours the gate's ``on_unavailable`` policy of warn or abort. When the catalog
    cannot be read or the model needs no resolution, the requested value passes through.
    """
    import logging

    from services.integration.credentials import ConfigError, load_env_config
    from services.orchestration.gates.model_resolver import (
        available_gate_model_groups,
        resolve_from_available,
    )

    requested = getattr(config, "review_model", "")
    if not requested or not getattr(config, "needs_model", False):
        return requested
    try:
        litellm_config = load_env_config("litellm-config", environment=settings.environment)
    except ConfigError:
        return requested
    groups = available_gate_model_groups(litellm_config)
    if not groups:
        return requested
    resolution = resolve_from_available(
        requested, groups, on_unavailable=getattr(config, "on_unavailable", "abort")
    )
    if resolution.substituted:
        logging.getLogger("gates").warning(resolution.note)
    return resolution.model_id


def engine_review_backend(config: Any, profile: str = "") -> Any:
    from scripts.config import get_settings

    from services.orchestration.gates.gate_engine import AiReviewScanner

    settings = get_settings()
    pod_runner = None
    litellm_base_url = settings.litellm_base_url
    if settings.gate_engine_execution == "pod":
        from services.orchestration.gates.pod_runner import KubectlPodOps, PodEngineRunner

        pod_runner = PodEngineRunner(
            KubectlPodOps(),
            namespace=settings.gate_engine_namespace,
            builder_image=settings.gate_engine_builder_image,
            privileged=settings.gate_engine_privileged,
        )
        litellm_base_url = settings.sandbox_litellm_base_url
    return AiReviewScanner(
        name=config.name,
        config=config,
        profile=profile,
        litellm_base_url=litellm_base_url,
        litellm_api_key=settings.litellm_master_key,
        pod_runner=pod_runner,
        tls_verify=settings.gate_registry_tls_verify,
        review_model=_resolve_gate_model(config, settings),
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
