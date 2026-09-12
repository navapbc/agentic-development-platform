"""Smoke tests for the repository-intelligence server entrypoint."""

from __future__ import annotations

import io
import json

import pytest
from services.repository_intelligence import server
from services.repository_intelligence.normalization.models import ContextPackage


@pytest.fixture(autouse=True)
def _allowlist_from_config(monkeypatch):
    monkeypatch.setattr(
        server,
        "source_control_repos",
        lambda settings=None: {"agentic-demo-service", "agentic-demo-greenfield"},
    )


class _Recorder(server.Handler):
    def __init__(self, path, body=b""):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self._status = None
        self._headers: dict[str, str] = {}
        self._body = b""
        self.wfile = self

    def send_response(self, code):
        self._status = code

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        return

    def write(self, data):
        self._body = data


def test_server_exposes_entrypoint():
    assert callable(server.main)
    assert isinstance(server.PORT, int)
    assert server.PORT != 8100


def test_handler_reports_health():
    class _Recorder(server.Handler):
        def __init__(self):
            self.path = "/health"
            self._status = None
            self._headers: dict[str, str] = {}
            self._body = b""
            self.wfile = self

        def send_response(self, code):
            self._status = code

        def send_header(self, key, value):
            self._headers[key] = value

        def end_headers(self):
            return

        def write(self, data):
            self._body = data

    recorder = _Recorder()
    recorder.do_GET()

    assert recorder._status == 200
    assert b"repository-intelligence" in recorder._body


class _FakeSettings:
    pass


class _FakeService:
    def context_package(self, repo, focus, detail=""):
        return ContextPackage(
            focus=focus,
            symbols=(),
            references=(),
            related_tests=(),
            dependencies=(),
            repository=repo.repo,
            indexed_commit="0744a6e",
        )


class _FakeRegistry:
    def __init__(self, services):
        self._services = services

    def service(self, repo):
        return self._services.get(repo)


def test_context_package_endpoint_returns_serialized_package(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(
        server, "build_registry", lambda: _FakeRegistry({"agentic-demo-service": _FakeService()})
    )
    body = json.dumps(
        {"repo": "agentic-demo-service", "commit": "0744a6e", "focus": "health"}
    ).encode()

    recorder = _Recorder("/context_package", body)
    recorder.do_POST()

    assert recorder._status == 200
    payload = json.loads(recorder._body)
    assert payload["repository"] == "agentic-demo-service"
    assert payload["focus"] == "health"


def test_context_package_endpoint_routes_by_repo(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(
        server,
        "build_registry",
        lambda: _FakeRegistry(
            {"agentic-demo-service": _FakeService(), "agentic-demo-greenfield": _FakeService()}
        ),
    )
    body = json.dumps({"repo": "agentic-demo-greenfield", "focus": "health"}).encode()

    recorder = _Recorder("/context_package", body)
    recorder.do_POST()

    assert recorder._status == 200
    assert json.loads(recorder._body)["repository"] == "agentic-demo-greenfield"


def test_context_package_endpoint_rejects_unlisted_repo(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(
        server, "build_registry", lambda: _FakeRegistry({"agentic-demo-service": _FakeService()})
    )
    body = json.dumps({"repo": "other-repo", "focus": "health"}).encode()

    recorder = _Recorder("/context_package", body)
    recorder.do_POST()

    assert recorder._status == 403


def test_context_package_endpoint_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(server, "build_registry", lambda: None)
    body = json.dumps({"repo": "agentic-demo-service", "focus": "health"}).encode()

    recorder = _Recorder("/context_package", body)
    recorder.do_POST()

    assert recorder._status == 503


def test_context_package_endpoint_unregistered_repo_returns_503(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(server, "build_registry", lambda: _FakeRegistry({}))
    body = json.dumps({"repo": "agentic-demo-greenfield", "focus": "health"}).encode()

    recorder = _Recorder("/context_package", body)
    recorder.do_POST()

    assert recorder._status == 503
