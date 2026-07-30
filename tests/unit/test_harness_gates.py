from types import SimpleNamespace

from services.repository_intelligence.eval.gold_schema import QueryType
from services.repository_intelligence.eval.harness import _hard_gates


def _result(type_scores):
    return SimpleNamespace(
        type_scores=type_scores,
        search_overlap=1.0,
        reindex_ok=True,
    )


def test_hard_gates_skip_query_types_absent_from_gold():
    result = _result({})
    gates = _hard_gates(result, present_types=set())
    assert not any(key.endswith("_precision>=0.95") for key in gates)
    assert not any("_precision>=" in key for key in gates)
    assert gates["reindex_without_repair"] is True


def test_hard_gates_apply_present_query_types():
    score = SimpleNamespace(precision=0.99)
    result = _result({QueryType.SYMBOL_DEFINITION: score})
    gates = _hard_gates(result, present_types={QueryType.SYMBOL_DEFINITION})
    key = f"{QueryType.SYMBOL_DEFINITION}_precision>=0.95"
    assert gates[key] is True


def test_hard_gates_fail_present_type_without_score():
    result = _result({})
    gates = _hard_gates(result, present_types={QueryType.CALLERS})
    key = f"{QueryType.CALLERS}_precision>=0.90"
    assert gates[key] is False
