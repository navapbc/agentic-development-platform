"""Unit tests for context-package serialization and prompt rendering."""

from __future__ import annotations

from services.repository_intelligence.normalization.models import (
    CallEdge,
    ContextPackage,
    Location,
    Provenance,
    RecentChange,
    Reference,
    SearchHit,
    Source,
    Symbol,
    SymbolKind,
)
from services.repository_intelligence.normalization.serialization import (
    context_package_to_dict,
    render_context_summary,
)


def _prov(source: Source = Source.SCIP, conf: float = 0.98) -> Provenance:
    return Provenance(source=source, confidence=conf)


def _sym(name: str, path: str, line: int) -> Symbol:
    return Symbol(name, SymbolKind.FUNCTION, Location(path, line), _prov())


def _package() -> ContextPackage:
    return ContextPackage(
        focus="health",
        symbols=(_sym("app.main.health", "app/main.py", 17),),
        references=(Reference("app.main.health", Location("app/main.py", 17), True, _prov()),),
        related_tests=(),
        dependencies=(),
        callers=(
            CallEdge(_sym("caller", "app/main.py", 5), _sym("callee", "app/store.py", 1), _prov()),
        ),
        candidate_files=("app/store.py",),
        structural=(SearchHit(Location("app/main.py", 17), '@app.get("/health")', 1.0, _prov()),),
        build_targets=("console_script:pytest",),
        config_files=("pyproject.toml",),
        repository="agentic-demo-service",
        indexed_commit="0744a6e",
        recent_changes=(RecentChange("abcdef1234567", "add health endpoint", "app/main.py"),),
    )


def test_context_package_to_dict_is_json_native():
    result = context_package_to_dict(_package())

    assert result["repository"] == "agentic-demo-service"
    assert result["indexed_commit"] == "0744a6e"
    assert result["symbols"][0]["provenance"]["source"] == "scip"
    assert result["symbols"][0]["kind"] == "function"
    assert result["recent_changes"][0]["summary"] == "add health endpoint"


def test_render_context_summary_includes_key_sections():
    summary = render_context_summary(context_package_to_dict(_package()))

    assert "Repository: agentic-demo-service @ 0744a6e" in summary
    assert "Focus: health" in summary
    assert "app.main.health (function) app/main.py:17" in summary
    assert "Candidate files:" in summary
    assert "  - app/store.py" in summary
    assert "Structural matches:" in summary
    assert 'app/main.py:17 @app.get("/health")' in summary
    assert "caller -> callee" in summary
    assert "Build targets: console_script:pytest" in summary
    assert "Config files: pyproject.toml" in summary
    assert "abcdef1 add health endpoint (app/main.py)" in summary


def test_render_context_summary_handles_minimal_package():
    minimal = context_package_to_dict(
        ContextPackage(
            focus="x",
            symbols=(),
            references=(),
            related_tests=(),
            dependencies=(),
        )
    )

    summary = render_context_summary(minimal)

    assert "Repository: unknown @ unindexed" in summary
    assert "Focus: x" in summary
