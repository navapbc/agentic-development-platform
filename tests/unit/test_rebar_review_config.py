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
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _model_classes() -> dict:
    return tomllib.loads(REBAR_REVIEW.read_text(encoding="utf-8"))["llm"]["model_classes"]


def _tracked_rebar_table() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["rebar"]


def test_the_tracked_project_config_carries_the_gate_tables():
    """rebar discovers pyproject; it never discovers config/local/rebar-review.toml."""
    table = _tracked_rebar_table()

    assert table["llm"]["model_classes"] == _model_classes()
    assert table["verify"] == {
        "require_plan_review_for_close": True,
        "require_plan_review_for_claim": True,
        "require_completion_verification_for_close": True,
    }


def test_rebar_discovers_the_tracked_config_without_a_local_rebar_toml(tmp_path):
    """A gate pins its read-root to an attested snapshot, which holds no untracked files.

    A gitignored rebar.toml is absent there, and it also shadows pyproject.toml in the
    discovery walk while it exists, so the tables have to resolve from tracked content alone.
    """
    from rebar import config as rebar_config

    (tmp_path / "pyproject.toml").write_text(PYPROJECT.read_text(encoding="utf-8"))
    (tmp_path / ".git").mkdir()

    llm = rebar_config.read_reserved_section("llm", str(tmp_path))
    verify = rebar_config.read_reserved_section("verify", str(tmp_path))

    assert llm["model_classes"] == _model_classes()
    assert verify["require_completion_verification_for_close"] is True


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


def test_every_gateway_deployment_is_a_bedrock_claude_model():
    config = yaml.safe_load(LITELLM.read_text(encoding="utf-8"))
    for entry in config["model_list"]:
        model = entry["litellm_params"]["model"]
        assert model.startswith("bedrock/us.anthropic.claude"), (
            f"{entry['model_name']} serves {model}"
        )


def test_no_gateway_group_carries_a_second_deployment():
    """A second deployment in a group is retried instantly, which defeats the backoff."""
    config = yaml.safe_load(LITELLM.read_text(encoding="utf-8"))
    names = [entry["model_name"] for entry in config["model_list"]]
    assert len(names) == len(set(names)), "a model_name appears twice"


def test_the_gateway_declares_no_cross_group_fallbacks():
    config = yaml.safe_load(LITELLM.read_text(encoding="utf-8"))
    assert "fallbacks" not in config["router_settings"]


def test_the_gateway_retries_transient_failures_and_rejects_permanent_ones():
    config = yaml.safe_load(LITELLM.read_text(encoding="utf-8"))
    policy = config["router_settings"]["retry_policy"]

    assert policy["TimeoutErrorRetries"] > 0
    assert policy["RateLimitErrorRetries"] > 0
    assert policy["InternalServerErrorRetries"] > 0
    assert policy["BadRequestErrorRetries"] == 0
    assert policy["AuthenticationErrorRetries"] == 0
