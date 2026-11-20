"""Unit tests for the Repository Intelligence Service composition and freshness."""

from __future__ import annotations

import subprocess

from services.repository_intelligence.context_builder.context_service import (
    RepositoryIntelligenceService,
)
from services.repository_intelligence.normalization.models import (
    CallEdge,
    ContextPackage,
    IndexStatus,
    Location,
    Provenance,
    Reference,
    RepoRef,
    Source,
    Symbol,
    SymbolKind,
)
from services.repository_intelligence.providers.ast_grep.ast_grep_provider import StructuralMatch
from services.repository_intelligence.providers.build_metadata.build_metadata_provider import (
    BuildTarget,
)

_REPO = RepoRef("agentic-demo-service", "0744a6e")


def _prov(source: Source = Source.SCIP, conf: float = 0.98) -> Provenance:
    return Provenance(source=source, confidence=conf)


def _sym(name: str, path: str, line: int) -> Symbol:
    return Symbol(name, SymbolKind.FUNCTION, Location(path, line), _prov())


def _call(name: str, path: str, line: int) -> CallEdge:
    return CallEdge(_sym(name, path, line), _sym("callee", "app/store.py", 1), _prov())


def _match(path: str, line: int, text: str) -> StructuralMatch:
    return StructuralMatch(text=text, location=Location(path, line), single={}, multi={})


class FakeRouter:
    def __init__(
        self,
        *,
        status=None,
        package=None,
        callers=(),
        callees=(),
        implementations=(),
        find_symbols=None,
        find_refs=None,
    ):
        self._status = status
        self._package = package
        self._callers = list(callers)
        self._callees = list(callees)
        self._implementations = list(implementations)
        self._find_symbols = dict(find_symbols or {})
        self._find_refs = dict(find_refs or {})

    def repo_get_index_status(self, repo):
        return self._status

    def repo_get_context_package(self, repo, focus):
        return self._package

    def repo_find_symbol(self, repo, name, *, limit=50):
        return self._find_symbols.get(name, [])

    def repo_find_references(self, repo, symbol, *, include_definition=True):
        return self._find_refs.get(symbol, [])

    def repo_get_callers(self, repo, symbol):
        return self._callers

    def repo_get_callees(self, repo, symbol):
        return self._callees

    def repo_find_implementations(self, repo, symbol):
        return self._implementations


class FakeAstGrep:
    def __init__(self, routes=(), models=(), raises=()):
        self._routes = list(routes)
        self._models = list(models)
        self._raises = list(raises)

    def find_routes(self):
        return self._routes

    def find_models(self):
        return self._models

    def find_raise_sites(self):
        return self._raises


class FakeCbm:
    def __init__(self, *, available=True, candidates=()):
        self._available = available
        self._candidates = list(candidates)
        self.calls: list[str] = []

    def is_available(self):
        return self._available

    def candidate_references(self, repo, symbol):
        self.calls.append("candidate_references")
        return self._candidates


class FakeBuild:
    def __init__(self, targets=()):
        self._targets = list(targets)

    def build_targets(self):
        return self._targets


def _base_package(focus: str = "health") -> ContextPackage:
    return ContextPackage(
        focus=focus,
        symbols=(_sym("app.main.health", "app/main.py", 17),),
        references=(Reference("app.main.health", Location("app/main.py", 17), True, _prov()),),
        related_tests=(),
        dependencies=(),
    )


def test_ensure_fresh_skips_reindex_when_fresh_and_commit_matches(tmp_path):
    status = IndexStatus(_REPO, "0744a6e", True, ("python",))
    reindexed: list[tuple[RepoRef, str | None]] = []
    service = RepositoryIntelligenceService(
        FakeRouter(status=status),
        repo_root=tmp_path,
        reindex=lambda repo, prior: reindexed.append((repo, prior)),
    )

    assert service.ensure_fresh(_REPO) is True
    assert reindexed == []


def test_ensure_fresh_reindexes_on_commit_change(tmp_path):
    status = IndexStatus(_REPO, "oldsha", True, ("python",))
    reindexed: list[tuple[RepoRef, str | None]] = []
    service = RepositoryIntelligenceService(
        FakeRouter(status=status),
        repo_root=tmp_path,
        reindex=lambda repo, prior: reindexed.append((repo, prior)),
    )

    assert service.ensure_fresh(_REPO) is False
    assert reindexed == [(_REPO, "oldsha")]


def test_ensure_fresh_resolves_empty_commit_to_head(tmp_path, monkeypatch):
    from services.repository_intelligence.context_builder import context_service as cs

    monkeypatch.setattr(cs, "resolve_commit", lambda root: "headsha1")
    reindexed: list[tuple[RepoRef, str | None]] = []
    service = RepositoryIntelligenceService(
        FakeRouter(status=None),
        repo_root=tmp_path,
        reindex=lambda repo, prior: reindexed.append((repo, prior)),
    )

    assert service.ensure_fresh(RepoRef("example-service", "")) is False
    assert reindexed == [(RepoRef("example-service", "headsha1"), None)]


def test_ensure_fresh_reindexes_when_index_stale(tmp_path):
    status = IndexStatus(_REPO, "0744a6e", False, ("python",))
    reindexed: list[tuple[RepoRef, str | None]] = []
    service = RepositoryIntelligenceService(
        FakeRouter(status=status),
        repo_root=tmp_path,
        reindex=lambda repo, prior: reindexed.append((repo, prior)),
    )

    assert service.ensure_fresh(_REPO) is False
    assert reindexed == [(_REPO, "0744a6e")]


def test_context_package_composes_all_enrichments(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    router = FakeRouter(
        status=IndexStatus(_REPO, "0744a6e", True, ("python",)),
        package=_base_package(),
        callers=[_call("app.main.create_task", "app/main.py", 30)],
        callees=[_call("app.store.save", "app/store.py", 12)],
        implementations=[_sym("app.store.Store", "app/store.py", 5)],
    )
    ast_grep = FakeAstGrep(
        routes=[_match("app/main.py", 22, "@app.get('/health')")],
        models=[_match("app/models.py", 8, "class Task(BaseModel):")],
    )
    cbm = FakeCbm(
        candidates=[Reference("health", Location("app/main.py", 40), False, _prov(Source.CBM, 0.5))]
    )
    build = FakeBuild(targets=[BuildTarget("pytest", "console_script", "pytest:main")])
    service = RepositoryIntelligenceService(
        router,
        repo_root=tmp_path,
        ast_grep=ast_grep,
        cbm=cbm,
        build_metadata=build,
    )

    package = service.context_package(_REPO, "health")

    assert package.focus == "health"
    assert len(package.symbols) == 1
    assert [edge.caller.name for edge in package.callers] == ["app.main.create_task"]
    assert [edge.callee.name for edge in package.callees] == ["callee"]
    assert [sym.name for sym in package.implementations] == ["app.store.Store"]
    assert [hit.location.path for hit in package.structural] == ["app/main.py", "app/models.py"]
    assert package.candidate_files == ("app/main.py",)
    assert package.build_targets == ("console_script:pytest",)
    assert package.config_files == ("pyproject.toml", "requirements.txt")


def test_context_package_extracts_terms_and_paths_from_detail(tmp_path):
    (tmp_path / "portal" / "src" / "components").mkdir(parents=True)
    (tmp_path / "portal" / "src" / "components" / "ExternalLink.tsx").write_text("export {}\n")
    router = FakeRouter(
        status=IndexStatus(_REPO, "0744a6e", True, ("typescript",)),
        package=_base_package("Convert anchors to ExternalLink"),
        find_symbols={
            "ExternalLink": [_sym("ExternalLink", "portal/src/components/ExternalLink.tsx", 5)]
        },
        find_refs={
            "ExternalLink": [
                Reference(
                    "ExternalLink",
                    Location("portal/src/pages/get-ready.tsx", 97),
                    False,
                    _prov(),
                )
            ]
        },
    )
    service = RepositoryIntelligenceService(router, repo_root=tmp_path, cbm=FakeCbm())

    package = service.context_package(
        _REPO,
        "Convert anchors to ExternalLink",
        "Affected Code: portal/src/components/ExternalLink.tsx#L1-L5 and missing/gone.tsx",
    )

    assert [sym.name for sym in package.symbols] == ["ExternalLink"]
    assert [ref.location.path for ref in package.references] == ["portal/src/pages/get-ready.tsx"]
    assert "portal/src/components/ExternalLink.tsx" in package.candidate_files
    assert "portal/src/pages/get-ready.tsx" in package.candidate_files
    assert "missing/gone.tsx" not in package.candidate_files


def test_candidate_files_empty_when_cbm_unavailable(tmp_path):
    cbm = FakeCbm(
        available=False,
        candidates=[Reference("x", Location("app/main.py", 1), False, _prov(Source.CBM, 0.5))],
    )
    service = RepositoryIntelligenceService(
        FakeRouter(status=IndexStatus(_REPO, "0744a6e", True, ()), package=_base_package()),
        repo_root=tmp_path,
        cbm=cbm,
    )

    package = service.context_package(_REPO, "health")

    assert package.candidate_files == ()
    assert cbm.calls == []


def test_structural_empty_without_ast_grep(tmp_path):
    service = RepositoryIntelligenceService(
        FakeRouter(status=IndexStatus(_REPO, "0744a6e", True, ()), package=_base_package()),
        repo_root=tmp_path,
    )

    package = service.context_package(_REPO, "health")

    assert package.structural == ()


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def test_context_package_sets_identity_and_recent_changes(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def health():\n    return {}\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add health endpoint")

    service = RepositoryIntelligenceService(
        FakeRouter(status=IndexStatus(_REPO, "0744a6e", True, ()), package=_base_package()),
        repo_root=tmp_path,
    )

    package = service.context_package(_REPO, "health")

    assert package.repository == "agentic-demo-service"
    assert package.indexed_commit == "0744a6e"
    assert package.recent_changes
    assert package.recent_changes[0].path == "app/main.py"
    assert package.recent_changes[0].summary == "add health endpoint"


def test_recent_changes_empty_without_git(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def health():\n    return {}\n")
    service = RepositoryIntelligenceService(
        FakeRouter(status=IndexStatus(_REPO, "0744a6e", True, ()), package=_base_package()),
        repo_root=tmp_path,
    )

    package = service.context_package(_REPO, "health")

    assert package.recent_changes == ()


def test_from_repo_wires_python_backends_for_a_python_repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def health():\n    return {}\n")

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._scip is not None
    assert service._reindex is not None


def test_from_repo_wires_scip_typescript_for_a_typescript_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const health = () => ({});\n")

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._scip is not None
    assert service._router._scip._indexer.language_id == "typescript"
    assert service._router._coverage is None
    assert service._router._custom is None
    assert service._reindex is not None


def test_from_repo_wires_scip_ruby_for_a_ruby_repo(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "app.rb").write_text("class App\n  def health\n    {}\n  end\nend\n")

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._scip is not None
    assert service._router._scip._indexer.language_id == "ruby"
    assert service._router._lsp is None
    assert service._router._coverage is None
    assert service._router._custom is None


def test_from_repo_degrades_for_a_non_python_repo(tmp_path):
    (tmp_path / "main.tf").write_text('resource "null_resource" "x" {}\n')

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._scip is None
    assert service._router._zoekt is not None
    assert service._reindex is None


def test_from_repo_wires_lsp_backend_for_a_terraform_repo(tmp_path):
    (tmp_path / "main.tf").write_text('resource "null_resource" "x" {}\n')

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._lsp is not None
    assert service._router._scip is None


def test_from_repo_has_no_lsp_backend_for_a_repo_without_a_language_server(tmp_path):
    (tmp_path / "notes.txt").write_text("no supported language here\n")

    service = RepositoryIntelligenceService.from_repo(tmp_path)

    assert service._router._lsp is None
    assert service._router._scip is None
