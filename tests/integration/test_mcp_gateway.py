"""MCP Gateway: serves exactly the read tools and refuses anything outside the catalog."""

from __future__ import annotations

import inspect
import json

import pytest
from adapters.interfaces.git_host import CodeHit, FileContent, PRComment, PRFeedback
from mcp.shared.memory import create_connected_server_and_client_session
from services.integration.mcp_gateway import server as gateway
from services.integration.mcp_gateway.server import HEALTH_PATHS, build_app, build_server
from services.integration.mcp_gateway.tools import READ_TOOL_NAMES, ReadOnlyToolset
from starlette.testclient import TestClient


class _FakeJira:
    def get_issue(self, issue_key: str) -> dict:
        return {
            "key": issue_key,
            "fields": {
                "summary": "Do the thing",
                "labels": [],
                "description": None,
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "Medium"},
            },
        }


class _FakeGitHub:
    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        return FileContent(path=path, text="print('hi')", ref=ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        return [CodeHit(path="a.py", line=1, text=query)]

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        return PRFeedback(
            repo=repo, number=number, state="open", comments=(PRComment(author="rev", body="lgtm"),)
        )


class _FakeEvidence:
    async def get_artifact(self, artifact_id: str) -> dict:
        return {"artifact_id": artifact_id, "execution_id": "exec-1", "metadata": {"k": "v"}}

    async def get_security_findings(self, execution_id: str) -> dict:
        return {"finding_count": 1, "findings": [{"rule": "x"}]}


async def _fake_context(repo: str, commit: str, focus: str) -> dict:
    return {"repo": repo, "commit": commit, "focus": focus, "package": {}}


def _toolset() -> ReadOnlyToolset:
    return ReadOnlyToolset(
        jira=_FakeJira(),
        github=_FakeGitHub(),
        evidence=_FakeEvidence(),
        context_fetch=_fake_context,
        acceptance_field="customfield_accept",
    )


def _result_dict(result) -> dict:
    return json.loads(result.content[0].text)


TOKEN = "gateway-token-for-tests"


def _initialize(client: TestClient, host: str = "localhost", token: str | None = TOKEN):
    headers = {"Accept": "application/json, text/event-stream", "Host": host}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        },
        headers=headers,
    )


async def test_gateway_serves_exactly_the_read_tools():
    async with create_connected_server_and_client_session(build_server(_toolset())) as session:
        listed = await session.list_tools()

    names = {tool.name for tool in listed.tools}
    assert names == set(READ_TOOL_NAMES)
    assert len(listed.tools) == len(READ_TOOL_NAMES)


async def test_gateway_read_tools_return_results():
    async with create_connected_server_and_client_session(build_server(_toolset())) as session:
        task = _result_dict(await session.call_tool("jira_get_task", {"issue_key": "T-1"}))
        assert task["key"] == "T-1"

        compared = _result_dict(
            await session.call_tool(
                "jira_compare_snapshot", {"issue_key": "T-1", "stored_payload": {}}
            )
        )
        assert "category" in compared
        assert "requires_pause" in compared

        file_content = _result_dict(
            await session.call_tool(
                "github_read_file", {"repo": "agentic-demo-service", "path": "a.py", "ref": "main"}
            )
        )
        assert file_content["path"] == "a.py"

        hits = _result_dict(
            await session.call_tool(
                "github_search_code", {"repo": "agentic-demo-service", "query": "todo"}
            )
        )
        assert hits["hits"][0]["text"] == "todo"

        feedback = _result_dict(
            await session.call_tool(
                "github_read_pr_feedback", {"repo": "agentic-demo-service", "number": 5}
            )
        )
        assert feedback["comments"][0]["author"] == "rev"

        findings = _result_dict(
            await session.call_tool("security_get_findings", {"execution_id": "exec-1"})
        )
        assert findings["finding_count"] == 1

        artifact = _result_dict(
            await session.call_tool("evidence_read_artifact", {"artifact_id": "art-1"})
        )
        assert artifact["artifact_id"] == "art-1"

        package = _result_dict(
            await session.call_tool(
                "repo_get_context_package", {"repo": "agentic-demo-service", "focus": "auth"}
            )
        )
        assert package["focus"] == "auth"


async def test_gateway_refuses_a_tool_outside_the_catalog():
    async with create_connected_server_and_client_session(build_server(_toolset())) as session:
        result = await session.call_tool(
            "promote_pr", {"repo": "agentic-demo-service", "number": 5}
        )

    assert result.isError is True


def test_gateway_health_paths_answer_an_alb_target_health_check_without_a_token():
    app = build_app(build_server(_toolset()), TOKEN)
    pod_address = {"Host": "10.0.1.23:8150"}

    with TestClient(app) as client:
        responses = {path: client.get(path, headers=pod_address) for path in HEALTH_PATHS}
        unknown = client.get("/not-a-route", headers=pod_address)

    for path, response in responses.items():
        assert response.status_code == 200, path
        assert response.json() == {"status": "ok", "service": "mcp-gateway"}
    assert unknown.status_code == 401


def test_gateway_checks_the_host_header_against_the_configured_list_when_bound_to_all_interfaces():
    app = build_app(
        build_server(_toolset(), host="0.0.0.0", allowed_hosts=["mcp.internal.example:443"]),
        TOKEN,
    )

    with TestClient(app) as client:
        refused = _initialize(client, "rebind.example:443")
        accepted = _initialize(client, "mcp.internal.example:443")
        health = client.get("/health", headers={"Host": "10.0.1.23:8150"})

    assert refused.status_code == 421
    assert accepted.status_code == 200
    assert health.status_code == 200


def test_gateway_leaves_the_host_header_unchecked_when_no_allowed_hosts_are_configured():
    app = build_app(build_server(_toolset(), host="0.0.0.0", allowed_hosts=[]), TOKEN)

    with TestClient(app) as client:
        response = _initialize(client, "anything.example")

    assert response.status_code == 200


class _CountingToolset:
    """Wraps the fake toolset so a test can see whether any tool ran."""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = _toolset()

    def __getattr__(self, name: str):
        tool = getattr(self._inner, name)

        async def counted(*args, **kwargs):
            self.calls += 1
            return await tool(*args, **kwargs)

        counted.__name__ = tool.__name__
        counted.__doc__ = tool.__doc__
        counted.__signature__ = inspect.signature(tool)
        return counted


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", "Basic Z2F0ZXdheS10b2tlbg==", f"Bearer {TOKEN}x", "Bearer "],
)
def test_a_request_without_the_token_is_refused_and_invokes_no_tool(authorization):
    toolset = _CountingToolset()
    app = build_app(build_server(toolset, host="0.0.0.0", allowed_hosts=[]), TOKEN)
    headers = {"Accept": "application/json, text/event-stream", "Host": "localhost"}
    if authorization is not None:
        headers["Authorization"] = authorization

    with TestClient(app) as client:
        listed = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
        )
        called = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "jira_get_task", "arguments": {"issue_key": "T-1"}},
            },
            headers=headers,
        )

    assert listed.status_code == 401
    assert called.status_code == 401
    assert listed.headers["WWW-Authenticate"] == "Bearer"
    assert toolset.calls == 0


def test_a_request_with_the_token_lists_and_invokes_the_tools():
    toolset = _CountingToolset()
    app = build_app(build_server(toolset, host="0.0.0.0", allowed_hosts=[]), TOKEN)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Host": "localhost",
        "Authorization": f"Bearer {TOKEN}",
    }

    with TestClient(app) as client:
        initialized = _initialize(client)
        session = {**headers, "mcp-session-id": initialized.headers["mcp-session-id"]}
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session,
        )
        listed = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=session
        )
        called = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "jira_get_task", "arguments": {"issue_key": "T-1"}},
            },
            headers=session,
        )

    assert initialized.status_code == 200
    assert listed.status_code == 200
    assert all(name in listed.text for name in READ_TOOL_NAMES)
    assert called.status_code == 200
    assert '"isError":false' in called.text.replace(" ", "")
    assert toolset.calls == 1


def test_the_gateway_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setattr(gateway, "get_settings", lambda: type("S", (), {"mcp_gateway_token": ""})())

    with pytest.raises(SystemExit, match="MCP_GATEWAY_TOKEN.*\\.env.*inv k8s-deploy"):
        gateway.main()


def test_the_gate_refuses_to_wrap_the_app_without_a_token():
    with pytest.raises(ValueError, match="MCP_GATEWAY_TOKEN"):
        build_app(build_server(_toolset()), "")
