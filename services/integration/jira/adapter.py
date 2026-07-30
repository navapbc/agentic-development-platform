"""Facade over rebar's vendor Jira clients, selecting a backend by auth mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from scripts.config import Settings, get_settings

from services.integration.jira._engine import ensure_engine_on_path
from services.integration.jira.normalize import _name_of, _normalize_rich_text

CLOUD = "cloud"
DATACENTER = "datacenter"


class _VendorJiraClient(Protocol):
    def get_issue(self, remote_id: str) -> dict[str, Any]: ...

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]: ...

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None: ...


@dataclass(frozen=True)
class JiraAdapter:
    source: str
    client: _VendorJiraClient
    project: str = ""

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        return self.client.get_issue(issue_key)

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


def _site_url(settings: Settings) -> str:
    return settings.jira_base_url or settings.jira_url


def _build_datacenter_client(settings: Settings) -> _VendorJiraClient:
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira_datacenter.rest_client import JiraDataCenterClient

    return JiraDataCenterClient(
        base_url=_site_url(settings),
        pat=settings.jira_pat,
        project=settings.jira_project,
    )


def _build_cloud_client(settings: Settings) -> _VendorJiraClient:
    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira.acli import AcliClient

    acli_cmd = [settings.jira_acli_path] if settings.jira_acli_path else None
    return AcliClient(
        _site_url(settings),
        settings.jira_email,
        settings.jira_api_token,
        jira_project=settings.jira_project,
        acli_cmd=acli_cmd,
    )


def build_jira_adapter(settings: Settings | None = None) -> JiraAdapter:
    settings = settings or get_settings()
    mode = settings.jira_auth_mode.strip().lower()
    if mode == DATACENTER:
        return JiraAdapter(
            source=DATACENTER,
            client=_build_datacenter_client(settings),
            project=settings.jira_project,
        )
    if mode == CLOUD:
        return JiraAdapter(
            source=CLOUD,
            client=_build_cloud_client(settings),
            project=settings.jira_project,
        )
    raise ValueError(f"unsupported jira_auth_mode: {settings.jira_auth_mode!r}")
