"""Tests for the provider-agnostic gate model nearest-match resolver."""

from __future__ import annotations

import pytest
from services.orchestration.gates.model_resolver import (
    ABORT,
    WARN,
    ModelUnavailableError,
    available_gate_model_groups,
    available_models_from_litellm,
    build_catalog,
    parse_model_ref,
    resolve_from_available,
    resolve_model,
)

_BEDROCK_IDS = [
    "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0",
    "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "bedrock/us.amazon.nova-pro-v1:0",
    "bedrock/us.amazon.nova-2-lite-v1:0",
    "bedrock/us.meta.llama3-3-70b-instruct-v1:0",
]


def _catalog():
    return build_catalog(_BEDROCK_IDS)


def test_parse_bedrock_claude_id():
    ref = parse_model_ref("bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0")
    assert ref is not None
    assert ref.vendor == "anthropic"
    assert ref.family == "claude-opus"
    assert ref.version == (4, 5)
    assert ref.date == "20251101"


def test_parse_user_request_with_dotted_version_and_spaces():
    ref = parse_model_ref("Claude Opus 4.8")
    assert ref is not None
    assert ref.vendor == "anthropic"
    assert ref.family == "claude-opus"
    assert ref.version == (4, 8)


def test_parse_openai_gpt():
    ref = parse_model_ref("gpt-4.1")
    assert ref is not None
    assert ref.vendor == "openai"
    assert ref.family == "gpt"
    assert ref.version == (4, 1)


def test_parse_openai_variant_keeps_letter_token_in_family():
    ref = parse_model_ref("gpt-4o-mini")
    assert ref is not None
    assert ref.vendor == "openai"
    assert ref.family == "gpt-4o-mini"
    assert ref.version == ()


def test_parse_gemini():
    ref = parse_model_ref("gemini-2.5-pro")
    assert ref is not None
    assert ref.vendor == "google"
    assert ref.family == "gemini-pro"
    assert ref.version == (2, 5)


def test_parse_llama_keeps_size_token_in_family():
    ref = parse_model_ref("bedrock/us.meta.llama3-3-70b-instruct-v1:0")
    assert ref is not None
    assert ref.vendor == "meta"
    assert ref.family == "llama3-70b-instruct"
    assert ref.version == (3,)


def test_parse_empty_returns_none():
    assert parse_model_ref("") is None
    assert parse_model_ref("   ") is None


def test_exact_match_is_not_a_substitution():
    result = resolve_model(
        "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0", _catalog(), on_unavailable=ABORT
    )
    assert result.substituted is False


def test_exact_match_by_serving_id_ignores_bedrock_prefix():
    result = resolve_model(
        "us.anthropic.claude-opus-4-5-20251101-v1:0", _catalog(), on_unavailable=ABORT
    )
    assert result.substituted is False
    assert result.model_id == "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0"


def test_nearest_match_within_family_warns_and_substitutes():
    result = resolve_model("claude-opus-4.8", _catalog(), on_unavailable=WARN)
    assert result.substituted is True
    assert result.model_id == "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0"
    assert "substituting nearest" in result.note


def test_nearest_match_aborts_when_configured():
    with pytest.raises(ModelUnavailableError) as exc:
        resolve_model("claude-opus-4.8", _catalog(), on_unavailable=ABORT)
    assert "on_unavailable=abort" in str(exc.value)


def test_nearest_match_stays_in_family_and_never_crosses_tier():
    # An unavailable opus must not fall back to an available sonnet or haiku.
    catalog = build_catalog(
        [
            "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ]
    )
    with pytest.raises(ModelUnavailableError) as exc:
        resolve_model("claude-opus-4.5", catalog, on_unavailable=WARN)
    assert "family anthropic/claude-opus" in str(exc.value)


def test_nearest_match_never_crosses_vendor_line():
    # A request for an unavailable gpt must not resolve to a claude, even though both are text LLMs.
    catalog = build_catalog(["bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0"])
    with pytest.raises(ModelUnavailableError):
        resolve_model("gpt-4.1", catalog, on_unavailable=WARN)


def test_nearest_match_prefers_smallest_version_distance():
    catalog = build_catalog(
        [
            "bedrock/us.anthropic.claude-opus-4-1-20250101-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-20260101-v1:0",
        ]
    )
    result = resolve_model("claude-opus-4.5", catalog, on_unavailable=WARN)
    assert result.model_id == "bedrock/us.anthropic.claude-opus-4-6-20260101-v1:0"


def test_nearest_match_tiebreak_prefers_newer_version():
    catalog = build_catalog(
        [
            "bedrock/us.anthropic.claude-opus-4-4-20250101-v1:0",
            "bedrock/us.anthropic.claude-opus-4-6-20260101-v1:0",
        ]
    )
    result = resolve_model("claude-opus-4.5", catalog, on_unavailable=WARN)
    assert result.model_id == "bedrock/us.anthropic.claude-opus-4-6-20260101-v1:0"


def test_fallback_never_crosses_major_even_when_decimally_closer():
    catalog = build_catalog(["gpt-4-1", "gpt-5"])
    result = resolve_model("gpt-4.9", catalog, on_unavailable=WARN)
    # 4.9 reads as decimally closer to 5.0, but semver pins the fallback to major 4.
    assert result.model_id == "gpt-4-1"


def test_nearest_match_prefers_same_major_when_closer():
    catalog = build_catalog(["gpt-4-1", "gpt-5"])
    result = resolve_model("gpt-4.5", catalog, on_unavailable=WARN)
    assert result.model_id == "gpt-4-1"


def test_no_model_in_requested_major_aborts():
    catalog = build_catalog(
        [
            "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0",
            "bedrock/us.anthropic.claude-opus-4-1-20250101-v1:0",
        ]
    )
    with pytest.raises(ModelUnavailableError) as exc:
        resolve_model("claude-opus-3.5", catalog, on_unavailable=WARN)
    assert "major version 3" in str(exc.value)


def test_build_catalog_keeps_all_parseable_entries():
    catalog = build_catalog(
        ["bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0", "gpt-4o", "gemini-2.0-flash"]
    )
    families = sorted(entry.ref.family for entry in catalog)
    assert families == ["claude-opus", "gemini-flash", "gpt-4o"]


def test_same_version_different_date_is_not_a_substitution():
    # A request for the undated version resolves to the dated group as an exact version match.
    catalog = build_catalog(["claude-opus-4-5-20251101"])
    result = resolve_model("claude-opus-4.5", catalog, on_unavailable=ABORT)
    assert result.substituted is False
    assert result.model_id == "claude-opus-4-5-20251101"


def test_same_version_prefers_newest_release_date():
    catalog = build_catalog(["claude-opus-4-5-20250101", "claude-opus-4-5-20251101"])
    result = resolve_model("claude-opus-4-5", catalog, on_unavailable=ABORT)
    assert result.substituted is False
    assert result.model_id == "claude-opus-4-5-20251101"


def test_available_gate_model_groups_excludes_semantic_aliases():
    config = {
        "model_list": [
            {"model_name": "security-review-model", "litellm_params": {"model": "bedrock/x"}},
            {"model_name": "coding-model", "litellm_params": {"model": "bedrock/y"}},
            {"model_name": "claude-opus-4-5-20251101", "litellm_params": {"model": "bedrock/z"}},
            {"model_name": "gpt-4o", "litellm_params": {"model": "bedrock/w"}},
        ]
    }
    assert available_gate_model_groups(config) == ["claude-opus-4-5-20251101", "gpt-4o"]


def test_available_gate_model_groups_empty():
    assert available_gate_model_groups({}) == []
    config = {
        "model_list": [
            {"model_name": "security-review-model", "litellm_params": {"model": "bedrock/a"}},
            {"model_name": "security-review-model", "litellm_params": {"model": "bedrock/b"}},
            {"model_name": "planning-model", "litellm_params": {"model": "bedrock/a"}},
            {"not_a_dict": True},
            "junk",
        ]
    }
    assert available_models_from_litellm(config) == ["bedrock/a", "bedrock/b"]


def test_available_models_from_litellm_empty():
    assert available_models_from_litellm({}) == []
    assert available_models_from_litellm({"model_list": None}) == []


def test_resolve_from_available_end_to_end():
    available = [
        "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0",
        "bedrock/us.amazon.nova-pro-v1:0",
    ]
    result = resolve_from_available("claude-opus-4.8", available, on_unavailable=WARN)
    assert result.substituted is True
    assert result.model_id == "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0"
