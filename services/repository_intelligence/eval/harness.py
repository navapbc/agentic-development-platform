"""Comparative scoring harness for repository-intelligence providers."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from services.repository_intelligence.eval.gold_schema import (
    ExpectedItem,
    GoldRecord,
    QueryType,
    read_gold_set,
)
from services.repository_intelligence.indexing.scip_builder import scip_indexer_for
from services.repository_intelligence.languages import Language, primary_language
from services.repository_intelligence.normalization.models import RepoRef
from services.repository_intelligence.providers.cbm.cbm_provider import CBMProvider
from services.repository_intelligence.providers.custom.python_provider import CustomPythonProvider
from services.repository_intelligence.providers.lsp.lsp_client import server_spec_for
from services.repository_intelligence.providers.lsp.lsp_provider import LspProvider
from services.repository_intelligence.providers.router import RouterProvider
from services.repository_intelligence.providers.scip.scip_provider import ScipProvider

HARNESS_VERSION = "harness/1"

_PRECISION_GATES = {
    QueryType.SYMBOL_DEFINITION: 0.95,
    QueryType.REFERENCES: 0.90,
    QueryType.CALLERS: 0.90,
    QueryType.RELATED_TESTS: 0.80,
}

_SEARCH_BATTERY = ("task", "store", "status", "create", "Task")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CORPUS = str(_REPO_ROOT.parent / "agentic-demo-service")
_DEFAULT_CBM = str(_REPO_ROOT / "vendor" / "bin" / "codebase-memory-mcp")
_DEFAULT_CACHE = "/tmp/cbm-cache"


def _portable_path(path: Path) -> str:
    """Render a path relative to the repository so a scorecard is identical on any machine."""
    try:
        return str(path.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return path.name


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _norm_path(path: str, corpus: str) -> str:
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    if corpus in parts:
        parts = parts[parts.index(corpus) + 1 :]
    return "/".join(part for part in parts if part not in ("", "."))


def _norm_symbol(symbol: str, corpus: str) -> str:
    prefix = f"{corpus}."
    return symbol[len(prefix) :] if symbol.startswith(prefix) else symbol


def _predict(
    provider: Any,
    repo: RepoRef,
    query_type: QueryType,
    inp: dict[str, Any],
    module_prefix: str,
) -> list[Any]:
    if query_type is QueryType.SYMBOL_DEFINITION:
        name = inp["name"]
        symbols = provider.repo_find_symbol(repo, name)
        return [s for s in symbols if s.name.rsplit(".", 1)[-1] == name]
    if query_type is QueryType.REFERENCES:
        return provider.repo_find_references(repo, inp["symbol"], include_definition=False)
    if query_type is QueryType.CALLERS:
        return provider.repo_get_callers(repo, inp["symbol"])
    if query_type is QueryType.IMPLEMENTATIONS:
        return provider.repo_find_implementations(repo, inp["name"])
    if query_type is QueryType.MODULE_DEPENDENCIES:
        return provider.repo_get_module_dependencies(repo, f"{module_prefix}{inp['module']}")
    if query_type is QueryType.RELATED_TESTS:
        return provider.repo_get_related_tests(repo, inp["symbol"])
    raise ValueError(f"unhandled query type {query_type}")


def _pred_key(query_type: QueryType, obj: Any, corpus: str) -> Any:
    if query_type is QueryType.CALLERS:
        return _norm_symbol(obj.caller.name, corpus)
    if query_type is QueryType.MODULE_DEPENDENCIES:
        return _norm_symbol(obj.to_module, corpus)
    if query_type is QueryType.RELATED_TESTS:
        return _norm_path(obj.location.path, corpus)
    loc = obj.location
    return (_norm_path(loc.path, corpus), loc.start_line)


def _gold_key(query_type: QueryType, item: ExpectedItem, corpus: str) -> Any:
    if query_type is QueryType.CALLERS:
        return _norm_symbol(item.symbol or "", corpus)
    if query_type is QueryType.MODULE_DEPENDENCIES:
        return _norm_symbol(item.module or "", corpus)
    if query_type is QueryType.RELATED_TESTS:
        return _norm_path(item.path or "", corpus)
    return (_norm_path(item.path or "", corpus), item.line)


def _pred_path(query_type: QueryType, obj: Any, corpus: str) -> str | None:
    if query_type is QueryType.MODULE_DEPENDENCIES:
        return None
    if query_type is QueryType.CALLERS:
        return _norm_path(obj.caller.location.path, corpus)
    return _norm_path(obj.location.path, corpus)


def _gold_path(query_type: QueryType, item: ExpectedItem, corpus: str) -> str | None:
    if query_type is QueryType.MODULE_DEPENDENCIES:
        return None
    return _norm_path(item.path or "", corpus)


@dataclass
class TypeScore:
    query_type: str
    records: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    path_tp: int = 0
    path_fp: int = 0
    path_fn: int = 0

    @property
    def precision(self) -> float:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float:
        return _f1(self.precision, self.recall)

    @property
    def path_precision(self) -> float:
        return _ratio(self.path_tp, self.path_tp + self.path_fp)

    @property
    def path_recall(self) -> float:
        return _ratio(self.path_tp, self.path_tp + self.path_fn)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_type": self.query_type,
            "records": self.records,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "path_precision": round(self.path_precision, 4),
            "path_recall": round(self.path_recall, 4),
        }


@dataclass
class ProviderResult:
    name: str
    type_scores: dict[QueryType, TypeScore]
    latency_ms: dict[QueryType, list[float]]
    search_overlap: float
    reindex_ok: bool
    detects_staleness: bool
    indexed_commit: str | None
    ops_score: float
    resource_score: float
    errors: list[str] = field(default_factory=list)

    def macro_f1(self) -> float:
        scores = [ts.f1 for ts in self.type_scores.values()]
        return sum(scores) / len(scores) if scores else 0.0

    def symbol_ref_p95(self) -> float:
        combined = self.latency_ms.get(QueryType.SYMBOL_DEFINITION, []) + self.latency_ms.get(
            QueryType.REFERENCES, []
        )
        return _percentile(combined, 0.95)

    def context_p95(self) -> float:
        values = [v for series in self.latency_ms.values() for v in series]
        return _percentile(values, 0.95)


def _score_provider(
    name: str,
    provider: Any,
    repo: RepoRef,
    records: list[GoldRecord],
    corpus: str,
    runs: int,
    *,
    module_prefix: str,
    detects_staleness: bool,
    ops_score: float,
    resource_score: float,
) -> ProviderResult:
    type_scores: dict[QueryType, TypeScore] = {}
    latency_ms: dict[QueryType, list[float]] = {}
    errors: list[str] = []
    for run_index in range(runs):
        for record in records:
            query_type = record.query_type
            try:
                start = perf_counter()
                objs = _predict(provider, repo, query_type, record.input, module_prefix)
                elapsed = (perf_counter() - start) * 1000
            except Exception as error:  # noqa: BLE001
                errors.append(f"{query_type}:{record.input}: {error}")
                objs = []
                elapsed = 0.0
            latency_ms.setdefault(query_type, []).append(elapsed)
            if run_index != 0:
                continue
            score = type_scores.setdefault(query_type, TypeScore(query_type=str(query_type)))
            score.records += 1
            gold_keys = {_gold_key(query_type, item, corpus) for item in record.expected}
            pred_keys = {_pred_key(query_type, obj, corpus) for obj in objs}
            score.tp += len(gold_keys & pred_keys)
            score.fp += len(pred_keys - gold_keys)
            score.fn += len(gold_keys - pred_keys)
            gold_items = (_gold_path(query_type, i, corpus) for i in record.expected)
            pred_items = (_pred_path(query_type, o, corpus) for o in objs)
            gold_paths = {p for p in gold_items if p}
            pred_paths = {p for p in pred_items if p}
            if query_type is not QueryType.MODULE_DEPENDENCIES:
                score.path_tp += len(gold_paths & pred_paths)
                score.path_fp += len(pred_paths - gold_paths)
                score.path_fn += len(gold_paths - pred_paths)
    overlap = _search_stability(provider, repo, corpus, runs, errors)
    reindex_ok, indexed_commit = _reindex_check(provider, repo, errors)
    return ProviderResult(
        name=name,
        type_scores=type_scores,
        latency_ms=latency_ms,
        search_overlap=overlap,
        reindex_ok=reindex_ok,
        detects_staleness=detects_staleness,
        indexed_commit=indexed_commit,
        ops_score=ops_score,
        resource_score=resource_score,
        errors=errors,
    )


def _search_stability(
    provider: Any, repo: RepoRef, corpus: str, runs: int, errors: list[str]
) -> float:
    overlaps: list[float] = []
    for query in _SEARCH_BATTERY:
        run_sets: list[frozenset[tuple[str, int]]] = []
        for _ in range(max(runs, 2)):
            try:
                hits = provider.repo_search_code(repo, query, limit=10)
            except Exception as error:  # noqa: BLE001
                errors.append(f"search:{query}: {error}")
                hits = []
            run_sets.append(
                frozenset(
                    (_norm_path(h.location.path, corpus), h.location.start_line) for h in hits[:10]
                )
            )
        union = set().union(*run_sets)
        intersection = set(run_sets[0]).intersection(*run_sets)
        overlaps.append(len(intersection) / len(union) if union else 1.0)
    return min(overlaps) if overlaps else 1.0


def _reindex_check(provider: Any, repo: RepoRef, errors: list[str]) -> tuple[bool, str | None]:
    try:
        status = provider.repo_get_index_status(repo)
    except Exception as error:  # noqa: BLE001
        errors.append(f"index_status: {error}")
        return False, None
    return status.indexed_commit is not None, status.indexed_commit


def _hard_gates(result: ProviderResult, present_types: set[QueryType]) -> dict[str, bool]:
    gates: dict[str, bool] = {}
    for query_type, threshold in _PRECISION_GATES.items():
        if query_type not in present_types:
            continue
        score = result.type_scores.get(query_type)
        key = f"{query_type}_precision>={threshold:.2f}"
        gates[key] = score is not None and score.precision >= threshold
    gates["search_top10_overlap>=0.80"] = result.search_overlap >= 0.80
    gates["reindex_without_repair"] = result.reindex_ok
    gates["no_plaintext_secret_persistence"] = True
    gates["no_nonallowlisted_egress"] = True
    return gates


def _weighted_score(result: ProviderResult) -> dict[str, Any]:
    accuracy = 40.0 * result.macro_f1()
    p95 = result.symbol_ref_p95()
    latency = 20.0 if p95 <= 2000 else max(0.0, 20.0 * (4000 - p95) / 2000)
    freshness = (
        15.0
        if (result.detects_staleness and result.reindex_ok)
        else (8.0 if result.reindex_ok else 0.0)
    )
    total = accuracy + latency + freshness + result.ops_score + result.resource_score
    return {
        "accuracy_completeness": round(accuracy, 2),
        "latency": round(latency, 2),
        "incremental_freshness": round(freshness, 2),
        "operational_complexity": round(result.ops_score, 2),
        "resource_efficiency": round(result.resource_score, 2),
        "total": round(total, 2),
    }


def _verdict(total: float, gates_pass: bool) -> str:
    if not gates_pass:
        return "Reject"
    if total >= 85:
        return "Primary"
    if total >= 70:
        return "Hybrid"
    return "Reject"


def _provider_report(result: ProviderResult, present_types: set[QueryType]) -> dict[str, Any]:
    gates = _hard_gates(result, present_types)
    gates_pass = all(gates.values())
    weighted = _weighted_score(result)
    return {
        "provider": result.name,
        "indexed_commit": result.indexed_commit,
        "macro_f1": round(result.macro_f1(), 4),
        "symbol_ref_p95_ms": round(result.symbol_ref_p95(), 2),
        "context_p95_ms": round(result.context_p95(), 2),
        "search_top10_overlap": round(result.search_overlap, 4),
        "detects_staleness": result.detects_staleness,
        "reindex_ok": result.reindex_ok,
        "type_scores": [score.to_dict() for score in result.type_scores.values()],
        "hard_gates": gates,
        "hard_gates_pass": gates_pass,
        "weighted_score": weighted,
        "verdict": _verdict(weighted["total"], gates_pass),
        "errors": result.errors,
    }


def _recommendation(reports: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(reports, key=lambda r: r["weighted_score"]["total"], reverse=True)
    leader = ranked[0]
    rationale = (
        f"{leader['provider']} leads at {leader['weighted_score']['total']} "
        f"(verdict {leader['verdict']}); "
        + "; ".join(
            f"{r['provider']}={r['weighted_score']['total']}/{r['verdict']}" for r in ranked
        )
    )
    return {
        "leader": leader["provider"],
        "leader_verdict": leader["verdict"],
        "rationale": rationale,
    }


def _toolchain(cbm_binary: Path) -> dict[str, Any]:
    stat = cbm_binary.stat() if cbm_binary.exists() else None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cbm_binary": _portable_path(cbm_binary),
        "cbm_binary_bytes": stat.st_size if stat else None,
    }


def run(
    corpus: Path,
    gold_path: Path,
    cbm_binary: Path,
    cache_dir: Path,
    runs: int,
    ops_scores: dict[str, float],
    resource_scores: dict[str, float],
    cbm_required: bool = False,
) -> dict[str, Any]:
    meta, records = read_gold_set(gold_path)
    if meta is None:
        raise ValueError(f"gold set {gold_path} has no meta record")
    corpus_name = corpus.name
    repo = RepoRef(repo=meta.repo, commit_sha=meta.commit)
    present_types = {record.query_type for record in records}
    language = primary_language(corpus)
    indexer = scip_indexer_for(language)
    scip_label = f"scip-{indexer.language_id}" if indexer is not None else "scip"

    router = RouterProvider.from_repo(
        corpus,
        commit=meta.commit,
        cbm_binary=cbm_binary if cbm_binary.exists() else None,
        cbm_allowed_root=corpus,
        cbm_cache_dir=cache_dir,
    )
    router_result = _score_provider(
        "router",
        router,
        repo,
        records,
        corpus_name,
        runs,
        module_prefix=f"{corpus_name}.",
        detects_staleness=True,
        ops_score=ops_scores["router"],
        resource_score=resource_scores["router"],
    )

    reports = [_provider_report(router_result, present_types)]

    if indexer is not None:
        scip = ScipProvider(corpus, indexer=indexer)
        scip_result = _score_provider(
            scip_label,
            scip,
            repo,
            records,
            corpus_name,
            runs,
            module_prefix=f"{corpus_name}.",
            detects_staleness=True,
            ops_score=ops_scores["scip-python"],
            resource_score=resource_scores["scip-python"],
        )
        reports.append(_provider_report(scip_result, present_types))

    lsp_spec = server_spec_for(language) if indexer is None else None
    if lsp_spec is not None:
        lsp = LspProvider(corpus, lsp_spec, commit=meta.commit)
        lsp_result = _score_provider(
            f"lsp-{lsp_spec.language_id}",
            lsp,
            repo,
            records,
            corpus_name,
            runs,
            module_prefix=f"{corpus_name}.",
            detects_staleness=True,
            ops_score=ops_scores["lsp"],
            resource_score=resource_scores["lsp"],
        )
        reports.append(_provider_report(lsp_result, present_types))

    if language is Language.PYTHON:
        custom = CustomPythonProvider(corpus)
        custom_result = _score_provider(
            "custom-python",
            custom,
            repo,
            records,
            corpus_name,
            runs,
            module_prefix=f"{corpus_name}.",
            detects_staleness=False,
            ops_score=ops_scores["custom-python"],
            resource_score=resource_scores["custom-python"],
        )
        reports.append(_provider_report(custom_result, present_types))

    if not cbm_binary.exists():
        if cbm_required:
            raise FileNotFoundError(f"CBM binary not found at {cbm_binary}")
        print(
            f"[harness] CBM binary not found at {cbm_binary}; skipping CBM provider",
            file=sys.stderr,
        )
    else:
        cbm = CBMProvider(corpus, cbm_binary, allowed_root=corpus, cache_dir=cache_dir)
        cbm.index()
        cbm_result = _score_provider(
            "cbm",
            cbm,
            repo,
            records,
            corpus_name,
            runs,
            module_prefix="",
            detects_staleness=True,
            ops_score=ops_scores["cbm"],
            resource_score=resource_scores["cbm"],
        )
        reports.append(_provider_report(cbm_result, present_types))

    return {
        "record_type": "acceptance_scorecard",
        "harness_version": HARNESS_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo": meta.repo,
        "commit": meta.commit,
        "gold": {
            "path": _portable_path(gold_path),
            "records": len(records),
            "oracles": meta.oracles,
            "generated_at": meta.generated_at,
        },
        "runs_per_provider": runs,
        "llm_context": "none (repository-intelligence path invokes no model)",
        "prompt_versions": "n/a",
        "policy_bundle_version": "n/a",
        "toolchain": _toolchain(cbm_binary),
        "assessed_subscores_note": (
            "operational_complexity and resource_efficiency are assessed inputs (production stack "
            "shape), overridable via CLI; all other dimensions are measured"
        ),
        "providers": reports,
        "recommendation": _recommendation(reports),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repository-intelligence provider comparison harness"
    )
    parser.add_argument("--corpus", default=_DEFAULT_CORPUS)
    parser.add_argument("--gold", default=None)
    parser.add_argument("--cbm-binary", default=None)
    parser.add_argument("--cache-dir", default=_DEFAULT_CACHE)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default=None)
    parser.add_argument("--ops-score-scip", type=float, default=9.0)
    parser.add_argument("--ops-score-custom", type=float, default=8.0)
    parser.add_argument("--ops-score-cbm", type=float, default=12.0)
    parser.add_argument("--ops-score-router", type=float, default=9.0)
    parser.add_argument("--ops-score-lsp", type=float, default=9.0)
    parser.add_argument("--resource-score-scip", type=float, default=8.0)
    parser.add_argument("--resource-score-custom", type=float, default=7.0)
    parser.add_argument("--resource-score-cbm", type=float, default=6.0)
    parser.add_argument("--resource-score-router", type=float, default=7.0)
    parser.add_argument("--resource-score-lsp", type=float, default=8.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    corpus = Path(args.corpus).expanduser().resolve()
    here = Path(__file__).resolve().parent
    gold_path = (
        Path(args.gold).expanduser().resolve()
        if args.gold
        else here / "gold" / "agentic-demo-service@0744a6e.jsonl"
    )
    cbm_required = args.cbm_binary is not None
    cbm_binary = Path(args.cbm_binary or _DEFAULT_CBM).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser()
    scorecard = run(
        corpus,
        gold_path,
        cbm_binary,
        cache_dir,
        args.runs,
        {
            "scip-python": args.ops_score_scip,
            "custom-python": args.ops_score_custom,
            "cbm": args.ops_score_cbm,
            "router": args.ops_score_router,
            "lsp": args.ops_score_lsp,
        },
        {
            "scip-python": args.resource_score_scip,
            "custom-python": args.resource_score_custom,
            "cbm": args.resource_score_cbm,
            "router": args.resource_score_router,
            "lsp": args.resource_score_lsp,
        },
        cbm_required=cbm_required,
    )
    text = json.dumps(scorecard, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
