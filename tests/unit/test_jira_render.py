"""Outbound render tests: wiki markup for Data Center, ADF for Cloud, round-trip stable."""

from __future__ import annotations

import pytest
from services.integration.jira._engine import ensure_engine_on_path
from services.integration.jira.render import (
    bullet_list,
    code,
    heading,
    link,
    paragraph,
    plain_text_to_adf,
    render,
    render_adf,
    render_wiki,
)


def _document():
    return [
        heading("Execution summary"),
        paragraph("Opened ", link("PR #12", "https://ghes/pr/12"), " for the retry work."),
        bullet_list(
            [
                "All checks passed",
                ["See ", link("build", "https://ci/build/9")],
            ]
        ),
        code("retry(max=3)", language="python"),
    ]


def test_render_wiki_emits_data_center_markup():
    markup = render_wiki(_document())

    assert markup == (
        "h2. Execution summary\n\n"
        "Opened [PR #12|https://ghes/pr/12] for the retry work.\n\n"
        "* All checks passed\n"
        "* See [build|https://ci/build/9]\n\n"
        "{code:python}\nretry(max=3)\n{code}"
    )


def test_render_adf_emits_cloud_document():
    doc = render_adf(_document())

    assert doc["type"] == "doc"
    assert doc["version"] == 1
    types = [node["type"] for node in doc["content"]]
    assert types == ["heading", "paragraph", "bulletList", "codeBlock"]
    assert doc["content"][0]["attrs"]["level"] == 2

    paragraph_node = doc["content"][1]
    link_span = paragraph_node["content"][1]
    assert link_span["marks"][0] == {"type": "link", "attrs": {"href": "https://ghes/pr/12"}}

    bullet_items = doc["content"][2]["content"]
    assert len(bullet_items) == 2
    assert bullet_items[0]["type"] == "listItem"

    code_node = doc["content"][3]
    assert code_node["attrs"] == {"language": "python"}
    assert code_node["content"][0]["text"] == "retry(max=3)"


def test_code_block_without_language():
    assert render_wiki([code("plain body")]) == "{code}\nplain body\n{code}"
    doc = render_adf([code("plain body")])
    assert doc["content"][0]["attrs"] == {}


def test_render_dispatches_by_source():
    doc = _document()
    assert isinstance(render(doc, "cloud"), dict)
    assert isinstance(render(doc, "DataCenter"), str)


def test_render_rejects_unknown_source():
    with pytest.raises(ValueError, match="unknown jira source"):
        render(_document(), "server")


def test_adf_round_trips_to_readable_text():
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.adf import adf_to_text
    from rebar_reconciler.inbound_fields import normalize_rich_text

    doc = render_adf(_document())
    recovered = adf_to_text(doc)

    assert "# Execution summary" in recovered
    assert "Opened PR #12 for the retry work." in recovered
    assert "- All checks passed" in recovered
    assert "retry(max=3)" in recovered
    assert normalize_rich_text(doc) == recovered


def test_wiki_passes_through_the_inbound_normalizer():
    ensure_engine_on_path()
    from rebar_reconciler.inbound_fields import normalize_rich_text

    markup = render_wiki(_document())
    assert normalize_rich_text(markup) == markup


def test_plain_text_to_adf_reuses_the_rebar_baseline():
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.adf import adf_to_text, text_to_adf

    body = "line one\nline two"
    assert plain_text_to_adf(body) == text_to_adf(body)
    assert adf_to_text(plain_text_to_adf(body)) == body
