"""The local posting service certificate is issued once and reused until it nears its expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.slack_poster_tls import TlsStateError, local_material

NAMES = ["slack-poster.agentic-dev-platform.svc.cluster.local", "127.0.0.1"]
ISSUED = datetime.now(UTC)


def test_a_second_render_reuses_the_ca_and_the_leaf(tmp_path: Path) -> None:
    first = local_material(NAMES, directory=tmp_path / "state", now=ISSUED)
    second = local_material(NAMES, directory=tmp_path / "state", now=ISSUED + timedelta(days=1))

    assert second == first


def test_a_leaf_within_30_days_of_its_expiry_is_issued_again_from_the_same_ca(
    tmp_path: Path,
) -> None:
    first = local_material(NAMES, directory=tmp_path / "state", now=ISSUED)

    renewed = local_material(NAMES, directory=tmp_path / "state", now=ISSUED + timedelta(days=368))

    assert renewed.ca == first.ca
    assert renewed.certificate != first.certificate


def test_a_leaf_for_other_names_is_issued_again(tmp_path: Path) -> None:
    first = local_material(NAMES, directory=tmp_path / "state", now=ISSUED)

    moved = local_material(
        ["slack-poster.platform.svc.cluster.local", "127.0.0.1"],
        directory=tmp_path / "state",
        now=ISSUED,
    )

    assert moved.ca == first.ca
    assert moved.certificate != first.certificate


def test_a_state_directory_inside_the_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TlsStateError, match="inside the repository"):
        local_material(NAMES, directory=tmp_path / "repo" / "tls", repo_root=tmp_path / "repo")
