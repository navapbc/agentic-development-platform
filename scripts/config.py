from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://platform:change-me-locally@localhost:5432/agent_platform"

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-platform"

    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "change-me-locally"

    aws_profile: str = "mpse-legacy"
    aws_region: str = "us-east-1"
    bedrock_runtime_endpoint: str = "com.amazonaws.us-east-1.bedrock-runtime"
    bedrock_proxy_url: str = ""
    bedrock_runtime_host: str = "bedrock-runtime.us-east-1.amazonaws.com"

    jumphost_name: str = "osre-opis-jumphost"
    jumphost_env: str = "util"
    jumphost_connect_script: str = ""
    bedrock_tunnel_local_port: int = 8443

    opa_url: str = "http://localhost:8181"
    langfuse_host: str = "http://localhost:3000"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    allowlist_jira_projects: str = "AGENTDEV"
    allowlist_repos: str = "agentic-demo-service,agentic-demo-greenfield"

    def allowed_projects(self) -> set[str]:
        return {p.strip() for p in self.allowlist_jira_projects.split(",") if p.strip()}

    def allowed_repos(self) -> set[str]:
        return {r.strip() for r in self.allowlist_repos.split(",") if r.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


_COMPOSE_SERVICE_HOSTS = frozenset(
    {"litellm", "opa", "langfuse", "postgres", "temporal", "langgraph-service", "otel-collector"}
)


def to_host_url(url: str) -> str:
    """Rewrite a Compose-internal service URL to its host-published localhost equivalent.

    Host-run tooling shares .env with in-network services, which address each other by
    Compose service name. Those names do not resolve on the host, where the same ports
    are published on localhost.
    """
    parsed = urlparse(url)
    if parsed.hostname in _COMPOSE_SERVICE_HOSTS:
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse(parsed._replace(netloc=f"localhost{port}"))
    return url


def to_host_hostport(value: str) -> str:
    """Rewrite a Compose-internal ``host:port`` (e.g. Temporal) to its localhost form."""
    host, sep, port = value.partition(":")
    if host in _COMPOSE_SERVICE_HOSTS:
        return f"localhost{sep}{port}"
    return value
