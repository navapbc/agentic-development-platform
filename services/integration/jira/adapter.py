"""Facade over rebar's vendor Jira clients, selecting a backend by the resolved project site."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from scripts.config import Settings, get_settings

from services.integration.jira._engine import ensure_engine_on_path
from services.integration.jira.acli_auth import ensure_acli_authenticated_cached
from services.integration.jira.credentials import (
    CLOUD,
    DATACENTER,
    JiraCredentialResolver,
    JiraProjectCredential,
    UnconfiguredProjectError,
    build_jira_credential_resolver,
)
from services.integration.jira.markdown_adf import markdown_to_adf_json
from services.integration.jira.normalize import _name_of, _normalize_rich_text

if TYPE_CHECKING:
    from services.integration.jira.render import Block


class _VendorJiraClient(Protocol):
    def get_issue(self, remote_id: str, /) -> dict[str, Any]: ...

    def add_comment(self, remote_id: str, body: str, /) -> dict[str, Any]: ...

    def transition_issue_by_name(self, remote_id: str, target_status: str, /) -> None: ...

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]: ...

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]: ...

    def set_relationship(
        self, from_key: str, to_key: str, link_type: str = "Blocks", /
    ) -> dict[str, Any]: ...

    def get_issue_links(self, remote_id: str, /) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class JiraAdapter:
    source: str
    client: _VendorJiraClient
    project: str = ""
    dependency_link_type: str = ""

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        return self.client.get_issue(issue_key)

    def recheck_payload(
        self, issue_key: str, *, raw_comments: Sequence[Any] = ()
    ) -> dict[str, Any]:
        from services.integration.jira.credentials import acceptance_field
        from services.integration.jira.normalize import normalize_issue

        return normalize_issue(
            self.get_issue(issue_key),
            raw_comments=tuple(raw_comments),
            acceptance_field=acceptance_field(get_settings()),
        ).to_dict()

    def search_issues(self, jql: str, *, page_size: int = 50) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self.client.search_issues(jql, start_at=start, max_results=page_size)
            issues.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        return issues

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        if self.source == CLOUD and ticket_data.get("description"):
            ticket_data = {
                **ticket_data,
                "description": markdown_to_adf_json(str(ticket_data["description"])),
            }
        return self.client.create_issue(ticket_data)

    def get_status(self, issue_key: str) -> str:
        fields = self.get_issue(issue_key).get("fields") or {}
        return _name_of(fields.get("status"))

    def transition(self, issue_key: str, target_status: str) -> None:
        self.client.transition_issue_by_name(issue_key, target_status)

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        return self.client.add_comment(issue_key, body)

    def list_comment_bodies(self, issue_key: str) -> tuple[str, ...]:
        if self.source == CLOUD:
            raw = self.client.get_comments(issue_key)
        else:
            entry = self.client.get_comment_map(self.project).get(issue_key) or {}
            raw = entry.get("comments") or []
        return tuple(_normalize_rich_text(item.get("body")) for item in raw)

    def render_comment(self, document: list[Block]) -> str:
        from services.integration.jira.linkback import render_linkback

        return render_linkback(document, source=self.source)

    def dependency_graph_active(self) -> bool:
        return bool(self.dependency_link_type)

    def link(self, from_issue: str, to_issue: str, *, relationship: str = "depends_on") -> None:
        if not self.dependency_link_type:
            return
        if to_issue in self.declared_dependencies(from_issue):
            return
        self.client.set_relationship(to_issue, from_issue, self.dependency_link_type)

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return self._linked_keys(issue_key, "inwardIssue")

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        return self._linked_keys(issue_key, "outwardIssue")

    def _linked_keys(self, issue_key: str, side: str) -> tuple[str, ...]:
        if not self.dependency_link_type:
            return ()
        keys: set[str] = set()
        for link in self.client.get_issue_links(issue_key):
            link_type = (link.get("type") or {}).get("name")
            if link_type != self.dependency_link_type:
                continue
            linked = link.get(side) or {}
            key = linked.get("key")
            if key:
                keys.add(str(key))
        return tuple(sorted(keys))


def _build_datacenter_client(cred: JiraProjectCredential) -> _VendorJiraClient:
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = JiraDataCenterSettings(
        url=cred.url,
        project=cred.project,
        allow_insecure=False,
        ca_bundle="",
        pat=cred.token,
    )
    client = build_client_from_settings(settings)
    return JiraDataCenterTransport(client=client, project=cred.project)


def _build_cloud_client(cred: JiraProjectCredential, *, acli_path: str) -> _VendorJiraClient:
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.acli import AcliClient

    ensure_acli_authenticated_cached(cred, acli_path=acli_path)
    acli_cmd = [acli_path] if acli_path else None
    return AcliClient(
        cred.url,
        cred.email,
        cred.token,
        jira_project=cred.project,
        acli_cmd=acli_cmd,
    )


def project_of(issue_key: str) -> str:
    return issue_key.split("-", 1)[0]


def build_jira_adapter(
    settings: Settings | None = None,
    *,
    project: str,
    resolver: JiraCredentialResolver | None = None,
) -> JiraAdapter:
    resolved = settings or get_settings()
    resolver = resolver or build_jira_credential_resolver(resolved)
    cred = resolver.resolve(project)
    if cred is None:
        raise UnconfiguredProjectError(project)
    if cred.auth_mode == DATACENTER:
        client = _build_datacenter_client(cred)
    else:
        client = _build_cloud_client(cred, acli_path=resolved.jira_acli_path)
    return JiraAdapter(
        source=cred.auth_mode,
        client=client,
        project=cred.project,
        dependency_link_type=cred.dependency_link_type,
    )


def build_jira_adapter_for_issue(
    issue_key: str,
    settings: Settings | None = None,
    *,
    resolver: JiraCredentialResolver | None = None,
) -> JiraAdapter:
    return build_jira_adapter(settings, project=project_of(issue_key), resolver=resolver)


def apply_status(reader: Any, issue_key: str, target_status: str) -> bool:
    if not target_status:
        return False
    current = reader.get_status(issue_key)
    if current.strip().lower() == target_status.strip().lower():
        return False
    reader.transition(issue_key, target_status)
    return True


class ProjectRoutingJiraReader:
    """Resolves a per-project Jira adapter for each issue key it reads."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._resolver = build_jira_credential_resolver(self._settings)

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        adapter = build_jira_adapter_for_issue(issue_key, self._settings, resolver=self._resolver)
        return adapter.get_issue(issue_key)
