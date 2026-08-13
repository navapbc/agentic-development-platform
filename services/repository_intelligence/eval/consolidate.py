"""Consolidate per-repo acceptance scorecards into a definition-of-done summary."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONSOLIDATION_VERSION = "acceptance_consolidation/1"

_Card = dict[str, Any]
_NamedCard = tuple[str, _Card]

_SCIP_TIER = "scip"
_LSP_TIER = "lsp"

_STRUCTURE_ORACLE_TIERS: dict[str, tuple[str, str]] = {
    "scip-python": ("python", _SCIP_TIER),
    "scip-typescript": ("typescript", _SCIP_TIER),
    "scip-java": ("java", _SCIP_TIER),
    "scip-ruby": ("ruby", _SCIP_TIER),
    "terraform-ls": ("terraform", _LSP_TIER),
    "phpactor": ("php", _LSP_TIER),
}


def language_and_tier(structure_oracle: str) -> tuple[str, str]:
    """Resolve a gold structure-oracle label to its language and semantic tier."""
    name = structure_oracle.split("@", 1)[0]
    if name not in _STRUCTURE_ORACLE_TIERS:
        raise ValueError(f"unknown structure oracle: {structure_oracle}")
    return _STRUCTURE_ORACLE_TIERS[name]


def authoritative_provider_name(language: str, tier: str) -> str:
    """Return the authoritative backend provider name for a language tier."""
    return f"{tier}-{language}"


def load_scorecards(scorecard_dir: Path) -> list[_NamedCard]:
    """Load every scorecard JSON in a directory as (filename, parsed) pairs."""
    cards: list[_NamedCard] = []
    for path in sorted(scorecard_dir.glob("*.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        if card.get("record_type") == "acceptance_consolidation":
            continue
        cards.append((path.name, card))
    return cards


def latest_per_repo(cards: list[_NamedCard]) -> dict[str, _NamedCard]:
    """Keep the newest scorecard per repo, ordered by generated_at."""
    latest: dict[str, _NamedCard] = {}
    for filename, card in cards:
        repo = card["repo"]
        current = latest.get(repo)
        if current is None or card["generated_at"] > current[1]["generated_at"]:
            latest[repo] = (filename, card)
    return latest


def _provider(card: _Card, name: str) -> dict[str, Any]:
    for provider in card["providers"]:
        if provider["provider"] == name:
            return provider
    raise ValueError(f"scorecard for {card['repo']} has no provider {name!r}")


def _slot(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": provider["provider"],
        "verdict": provider["verdict"],
        "total": provider["weighted_score"]["total"],
        "gates_pass": provider["hard_gates_pass"],
        "macro_f1": provider["macro_f1"],
    }


def consolidate(cards: list[_NamedCard]) -> dict[str, Any]:
    """Fold the latest scorecard per repo into a definition-of-done record."""
    latest = latest_per_repo(cards)
    repos: list[dict[str, Any]] = []
    gate_results: dict[str, bool] = {}
    tiers: dict[str, list[str]] = {_SCIP_TIER: [], _LSP_TIER: []}

    for filename, card in latest.values():
        structure = card["gold"]["oracles"]["structure"]
        language, tier = language_and_tier(structure)
        router = _provider(card, "router")
        authoritative = _provider(card, authoritative_provider_name(language, tier))
        passes = (
            router["verdict"] == "Primary"
            and router["hard_gates_pass"]
            and authoritative["verdict"] == "Primary"
            and authoritative["hard_gates_pass"]
        )
        for provider in (router, authoritative):
            for gate, result in provider["hard_gates"].items():
                gate_results[gate] = gate_results.get(gate, True) and result
        tiers[tier].append(language)
        repos.append(
            {
                "repo": card["repo"],
                "commit": card["commit"],
                "language": language,
                "tier": tier,
                "structure_oracle": structure,
                "runs": card["runs_per_provider"],
                "router": _slot(router),
                "authoritative": _slot(authoritative),
                "passes": passes,
                "source_scorecard": filename,
            }
        )

    repos.sort(key=lambda entry: (entry["tier"], entry["language"]))
    for tier in tiers:
        tiers[tier] = sorted(tiers[tier])
    definition_of_done = bool(repos) and all(entry["passes"] for entry in repos)

    return {
        "record_type": "acceptance_consolidation",
        "consolidation_version": CONSOLIDATION_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "definition_of_done": definition_of_done,
        "language_tiers": tiers,
        "hard_gates": dict(sorted(gate_results.items())),
        "repos": repos,
        "summary": {
            "repos": len(repos),
            "languages": len({entry["language"] for entry in repos}),
            "scip_tier": len(tiers[_SCIP_TIER]),
            "lsp_tier": len(tiers[_LSP_TIER]),
            "all_router_primary": all(entry["router"]["verdict"] == "Primary" for entry in repos),
            "all_authoritative_primary": all(
                entry["authoritative"]["verdict"] == "Primary" for entry in repos
            ),
        },
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--scorecards-dir", default=str(here / "scorecards"))
    parser.add_argument("--out", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cards = load_scorecards(Path(args.scorecards_dir).expanduser().resolve())
    record = consolidate(cards)
    text = json.dumps(record, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
