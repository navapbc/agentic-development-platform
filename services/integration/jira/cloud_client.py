"""Jira Cloud client over the REST API v3, authenticated with an account email and API token.

It implements the operations `JiraAdapter` calls on a vendor client, with the payload shapes
the Jira Data Center transport returns, so the adapter treats both sites alike. Comment bodies
are fitted and encoded by rebar's ADF codec, which keeps the reconciler echo marker intact.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

_TIMEOUT_SECONDS = 30.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_ATTEMPTS = 4
_SUMMARY_LIMIT = 255
SEARCH_FIELDS = (
    "issuetype",
    "key",
    "assignee",
    "priority",
    "status",
    "summary",
    "description",
    "labels",
    "fixVersions",
)


class JiraCloudError(RuntimeError):
    """Jira Cloud refused a request or answered with a payload the client cannot read."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JiraCloudClient:
    def __init__(
        self,
        url: str,
        email: str,
        token: str,
        *,
        project: str = "",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project = project
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._searches: dict[str, tuple[list[dict[str, Any]], Iterator[dict[str, Any]]]] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(1, _ATTEMPTS + 1):
            response = self._http.request(method, path, **kwargs)
            if response.status_code in _RETRY_STATUSES and attempt < _ATTEMPTS:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else 2.0**attempt)
                continue
            if response.is_error:
                raise JiraCloudError(
                    f"Jira Cloud answered {response.status_code} to {method} {path}: "
                    f"{response.text[:500]}",
                    response.status_code,
                )
            return response.json() if response.content else {}
        raise JiraCloudError(f"Jira Cloud kept answering {method} {path} with a retryable status")

    def myself(self) -> dict[str, Any]:
        """Return the account the email and token authenticate as."""
        return self._request("GET", "/rest/api/3/myself")

    def get_issue(self, remote_id: str, /) -> dict[str, Any]:
        return self._request("GET", f"/rest/api/3/issue/{remote_id}")

    def get_issue_links(self, remote_id: str, /) -> list[dict[str, Any]]:
        issue = self._request(
            "GET", f"/rest/api/3/issue/{remote_id}", params={"fields": "issuelinks"}
        )
        links = (issue.get("fields") or {}).get("issuelinks")
        return links if isinstance(links, list) else []

    def get_comments(self, remote_id: str, /) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        while True:
            page = self._request(
                "GET",
                f"/rest/api/3/issue/{remote_id}/comment",
                params={"startAt": len(comments), "maxResults": 100},
            )
            batch = page.get("comments") or []
            comments.extend(batch)
            if not batch or len(comments) >= int(page.get("total") or 0):
                return comments

    def add_comment(self, remote_id: str, body: str, /) -> dict[str, Any]:
        from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec
        from rebar.reconciler.adapters.jira_family.rich_text import cutover_clients
        from rebar.reconciler.outbound_comments import fit_preserving_marker

        codec = AdfCodec(rich="cloud" in cutover_clients())
        document = codec.to_wire(fit_preserving_marker(body, codec.fit_outbound))
        return self._request(
            "POST", f"/rest/api/3/issue/{remote_id}/comment", json={"body": document}
        )

    def transition_issue_by_name(self, remote_id: str, target_status: str, /) -> None:
        """Apply the transition whose name, or whose target status, is `target_status`."""
        listing = self._request("GET", f"/rest/api/3/issue/{remote_id}/transitions")
        transitions = listing.get("transitions") or []
        wanted = target_status.strip().lower()
        for transition in transitions:
            name = str(transition.get("name") or "").strip().lower()
            to_name = str((transition.get("to") or {}).get("name") or "").strip().lower()
            if wanted in (name, to_name) and transition.get("id"):
                self._request(
                    "POST",
                    f"/rest/api/3/issue/{remote_id}/transitions",
                    json={"transition": {"id": str(transition["id"])}},
                )
                return
        available = sorted(str(t.get("name")) for t in transitions)
        raise JiraCloudError(
            f"no transition takes {remote_id} to {target_status!r}; available: {available}"
        )

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create an issue from rebar's ticket fields, as the Data Center transport does."""
        import json

        project = ticket_data.get("_bridge_target_project") or self.project
        summary = str(ticket_data.get("title") or "").strip()
        if not summary:
            raise ValueError("cannot create a Jira issue with an empty title")
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": str(ticket_data.get("ticket_type") or "Task").capitalize()},
            "summary": summary[:_SUMMARY_LIMIT],
        }
        description = ticket_data.get("description")
        if description:
            fields["description"] = (
                json.loads(description) if isinstance(description, str) else description
            )
        priority = ticket_data.get("priority")
        if isinstance(priority, dict):
            fields["priority"] = priority
        elif priority:
            fields["priority"] = {"name": str(priority)}
        if ticket_data.get("labels"):
            fields["labels"] = list(ticket_data["labels"])
        parent = ticket_data.get("parent")
        parent_key = parent.get("key") if isinstance(parent, dict) else parent
        if parent_key:
            fields["parent"] = {"key": str(parent_key)}
        assignee = self._assignable_account(str(ticket_data.get("assignee") or ""), project)
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        created = self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        return self.get_issue(str(created["key"]))

    def _assignable_account(self, name: str, project: str) -> str:
        """Return the account ID of an assignable user named `name`, or an empty string."""
        if not name:
            return ""
        users = self._request(
            "GET",
            "/rest/api/3/user/assignable/search",
            params={"project": project, "query": name},
        )
        lowered = name.lower()
        for user in users if isinstance(users, list) else []:
            if lowered in (
                str(user.get("accountId") or "").lower(),
                str(user.get("emailAddress") or "").lower(),
                str(user.get("displayName") or "").lower(),
            ):
                return str(user["accountId"])
        return ""

    def set_relationship(
        self, from_key: str, to_key: str, link_type: str = "Blocks", /
    ) -> dict[str, Any]:
        """Link the issues so that `from_key` `link_type` `to_key`, for example from blocks to."""
        self._request(
            "POST",
            "/rest/api/3/issueLink",
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": from_key},
                "outwardIssue": {"key": to_key},
            },
        )
        return {"status": "created", "from": from_key, "to": to_key}

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Return an offset slice of the JQL result, reading cursor pages only as needed."""
        if start_at < 0 or max_results < 0:
            raise ValueError("search offsets and limits must be non-negative")
        if jql not in self._searches:
            self._searches[jql] = ([], self._search_pages(jql))
        issues, pages = self._searches[jql]
        end = start_at + max_results
        try:
            while len(issues) < end:
                issues.append(next(pages))
        except StopIteration:
            pass
        except Exception:
            del self._searches[jql]
            raise
        return issues[start_at:end]

    def _search_pages(self, jql: str) -> Iterator[dict[str, Any]]:
        token = ""
        while True:
            body: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": list(SEARCH_FIELDS)}
            if token:
                body["nextPageToken"] = token
            page = self._request("POST", "/rest/api/3/search/jql", json=body)
            issues = page.get("issues")
            if not isinstance(issues, list) or any(
                not isinstance(issue, dict) or not issue.get("key") for issue in issues
            ):
                raise JiraCloudError("Jira Cloud returned a malformed search page")
            yield from issues
            token = str(page.get("nextPageToken") or "")
            if page.get("isLast", not token) or not token:
                return
