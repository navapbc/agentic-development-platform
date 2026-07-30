"""Minimal Language Server Protocol client: stdio framing and the initialize handshake."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from services.repository_intelligence.languages import Language

_ENCODING = "utf-8"


class LspError(RuntimeError):
    """Raised when a language server returns an error or closes unexpectedly."""


@dataclass(frozen=True)
class LspServerSpec:
    command: tuple[str, ...]
    language_id: str


_SERVER_SPECS: dict[Language, LspServerSpec] = {
    Language.TERRAFORM: LspServerSpec(("terraform-ls", "serve"), "terraform"),
    Language.PHP: LspServerSpec(("phpactor", "language-server"), "php"),
}


def server_spec_for(language: Language) -> LspServerSpec | None:
    return _SERVER_SPECS.get(language)


def encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode(_ENCODING)
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def read_message(reader: BinaryIO) -> dict[str, Any] | None:
    length = _read_content_length(reader)
    if length is None:
        return None
    body = reader.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode(_ENCODING))


def _read_content_length(reader: BinaryIO) -> int | None:
    length: int | None = None
    while True:
        line = reader.readline()
        if not line:
            return None
        text = line.decode("ascii", errors="replace").strip()
        if not text:
            return length
        name, _, value = text.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None


class LspClient:
    """JSON-RPC client speaking LSP over a reader/writer pair."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    def initialize(self, root: Path) -> dict[str, Any]:
        root_uri = Path(root).resolve().as_uri()
        result = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
                "workspaceFolders": [{"uri": root_uri, "name": Path(root).name}],
            },
        )
        self.notify("initialized", {})
        return result if isinstance(result, dict) else {}

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = read_message(self._reader)
            if message is None:
                raise LspError(f"no response to {method}")
            if "method" in message and "id" in message:
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise LspError(str(message["error"]))
            return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {})
            self.notify("exit", {})
        except LspError:
            return

    def _write(self, payload: dict[str, Any]) -> None:
        self._writer.write(encode_message(payload))
        self._writer.flush()


def spawn(spec: LspServerSpec) -> tuple[subprocess.Popen[bytes], LspClient]:
    process = subprocess.Popen(  # noqa: S603
        list(spec.command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    return process, LspClient(process.stdout, process.stdin)
