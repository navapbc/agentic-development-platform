"""Derive the local cluster's environment from .env and the compose stack's published ports.

The in-cluster workers read the settings the compose services read from `.env`, with each
compose service address replaced by the address the pods reach the compose stack on and the
port compose publishes. LiteLLM runs in the cluster under the same Service name, and OPA runs
beside the activity worker on loopback. Settings that only name a workstation resource, such
as the AWS profile or a kubeconfig, are dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from services import process

from scripts.config import Settings

IN_CLUSTER_SERVICES = frozenset({"litellm"})
LOOPBACK_SERVICES = frozenset({"opa"})
WORKSTATION_SETTINGS = frozenset(
    {
        "AWS_PROFILE",
        "PLATFORM_AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "KUBECONFIG",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "REPOSITORY_ROOT",
        "EXECUTION_WORKSPACE_ROOT",
    }
)
_ADDRESS = re.compile(r"(?P<lead>^|[/@])(?P<host>[a-z][a-z0-9-]*):(?P<port>\d+)")


@dataclass(frozen=True)
class ComposeNetwork:
    """The compose services and the host port each published container port maps to."""

    services: frozenset[str]
    ports: Mapping[str, Mapping[int, int]]

    def published(self, service: str, port: int) -> int | None:
        return self.ports.get(service, {}).get(port)


@dataclass
class Rewrite:
    values: dict[str, str] = field(default_factory=dict)
    unreachable: set[str] = field(default_factory=set)


def compose_network(compose_config: Mapping) -> ComposeNetwork:
    services = compose_config.get("services") or {}
    ports: dict[str, dict[int, int]] = {}
    for name, service in services.items():
        for entry in service.get("ports") or []:
            published = entry.get("published")
            if published in (None, ""):
                continue
            ports.setdefault(name, {})[int(entry["target"])] = int(published)
    return ComposeNetwork(services=frozenset(services), ports=ports)


def rewrite_address(value: str, network: ComposeNetwork, address: str) -> tuple[str, set[str]]:
    """Point every compose service address in value at the address pods reach it on."""
    unreachable: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        host, port = match["host"], int(match["port"])
        if host in IN_CLUSTER_SERVICES:
            return match[0]
        if host in LOOPBACK_SERVICES:
            return f"{match['lead']}127.0.0.1:{port}"
        published = network.published(host, port)
        if published is None:
            if host in network.services:
                unreachable.add(f"{host}:{port}")
            return match[0]
        return f"{match['lead']}{address}:{published}"

    return _ADDRESS.sub(replace, value), unreachable


def rewrite_env(env: Mapping[str, str | None], network: ComposeNetwork, address: str) -> Rewrite:
    result = Rewrite()
    for key, raw in env.items():
        if key in WORKSTATION_SETTINGS or raw is None:
            continue
        value, unreachable = rewrite_address(raw, network, address)
        if "\n" in value:
            raise ValueError(f"{key} holds a multi-line value, which a kustomize env file cannot")
        result.values[key] = value
        result.unreachable |= {f"{key} -> {target}" for target in unreachable}
    return result


def endpoint_values(settings: Settings, network: ComposeNetwork, address: str) -> dict[str, str]:
    """Return the compose endpoints the workload manifests read from the agentic-platform map."""

    def endpoint(service: str, port: int, scheme: str = "http://") -> str:
        published = network.published(service, port)
        if published is None:
            raise ValueError(
                f"compose does not publish {service} port {port}, which the cluster reaches"
            )
        return f"{scheme}{address}:{published}"

    return {
        "repo_intelligence_url": endpoint("repo-intelligence", 8120),
        "coordination_url": endpoint("coordination-service", 8130),
        "temporal_host": endpoint("temporal", 7233, scheme=""),
        "temporal_namespace": settings.temporal_namespace,
    }


def workload_secret_values(
    env: Mapping[str, str], network: ComposeNetwork, address: str
) -> dict[str, str]:
    database_url, _ = rewrite_address(env.get("DATABASE_URL", ""), network, address)
    return {
        "database_url": database_url,
        "github_webhook_secret": env.get("GITHUB_WEBHOOK_SECRET") or "local-demo-secret",
        "jira_webhook_secret": env.get("JIRA_WEBHOOK_SECRET") or "local-demo-secret",
        "coordination_shared_secret": env.get("COORDINATION_SHARED_SECRET", ""),
    }


def compose_config(files: list[str]) -> dict:
    arguments = [part for name in files for part in ("-f", name)]
    result = process.run(
        ["docker", "compose", *arguments, "config", "--format", "json"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)
