"""Unit tests for the minimal LSP client framing and handshake."""

from __future__ import annotations

import io

import pytest
from services.repository_intelligence.languages import Language
from services.repository_intelligence.providers.lsp.lsp_client import (
    LspClient,
    LspError,
    encode_message,
    read_message,
    server_spec_for,
)


def _reader(*payloads: dict) -> io.BytesIO:
    buffer = io.BytesIO()
    for payload in payloads:
        buffer.write(encode_message(payload))
    buffer.seek(0)
    return buffer


def _sent(writer: io.BytesIO) -> list[dict]:
    writer.seek(0)
    messages: list[dict] = []
    while True:
        message = read_message(writer)
        if message is None:
            break
        messages.append(message)
    return messages


def test_encode_and_read_message_roundtrip():
    assert read_message(_reader({"jsonrpc": "2.0", "value": 7})) == {"jsonrpc": "2.0", "value": 7}


def test_read_message_returns_none_on_empty_stream():
    assert read_message(io.BytesIO()) is None


def test_request_returns_result_for_matching_id():
    writer = io.BytesIO()
    client = LspClient(_reader({"id": 1, "result": {"ok": True}}), writer)

    assert client.request("workspace/symbol", {"query": "x"}) == {"ok": True}
    sent = _sent(writer)
    assert sent[0]["method"] == "workspace/symbol"
    assert sent[0]["id"] == 1


def test_request_skips_notifications():
    reader = _reader({"method": "window/logMessage", "params": {}}, {"id": 1, "result": 5})
    assert LspClient(reader, io.BytesIO()).request("m", {}) == 5


def test_request_replies_to_server_initiated_request():
    writer = io.BytesIO()
    reader = _reader(
        {"id": 99, "method": "client/registerCapability", "params": {}},
        {"id": 1, "result": "done"},
    )

    assert LspClient(reader, writer).request("m", {}) == "done"
    replies = [msg for msg in _sent(writer) if msg.get("id") == 99]
    assert replies == [{"jsonrpc": "2.0", "id": 99, "result": None}]


def test_request_raises_on_error_response():
    client = LspClient(_reader({"id": 1, "error": {"code": -1, "message": "boom"}}), io.BytesIO())
    with pytest.raises(LspError):
        client.request("m", {})


def test_request_raises_when_stream_closes():
    with pytest.raises(LspError):
        LspClient(io.BytesIO(), io.BytesIO()).request("m", {})


def test_initialize_sends_initialized_notification(tmp_path):
    writer = io.BytesIO()
    client = LspClient(_reader({"id": 1, "result": {"capabilities": {}}}), writer)

    client.initialize(tmp_path)

    sent = _sent(writer)
    assert sent[0]["method"] == "initialize"
    assert sent[1]["method"] == "initialized"


def test_server_spec_for_known_and_unknown_languages():
    assert server_spec_for(Language.TERRAFORM).command[0] == "terraform-ls"
    assert server_spec_for(Language.PHP).command[0] == "phpactor"
    assert server_spec_for(Language.RUBY) is None
