from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql+asyncpg://platform:change-me-locally@localhost:5432/agent_platform"
    )

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-platform"
    review_task_queue: str = "agent-platform-review"
    agent_build_id: str = "dev"
    worker_versioning_enabled: bool = False
    workflow_execution_timeout_seconds: float = 3600.0
    draft_pr_execution_timeout_seconds: float = 86400.0

    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "change-me-locally"

    rebar_llm_base_url: str = "http://localhost:4000"
    rebar_llm_api_key: str = "change-me-locally"
    rebar_llm_model_provider: str = "openai"
    rebar_llm_max_tokens: int = 8192
    rebar_llm_temperature: float = 0.0
    rebar_review_native: bool = True
    review_attestation_secret: str = "change-me-locally"
    repair_analysis_use_llm: bool = True

    slack_webhook_url: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587

    aws_profile: str = "agentic-platform"
    aws_region: str = "us-east-1"
    bedrock_runtime_endpoint: str = "com.amazonaws.us-east-1.bedrock-runtime"
    bedrock_proxy_url: str = ""
    bedrock_runtime_host: str = "bedrock-runtime.us-east-1.amazonaws.com"
    bedrock_endpoint_url: str = ""
    bedrock_connectivity_provider: str = "jumphost"

    jumphost_name: str = ""
    jumphost_env: str = ""
    jumphost_connect_script: str = ""
    jumphost_session_duration_seconds: int = 0
    jumphost_keepalive_seconds: int = 0
    ssh_bastion_target: str = ""
    bedrock_tunnel_local_port: int = 8444

    @field_validator(
        "jumphost_session_duration_seconds",
        "jumphost_keepalive_seconds",
        "execution_orphan_grace_seconds",
        mode="before",
    )
    @classmethod
    def _blank_optional_int_to_default(cls, value: object, info: ValidationInfo) -> object:
        if info.field_name is not None and (value is None or value == ""):
            return cls.model_fields[info.field_name].default
        return value

    opa_url: str = "http://localhost:8181"
    langfuse_host: str = "http://localhost:3000"
    langfuse_project_id: str = "agentic-dev-platform"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    repo_intelligence_url: str = "http://localhost:8120"
    repo_intelligence_repo_root: str = ""
    repo_intelligence_commit: str = ""
    repo_intelligence_cbm_binary: str = ""

    coordination_url: str = "http://localhost:8130"
    repo_intelligence_repos: str = ""
    repo_index_poll_interval_seconds: float = 30.0
    repo_index_worker_concurrency: int = 2

    jira_acli_path: str = ""
    jira_parked_status: str = ""

    ticket_backend: str = "jira"
    source_control_backend: str = "github"
    source_control_local_root: str = ""
    source_control_local_artifacts_root: str = ""
    source_control_local_operators: str = ""
    source_control_local_repos: str = ""

    github_default_base_branch: str = "main"
    github_webhook_secret: str = ""
    jira_webhook_secret: str = ""


    semgrep_binary: str = "semgrep"
    trivy_binary: str = "trivy"
    security_blocking_threshold: str = "high"

    openhands_agent_server_image: str = "ghcr.io/openhands/agent-server:latest-python"
    openhands_runtime_class: str = ""
    openhands_sandbox_namespace: str = "agent-sandbox"
    openhands_server_port: int = 8000
    openhands_max_tool_calls: int = 60
    openhands_max_wall_clock_seconds: float = 1800.0
    openhands_in_cluster: bool = False
    coding_model: str = "coding-model"

    execution_workspace_root: str = ".agent-workspaces"
    implementation_max_cost_usd: float = 5.0
    implementation_max_llm_calls: int = 200
    implementation_max_tokens: int = 2_000_000
    implementation_activity_timeout_seconds: float = 2400.0
    sandbox_workspace_mount_path: str = "/workspace"
    sandbox_litellm_base_url: str = "http://host.docker.internal:4000"
    gate_engine_execution: str = "subprocess"
    gate_engine_namespace: str = "agent-sandbox"
    gate_engine_builder_image: str = "quay.io/buildah/stable:v1.43.2"
    gate_registry_tls_verify: bool = True

    reap_schedule_id: str = "orphan-reaper"
    reap_schedule_interval_minutes: int = 60
    reap_schedule_dry_run: bool = False

    intake_schedule_id: str = "jira-intake"
    intake_schedule_interval_seconds: int = 60
    intake_run_reviews: bool = False
    intake_run_policy: bool = False
    intake_run_approval: bool = False

    max_concurrent_runs: int = 4
    execution_orphan_grace_seconds: int = 120

    dependency_detection_enabled: bool = False
    dependency_replan_attempts: int = 3

    def repo_intelligence_registry(self) -> dict[str, tuple[str, str | None]]:
        """Parse configured repositories into a name to (root, commit) mapping."""
        registry: dict[str, tuple[str, str | None]] = {}
        raw = self.repo_intelligence_repos.replace("\n", ",").replace(";", ",")
        for item in raw.split(","):
            entry = item.strip()
            if not entry or "=" not in entry:
                continue
            name, _, location = entry.partition("=")
            path, _, commit = location.strip().partition("@")
            name, path = name.strip(), path.strip()
            if name and path:
                registry[name] = (path, commit.strip() or None)
        if not registry and self.repo_intelligence_repo_root:
            root = self.repo_intelligence_repo_root
            registry[Path(root).name] = (root, self.repo_intelligence_commit or None)
        return registry

    def bedrock_client_kwargs(self) -> dict[str, str]:
        """Boto3/BedrockConverseModel kwargs: region, plus endpoint_url when configured."""
        kwargs = {"region_name": self.aws_region}
        if self.bedrock_endpoint_url:
            kwargs["endpoint_url"] = self.bedrock_endpoint_url
        return kwargs


@lru_cache
def get_settings() -> Settings:
    return Settings()


_COMPOSE_SERVICE_HOSTS = frozenset(
    {
        "litellm",
        "opa",
        "langfuse-web",
        "postgres",
        "temporal",
        "langgraph-service",
        "otel-collector",
    }
)


def to_host_url(url: str) -> str:
    """Rewrite a Compose-internal service URL to its host-published localhost equivalent.

    Host-run tooling shares .env with in-network services, which address each other by
    Compose service name. Those names do not resolve on the host, where the same ports
    are published on localhost.
    """
    parsed = urlparse(url)
    if parsed.hostname in _COMPOSE_SERVICE_HOSTS:
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse(parsed._replace(netloc=f"{userinfo}localhost{port}"))
    return url


def to_host_hostport(value: str) -> str:
    """Rewrite a Compose-internal ``host:port`` (e.g. Temporal) to its localhost form."""
    host, sep, port = value.partition(":")
    if host in _COMPOSE_SERVICE_HOSTS:
        return f"localhost{sep}{port}"
    return value
