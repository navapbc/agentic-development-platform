from __future__ import annotations

from pathlib import Path

import pytest
from scripts.config import TenantSettingError, get_settings
from scripts.local_tenants import TenantLayoutError, local_tenant_names, select_tenant
from tests.support import local_tenants


def test_the_tenants_are_the_folders_with_their_settings_files_and_no_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenants = local_tenants(monkeypatch, tmp_path, "beta", "adp")
    (tenants / "tenant.example").mkdir()
    (tenants / "tenant.example.env").touch()

    assert local_tenant_names() == ["adp", "beta"]


@pytest.mark.parametrize(
    ("remove", "refusal"),
    [("beta.env", "tenants/beta/ has no settings file"), ("beta", "has no tenant folder")],
)
def test_a_folder_or_settings_file_without_the_other_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove: str, refusal: str
) -> None:
    tenants = local_tenants(monkeypatch, tmp_path, "adp", "beta")
    removed = tenants / remove
    if removed.is_dir():
        removed.rmdir()
    else:
        removed.unlink()

    with pytest.raises(TenantLayoutError, match=refusal):
        local_tenant_names()


def test_a_host_command_may_omit_the_tenant_only_when_one_exists() -> None:
    assert select_tenant("", ["adp"]) == "adp"
    assert select_tenant("beta", ["adp", "beta"]) == "beta"
    with pytest.raises(TenantLayoutError, match="2 local tenants exist, adp, beta"):
        select_tenant("", ["adp", "beta"])
    with pytest.raises(TenantLayoutError, match="gamma is not a local tenant"):
        select_tenant("gamma", ["adp", "beta"])


@pytest.mark.parametrize(
    "setting",
    ["GITHUB_TOKEN", "TENANT", "TENANTS", "SLACK_SIGNING_SECRET", "GITHUB_WEBHOOK_SECRET_ADP"],
)
def test_the_platform_settings_file_refuses_a_tenant_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    local_tenants(monkeypatch, tmp_path, "adp")
    (tmp_path / "config" / "local" / "platform.env").write_text(f"{setting}=value\n")

    with pytest.raises(TenantSettingError) as refused:
        get_settings()

    message = str(refused.value)
    assert f"config/local/platform.env sets {setting}," in message
    assert "Move it to config/local/tenants/<tenant>.env" in message


def test_a_host_command_for_a_tenant_reads_its_settings_file_over_the_platform_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenants = local_tenants(monkeypatch, tmp_path, "adp")
    (tmp_path / "config" / "local" / "platform.env").write_text("LOG_LEVEL=INFO\n")
    (tenants / "adp.env").write_text("LOG_LEVEL=DEBUG\nTICKET_BACKEND=rebar\n")
    monkeypatch.setenv("TENANT", "adp")

    settings = get_settings()

    assert settings.log_level == "DEBUG"
    assert settings.ticket_backend == "rebar"
