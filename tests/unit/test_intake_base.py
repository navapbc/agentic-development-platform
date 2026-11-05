from __future__ import annotations

from services.orchestration.intake_base import declared_base, declared_issue_key


def test_single_declaration_is_read() -> None:
    assert declared_base(["base:feature-x"]).branch == "feature-x"


def test_no_label_is_no_declaration() -> None:
    declaration = declared_base(["a11y", "wave2"])

    assert declaration.branch == ""
    assert declaration.conflict == ""
    assert declaration.declared is False


def test_empty_value_is_no_declaration() -> None:
    declaration = declared_base(["base:"])

    assert declaration.branch == ""
    assert declaration.conflict == ""


def test_whitespace_value_is_no_declaration() -> None:
    assert declared_base(["base:   "]).branch == ""


def test_prefix_only_label_is_not_a_declaration() -> None:
    assert declared_base(["baseline", "based"]).branch == ""


def test_prefix_match_is_case_sensitive() -> None:
    assert declared_base(["Base:feature-x", "BASE:other"]).branch == ""


def test_repeated_identical_value_is_a_duplicate() -> None:
    declaration = declared_base(["base:x", "base:x"])

    assert declaration.branch == "x"
    assert declaration.conflict == ""


def test_two_different_values_conflict() -> None:
    declaration = declared_base(["base:x", "base:y"])

    assert declaration.branch == ""
    assert "x" in declaration.conflict
    assert "y" in declaration.conflict


def test_conflict_is_returned_as_data_not_raised() -> None:
    declaration = declared_base(["base:x", "base:y"])

    assert declaration.conflict != ""
    assert declaration.declared is False


def test_branch_containing_a_colon_is_preserved() -> None:
    assert declared_base(["base:feature/a:b"]).branch == "feature/a:b"


def test_slashed_branch_is_preserved() -> None:
    assert declared_base(["base:alice/PLAT-482"]).branch == "alice/PLAT-482"


def test_surrounding_whitespace_is_trimmed() -> None:
    assert declared_base(["base: feature-x "]).branch == "feature-x"


def test_tracker_key_is_read_and_upper_cased() -> None:
    assert declared_issue_key(["jira-proj-33739"]) == "PROJ-33739"


def test_tracker_key_ignores_unrelated_labels() -> None:
    assert declared_issue_key(["adp", "adp-pilot", "a11y-audit-2026"]) == ""


def test_tracker_key_is_empty_without_labels() -> None:
    assert declared_issue_key([]) == ""


def test_first_tracker_key_wins_when_several_are_declared() -> None:
    assert declared_issue_key(["jira-proj-1", "jira-proj-2"]) == "PROJ-1"


def test_tracker_key_rejects_a_non_numeric_issue_number() -> None:
    assert declared_issue_key(["jira-proj-backlog"]) == ""


def test_tracker_key_rejects_a_bare_prefix() -> None:
    assert declared_issue_key(["jira-"]) == ""


def test_tracker_key_trims_surrounding_whitespace() -> None:
    assert declared_issue_key([" jira-proj-33739 "]) == "PROJ-33739"
