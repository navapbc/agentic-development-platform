"""Unit tests for the hybrid router provider (routing, merging, degradation)."""

from __future__ import annotations

from services.repository_intelligence.api.provider import RepositoryIntelligenceProvider
from services.repository_intelligence.normalization.models import (
    CallEdge,
    ChangeImpact,
    DependencyEdge,
    IndexStatus,
    Location,
    Provenance,
    Reference,
    RelatedTest,
    RepoRef,
    SearchHit,
    Source,
    Symbol,
    SymbolKind,
)
from services.repository_intelligence.providers.router import RouterProvider

_REPO = RepoRef("agentic-demo-service", "deadbeef")


class Fake:
    def __init__(self, **behaviors: object) -> None:
        self._behaviors = behaviors
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in self.__dict__["_behaviors"]:
            raise AttributeError(name)
        behavior = self._behaviors[name]

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if isinstance(behavior, BaseException):
                raise behavior
            if callable(behavior):
                return behavior(*args, **kwargs)
            return behavior

        return method

    def called(self, name: str) -> bool:
        return any(call[0] == name for call in self.calls)


def _prov(source: Source = Source.SCIP, conf: float = 0.98) -> Provenance:
    return Provenance(source=source, confidence=conf)


def _ref(path: str, line: int, *, is_def: bool = False, source: Source = Source.SCIP) -> Reference:
    return Reference("sym", Location(path, line), is_def, _prov(source))


def _sym(name: str, path: str, line: int, *, source: Source = Source.SCIP) -> Symbol:
    return Symbol(name, SymbolKind.FUNCTION, Location(path, line), _prov(source))


def _call(name: str, path: str, line: int) -> CallEdge:
    return CallEdge(_sym(name, path, line), _sym("callee", "app/store.py", 1), _prov())


def _test(test_id: str, path: str, line: int, *, source: Source = Source.COVERAGE) -> RelatedTest:
    return RelatedTest(test_id, Location(path, line), "covers", _prov(source))


def _dep(frm: str, to: str, kind: str, source: Source) -> DependencyEdge:
    return DependencyEdge(frm, to, kind, _prov(source))


def test_router_conforms_to_provider_protocol():
    router = RouterProvider(scip=Fake())

    assert isinstance(router, RepositoryIntelligenceProvider)


def test_search_prefers_zoekt_and_falls_back_to_scip_on_error():
    hit = SearchHit(Location("app/main.py", 1), "x", 1.0, _prov(Source.ZOEKT, 1.0))
    zoekt = Fake(repo_search_code=[hit])
    scip = Fake(repo_search_code=[SearchHit(Location("s", 1), "s", 1.0, _prov())])
    router = RouterProvider(scip=scip, zoekt=zoekt)

    assert router.repo_search_code(_REPO, "q") == [hit]
    assert not scip.called("repo_search_code")

    broken = Fake(repo_search_code=RuntimeError("zoekt down"))
    router = RouterProvider(scip=scip, zoekt=broken)
    result = router.repo_search_code(_REPO, "q")

    assert result and result[0].provenance.source is Source.SCIP


def test_find_symbol_filters_non_authoritative():
    good = _sym("app.main.health", "app/main.py", 17)
    bad = _sym("guess", "app/main.py", 99, source=Source.EMBEDDING)
    scip = Fake(repo_find_symbol=[good, bad])
    router = RouterProvider(scip=scip)

    assert router.repo_find_symbol(_REPO, "health") == [good]


def test_references_use_scip_and_ignore_cbm_when_scip_has_results():
    refs = [_ref("app/main.py", 40), _ref("app/main.py", 10, is_def=True)]
    scip = Fake(repo_find_references=refs)
    cbm = Fake(is_available=True, candidate_references=[_ref("other.py", 1)])
    router = RouterProvider(scip=scip, cbm=cbm)

    result = router.repo_find_references(_REPO, "sym")

    assert [(r.location.path, r.location.start_line) for r in result] == [
        ("app/main.py", 10),
        ("app/main.py", 40),
    ]
    assert not cbm.called("candidate_references")


def test_references_fall_back_to_cbm_candidates_when_scip_empty():
    scip = Fake(repo_find_references=[])
    candidates = [_ref("app/store.py", 5, source=Source.CBM)]
    cbm = Fake(is_available=True, candidate_references=candidates)
    router = RouterProvider(scip=scip, cbm=cbm)

    result = router.repo_find_references(_REPO, "sym")

    assert result == candidates
    assert cbm.called("candidate_references")


def test_references_return_empty_when_scip_empty_and_cbm_unavailable():
    scip = Fake(repo_find_references=[])
    cbm = Fake(is_available=False, candidate_references=[_ref("x", 1)])
    router = RouterProvider(scip=scip, cbm=cbm)

    assert router.repo_find_references(_REPO, "sym") == []
    assert not cbm.called("candidate_references")


def test_callers_fall_back_to_cbm_candidates_when_scip_empty():
    scip = Fake(repo_get_callers=[])
    candidates = [_call("app.main.create_task", "app/main.py", 30)]
    cbm = Fake(is_available=True, candidate_callers=candidates)
    router = RouterProvider(scip=scip, cbm=cbm)

    assert router.repo_get_callers(_REPO, "add") == candidates


def test_related_tests_trust_coverage_even_when_empty():
    coverage = Fake(repo_get_related_tests=[])
    cbm = Fake(is_available=True, repo_get_related_tests=[_test("t", "tests/test_api.py", 1)])
    router = RouterProvider(scip=Fake(), coverage=coverage, cbm=cbm)

    assert router.repo_get_related_tests(_REPO, "add") == []
    assert not cbm.called("repo_get_related_tests")


def test_related_tests_fall_back_to_cbm_without_coverage():
    tests = [_test("t", "tests/test_api.py", 1, source=Source.CBM)]
    cbm = Fake(is_available=True, repo_get_related_tests=tests)
    router = RouterProvider(scip=Fake(), coverage=None, cbm=cbm)

    assert router.repo_get_related_tests(_REPO, "add") == tests


def test_module_dependencies_merge_intra_and_external():
    custom = Fake(
        repo_get_module_dependencies=[_dep("app.main", "app.store", "import", Source.AST)]
    )
    build = Fake(
        repo_get_module_dependencies=[
            _dep("agentic-demo-service", "fastapi", "runtime_dependency", Source.BUILD_METADATA)
        ]
    )
    router = RouterProvider(scip=Fake(), custom=custom, build_metadata=build)

    edges = router.repo_get_module_dependencies(_REPO, "app.main")

    assert [(e.to_module, e.kind) for e in edges] == [
        ("app.store", "import"),
        ("fastapi", "runtime_dependency"),
    ]
    assert build.calls[0] == ("repo_get_module_dependencies", (_REPO, ""), {})


def test_index_status_prefers_cbm_staleness():
    cbm_status = IndexStatus(_REPO, "deadbeef", True, ("python",))
    cbm = Fake(is_available=True, repo_get_index_status=cbm_status)
    scip = Fake(repo_get_index_status=IndexStatus(_REPO, "other", False, ("python",)))
    router = RouterProvider(scip=scip, cbm=cbm)

    assert router.repo_get_index_status(_REPO) is cbm_status


def test_index_status_falls_back_to_scip_when_cbm_uncommitted():
    cbm = Fake(is_available=True, repo_get_index_status=IndexStatus(_REPO, None, False, ()))
    scip_status = IndexStatus(_REPO, "deadbeef", True, ("python",))
    scip = Fake(repo_get_index_status=scip_status)
    router = RouterProvider(scip=scip, cbm=cbm)

    assert router.repo_get_index_status(_REPO) is scip_status


def test_change_impact_enriches_tests_from_coverage():
    base = ChangeImpact(
        changed=("app.main.create_task",),
        impacted_symbols=(),
        impacted_files=("app/main.py",),
        impacted_tests=(),
        provenance=_prov(),
    )
    scip = Fake(repo_get_change_impact=base)
    coverage = Fake(repo_get_related_tests=[_test("test_api.test_create", "tests/test_api.py", 39)])
    router = RouterProvider(scip=scip, coverage=coverage)

    impact = router.repo_get_change_impact(_REPO, ["app.main.create_task"])

    assert impact.impacted_files == ("app/main.py",)
    assert [t.test_id for t in impact.impacted_tests] == ["test_api.test_create"]


def test_context_package_composes_router_outputs():
    scip = Fake(
        repo_find_symbol=[_sym("app.main.health", "app/main.py", 17)],
        repo_find_references=[_ref("app/main.py", 17, is_def=True)],
    )
    coverage = Fake(repo_get_related_tests=[_test("test_api.test_health", "tests/test_api.py", 27)])
    build = Fake(repo_get_module_dependencies=[])
    router = RouterProvider(scip=scip, coverage=coverage, build_metadata=build)

    package = router.repo_get_context_package(_REPO, "health")

    assert package.focus == "health"
    assert len(package.symbols) == 1
    assert len(package.references) == 1
    assert package.related_tests[0].test_id == "test_api.test_health"


def test_degrades_when_optional_backends_raise():
    scip = Fake(
        repo_find_symbol=[_sym("s", "app/main.py", 1)],
        repo_get_callers=RuntimeError("scip graph error"),
    )
    router = RouterProvider(scip=scip)

    assert router.repo_get_callers(_REPO, "x") == []
    assert router.repo_find_symbol(_REPO, "s")[0].name == "s"
