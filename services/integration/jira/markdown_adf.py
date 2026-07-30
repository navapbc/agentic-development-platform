"""Convert markdown to Atlassian Document Format, the inverse of rebar's adf_to_text."""

from __future__ import annotations

import json
import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6}) (.*)$")
_BULLET_RE = re.compile(r"^- (.*)$")
_ORDERED_RE = re.compile(r"^(\d+)\. (.*)$")
_QUOTE_RE = re.compile(r"^> (.*)$")
_INLINE_RE = re.compile(r"\*\*(?P<strong>.+?)\*\*|\*(?P<em>.+?)\*|`(?P<code>[^`]+)`")
_FENCE = "```"


def _marked(text: str, mark: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "marks": [{"type": mark}]}


def _inline(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    nodes: list[dict[str, Any]] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            nodes.append({"type": "text", "text": text[pos : match.start()]})
        if match.group("strong") is not None:
            nodes.append(_marked(match.group("strong"), "strong"))
        elif match.group("em") is not None:
            nodes.append(_marked(match.group("em"), "em"))
        else:
            nodes.append(_marked(match.group("code"), "code"))
        pos = match.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": _inline(text)}


def _list_item(text: str) -> dict[str, Any]:
    return {"type": "listItem", "content": [_paragraph(text)]}


def markdown_to_adf(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    content: list[dict[str, Any]] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if line == _FENCE:
            code_lines: list[str] = []
            i += 1
            while i < total and lines[i] != _FENCE:
                code_lines.append(lines[i])
                i += 1
            i += 1
            node: dict[str, Any] = {"type": "codeBlock", "content": []}
            if code_lines:
                node["content"] = [{"type": "text", "text": "\n".join(code_lines)}]
            content.append(node)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": _inline(heading.group(2)),
                }
            )
            i += 1
            continue
        if line == "---":
            content.append({"type": "rule"})
            i += 1
            continue
        if _BULLET_RE.match(line):
            items: list[dict[str, Any]] = []
            while i < total and (bullet := _BULLET_RE.match(lines[i])):
                items.append(_list_item(bullet.group(1)))
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue
        if _ORDERED_RE.match(line):
            ordered_items: list[dict[str, Any]] = []
            while i < total and (ordered := _ORDERED_RE.match(lines[i])):
                ordered_items.append(_list_item(ordered.group(2)))
                i += 1
            content.append({"type": "orderedList", "content": ordered_items})
            continue
        if _QUOTE_RE.match(line):
            quote_paragraphs: list[dict[str, Any]] = []
            while i < total and (quote := _QUOTE_RE.match(lines[i])):
                quote_paragraphs.append(_paragraph(quote.group(1)))
                i += 1
            content.append({"type": "blockquote", "content": quote_paragraphs})
            continue
        content.append(_paragraph(line))
        i += 1
    return {"type": "doc", "version": 1, "content": content}


def markdown_to_adf_json(text: str) -> str:
    return json.dumps(markdown_to_adf(text))
