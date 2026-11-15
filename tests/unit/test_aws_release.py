from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import tasks as tasks_module
from scripts import aws_release
from scripts.publish_platform_images import TFVARS_KEYS
from tests.support import hermetic_settings

IMAGES = {
    key: f"111111111111.dkr.ecr.us-east-1.amazonaws.com/x/{key}:1@sha256:{'a' * 64}"
    for key in TFVARS_KEYS.values()
}


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(aws_release, "ROOT", tmp_path)
    monkeypatch.setattr(aws_release, "IMAGES_FILE", tmp_path / "images.auto.tfvars.json")
    (tmp_path / "images.auto.tfvars.json").write_text(json.dumps(IMAGES))
    (tmp_path / "greenfield.tfvars").write_text('region = "us-east-1"\n')
    return tmp_path


class _Terraform:
    def __init__(self, plan_code: int) -> None:
        self.plan_code = plan_code
        self.commands: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        code = self.plan_code if argv[2] == "plan" else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    def verbs(self) -> list[str]:
        return [argv[2] for argv in self.commands]


def _release(root: Path, terraform: _Terraform, *, confirmed: bool = True) -> tuple[int, list]:
    deployed: list[bool] = []
    code = aws_release.release(
        root / "greenfield.tfvars",
        confirm=lambda: confirmed,
        deploy=lambda: deployed.append(True) or 0,
        run=terraform,
    )
    return code, deployed


def test_a_confirmed_plan_with_changes_is_applied_then_deployed(root: Path) -> None:
    terraform = _Terraform(aws_release.CHANGES)

    code, deployed = _release(root, terraform)

    assert code == 0
    assert terraform.verbs() == ["plan", "apply"]
    assert "-var-file=greenfield.tfvars" in terraform.commands[0]
    assert deployed == [True]


def test_a_declined_plan_applies_nothing_and_deploys_nothing(root: Path) -> None:
    terraform = _Terraform(aws_release.CHANGES)

    code, deployed = _release(root, terraform, confirmed=False)

    assert code == 1
    assert terraform.verbs() == ["plan"]
    assert deployed == []


def test_an_unchanged_root_skips_the_apply_and_still_deploys(root: Path) -> None:
    terraform = _Terraform(aws_release.NO_CHANGES)

    code, deployed = _release(root, terraform)

    assert terraform.verbs() == ["plan"]
    assert deployed == [True]
    assert code == 0


def test_a_failed_plan_stops_the_release(root: Path) -> None:
    with pytest.raises(aws_release.ReleaseError, match="plan exited 1"):
        _release(root, _Terraform(1))


def test_a_missing_images_file_names_how_to_produce_it(root: Path) -> None:
    (root / "images.auto.tfvars.json").unlink()

    with pytest.raises(aws_release.ReleaseError, match="inv publish-platform-images"):
        _release(root, _Terraform(aws_release.CHANGES))


def test_a_var_file_that_still_sets_an_image_is_refused(root: Path) -> None:
    (root / "greenfield.tfvars").write_text('container_platform_image = "old@sha256:b"\n')

    with pytest.raises(aws_release.ReleaseError, match="container_platform_image"):
        _release(root, _Terraform(aws_release.CHANGES))


def test_a_ci_job_signs_with_its_role_credentials_rather_than_a_profile(monkeypatch) -> None:
    settings = hermetic_settings(aws_profile="agentic-platform", aws_region="us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIA")

    assert tasks_module._aws_env(settings) == {"AWS_REGION": "us-east-1"}

    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    assert tasks_module._aws_env(settings) == {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "agentic-platform",
    }
