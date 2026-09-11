"""Unit tests for ticket focus-term and file-path extraction."""

from __future__ import annotations

from services.repository_intelligence.context_builder.focus import (
    extract_file_paths,
    extract_focus_terms,
)


def test_extract_focus_terms_keeps_camel_case_and_snake_case():
    text = "Convert raw anchor tags to ExternalLink and call get_claims"
    assert extract_focus_terms(text) == ["ExternalLink", "get_claims"]


def test_extract_focus_terms_drops_plain_words_and_short_tokens():
    text = "Add external link indicators to the buttons in a UI"
    assert extract_focus_terms(text) == []


def test_extract_focus_terms_dedupes_preserving_order():
    text = "ExternalLink then ExternalLink again with OAuthLaunchButton"
    assert extract_focus_terms(text) == ["ExternalLink", "OAuthLaunchButton"]


def test_extract_focus_terms_respects_limit():
    text = "AlphaOne BetaTwo GammaThree DeltaFour"
    assert extract_focus_terms(text, limit=2) == ["AlphaOne", "BetaTwo"]


def test_extract_file_paths_handles_jira_link_and_line_anchors():
    text = (
        "* [portal/src/components/FileCard.tsx|portal/src/components/FileCard.tsx#L52-L75]\n"
        "* features/status/PaymentFAQSection.tsx:129\n"
        "api/massgov/pfml/api/claims.py"
    )
    assert extract_file_paths(text) == [
        "portal/src/components/FileCard.tsx",
        "features/status/PaymentFAQSection.tsx",
        "api/massgov/pfml/api/claims.py",
    ]


def test_extract_file_paths_ignores_non_code_and_bare_names():
    text = "See README and config.yaml plus ExternalLink and portal/src/notes.txt"
    assert extract_file_paths(text) == []


def test_extractors_return_empty_for_empty_text():
    assert extract_focus_terms("") == []
    assert extract_file_paths("") == []
