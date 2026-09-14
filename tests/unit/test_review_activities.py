"""Hermetic tests for the code-review and security-review gate activities."""

from __future__ import annotations

from services.orchestration.temporal_activities import review
from services.orchestration.temporal_activities.review import (
    ReviewRequest,
    review_code,
    review_plan,
    review_security,
    verify_acceptance,
)
from services.review.attestation import HmacAttestor
from services.review.gate import ReviewGate
from services.review.types import (
    STAGE_TIERS,
    Finding,
    ModelProvenance,
    RawReview,
    ReviewStage,
    Verdict,
)


class _CapturingKernel:
    def __init__(self, raw: RawReview) -> None:
        self._raw = raw
        self.seen: list = []

    def run(self, review_input) -> RawReview:
        self.seen.append(review_input)
        return self._raw


class _Sink:
    def __init__(self) -> None:
        self.records: list = []

    async def persist(self, record) -> str:
        self.records.append(record)
        return record.artifact_id


def _provenance(tier: str) -> list[ModelProvenance]:
    return [ModelProvenance(leg="finder", tier=tier, model_id=f"openai:{tier}")]


def _clean(tier: str) -> RawReview:
    return RawReview(verdict=Verdict.PASS, provenance=_provenance(tier), summary="clean")


def _blocking(tier: str) -> RawReview:
    return RawReview(
        verdict=Verdict.BLOCK,
        findings=[
            Finding(finding_id="1", severity="high", blocking=True, title="SQLi", detail="x")
        ],
        provenance=_provenance(tier),
        summary="1 blocking",
    )


async def _invoke(monkeypatch, activity_fn, raw: RawReview):
    kernel = _CapturingKernel(raw)
    sink = _Sink()
    monkeypatch.setattr(
        review,
        "build_review_gate",
        lambda stage=None: ReviewGate(kernel, sink, HmacAttestor("t")),
    )
    req = ReviewRequest(
        task_id="T-1", execution_id="exec-1", subject="a diff", context_summary="ctx"
    )
    outcome = await activity_fn(req)
    return outcome, kernel, sink


async def test_review_code_clean(monkeypatch):
    outcome, kernel, sink = await _invoke(monkeypatch, review_code, _clean("review-model"))

    assert outcome.verdict == "pass"
    assert outcome.blocking_findings == 0
    assert outcome.findings == []
    assert outcome.prompt_version == "code-review/v1"
    assert kernel.seen[0].stage is ReviewStage.CODE_REVIEW
    assert STAGE_TIERS[kernel.seen[0].stage] == "review-model"
    assert len(sink.records) == 1
    assert sink.records[0].content_hash == outcome.attestation_hash


async def test_review_code_blocking(monkeypatch):
    outcome, _, sink = await _invoke(monkeypatch, review_code, _blocking("review-model"))

    assert outcome.verdict == "block"
    assert outcome.blocking_findings == 1
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1


async def test_review_security_clean(monkeypatch):
    outcome, kernel, _ = await _invoke(
        monkeypatch, review_security, _clean("security-review-model")
    )

    assert outcome.verdict == "pass"
    assert outcome.prompt_version == "security-review/v1"
    assert kernel.seen[0].stage is ReviewStage.SECURITY_REVIEW
    assert STAGE_TIERS[kernel.seen[0].stage] == "security-review-model"


async def test_review_security_blocking(monkeypatch):
    outcome, _, sink = await _invoke(
        monkeypatch, review_security, _blocking("security-review-model")
    )

    assert outcome.verdict == "block"
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1


async def test_verify_acceptance_clean(monkeypatch):
    outcome, kernel, _ = await _invoke(monkeypatch, verify_acceptance, _clean("evaluation-model"))

    assert outcome.verdict == "pass"
    assert outcome.prompt_version == "acceptance-verification/v1"
    assert kernel.seen[0].stage is ReviewStage.ACCEPTANCE_VERIFICATION
    assert STAGE_TIERS[kernel.seen[0].stage] == "evaluation-model"


async def test_verify_acceptance_blocking(monkeypatch):
    outcome, _, sink = await _invoke(monkeypatch, verify_acceptance, _blocking("evaluation-model"))

    assert outcome.verdict == "block"
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1


def test_resolve_repo_root_prefers_explicit_existing_root(tmp_path):
    req = ReviewRequest(
        task_id="T", execution_id="E", subject="s", repo_root=str(tmp_path)
    )
    assert review._resolve_repo_root(req) == str(tmp_path)


def test_resolve_repo_root_maps_repo_to_ris_path(tmp_path, monkeypatch):
    class _Settings:
        def repo_intelligence_registry(self):
            return {"demo": (str(tmp_path), None)}

    monkeypatch.setattr(review, "get_settings", lambda: _Settings(), raising=False)
    monkeypatch.setattr(
        "scripts.config.get_settings", lambda: _Settings(), raising=False
    )
    req = ReviewRequest(task_id="T", execution_id="E", subject="s", repo="demo")
    assert review._resolve_repo_root(req) == str(tmp_path)


def test_resolve_repo_root_returns_none_for_missing_path(monkeypatch):
    class _Settings:
        def repo_intelligence_registry(self):
            return {"demo": ("/nonexistent/path/xyz", None)}

    monkeypatch.setattr(
        "scripts.config.get_settings", lambda: _Settings(), raising=False
    )
    req = ReviewRequest(task_id="T", execution_id="E", subject="s", repo="demo")
    assert review._resolve_repo_root(req) is None


def test_resolve_repo_root_none_without_repo():
    req = ReviewRequest(task_id="T", execution_id="E", subject="s")
    assert review._resolve_repo_root(req) is None


async def test_code_review_is_repo_grounded(monkeypatch):
    monkeypatch.setattr(review, "_resolve_repo_root", lambda req: "/repos/demo")
    _, kernel, _ = await _invoke(monkeypatch, review_code, _clean("review-model"))
    assert kernel.seen[0].repo_root == "/repos/demo"


async def test_security_review_is_repo_grounded(monkeypatch):
    monkeypatch.setattr(review, "_resolve_repo_root", lambda req: "/repos/demo")
    _, kernel, _ = await _invoke(monkeypatch, review_security, _clean("security-review-model"))
    assert kernel.seen[0].repo_root == "/repos/demo"


async def test_acceptance_is_repo_grounded(monkeypatch):
    monkeypatch.setattr(review, "_resolve_repo_root", lambda req: "/repos/demo")
    _, kernel, _ = await _invoke(monkeypatch, verify_acceptance, _clean("evaluation-model"))
    assert kernel.seen[0].repo_root == "/repos/demo"


async def test_plan_review_stays_single_turn(monkeypatch):
    monkeypatch.setattr(
        review, "_resolve_repo_root", lambda req: (_ for _ in ()).throw(AssertionError("called"))
    )
    _, kernel, _ = await _invoke(monkeypatch, review_plan, _clean("review-model"))
    assert kernel.seen[0].repo_root is None
