"""Shared test helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts.config import Settings
from services.review.evidence import EvidenceRecord

SHARED_APP_ID = "A0SHARED001"
SHARED_ORGANIZATION_ID = "E0SHARED001"
SHARED_APP_YAML = (
    f"config_version: 1\napp_id: {SHARED_APP_ID}\norganization_id: {SHARED_ORGANIZATION_ID}\n"
    "api_base_url: https://slack.com/api/\n"
)


def hermetic_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def capture_evidence(monkeypatch: pytest.MonkeyPatch) -> list[EvidenceRecord]:
    """Swap the Postgres evidence sink for a recorder and return the records it collects.

    ``build_evidence_sink`` resolves ``PostgresEvidenceSink`` by name at call time, so
    patching the attribute reaches every producer without touching the factory.
    """
    records: list[EvidenceRecord] = []

    class _RecordingSink:
        async def persist(self, record: EvidenceRecord) -> str:
            records.append(record)
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _RecordingSink)
    return records


def seed_rebar_store(root: Path, tickets: list[dict] | None = None) -> list[str]:
    """Init a local rebar store with a reader identity and a few tickets for tests.

    Kept independent of the xlsx importer so tests do not depend on any export.
    """
    import rebar

    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "agent-platform@users.noreply.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "agent-platform"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root), force_new_store=True)
    if not rebar.resolve_current_identity(repo_root=str(root)):
        identity_id = rebar.create_identity(
            "agent-platform", "agent-platform@users.noreply.invalid", repo_root=str(root)
        )
        rebar.use_identity(str(identity_id), repo_root=str(root))

    specs = tickets if tickets is not None else _DEFAULT_TICKETS
    created: list[str] = []
    for spec in specs:
        created.append(
            str(
                rebar.create_ticket(
                    spec.get("ticket_type", "task"),
                    spec["title"],
                    description=spec.get("description"),
                    priority=spec.get("priority", 2),
                    repos=spec.get("repos"),
                    tags=spec.get("tags"),
                    repo_root=str(root),
                )
            )
        )
    return created


_DEFAULT_TICKETS: list[dict] = [
    {
        "ticket_type": "task",
        "title": "Add pagination to the task list endpoint",
        "description": "Add limit and offset to GET /tasks.",
        "priority": 2,
        "repos": ["agentic-demo-service"],
        "tags": ["enhancement", "api"],
    },
    {
        "ticket_type": "task",
        "title": "Add due-date filtering to task queries",
        "description": "Add due_before and due_after filters.",
        "priority": 3,
        "repos": ["agentic-demo-service"],
        "tags": ["enhancement", "api"],
    },
]


TEST_TENANT = "adp"


def configure_tenant(
    monkeypatch: pytest.MonkeyPatch, directory: Path, tenant: str = TEST_TENANT
) -> Path:
    """Serve the tenant with the platform config in directory, and return its bundle directory.

    A tenant-only file is read only from the tenant's bundle, so a test of a settings-driven
    loader writes it into the returned directory.
    """
    from scripts.config import get_settings

    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(directory))
    monkeypatch.setenv("TENANT", tenant)
    get_settings.cache_clear()
    bundle = directory / "tenants" / tenant
    bundle.mkdir(parents=True, exist_ok=True)
    return bundle


def tenant_dir(directory: Path, tenant: str = TEST_TENANT) -> Path:
    """Return the bundle directory of the tenant under the platform directory, created."""
    bundle = directory / "tenants" / tenant
    bundle.mkdir(parents=True, exist_ok=True)
    return bundle


def local_tenants(monkeypatch: pytest.MonkeyPatch, root: Path, *names: str) -> Path:
    """Make root the checkout whose config/local/tenants/ holds each named tenant.

    Each tenant gets its folder and an empty settings file. Return the tenants directory.
    """
    import scripts.env_file

    monkeypatch.setattr(scripts.env_file, "CHECKOUT_ROOT", root)
    tenants = root / "config" / "local" / "tenants"
    tenants.mkdir(parents=True, exist_ok=True)
    for name in names:
        (tenants / name).mkdir(exist_ok=True)
        (tenants / f"{name}.env").touch()
    return tenants


def configure_repositories(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    repositories: str,
    *,
    github: str | None = None,
) -> Path:
    """Write repositories.yaml, and github.yaml when given, into the test tenant's bundle.

    The platform config is directory, so a test that calls a settings-driven factory sees these
    files and not the operator's own. Return the bundle directory.
    """
    bundle = configure_tenant(monkeypatch, directory)
    (bundle / "repositories.yaml").write_text(
        "config_version: 1\n" + repositories, encoding="utf-8"
    )
    if github is not None:
        (bundle / "github.yaml").write_text("config_version: 2\n" + github, encoding="utf-8")
    return bundle


# Admits the images the settings default to, so a test of another concern validates its bundle.
PERMISSIVE_IMAGE_POLICY = """config_version: 1
registries: [docker.io/, quay.io/, ghcr.io/, host.docker.internal:5001/, registry.example/]
"""


def write_image_policy(directory: Path, policy: str = PERMISSIVE_IMAGE_POLICY) -> None:
    (directory / "image_policy.yaml").write_text(policy, encoding="utf-8")


class InMemoryModelPathEpisodes:
    def __init__(self) -> None:
        self.open_episodes: dict[str, Any] = {}
        self.closed: list[Any] = []
        self.resolvers: list[str] = []

    async def open(self, tenant: str, hop: str) -> Any:
        from services.orchestration.model_path import ModelPathEpisode

        episode = self.open_episodes.get(tenant)
        if episode is None:
            episode = ModelPathEpisode(episode=f"episode-{len(self.closed) + 1}", hop=hop)
            self.open_episodes[tenant] = episode
        return episode

    async def current(self, tenant: str) -> Any:
        return self.open_episodes.get(tenant)

    async def close(self, tenant: str, episode: str, *, resolved_by: str = "") -> None:
        current = self.open_episodes.get(tenant)
        if current is not None and current.episode == episode:
            self.closed.append(self.open_episodes.pop(tenant))
            self.resolvers.append(resolved_by)

    async def record_post(self, tenant: str, episode: str, post: Any) -> None:
        if getattr(post, "admin", False):
            self._replace(
                tenant,
                episode,
                admin_slack_channel=post.channel,
                admin_slack_ts=post.ts,
                admin_slack_token=post.owner_token,
                admin_slack_blocks=post.blocks,
            )
        else:
            self._replace(
                tenant,
                episode,
                slack_channel=post.channel,
                slack_ts=post.ts,
                slack_blocks=post.blocks,
            )

    async def resolved(self, tenant: str, episode: str) -> Any:
        return next((closed for closed in self.closed if closed.episode == episode), None)

    async def mark_host_action_posted(self, tenant: str, episode: str, at: Any) -> None:
        current = self.open_episodes.get(tenant)
        if current is not None and current.host_action_at == at:
            self._replace(tenant, episode, host_action_posted_at=at)

    def record_host_action(self, text: str, at: Any) -> int:
        written = 0
        for tenant, current in list(self.open_episodes.items()):
            if current.host_action != text:
                self._replace(tenant, current.episode, host_action=text, host_action_at=at)
                written += 1
        return written

    async def acknowledge(self, tenant: str, episode: str, actor: str, at: Any) -> Any:
        return self._replace(tenant, episode, acknowledged_by=actor, acknowledged_at=at)

    def _replace(self, tenant: str, episode: str, **values: Any) -> Any:
        from dataclasses import replace

        current = self.open_episodes.get(tenant)
        if current is None or current.episode != episode:
            return None
        self.open_episodes[tenant] = replace(current, **values)
        return self.open_episodes[tenant]


class ModelPathStub:
    def __init__(self, hop: str = "") -> None:
        self.hop = hop
        self.probes = 0
        self.now = 0.0

    async def probe(self) -> Any:
        from services.orchestration.model_path import ModelPathHealth

        self.probes += 1
        return ModelPathHealth(hop=self.hop, error=f"{self.hop} failed" if self.hop else "")

    def next_pass(self) -> None:
        from services.orchestration.model_path import PROBE_CACHE_SECONDS

        self.now += PROBE_CACHE_SECONDS


def model_path_gate(
    path: ModelPathStub,
    *,
    ledger: Any,
    settings: Settings | None = None,
    episodes: InMemoryModelPathEpisodes | None = None,
    parks: Any = None,
) -> Any:
    from services.orchestration.intake_runtime import ModelPathNotifier
    from services.orchestration.model_path import ModelPathGate, ModelPathProbe

    resolved = settings or hermetic_settings()
    return ModelPathGate(
        probe=ModelPathProbe(path.probe, clock=lambda: path.now),
        episodes=episodes if episodes is not None else InMemoryModelPathEpisodes(),
        alerter=ModelPathNotifier(resolved),
        ledger=ledger,
        tenant=resolved.tenant,
        parks=parks,
    )


def capture_alerts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    messages: list[Any] = []

    def _deliver(message: Any, settings: Any = None) -> list[str]:
        messages.append(message)
        return ["slack"]

    monkeypatch.setattr("services.orchestration.notifications.deliver_alert", _deliver)
    return messages
