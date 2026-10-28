"""Tests for the db-bootstrap-roles invocation of the secret-store bootstrap function."""

from __future__ import annotations

import io
import json
from typing import Any

import boto3
import pytest
from scripts import db_bootstrap_roles


class _FakeLambda:
    def __init__(self, payload: Any, *, function_error: str = "") -> None:
        self._payload = payload
        self._function_error = function_error
        self.requests: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        response: dict[str, Any] = {"Payload": io.BytesIO(json.dumps(self._payload).encode())}
        if self._function_error:
            response["FunctionError"] = self._function_error
        return response


@pytest.fixture
def fake_lambda(monkeypatch):
    def install(client: _FakeLambda) -> _FakeLambda:
        monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)
        return client

    return install


def test_bootstrap_sends_the_bootstrap_action(fake_lambda, capsys):
    client = fake_lambda(_FakeLambda({"roles": ["langfuse", "temporal"], "steps": 8}))

    assert db_bootstrap_roles.main(["--function-name", "platform-postgres-rotation"]) == 0

    assert client.requests[0]["FunctionName"] == "platform-postgres-rotation"
    assert json.loads(client.requests[0]["Payload"]) == {"action": "bootstrap"}
    assert "langfuse, temporal" in capsys.readouterr().out


def test_a_function_error_fails_the_task(fake_lambda, capsys):
    fake_lambda(_FakeLambda({"errorMessage": "role bootstrap failed"}, function_error="Unhandled"))

    assert db_bootstrap_roles.main(["--function-name", "platform-postgres-rotation"]) == 1
    assert "role bootstrap failed" in capsys.readouterr().out
