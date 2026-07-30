"""Outbound rendering of a small block vocabulary to Jira wiki markup or Cloud ADF."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from services.integration.jira._engine import ensure_engine_on_path

CLOUD = "cloud"
DATACENTER = "datacenter"


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Link:
    text: str
    url: str


Inline = Text | Link


@dataclass(frozen=True)
class Heading:
    text: str
    level: int = 2


@dataclass(frozen=True)
class Paragraph:
    spans: tuple[Inline, ...]


@dataclass(frozen=True)
class BulletList:
    items: tuple[tuple[Inline, ...], ...]


@dataclass(frozen=True)
class Code:
    text: str
    language: str = ""


Block = Heading | Paragraph | BulletList | Code


def _coerce_span(part: str | Inline) -> Inline:
    return Text(part) if isinstance(part, str) else part


def _coerce_spans(parts: Iterable[str | Inline]) -> tuple[Inline, ...]:
    return tuple(_coerce_span(part) for part in parts)


def text(value: str) -> Text:
    return Text(value)


def link(anchor: str, url: str) -> Link:
    return Link(text=anchor, url=url)


def heading(anchor: str, level: int = 2) -> Heading:
    return Heading(text=anchor, level=level)


def paragraph(*parts: str | Inline) -> Paragraph:
    return Paragraph(_coerce_spans(parts))


def bullet_list(items: Iterable[str | Inline | Sequence[str | Inline]]) -> BulletList:
    coerced: list[tuple[Inline, ...]] = []
    for item in items:
        if isinstance(item, str | Text | Link):
            coerced.append((_coerce_span(item),))
        else:
            coerced.append(_coerce_spans(item))
    return BulletList(tuple(coerced))


def code(body: str, language: str = "") -> Code:
    return Code(text=body, language=language)


def _wiki_inline(node: Inline) -> str:
    if isinstance(node, Link):
        return f"[{node.text}|{node.url}]"
    return node.value


def _wiki_block(block: Block) -> str:
    if isinstance(block, Heading):
        return f"h{block.level}. {block.text}"
    if isinstance(block, Paragraph):
        return "".join(_wiki_inline(span) for span in block.spans)
    if isinstance(block, BulletList):
        return "\n".join(
            "* " + "".join(_wiki_inline(span) for span in item) for item in block.items
        )
    header = f"{{code:{block.language}}}" if block.language else "{code}"
    return f"{header}\n{block.text}\n{{code}}"


def render_wiki(blocks: Iterable[Block]) -> str:
    return "\n\n".join(_wiki_block(block) for block in blocks)


def _adf_inline(node: Inline) -> dict[str, Any]:
    if isinstance(node, Link):
        return {
            "type": "text",
            "text": node.text,
            "marks": [{"type": "link", "attrs": {"href": node.url}}],
        }
    return {"type": "text", "text": node.value}


def _adf_block(block: Block) -> dict[str, Any]:
    if isinstance(block, Heading):
        return {
            "type": "heading",
            "attrs": {"level": block.level},
            "content": [{"type": "text", "text": block.text}],
        }
    if isinstance(block, Paragraph):
        return {"type": "paragraph", "content": [_adf_inline(span) for span in block.spans]}
    if isinstance(block, BulletList):
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_adf_inline(span) for span in item]}
                    ],
                }
                for item in block.items
            ],
        }
    attrs = {"language": block.language} if block.language else {}
    return {"type": "codeBlock", "attrs": attrs, "content": [{"type": "text", "text": block.text}]}


def render_adf(blocks: Iterable[Block]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [_adf_block(block) for block in blocks]}


def plain_text_to_adf(body: str) -> dict[str, Any]:
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.adf import text_to_adf

    return text_to_adf(body)


def render(blocks: Iterable[Block], source: str) -> str | dict[str, Any]:
    normalized = source.strip().lower()
    if normalized == DATACENTER:
        return render_wiki(blocks)
    if normalized == CLOUD:
        return render_adf(blocks)
    raise ValueError(f"unknown jira source: {source!r}")
