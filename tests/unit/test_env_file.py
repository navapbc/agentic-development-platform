"""Unit tests for writing settings into the workstation's .env."""

from __future__ import annotations

from scripts.env_file import set_values


def test_a_key_is_replaced_in_place_and_a_new_key_is_appended(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("# RUNTIME_IMAGES=old\nTENANT=adp\nRUNTIME_IMAGES=old\n")

    set_values({"RUNTIME_IMAGES": "new", "GATE_ENGINE_BUILDER_IMAGE": "b@sha256:1"}, env)

    assert env.read_text() == (
        "# RUNTIME_IMAGES=old\nTENANT=adp\nRUNTIME_IMAGES=new\n"
        "GATE_ENGINE_BUILDER_IMAGE=b@sha256:1\n"
    )
