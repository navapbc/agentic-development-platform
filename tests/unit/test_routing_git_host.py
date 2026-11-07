from __future__ import annotations

import pytest
from adapters.real.routing_git_host import RoutingGitHost

_BACKENDS = {"service": "github", "notes": "local"}


class _Host:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []

    def source_location(self, repo: str) -> str:
        return f"{self.name}:{repo}"

    async def delete_branch(self, repo: str, name: str) -> bool:
        self.calls.append((repo, name))
        return True


def _routing(built: list[str]) -> tuple[RoutingGitHost, dict[str, _Host]]:
    hosts = {"github": _Host("github"), "local": _Host("local")}

    def factory(name: str):
        def build() -> _Host:
            built.append(name)
            return hosts[name]

        return build

    routing = RoutingGitHost(_BACKENDS.__getitem__, {name: factory(name) for name in hosts})
    return routing, hosts


def test_each_call_reaches_the_backend_its_repository_names() -> None:
    routing, _ = _routing([])

    assert routing.source_location("service") == "github:service"
    assert routing.source_location("notes") == "local:notes"


async def test_a_branch_call_is_sent_only_to_its_repository_backend() -> None:
    routing, hosts = _routing([])

    assert await routing.delete_branch("notes", "agent/ADEV-1") is True

    assert hosts["local"].calls == [("notes", "agent/ADEV-1")]
    assert hosts["github"].calls == []


def test_a_backend_is_built_once_and_only_when_a_repository_uses_it() -> None:
    built: list[str] = []
    routing, _ = _routing(built)

    routing.source_location("notes")
    routing.source_location("notes")

    assert built == ["local"]


def test_a_repository_outside_the_registry_is_refused() -> None:
    routing, _ = _routing([])

    with pytest.raises(KeyError):
        routing.source_location("rogue")
