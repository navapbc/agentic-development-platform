"""The platform review gate: run the kernel, attest the verdict, persist evidence."""

from __future__ import annotations

import uuid
from typing import Any

from scripts.config import get_settings

from services.review.attestation import Attestor, HmacAttestor, ReviewAttestationError
from services.review.evidence import EvidenceRecord, EvidenceSink
from services.review.kernel import ReviewKernel
from services.review.types import RawReview, ReviewInput, ReviewResult

SOURCE_COMPONENT = "review-kernel"


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
        classification: str = "final",
        retention: str | None = None,
    ) -> None:
        self._kernel = kernel
        self._sink = sink
        self._attestor = attestor or HmacAttestor(get_settings().review_attestation_secret)
        self._classification = classification
        self._retention = retention

    async def review(self, review: ReviewInput, *, execution_id: str) -> ReviewResult:
        raw = self._kernel.run(review)
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
            classification=self._classification,
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
                "finding_count": len(raw.findings),
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
        )
