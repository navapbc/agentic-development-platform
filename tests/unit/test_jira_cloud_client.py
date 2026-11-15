"""The Jira Cloud REST client the adapter uses in place of the Atlassian CLI."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from services.integration.jira.cloud_client import JiraCloudClient, JiraCloudError


class _Jira:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def queue(self, *responses: httpx.Response) -> None:
        self.responses.extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def client(self) -> JiraCloudClient:
        return JiraCloudClient(
            "https://example.atlassian.net/",
            "agent@example.com",
            "token",
            project="ADP",
            transport=httpx.MockTransport(self.handler),
            sleep=lambda seconds: None,
        )


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_requests_carry_basic_auth_with_the_email_and_token() -> None:
    jira = _Jira()
    jira.queue(httpx.Response(200, json={"key": "ADP-1", "fields": {"summary": "s"}}))

    issue = jira.client().get_issue("ADP-1")

    request = jira.requests[0]
    assert issue["key"] == "ADP-1"
    assert str(request.url) == "https://example.atlassian.net/rest/api/3/issue/ADP-1"
    expected = base64.b64encode(b"agent@example.com:token").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


def test_a_comment_is_posted_as_an_adf_document() -> None:
    jira = _Jira()
    jira.queue(httpx.Response(201, json={"id": "10"}))

    created = jira.client().add_comment("ADP-1", "Draft pull request opened")

    body = _body(jira.requests[0])["body"]
    assert created == {"id": "10"}
    assert jira.requests[0].url.path == "/rest/api/3/issue/ADP-1/comment"
    assert body["type"] == "doc"
    assert "Draft pull request opened" in json.dumps(body)


def test_comments_are_read_across_pages() -> None:
    jira = _Jira()
    jira.queue(
        httpx.Response(200, json={"comments": [{"id": "1"}, {"id": "2"}], "total": 3}),
        httpx.Response(200, json={"comments": [{"id": "3"}], "total": 3}),
    )

    comments = jira.client().get_comments("ADP-1")

    assert [c["id"] for c in comments] == ["1", "2", "3"]
    assert jira.requests[1].url.params["startAt"] == "2"


def test_a_transition_matches_its_name_or_its_target_status() -> None:
    jira = _Jira()
    jira.queue(
        httpx.Response(
            200,
            json={
                "transitions": [
                    {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
                    {"id": "31", "name": "Finish", "to": {"name": "Done"}},
                ]
            },
        ),
        httpx.Response(204),
    )

    jira.client().transition_issue_by_name("ADP-1", "in progress")

    assert _body(jira.requests[1]) == {"transition": {"id": "11"}}


def test_a_transition_the_workflow_does_not_allow_names_the_choices() -> None:
    jira = _Jira()
    jira.queue(httpx.Response(200, json={"transitions": [{"id": "11", "name": "Start"}]}))

    with pytest.raises(JiraCloudError, match=r"available: \['Start'\]"):
        jira.client().transition_issue_by_name("ADP-1", "Done")


def test_search_reads_cursor_pages_only_as_far_as_the_slice_needs() -> None:
    jira = _Jira()
    jira.queue(
        httpx.Response(
            200, json={"issues": [{"key": "ADP-1"}, {"key": "ADP-2"}], "nextPageToken": "t2"}
        ),
        httpx.Response(200, json={"issues": [{"key": "ADP-3"}], "isLast": True}),
    )
    client = jira.client()

    first = client.search_issues("project = ADP", start_at=0, max_results=2)
    rest = client.search_issues("project = ADP", start_at=2, max_results=2)

    assert [i["key"] for i in first] == ["ADP-1", "ADP-2"]
    assert [i["key"] for i in rest] == ["ADP-3"]
    assert len(jira.requests) == 2
    assert _body(jira.requests[1])["nextPageToken"] == "t2"


def test_a_blocks_link_makes_the_from_issue_the_inward_issue() -> None:
    jira = _Jira()
    jira.queue(httpx.Response(201))

    jira.client().set_relationship("ADP-1", "ADP-2", "Blocks")

    assert _body(jira.requests[0]) == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "ADP-1"},
        "outwardIssue": {"key": "ADP-2"},
    }


def test_create_maps_the_ticket_fields_and_returns_the_new_issue() -> None:
    jira = _Jira()
    description = json.dumps({"type": "doc", "version": 1, "content": []})
    jira.queue(
        httpx.Response(201, json={"key": "ADP-9"}),
        httpx.Response(200, json={"key": "ADP-9", "fields": {}}),
    )

    created = jira.client().create_issue(
        {"title": "Add a widget", "ticket_type": "story", "description": description}
    )

    assert created["key"] == "ADP-9"
    assert _body(jira.requests[0])["fields"] == {
        "project": {"key": "ADP"},
        "issuetype": {"name": "Story"},
        "summary": "Add a widget",
        "description": {"type": "doc", "version": 1, "content": []},
    }


def test_a_throttled_request_is_retried_and_an_error_is_raised_with_its_status() -> None:
    jira = _Jira()
    jira.queue(
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"key": "ADP-1"}),
        httpx.Response(404, text="Issue does not exist"),
    )
    client = jira.client()

    assert client.get_issue("ADP-1")["key"] == "ADP-1"
    with pytest.raises(JiraCloudError, match="404 to GET /rest/api/3/issue/ADP-2") as raised:
        client.get_issue("ADP-2")
    assert raised.value.status == 404
