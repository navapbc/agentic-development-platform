"""The rebar tier config the LLM gates and the review worker both read."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tasks as tasks_module
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REBAR_REVIEW = REPO_ROOT / "config" / "local" / "rebar-review.toml"
LITELLM = REPO_ROOT / "config" / "local" / "litellm-config.yaml"


def _model_classes() -> dict:
    return tomllib.loads(REBAR_REVIEW.read_text(encoding="utf-8"))["llm"]["model_classes"]


def _legs(entry: dict) -> list[dict]:
    return [entry, *entry.get("fallback", [])]


def test_every_rebar_class_rebar_uses_is_declared():
    assert set(_model_classes()) == {"trivial", "standard", "frontier"}


def test_no_class_leg_leaves_bedrock():
    """A non-Bedrock leg reaches a provider this deployment holds no credentials for."""
    for name, entry in _model_classes().items():
        for leg in _legs(entry):
            assert leg["provider"] == "bedrock", f"{name} leg {leg['model']} is not on bedrock"


def _litellm_models(model_name: str) -> list[str]:
    config = yaml.safe_load(LITELLM.read_text(encoding="utf-8"))
    return [
        entry["litellm_params"]["model"].removeprefix("bedrock/")
        for entry in config["model_list"]
        if entry["model_name"] == model_name
    ]


def test_the_trivial_tier_matches_the_gateway_classification_tier():
    trivial = _model_classes()["trivial"]
    gateway = _litellm_models("classification-summary")
    assert [leg["model"] for leg in _legs(trivial)] == gateway


def test_the_gate_config_path_is_absolute_and_independent_of_the_working_directory(tmp_path):
    before = tasks_module._rebar_gate_env()["REBAR_LLM_CONFIG_FILE"]
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        after = tasks_module._rebar_gate_env()["REBAR_LLM_CONFIG_FILE"]
    finally:
        os.chdir(cwd)

    assert before == after
    assert os.path.isabs(after)
    assert os.path.isfile(after)
