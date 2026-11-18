"""Target of the Langfuse forward through the jump-host."""

from __future__ import annotations

import pytest
from scripts import langfuse_ui


def test_the_forward_uses_the_port_of_the_public_url() -> None:
    target = langfuse_ui.forward_target(
        {
            "url": "https://langfuse.platform.internal",
            "public_url": "https://langfuse.platform.internal:8444",
        }
    )

    assert target == ("langfuse.platform.internal", 8444)


def test_a_public_url_under_another_name_is_refused() -> None:
    with pytest.raises(langfuse_ui.LangfuseUnavailableError, match="does not name"):
        langfuse_ui.forward_target(
            {
                "url": "https://langfuse.platform.internal",
                "public_url": "https://langfuse.example.com",
            }
        )


def test_a_root_without_langfuse_names_the_settings() -> None:
    with pytest.raises(langfuse_ui.LangfuseUnavailableError, match="container_langfuse"):
        langfuse_ui.forward_target(None)
