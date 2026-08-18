"""Unit tests for Jira intake target-repository resolution."""

from __future__ import annotations

from services.integration.jira.credentials import ComponentRepos
from services.integration.jira.intake_targets import resolve_targets

_ALLOW = frozenset({"repo-a", "repo-b", "repo-c"})

_COMPONENTS = ComponentRepos(
    (
        ("backend", ("repo-a",)),
        ("frontend", ("repo-b",)),
        ("shared", ("repo-a", "repo-c")),
        ("ghost", ("ghost-repo",)),
    )
)


def test_default_repo_when_no_marker_or_scan():
    resolution = resolve_targets("Plain description.", default_repo="repo-a", allowlist=_ALLOW)
    assert resolution.routable
    assert resolution.repos == ("repo-a",)
    assert resolution.reason == "default_repo"


def test_marker_single_repo_wins_over_default():
    resolution = resolve_targets("repos: repo-b", default_repo="repo-a", allowlist=_ALLOW)
    assert resolution.routable
    assert resolution.repos == ("repo-b",)
    assert resolution.reason == "marker"


def test_marker_multi_repo_fans_out():
    resolution = resolve_targets("repos: repo-a, repo-c", default_repo="", allowlist=_ALLOW)
    assert resolution.routable
    assert resolution.repos == ("repo-a", "repo-c")


def test_singular_repo_marker_is_accepted():
    resolution = resolve_targets("repo: repo-b", default_repo="", allowlist=_ALLOW)
    assert resolution.repos == ("repo-b",)


def test_scan_matches_whole_word_slug():
    resolution = resolve_targets(
        "Work happens in repo-c today.", default_repo="repo-a", allowlist=_ALLOW
    )
    assert resolution.routable
    assert resolution.repos == ("repo-c",)
    assert resolution.reason == "description"


def test_scan_ignores_substring_of_a_larger_token():
    resolution = resolve_targets(
        "See repo-ant for context.", default_repo="repo-b", allowlist=_ALLOW
    )
    assert resolution.repos == ("repo-b",)
    assert resolution.reason == "default_repo"


def test_marker_with_unlisted_repo_holds_all_or_nothing():
    resolution = resolve_targets(
        "repos: repo-a, ghost-repo", default_repo="repo-a", allowlist=_ALLOW
    )
    assert not resolution.routable
    assert resolution.repos == ()
    assert "ghost-repo" in resolution.reason


def test_no_repository_and_no_default_holds():
    resolution = resolve_targets("Nothing here.", default_repo="", allowlist=_ALLOW)
    assert not resolution.routable
    assert resolution.repos == ()


def test_duplicate_markers_are_deduplicated_in_order():
    resolution = resolve_targets("repos: repo-b, repo-b, repo-a", default_repo="", allowlist=_ALLOW)
    assert resolution.repos == ("repo-b", "repo-a")


def test_component_mapping_resolves_a_single_repo():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="",
        allowlist=_ALLOW,
        components=("Backend",),
        component_map=_COMPONENTS,
    )
    assert resolution.routable
    assert resolution.repos == ("repo-a",)
    assert resolution.reason == "components"


def test_component_mapping_fans_out_and_dedupes_the_union():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="",
        allowlist=_ALLOW,
        components=("Backend", "Shared"),
        component_map=_COMPONENTS,
    )
    assert resolution.repos == ("repo-a", "repo-c")
    assert resolution.reason == "components"


def test_component_mapping_is_case_insensitive():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="",
        allowlist=_ALLOW,
        components=("FRONTEND",),
        component_map=_COMPONENTS,
    )
    assert resolution.repos == ("repo-b",)


def test_explicit_marker_overrides_component_mapping():
    resolution = resolve_targets(
        "repos: repo-c",
        default_repo="",
        allowlist=_ALLOW,
        components=("Backend",),
        component_map=_COMPONENTS,
    )
    assert resolution.repos == ("repo-c",)
    assert resolution.reason == "marker"


def test_component_mapping_takes_precedence_over_scan_and_default():
    resolution = resolve_targets(
        "Work happens in repo-c today.",
        default_repo="repo-b",
        allowlist=_ALLOW,
        components=("Backend",),
        component_map=_COMPONENTS,
    )
    assert resolution.repos == ("repo-a",)
    assert resolution.reason == "components"


def test_unmapped_component_falls_through_to_default():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="repo-b",
        allowlist=_ALLOW,
        components=("Documentation",),
        component_map=_COMPONENTS,
    )
    assert resolution.repos == ("repo-b",)
    assert resolution.reason == "default_repo"


def test_component_mapping_to_unlisted_repo_holds_all_or_nothing():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="repo-a",
        allowlist=_ALLOW,
        components=("Ghost",),
        component_map=_COMPONENTS,
    )
    assert not resolution.routable
    assert resolution.repos == ()
    assert "ghost-repo" in resolution.reason


def test_no_component_map_preserves_legacy_precedence():
    resolution = resolve_targets(
        "Plain description.",
        default_repo="repo-a",
        allowlist=_ALLOW,
        components=("Backend",),
    )
    assert resolution.repos == ("repo-a",)
    assert resolution.reason == "default_repo"
