from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from scripts import fetch_results, repos_sync
from scripts.k8s_cluster import worker_registry
from services import process
from tests.support import hermetic_settings, local_tenants


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch):
    """Run each `kubectl exec ... -- sh -c SCRIPT` locally, with the pod's roots under tmp_path."""
    local_tenants(monkeypatch, tmp_path, "adp")
    pod = tmp_path / "pod"
    environment = {
        **os.environ,
        "REPOSITORY_ROOT": str(pod / "repositories"),
        "GATE_ENGINE_ROOT": str(pod / "engines"),
    }

    def run(argv, **kwargs):
        if argv[0] == "kubectl":
            argv = argv[argv.index("--") + 1 :]
            kwargs = {**kwargs, "env": environment}
        return process.run(argv, **kwargs)

    host = tmp_path / "host"
    host.mkdir()
    _git(host, "init", "-q", "-b", "main")
    _commit(host, "README.md", "base\n")
    return pod, host, run


def test_a_sync_copies_the_branches_and_checks_out_the_default(cluster) -> None:
    pod, host, run = cluster
    _git(host, "branch", "agent/T-1")

    result = repos_sync.sync_repository(hermetic_settings(tenant="adp"), "demo", host, "main", run)

    copy = pod / "repositories" / "demo"
    assert result.startswith("sent")
    assert _git(copy, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert (copy / "README.md").read_text() == "base\n"


def test_a_second_sync_sends_nothing_until_the_host_moves(cluster) -> None:
    pod, host, run = cluster
    settings = hermetic_settings(tenant="adp")
    repos_sync.sync_repository(settings, "demo", host, "main", run)

    assert repos_sync.sync_repository(settings, "demo", host, "main", run) == "up to date"

    head = _commit(host, "next.md", "next\n")
    repos_sync.sync_repository(settings, "demo", host, "main", run)
    assert _git(pod / "repositories" / "demo", "rev-parse", "main") == head


def test_a_sync_never_replaces_an_agent_branch_in_the_copy(cluster) -> None:
    pod, host, run = cluster
    settings = hermetic_settings(tenant="adp")
    repos_sync.sync_repository(settings, "demo", host, "main", run)
    copy = pod / "repositories" / "demo"
    agent = _git(copy, "rev-parse", "main")
    _git(copy, "branch", "agent/T-2", agent)
    _git(host, "branch", "agent/T-2")
    _commit(host, "host-only.md", "x\n")
    _git(host, "branch", "-f", "agent/T-2", "main")

    repos_sync.sync_repository(settings, "demo", host, "main", run)

    assert _git(copy, "rev-parse", "agent/T-2") == agent


def test_fetch_results_brings_the_agent_branch_and_its_results_ref_home(cluster) -> None:
    pod, host, run = cluster
    settings = hermetic_settings(tenant="adp")
    repos_sync.sync_repository(settings, "demo", host, "main", run)
    copy = pod / "repositories" / "demo"
    _git(copy, "checkout", "-q", "-b", "agent/T-3")
    change = _commit(copy, "feature.md", "feature\n")
    _git(copy, "update-ref", "refs/agent-results/agent/T-3", change)
    _git(copy, "checkout", "-q", "--detach", "main")

    result = fetch_results.fetch_repository(settings, "demo", host, run)

    assert "agent/T-3" in result
    assert _git(host, "rev-parse", "agent/T-3") == change
    assert _git(host, "rev-parse", "refs/agent-results/agent/T-3") == change


def test_fetch_results_reports_a_copy_with_no_agent_branches(cluster) -> None:
    _, host, run = cluster
    settings = hermetic_settings(tenant="adp")
    repos_sync.sync_repository(settings, "demo", host, "main", run)

    assert fetch_results.fetch_repository(settings, "demo", host, run) == "no agent branches"


def test_an_engine_sync_carries_uncommitted_edits_and_the_git_directory(cluster) -> None:
    pod, host, run = cluster
    (host / "README.md").write_text("edited\n")
    (host / "new.md").write_text("untracked\n")
    (host / ".gitignore").write_text("ignored.md\n")
    (host / "ignored.md").write_text("no\n")

    repos_sync.sync_engine(hermetic_settings(tenant="adp"), "review", host, run)

    copy = pod / "engines" / "review"
    assert (copy / "README.md").read_text() == "edited\n"
    assert (copy / "new.md").read_text() == "untracked\n"
    assert not (copy / "ignored.md").exists()
    assert _git(copy, "rev-parse", "HEAD") == _git(host, "rev-parse", "HEAD")


def _registry_root(tmp_path: Path, *, beta: bool = True) -> Path:
    root = tmp_path / "checkout"
    tenants = root / "config" / "local" / "tenants"
    for tenant in ("adp", "beta"):
        (tenants / tenant).mkdir(parents=True)
        (tenants / f"{tenant}.env").touch()
    (tenants / "adp" / "repositories.yaml").write_text(
        "config_version: 1\nrepositories:\n  adp-web:\n    backend: local\n    path: /src/adp-web\n"
    )
    if beta:
        (tenants / "beta" / "repositories.yaml").write_text(
            "config_version: 1\nrepositories:\n  beta-web:\n    backend: local\n"
            "    path: /src/beta-web\n"
        )
    return root


def test_a_host_task_reads_the_repositories_of_the_tenant_whose_worker_it_reaches(
    tmp_path: Path,
) -> None:
    root = _registry_root(tmp_path)

    beta = worker_registry(hermetic_settings(tenant="beta"), root)
    adp = worker_registry(hermetic_settings(tenant="adp"), root)

    assert sorted(beta.repositories) == ["beta-web"]
    assert sorted(adp.repositories) == ["adp-web"]


def test_a_host_task_for_a_tenant_without_its_own_repositories_is_refused(
    tmp_path: Path,
) -> None:
    root = _registry_root(tmp_path, beta=False)

    with pytest.raises(SystemExit, match="tenant beta names no repositories"):
        worker_registry(hermetic_settings(tenant="beta"), root)


def test_a_host_task_without_a_tenant_reaches_the_one_tenant_and_refuses_two(
    tmp_path: Path,
) -> None:
    root = _registry_root(tmp_path)

    with pytest.raises(SystemExit, match="2 local tenants exist, adp, beta"):
        worker_registry(hermetic_settings(), root)

    (root / "config" / "local" / "tenants" / "beta" / "repositories.yaml").unlink()
    (root / "config" / "local" / "tenants" / "beta").rmdir()
    (root / "config" / "local" / "tenants" / "beta.env").unlink()
    assert sorted(worker_registry(hermetic_settings(), root).repositories) == ["adp-web"]


@pytest.mark.parametrize("module", [repos_sync, fetch_results])
def test_sync_and_fetch_send_the_target_tenants_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module
) -> None:
    from scripts import env_file, k8s_cluster
    from services.orchestration.gates import engine_source

    root = _registry_root(tmp_path)
    checkout = tmp_path / "beta-web"
    (checkout / ".git").mkdir(parents=True)
    (root / "config" / "local" / "tenants" / "beta" / "repositories.yaml").write_text(
        f"config_version: 1\nrepositories:\n  beta-web:\n    backend: local\n    path: {checkout}\n"
    )
    sent: list[str] = []
    monkeypatch.setattr(k8s_cluster, "REPO_ROOT", root)
    monkeypatch.setattr(env_file, "CHECKOUT_ROOT", root)
    monkeypatch.setattr(module, "get_settings", lambda: hermetic_settings(tenant="beta"))
    monkeypatch.setattr(engine_source, "load_gate_engine_configs", lambda settings: ())
    for name in ("sync_repository", "fetch_repository"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda settings, repo, *_: sent.append(repo) or "ok")

    assert module.main([]) == 0
    assert sent == ["beta-web"]
