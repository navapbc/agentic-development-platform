import pytest
from services.integration.jira._engine import ensure_engine_on_path
from services.integration.jira.markdown_adf import markdown_to_adf


def _adf_to_text():
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.adf import adf_to_text

    return adf_to_text


_CASES = [
    "single line",
    "Plain, **bold**, *italic*, and `inline code` on one line.",
    "# Heading one",
    "## Heading two",
    "- first item\n- second item\n- third item",
    "1. step one\n2. step two\n3. step three",
    "```\ndef smoke():\n    return True\n```",
    "> quoted line one\n> quoted line two",
    "before\n\nafter",
    "\n".join(
        [
            "# title",
            "",
            "Plain, **bold**, *italic*, and `inline code` on one line.",
            "",
            "## Bullet list",
            "",
            "- first item",
            "- second item",
            "",
            "## Ordered list",
            "",
            "1. step one",
            "2. step two",
            "",
            "## Code block",
            "",
            "```",
            "def smoke():",
            "    return True",
            "```",
            "",
            "## Blockquote",
            "",
            "> quoted line one",
            "> quoted line two",
            "",
            "---",
            "",
            "Trailing paragraph after the rule.",
        ]
    ),
]


@pytest.mark.parametrize("text", _CASES)
def test_markdown_to_adf_inverts_adf_to_text(text):
    adf_to_text = _adf_to_text()
    assert adf_to_text(markdown_to_adf(text)) == text


def test_bold_span_becomes_strong_mark():
    doc = markdown_to_adf("a **b** c")
    marks = doc["content"][0]["content"][1]["marks"]
    assert marks == [{"type": "strong"}]


def test_heading_level_is_captured():
    doc = markdown_to_adf("### deep")
    assert doc["content"][0] == {
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{"type": "text", "text": "deep"}],
    }
