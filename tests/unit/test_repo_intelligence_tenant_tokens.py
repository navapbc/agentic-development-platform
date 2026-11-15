"""Unit tests for the bearer tokens that bind a repository intelligence request to a tenant."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from services.repository_intelligence import tenant_tokens
from services.repository_intelligence.tenant_tokens import (
    derive_token,
    single_tokens,
    tenant_for_token,
    token_env_name,
)
from services.repository_intelligence.tenant_tokens import (
    tenant_tokens as build_tokens,
)


def test_token_env_name_upper_cases_the_tenant():
    assert token_env_name("beta") == "REPO_INTELLIGENCE_TOKEN_BETA"


def test_derive_token_is_an_hmac_sha256_of_the_tenant_and_differs_per_tenant():
    expected = hmac.new(b"key", b"repo-intelligence:adp", hashlib.sha256).hexdigest()

    assert derive_token("key", "adp") == expected
    assert derive_token("key", "adp") != derive_token("key", "beta")
    assert derive_token("key", "adp") != derive_token("other", "adp")


def test_tenant_tokens_prefer_the_variable_then_the_key_and_skip_a_tenant_without_either():
    environ = {"REPO_INTELLIGENCE_TOKEN_ADP": " adp-secret "}

    assert build_tokens(["adp", "beta"], "key", environ) == {
        "adp": "adp-secret",
        "beta": derive_token("key", "beta"),
    }
    assert build_tokens(["adp", "beta"], "", environ) == {"adp": "adp-secret"}


def test_tenant_for_token_returns_the_matching_tenant_or_none():
    tokens = {"adp": ("token-a",), "beta": ("token-b",)}

    assert tenant_for_token(tokens, "token-b") == "beta"
    assert tenant_for_token(tokens, "token-c") is None
    assert tenant_for_token({}, "token-a") is None


def test_tenant_for_token_accepts_every_token_of_a_tenant():
    tokens = {"adp": ("current-a", "previous-a"), "beta": ("current-b",)}

    assert tenant_for_token(tokens, "current-a") == "adp"
    assert tenant_for_token(tokens, "previous-a") == "adp"
    assert tenant_for_token(tokens, "current-b") == "beta"
    assert tenant_for_token({"adp": ()}, "current-a") is None


def test_tenant_for_token_refuses_a_bare_string_in_place_of_a_token_sequence():
    with pytest.raises(TypeError):
        tenant_for_token({"adp": "token-a"}, "t")


def test_single_tokens_makes_each_token_the_only_accepted_one():
    assert single_tokens({"adp": "token-a"}) == {"adp": ("token-a",)}


def test_tenant_for_token_compares_every_token_in_constant_time(monkeypatch):
    compared: list[tuple[bytes, bytes]] = []
    compare_digest = hmac.compare_digest

    def recording(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(tenant_tokens.hmac, "compare_digest", recording)

    tokens = {"adp": ("token-a", "token-a0"), "beta": ("token-b", "token-b0")}

    assert tenant_for_token(tokens, "token-a") == "adp"
    assert compared == [
        (b"token-a", b"token-a"),
        (b"token-a0", b"token-a"),
        (b"token-b", b"token-a"),
        (b"token-b0", b"token-a"),
    ]
