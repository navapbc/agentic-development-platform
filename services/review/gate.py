"""The platform review gate: run the kernel, attest the verdict, persist evidence."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from scripts.config import Settings, get_settings

from services.review.attestation import Attestor, HmacAttestor, ReviewAttestationError
from services.review.classification import (
    CLASSIFICATION_SECURITY,
    CLASSIFICATION_STANDARD,
)
from services.review.evidence import EvidenceRecord, EvidenceSink
from services.review.kernel import ReviewKernel
from services.review.types import RawReview, ReviewInput, ReviewResult, ReviewStage

SOURCE_COMPONENT = "review-kernel"

SIGNED_STAGES = frozenset(ReviewStage)


def _classification_for_stage(stage: ReviewStage) -> str:
    if stage is ReviewStage.SECURITY_REVIEW:
        return CLASSIFICATION_SECURITY
    return CLASSIFICATION_STANDARD


class ReviewSigningNotConfigured(ReviewAttestationError):
    """Signing is enabled for a signed stage but the key or trust root is missing."""


def select_attestor(stage: ReviewStage, settings: Settings | None = None) -> Attestor:
    """Select the verdict attestor for a review stage.

    Signing is on by default and covers every review stage. A signed stage fails
    closed when the signing key or trust root is not configured, so a verdict never
    silently downgrades to the symmetric secret. Every stage uses the HMAC attestor
    only when signing is disabled.
    """
    resolved = settings or get_settings()
    if resolved.review_signing_enabled and stage in SIGNED_STAGES:
        if not resolved.review_signing_key_path or not resolved.review_trust_root_path:
            raise ReviewSigningNotConfigured(
                "review signing is enabled but review_signing_key_path or "
                f"review_trust_root_path is not set for stage {stage.value}"
            )
        from services.review.attestation import SshsigAttestor
        from services.review.trust_root import load_trust_root

        trust_root = load_trust_root(resolved.review_trust_root_path)
        return SshsigAttestor(
            key_path=resolved.review_signing_key_path,
            principal=trust_root.principal,
            trust_root=trust_root.allowed_signers(),
        )
    return HmacAttestor(resolved.review_attestation_secret)


def verdict_payload(review: ReviewInput, raw: RawReview) -> dict[str, Any]:
    return {
        "stage": review.stage.value,
        "subject_ref": review.subject_ref,
        "context_ref": review.context_ref,
        "prompt_name": review.prompt_name,
        "prompt_version": review.prompt_version,
        "verdict": raw.verdict.value,
        "summary": raw.summary,
        "findings": [
            {
                "finding_id": f.finding_id,
                "severity": f.severity,
                "blocking": f.blocking,
                "title": f.title,
                "detail": f.detail,
                "location": f.location,
            }
            for f in raw.findings
        ],
        "coaching": list(raw.coaching),
        "provenance": [
            {"leg": p.leg, "tier": p.tier, "model_id": p.model_id} for p in raw.provenance
        ],
    }


class ReviewGate:
    def __init__(
        self,
        kernel: ReviewKernel,
        sink: EvidenceSink,
        attestor: Attestor | None = None,
        *,
        retention: str | None = None,
    ) -> None:
        self._kernel = kernel
        self._sink = sink
        self._attestor = attestor or HmacAttestor(get_settings().review_attestation_secret)
        self._retention = retention

    async def review(self, review: ReviewInput, *, execution_id: str) -> ReviewResult:
        raw = await asyncio.to_thread(self._kernel.run, review)
        payload = verdict_payload(review, raw)
        attestation = self._attestor.sign(payload)
        if not self._attestor.verify(attestation, payload):
            raise ReviewAttestationError(
                f"review attestation failed to verify for stage {review.stage.value}"
            )
        record = EvidenceRecord(
            artifact_id=uuid.uuid4().hex,
            execution_id=execution_id,
            content_hash=attestation.content_hash,
            source_component=SOURCE_COMPONENT,
            classification=_classification_for_stage(review.stage),
            retention=self._retention,
            location=None,
            metadata={
                "stage": review.stage.value,
                "verdict": raw.verdict.value,
                "prompt_name": review.prompt_name,
                "prompt_version": review.prompt_version,
                "algorithm": attestation.algorithm,
                "key_id": attestation.key_id,
                "issued_at": attestation.issued_at,
                "signature": attestation.signature,
                "envelope": attestation.envelope,
                "finding_count": len(raw.findings),
                "findings": payload["findings"],
                "summary": raw.summary,
                "provenance": payload["provenance"],
            },
        )
        await self._sink.persist(record)
        return ReviewResult(
            stage=review.stage,
            subject_ref=review.subject_ref,
            prompt_name=review.prompt_name,
            prompt_version=review.prompt_version,
            verdict=raw.verdict,
            findings=raw.findings,
            coaching=raw.coaching,
            provenance=raw.provenance,
            summary=raw.summary,
            attestation=attestation,
            usage=raw.usage,
        )
