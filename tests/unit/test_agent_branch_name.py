"""Branch naming puts each tenant's agent branch under its own namespace."""

from __future__ import annotations

from adapters.interfaces.git_host import agent_branch_name


def test_platform_tenant_keeps_the_flat_form() -> None:
    assert agent_branch_name("AGENTDEV-1") == "agent/AGENTDEV-1"
    assert agent_branch_name("AGENTDEV-1", "") == "agent/AGENTDEV-1"


def test_named_tenant_is_namespaced() -> None:
    assert agent_branch_name("AGENTDEV-1", "beta") == "agent/beta/AGENTDEV-1"


def test_two_tenants_with_the_same_task_id_diverge() -> None:
    alpha = agent_branch_name("AGENTDEV-1", "alpha")
    beta = agent_branch_name("AGENTDEV-1", "beta")
    assert alpha != beta
    assert alpha.startswith("agent/")
    assert beta.startswith("agent/")
