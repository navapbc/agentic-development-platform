"""The Temporal UI forward reaches the load balancer through the jump-host connect script."""

from __future__ import annotations

import pytest
from scripts import temporal_ui
from scripts.connectivity_providers import JumphostConfigError, jumphost_command
from tests.support import hermetic_settings

UI_URL = "https://internal-platform-temporal-ui-1.us-east-1.elb.amazonaws.com"


def test_the_forward_targets_the_load_balancer_on_443(tmp_path):
    script = tmp_path / "connect.sh"
    script.write_text("#!/bin/sh\n")
    settings = hermetic_settings(
        jumphost_name="bedrock-jumphost",
        jumphost_env="sandbox",
        jumphost_connect_script=str(script),
    )

    host = temporal_ui.ui_host(UI_URL)

    assert jumphost_command(settings, f"8443:{host}:443") == [
        str(script),
        "-n",
        "bedrock-jumphost",
        "-e",
        "sandbox",
        "-p",
        "8443:internal-platform-temporal-ui-1.us-east-1.elb.amazonaws.com:443",
    ]


@pytest.mark.parametrize("ui_url", [None, ""])
def test_a_root_without_the_ui_is_reported(ui_url):
    with pytest.raises(temporal_ui.UiUnavailableError, match="temporal_ui_image"):
        temporal_ui.ui_host(ui_url)


def test_an_unnamed_jumphost_is_refused():
    with pytest.raises(JumphostConfigError, match="JUMPHOST_NAME"):
        jumphost_command(
            hermetic_settings(jumphost_name=""), f"8443:{temporal_ui.ui_host(UI_URL)}:443"
        )
