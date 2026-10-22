"""Unit tests for the pluggable Bedrock connectivity providers."""

from __future__ import annotations

import pytest
from scripts.config import Settings
from scripts.connectivity_providers import (
    DirectConnectivity,
    JumphostConnectivity,
    SshBastionConnectivity,
    get_provider,
)
from tests.support import hermetic_settings


def test_factory_returns_jumphost_by_default():
    assert isinstance(get_provider(hermetic_settings()), JumphostConnectivity)


def test_factory_dispatches_each_known_provider():
    cases = {
        "jumphost": JumphostConnectivity,
        "ssh-bastion": SshBastionConnectivity,
        "direct": DirectConnectivity,
    }
    for key, cls in cases.items():
        provider = get_provider(Settings(bedrock_connectivity_provider=key))
        assert isinstance(provider, cls)


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown bedrock_connectivity_provider"):
        get_provider(Settings(bedrock_connectivity_provider="nope"))


def test_public_provider_is_unknown():
    with pytest.raises(ValueError, match="unknown bedrock_connectivity_provider"):
        get_provider(Settings(bedrock_connectivity_provider="public"))


def test_direct_provider_summary_uses_injected_endpoint():
    url = "https://bedrock.example.internal"
    settings = Settings(bedrock_connectivity_provider="direct", bedrock_endpoint_url=url)
    assert url in get_provider(settings).summary(settings)
