"""Unit tests for the acceptance-scorecard consolidation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.repository_intelligence.eval import consolidate


def _scorecard(repo: str, structure: str, authoritative: str, *, generated_at: str) -> dict:
    def provider(name: str, verdict: str, total: float, gates_pass: bool) -> dict:
        return {
            "provider": name,
            "verdict": verdict,
            "macro_f1": 1.0,
            "hard_gates_pass": gates_pass,
            "hard_gates": {
                "symbol_definition_precision>=0.95": gates_pass,
                "references_precision>=0.90": gates_pass,
            },
            "weighted_score": {"total": total},
        }

    return {
        "repo": repo,
        "commit": f"{repo}-sha",
        "generated_at": generated_at,
        "runs_per_provider": 3,
        "gold": {"oracles": {"structure": structure}},
        "providers": [
            provider("router", "Primary", 91.0, True),
            provider(authoritative, "Primary", 92.0, True),
        ],
    }


def _write(tmp_path: Path, name: str, card: dict) -> None:
    (tmp_path / name).write_text(json.dumps(card), encoding="utf-8")


def test_language_and_tier_maps_scip_and_lsp_oracles() -> None:
    assert consolidate.language_and_tier("scip-ruby@0.4.7") == ("ruby", "scip")
    assert consolidate.language_and_tier("terraform-ls@0.39.0") == ("terraform", "lsp")
    assert consolidate.language_and_tier("phpactor@2026.07.22.0") == ("php", "lsp")


def test_language_and_tier_rejects_unknown_oracle() -> None:
    with pytest.raises(ValueError, match="unknown structure oracle"):
        consolidate.language_and_tier("mystery@1.0")


def test_consolidate_reports_definition_of_done_across_tiers(tmp_path: Path) -> None:
    _write(tmp_path, "ruby.json", _scorecard("r", "scip-ruby@0.4.7", "scip-ruby", generated_at="1"))
    _write(tmp_path, "php.json", _scorecard("p", "phpactor@1", "lsp-php", generated_at="1"))
    record = consolidate.consolidate(consolidate.load_scorecards(tmp_path))

    assert record["definition_of_done"] is True
    assert record["language_tiers"] == {"scip": ["ruby"], "lsp": ["php"]}
    assert record["summary"]["languages"] == 2
    assert [entry["language"] for entry in record["repos"]] == ["php", "ruby"]


def test_consolidate_keeps_newest_scorecard_per_repo(tmp_path: Path) -> None:
    old = _scorecard("svc", "scip-python@0.6.6", "scip-python", generated_at="2026-07-27")
    new = _scorecard("svc", "scip-python@0.6.6", "scip-python", generated_at="2026-07-29")
    new["commit"] = "newer"
    _write(tmp_path, "svc-old.json", old)
    _write(tmp_path, "svc-new.json", new)
    record = consolidate.consolidate(consolidate.load_scorecards(tmp_path))

    assert len(record["repos"]) == 1
    assert record["repos"][0]["commit"] == "newer"


def test_consolidate_fails_done_when_authoritative_is_not_primary(tmp_path: Path) -> None:
    card = _scorecard("r", "scip-ruby@0.4.7", "scip-ruby", generated_at="1")
    card["providers"][1]["verdict"] = "Reject"
    card["providers"][1]["hard_gates_pass"] = False
    _write(tmp_path, "ruby.json", card)
    record = consolidate.consolidate(consolidate.load_scorecards(tmp_path))

    assert record["definition_of_done"] is False
    assert record["repos"][0]["passes"] is False


def test_load_scorecards_skips_prior_consolidation(tmp_path: Path) -> None:
    _write(tmp_path, "ruby.json", _scorecard("r", "scip-ruby@0.4.7", "scip-ruby", generated_at="1"))
    (tmp_path / "acceptance-consolidated.json").write_text(
        json.dumps({"record_type": "acceptance_consolidation"}), encoding="utf-8"
    )
    cards = consolidate.load_scorecards(tmp_path)

    assert [name for name, _ in cards] == ["ruby.json"]
