"""The Jira dependency-graph capability over issue links."""

from __future__ import annotations

from services.integration.jira.adapter import JiraAdapter
from services.integration.tickets import DependencyBackend


class _FakeClient:
    def __init__(self, links: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._links = dict(links or {})

    def set_relationship(self, from_key: str, to_key: str, link_type: str = "Blocks") -> dict:
        self.calls.append((from_key, to_key, link_type))
        self._links.setdefault(from_key, []).append(
            {"type": {"name": link_type}, "outwardIssue": {"key": to_key}}
        )
        self._links.setdefault(to_key, []).append(
            {"type": {"name": link_type}, "inwardIssue": {"key": from_key}}
        )
        return {}

    def get_issue_links(self, remote_id: str) -> list[dict]:
        return self._links.get(remote_id, [])


def _adapter(client: _FakeClient, link_type: str = "Blocks") -> JiraAdapter:
    return JiraAdapter(
        source="datacenter", client=client, project="P", dependency_link_type=link_type
    )


def test_jira_adapter_is_a_dependency_backend():
    assert isinstance(_adapter(_FakeClient()), DependencyBackend)


def test_link_creates_a_blocking_relationship():
    client = _FakeClient()
    _adapter(client).link("D-1", "B-2", relationship="depends_on")
    assert client.calls == [("B-2", "D-1", "Blocks")]


def test_link_is_idempotent():
    client = _FakeClient()
    adapter = _adapter(client)
    adapter.link("D-1", "B-2", relationship="depends_on")
    adapter.link("D-1", "B-2", relationship="depends_on")
    assert client.calls == [("B-2", "D-1", "Blocks")]


def test_declared_dependencies_reads_inward_blockers():
    client = _FakeClient(
        links={"D-1": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "B-2"}}]}
    )
    assert _adapter(client).declared_dependencies("D-1") == ("B-2",)


def test_dependents_reads_outward_dependents():
    client = _FakeClient(
        links={"B-2": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "D-1"}}]}
    )
    assert _adapter(client).dependents("B-2") == ("D-1",)


def test_other_link_types_are_ignored():
    client = _FakeClient(
        links={"D-1": [{"type": {"name": "Relates"}, "inwardIssue": {"key": "X-9"}}]}
    )
    assert _adapter(client).declared_dependencies("D-1") == ()


def test_no_links_report_empty():
    adapter = _adapter(_FakeClient())
    assert adapter.declared_dependencies("D-1") == ()
    assert adapter.dependents("D-1") == ()


def test_graph_active_with_a_configured_link_type():
    assert _adapter(_FakeClient(), link_type="Blocks").dependency_graph_active() is True


def test_graph_inactive_without_a_link_type():
    client = _FakeClient(
        links={"D-1": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "B-2"}}]}
    )
    adapter = _adapter(client, link_type="")
    assert adapter.dependency_graph_active() is False
    adapter.link("D-1", "B-2", relationship="depends_on")
    assert client.calls == []
    assert adapter.declared_dependencies("D-1") == ()
