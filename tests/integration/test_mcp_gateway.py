"""MCP Gateway: serves exactly the read tools and refuses anything outside the catalog."""

from __future__ import annotations

import json

from adapters.interfaces.git_host import CodeHit, FileContent, PRComment, PRFeedback
from mcp.shared.memory import create_connected_server_and_client_session
from services.integration.mcp_gateway.server import build_server
from services.integration.mcp_gateway.tools import READ_TOOL_NAMES, ReadOnlyToolset


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

        build = _result_dict(
            await session.call_tool(
                {"repo": "agentic-demo-service", "job": "ci", "build_number": 7},
            )
        )
        assert build["result"] == "SUCCESS"

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
