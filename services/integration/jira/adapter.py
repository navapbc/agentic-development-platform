"""Facade over rebar's vendor Jira clients, selecting a backend by auth mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from scripts.config import Settings, get_settings

from services.integration.jira._engine import ensure_engine_on_path

CLOUD = "cloud"
DATACENTER = "datacenter"


class _VendorJiraClient(Protocol):
    def get_issue(self, remote_id: str) -> dict[str, Any]: ...

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class JiraAdapter:
    source: str
    client: _VendorJiraClient

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        return self.client.get_issue(issue_key)

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        return self.client.add_comment(issue_key, body)


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
        return JiraAdapter(source=DATACENTER, client=_build_datacenter_client(settings))
    if mode == CLOUD:
        return JiraAdapter(source=CLOUD, client=_build_cloud_client(settings))
    raise ValueError(f"unsupported jira_auth_mode: {settings.jira_auth_mode!r}")
