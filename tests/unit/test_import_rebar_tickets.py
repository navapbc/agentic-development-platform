from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("rebar")
pytest.importorskip("openpyxl")

from scripts.import_rebar_tickets import import_xlsx, reset_store  # noqa: E402

_HEADER = [
    "Summary",
    "Issue key",
    "Issue Type",
    "Status",
    "Priority",
    "Description",
    "Custom field (Acceptance Criteria)",
    "Components",
    "Parent key",
    "Parent summary",
    "Comment",
    "Comment",
]


def _write_xlsx(path: Path, rows: list[list[object]]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(_HEADER)
    for row in rows:
        ws.append(row + [None] * (len(_HEADER) - len(row)))
    wb.save(path)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    reset_store(root)
    return root


def _by_key(root: Path) -> dict[str, dict]:
    import rebar

    out: dict[str, dict] = {}
    for t in rebar.list_tickets(repo_root=str(root), include_archived=True):
        out[str(t.get("source_id") or t.get("title"))] = t
    return out


def test_type_is_coerced_without_dropping_tickets(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["A task", "P-1", "Task", "To Do", "Medium", "d", "", "", "", ""],
            ["A design", "P-2", "Design Task", "To Do", "Medium", "d", "", "", "", ""],
            ["A spike", "P-3", "Spike", "To Do", "Medium", "d", "", "", "", ""],
            ["A bug", "P-4", "Bug", "To Do", "Medium", "d", "", "", "", ""],
        ],
    )
    report = import_xlsx(xlsx, store)
    assert len(report.created) == 4
    tickets = _by_key(store)
    assert tickets["P-1"]["ticket_type"] == "task"
    assert tickets["P-2"]["ticket_type"] == "task"
    assert tickets["P-4"]["ticket_type"] == "bug"
    # the original Jira type is preserved in the description when coerced
    assert "Jira type: Design Task" in tickets["P-2"]["description"]
    assert "Jira type: Spike" in tickets["P-3"]["description"]


def test_priority_is_mapped_to_the_rebar_range(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["Highest", "P-1", "Task", "To Do", "Highest", "d", "", "", "", ""],
            ["Medium", "P-2", "Task", "To Do", "Medium", "d", "", "", "", ""],
            ["Lowest", "P-3", "Task", "To Do", "Lowest", "d", "", "", "", ""],
            ["None", "P-4", "Task", "To Do", "", "d", "", "", "", ""],
        ],
    )
    import_xlsx(xlsx, store)
    tickets = _by_key(store)
    assert tickets["P-1"]["priority"] == 0
    assert tickets["P-2"]["priority"] == 2
    assert tickets["P-3"]["priority"] == 4
    assert tickets["P-4"]["priority"] == 2


def test_status_is_mapped_to_the_rebar_vocabulary(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["todo", "P-1", "Task", "To Do", "Medium", "d", "", "", "", ""],
            ["done", "P-2", "Task", "Done", "Medium", "d", "", "", "", ""],
            ["backlog", "P-3", "Task", "Backlog", "Medium", "d", "", "", "", ""],
            ["prog", "P-4", "Task", "In Progress", "Medium", "d", "", "", "", ""],
        ],
    )
    import_xlsx(xlsx, store)
    tickets = _by_key(store)
    assert tickets["P-1"]["status"] == "open"
    assert tickets["P-2"]["status"] == "closed"
    assert tickets["P-3"]["status"] == "idea"
    assert tickets["P-4"]["status"] == "in_progress"


def test_backlog_and_done_are_excluded_from_ready(store: Path, tmp_path: Path) -> None:
    import rebar

    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["todo", "P-1", "Task", "To Do", "Medium", "d", "", "", "", ""],
            ["done", "P-2", "Task", "Done", "Medium", "d", "", "", "", ""],
            ["backlog", "P-3", "Task", "Backlog", "Medium", "d", "", "", "", ""],
        ],
    )
    import_xlsx(xlsx, store)
    ready_keys = {str(t.get("source_id")) for t in rebar.ready(repo_root=str(store))}
    assert ready_keys == {"P-1"}


def test_acceptance_criteria_folding_all_four_cases(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["both", "P-1", "Task", "To Do", "Medium", "the desc", "the ac", "", "", ""],
            ["desc only", "P-2", "Task", "To Do", "Medium", "the desc", "", "", "", ""],
            ["ac only", "P-3", "Task", "To Do", "Medium", "", "the ac", "", "", ""],
            ["neither", "P-4", "Task", "To Do", "Medium", "", "", "", "", ""],
        ],
    )
    import_xlsx(xlsx, store)
    tickets = _by_key(store)
    assert "the desc" in tickets["P-1"]["description"]
    assert "Acceptance Criteria:\nthe ac" in tickets["P-1"]["description"]
    assert tickets["P-2"]["description"] == "the desc"
    assert tickets["P-3"]["description"] == "Acceptance Criteria:\nthe ac"
    assert not (tickets["P-4"].get("description") or "")


def test_comments_are_carried_across(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [["c", "P-1", "Task", "To Do", "Medium", "d", "", "", "", "", "first note", "second note"]],
    )
    import_xlsx(xlsx, store)
    ticket = _by_key(store)["P-1"]
    bodies = [c.get("body") for c in ticket.get("comments") or []]
    assert "first note" in bodies
    assert "second note" in bodies


def test_limit_bounds_the_import(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            [f"t{i}", f"P-{i}", "Task", "To Do", "Medium", "d", "", "", "", ""]
            for i in range(10)
        ],
    )
    report = import_xlsx(xlsx, store, limit=3)
    assert len(report.created) == 3


def test_status_filter_selects_a_subset(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(
        xlsx,
        [
            ["todo", "P-1", "Task", "To Do", "Medium", "d", "", "", "", ""],
            ["done", "P-2", "Task", "Done", "Medium", "d", "", "", "", ""],
        ],
    )
    report = import_xlsx(xlsx, store, statuses={"to do"})
    assert report.created == ["P-1"]


def test_reimport_is_idempotent_by_issue_key(store: Path, tmp_path: Path) -> None:
    xlsx = tmp_path / "x.xlsx"
    _write_xlsx(xlsx, [["t", "P-1", "Task", "To Do", "Medium", "d", "", "", "", ""]])
    first = import_xlsx(xlsx, store)
    second = import_xlsx(xlsx, store)
    assert first.created == ["P-1"]
    assert second.created == []
    assert second.skipped == ["P-1"]
