"""Unit tests for the selective commit-delta reindex seam."""

from __future__ import annotations

from pathlib import Path

from services.repository_intelligence.indexing import reindex, scip_builder, zoekt_builder
from services.repository_intelligence.normalization.models import RepoRef

_REPO = RepoRef("agentic-demo-service", "targetsha")


def _fake_builder(recorder: list[str], name: str):
    def build(repo_root, *, commit, index_root, **_kwargs):
        recorder.append(name)
        target = index_root / f"{Path(repo_root).name}@{commit[:7]}"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.idx").write_text(name, encoding="utf-8")
        return (None, commit)

    return build


def _install(tmp_path, monkeypatch, recorder) -> tuple[Path, Path, Path]:
    scip_root = tmp_path / "scip"
    zoekt_root = tmp_path / "zoekt"
    monkeypatch.setattr(scip_builder, "default_index_root", lambda: scip_root)
    monkeypatch.setattr(zoekt_builder, "default_index_root", lambda: zoekt_root)
    monkeypatch.setattr(scip_builder, "build_scip_index", _fake_builder(recorder, "scip"))
    monkeypatch.setattr(zoekt_builder, "build_zoekt_index", _fake_builder(recorder, "zoekt"))
    repo = tmp_path / "agentic-demo-service"
    repo.mkdir()
    return repo, scip_root, zoekt_root


def test_first_build_runs_full_scip_and_zoekt(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, scip_root, zoekt_root = _install(tmp_path, monkeypatch, recorder)

    reindex.reindex_repo(repo, _REPO, prior_commit=None)

    assert recorder == ["scip", "zoekt"]
    assert (scip_root / "agentic-demo-service@targets" / "scip.idx").exists()
    assert (zoekt_root / "agentic-demo-service@targets" / "zoekt.idx").exists()


def test_reuses_scip_when_no_source_file_changed(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: ("README.md", "docs/guide.md"))

    prior_dir = scip_builder.index_dir("agentic-demo-service", "priorsha", scip_root)
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "index.scip.json").write_text("{}", encoding="utf-8")

    reindex.reindex_repo(repo, _REPO, prior_commit="priorsha")

    assert "scip" not in recorder
    assert recorder == ["zoekt"]
    target = scip_builder.index_dir("agentic-demo-service", "targetsha", scip_root)
    assert (target / "index.scip.json").read_text(encoding="utf-8") == "{}"


def test_rebuilds_scip_when_source_file_changed(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, _scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: ("app/main.py",))

    reindex.reindex_repo(repo, _REPO, prior_commit="priorsha")

    assert recorder == ["scip", "zoekt"]


def test_rebuilds_scip_when_diff_unavailable(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, _scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: None)

    reindex.reindex_repo(repo, _REPO, prior_commit="priorsha")

    assert recorder == ["scip", "zoekt"]


def test_full_build_publishes_and_preserves_colocated_artifacts(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: ("app/main.py",))

    target = scip_builder.index_dir("agentic-demo-service", "targetsha", scip_root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "zoekt.idx").write_text("colocated", encoding="utf-8")

    reindex.reindex_repo(repo, _REPO, prior_commit="priorsha")

    assert (target / "scip.idx").read_text(encoding="utf-8") == "scip"
    assert (target / "zoekt.idx").read_text(encoding="utf-8") == "colocated"


def test_shared_index_root_backends_coexist(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, _scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    shared = tmp_path / "shared"

    reindex.reindex_repo(repo, _REPO, prior_commit=None, index_root=shared)

    target = shared / "agentic-demo-service@targets"
    assert (target / "scip.idx").read_text(encoding="utf-8") == "scip"
    assert (target / "zoekt.idx").read_text(encoding="utf-8") == "zoekt"


def test_reuses_scip_for_ts_repo_when_only_python_file_changed(tmp_path, monkeypatch):
    recorder: list[str] = []
    repo, scip_root, _zoekt_root = _install(tmp_path, monkeypatch, recorder)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: ("app/main.py",))

    prior_dir = scip_builder.index_dir("agentic-demo-service", "priorsha", scip_root)
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "index.scip.json").write_text("{}", encoding="utf-8")

    reindex.reindex_repo(
        repo, _REPO, prior_commit="priorsha", indexer=scip_builder.SCIP_TYPESCRIPT_INDEXER
    )

    assert recorder == ["zoekt"]


def test_rebuilds_scip_for_ts_repo_forwarding_the_typescript_indexer(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    def fake_build(repo_root, *, commit, index_root, indexer, **_kwargs):
        captured["indexer"] = indexer
        target = index_root / f"{Path(repo_root).name}@{commit[:7]}"
        target.mkdir(parents=True, exist_ok=True)
        (target / "scip.idx").write_text("scip", encoding="utf-8")
        return (None, commit)

    repo, _scip_root, _zoekt_root = _install(tmp_path, monkeypatch, [])
    monkeypatch.setattr(scip_builder, "build_scip_index", fake_build)
    monkeypatch.setattr(reindex, "changed_paths", lambda *_a: ("src/app.ts",))

    reindex.reindex_repo(
        repo, _REPO, prior_commit="priorsha", indexer=scip_builder.SCIP_TYPESCRIPT_INDEXER
    )

    assert captured["indexer"] is scip_builder.SCIP_TYPESCRIPT_INDEXER
