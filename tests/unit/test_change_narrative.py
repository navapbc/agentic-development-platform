from services.orchestration.change_narrative import (
    PullRequestNarrative,
    compose_commit_message,
    compose_pull_request_body,
    compose_testing_evidence,
    read_pull_request_template,
)

MASSGOV_TEMPLATE = """## 🎫 Ticket

https://lwd.atlassian.net/browse/{TICKET ID}
Subtask: https://lwd.atlassian.net/browse/{SUBTASK TICKET ID}

## 🛠 Changes

(What was added, updated, or removed in this PR.)

## ℹ️ Context for reviewers

(Background context, more in-depth details of the implementation.)

## 👀 Testing

(Provide evidence that the code works as expected.)

#### Previews and UAT

Check the box below if your Portal PR will require UAT and is not feature-flagged.

- [ ] Deploy Portal preview (connected to API TST1)

## 📜 Link to relevant documentation

(Provide links to documentation, such as tech specs.)
"""


def narrative(**overrides: str) -> PullRequestNarrative:
    defaults = {
        "issue_key": "PSD-6948",
        "execution_id": "exec-9",
        "summary": "Removed the deprecated size prop from the Button component.",
        "description": "The size prop is unused and blocks the design system upgrade.",
        "testing": "Repository checks passed in the platform pipeline.",
    }
    defaults.update(overrides)
    return PullRequestNarrative(**defaults)  # type: ignore[arg-type]


def test_body_keeps_every_template_heading_in_order():
    body = compose_pull_request_body(MASSGOV_TEMPLATE, narrative())

    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 🎫 Ticket",
        "## 🛠 Changes",
        "## ℹ️ Context for reviewers",
        "## 👀 Testing",
        "## 📜 Link to relevant documentation",
    ]


def test_body_resolves_the_ticket_token_and_drops_the_unresolved_subtask_line():
    body = compose_pull_request_body(MASSGOV_TEMPLATE, narrative())

    assert "https://lwd.atlassian.net/browse/PSD-6948" in body
    assert "SUBTASK" not in body
    assert "{" not in body


def test_body_replaces_placeholder_prose_with_the_narrative():
    body = compose_pull_request_body(MASSGOV_TEMPLATE, narrative())

    assert "Removed the deprecated size prop from the Button component." in body
    assert "(What was added, updated, or removed in this PR.)" not in body
    assert "The size prop is unused and blocks the design system upgrade." in body
    assert "Repository checks passed in the platform pipeline." in body


def test_body_preserves_checkboxes_and_instructions_inside_a_filled_section():
    body = compose_pull_request_body(MASSGOV_TEMPLATE, narrative())

    assert "- [ ] Deploy Portal preview (connected to API TST1)" in body
    assert "#### Previews and UAT" in body


def test_body_marks_a_section_it_cannot_populate_rather_than_inventing_content():
    body = compose_pull_request_body(MASSGOV_TEMPLATE, narrative())

    documentation = body.split("## 📜 Link to relevant documentation", 1)[1]
    assert "(Provide links to documentation, such as tech specs.)" not in documentation
    assert "_None supplied by the platform._" in documentation


def test_body_falls_back_to_a_plain_summary_when_the_repository_has_no_template():
    body = compose_pull_request_body("", narrative())

    assert body.startswith("## Ticket")
    assert "PSD-6948" in body
    assert "Removed the deprecated size prop from the Button component." in body
    assert "Repository checks passed in the platform pipeline." in body


def test_preamble_before_the_first_heading_is_preserved():
    template = "Thanks for contributing!\n\n## Changes\n\n(Describe them.)\n"

    body = compose_pull_request_body(template, narrative())

    assert body.startswith("Thanks for contributing!")


def test_an_unrecognised_section_keeps_its_original_body():
    template = "## Deployment notes\n\nCoordinate with the platform team before merging.\n"

    body = compose_pull_request_body(template, narrative())

    assert "Coordinate with the platform team before merging." in body


def test_commit_message_leads_with_the_issue_key_and_summary():
    message = compose_commit_message(
        issue_key="PSD-6948",
        summary="Remove the deprecated size prop from the Button component",
        description="The size prop is unused and blocks the design system upgrade.",
    )

    subject, _, body = message.partition("\n\n")
    assert subject == "PSD-6948: Remove the deprecated size prop from the Button component"
    assert body == "The size prop is unused and blocks the design system upgrade."


def test_commit_message_subject_fits_the_git_convention():
    message = compose_commit_message(
        issue_key="PSD-6948",
        summary="Remove the deprecated size prop from every Button call site across the portal",
        description="",
    )

    subject = message.splitlines()[0]
    assert len(subject) <= 72
    assert subject.endswith("...")
    assert message.splitlines()[0].startswith("PSD-6948: Remove the deprecated size prop")


def test_commit_message_omits_the_prefix_when_there_is_no_issue_key():
    message = compose_commit_message(issue_key="", summary="Tidy the build", description="")

    assert message == "Tidy the build"


def test_commit_message_falls_back_when_there_is_no_summary():
    message = compose_commit_message(issue_key="PSD-6948", summary="", description="")

    assert message == "PSD-6948: Apply bounded agent change"


def test_testing_evidence_names_only_the_stages_that_ran():
    assert compose_testing_evidence(reviewed=False, policy_evaluated=False) == (
        "Repository checks passed in the platform pipeline. "
        "No manual verification has been performed."
    )
    assert compose_testing_evidence(reviewed=True, policy_evaluated=False) == (
        "Repository checks passed in the platform pipeline. "
        "Automated plan and code review passed. "
        "No manual verification has been performed."
    )
    assert compose_testing_evidence(reviewed=False, policy_evaluated=True) == (
        "Repository checks passed in the platform pipeline. "
        "Policy evaluation allowed the change. "
        "No manual verification has been performed."
    )
    assert compose_testing_evidence(reviewed=True, policy_evaluated=True) == (
        "Repository checks passed in the platform pipeline. "
        "Automated plan and code review passed. "
        "Policy evaluation allowed the change. "
        "No manual verification has been performed."
    )


def test_template_lookup_prefers_the_github_directory(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("## Preferred\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "pull_request_template.md").write_text("## Ignored\n")
    (tmp_path / "pull_request_template.md").write_text("## Ignored\n")

    assert read_pull_request_template(str(tmp_path)) == "## Preferred\n"


def test_template_lookup_finds_the_lowercase_repository_root_variant(tmp_path):
    (tmp_path / "pull_request_template.md").write_text("## Root\n")

    assert read_pull_request_template(str(tmp_path)) == "## Root\n"


def test_template_lookup_returns_empty_when_the_repository_has_no_template(tmp_path):
    assert read_pull_request_template(str(tmp_path)) == ""
