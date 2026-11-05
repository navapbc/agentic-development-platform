from __future__ import annotations

from services.integration.jira.intake_read import _candidate_jql


def test_candidate_jql_without_label_admits_all_open_issues():
    jql = _candidate_jql("ADEV")
    assert jql == 'project = "ADEV" AND statusCategory != Done ORDER BY created ASC'


def test_candidate_jql_with_label_gates_on_the_label():
    jql = _candidate_jql("ADEV", "adp")
    assert (
        jql == 'project = "ADEV" AND statusCategory != Done AND labels = "adp" ORDER BY created ASC'
    )
