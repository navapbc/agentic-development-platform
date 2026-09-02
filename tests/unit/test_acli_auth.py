import contextlib

import services.integration.jira.acli_auth as acli_auth
from services.integration.jira.credentials import JiraProjectCredential


def _cred() -> JiraProjectCredential:
    return JiraProjectCredential(
        url="https://site.atlassian.net",
        auth_mode="cloud",
        email="agent@example",
        token="tok",
        board="ADSER",
        project="ADSER",
    )


def test_cached_auth_skips_within_ttl(monkeypatch):
    monkeypatch.setattr(acli_auth, "_last_ensured", {})
    calls: list[str] = []
    monkeypatch.setattr(
        acli_auth,
        "ensure_acli_authenticated",
        lambda cred, *, acli_path="": calls.append(acli_path) or True,
    )

    assert acli_auth.ensure_acli_authenticated_cached(_cred(), acli_path="acli") is True
    assert acli_auth.ensure_acli_authenticated_cached(_cred(), acli_path="acli") is False
    assert calls == ["acli"]


def test_cached_auth_rechecks_after_ttl(monkeypatch):
    monkeypatch.setattr(acli_auth, "_last_ensured", {})
    calls: list[str] = []
    monkeypatch.setattr(
        acli_auth,
        "ensure_acli_authenticated",
        lambda cred, *, acli_path="": calls.append(acli_path) or True,
    )

    acli_auth.ensure_acli_authenticated_cached(_cred(), acli_path="acli", ttl_seconds=0.0)
    acli_auth.ensure_acli_authenticated_cached(_cred(), acli_path="acli", ttl_seconds=0.0)
    assert len(calls) == 2


def test_cached_auth_does_not_cache_on_failure(monkeypatch):
    monkeypatch.setattr(acli_auth, "_last_ensured", {})

    def boom(cred, *, acli_path=""):
        raise RuntimeError("login failed")

    monkeypatch.setattr(acli_auth, "ensure_acli_authenticated", boom)

    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            acli_auth.ensure_acli_authenticated_cached(_cred(), acli_path="acli")
    assert acli_auth._last_ensured == {}
