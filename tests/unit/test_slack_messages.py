from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from scripts.config import Settings
from services.integration.jira.credentials import build_single_project_resolver
from services.integration.slack.messages import (
    ACTIONS_BLOCK,
    ParkContext,
    PostState,
    SlackAlert,
    alert_payload,
    status_blocks,
)
from services.integration.slack.tokens import verify_action_token
from services.integration.tickets.factory import ticket_url
from services.operator_remedy import Remedy
from services.orchestration import remedies
from services.orchestration.notifications import render_remedy, render_remedy_commands

KEY = b"alpha-action-key"


def _alert(remedy: Remedy, *, stage: str = "review", detail: str = "pytest failed") -> SlackAlert:
    return SlackAlert(
        task_id="ADSER-24",
        repo="agentic-demo-service",
        issue_key="ADSER-24",
        stage=stage,
        reason="gate_paused:security-review",
        detail=detail,
        remedy_text="Run `inv gate --issue ADSER-24 --gate security-review --approve`.",
        remedy=remedy,
    )


def _park(park_key: str = "exec-1:review:security-review:1") -> ParkContext:
    return ParkContext(
        tenant="alpha",
        execution_id="exec-1",
        workflow_id="wf-1",
        park_key=park_key,
        issued_at=int(time.time()),
        ttl_hours=72,
        action_key=KEY,
    )


def _buttons(blocks: list[dict]) -> list[dict]:
    return [
        element for block in blocks if block["type"] == "actions" for element in block["elements"]
    ]


def _labels(blocks: list[dict]) -> list[str]:
    return [button["action_id"] for button in _buttons(blocks)]


def test_a_gate_park_offers_approve_and_reject_each_bound_to_its_park():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")

    _, blocks = alert_payload(_alert(gate), content="full", park=_park())

    assert _labels(blocks) == ["approve", "reject"]
    tokens = [verify_action_token(button["value"], KEY) for button in _buttons(blocks)]
    assert [(token.kind, token.actions, token.park_key) for token in tokens] == [
        ("gate", ("approve",), "exec-1:review:security-review:1"),
        ("gate", ("reject",), "exec-1:review:security-review:1"),
    ]
    assert tokens[0].expires_at == _park().issued_at + 72 * 3600


def test_a_repair_exhausted_park_offers_retry_and_abandon():
    repair = remedies.repair_remedy("repair_exhausted", issue_key="ADSER-24", execution_id="e")

    _, blocks = alert_payload(_alert(repair, stage="repair"), content="full", park=_park())

    assert _labels(blocks) == ["retry", "abandon"]


def test_a_budget_park_offers_abandon_only():
    budget = remedies.repair_remedy("cost_budget_exceeded", issue_key="ADSER-24", execution_id="e")

    _, blocks = alert_payload(_alert(budget, stage="repair"), content="full", park=_park())

    assert _labels(blocks) == ["abandon"]


def test_an_alert_whose_remedy_names_no_decision_offers_no_buttons():
    generic = remedies.generic_remedy("ADSER-24", "intake")

    _, blocks = alert_payload(_alert(generic), content="full", park=_park())

    assert _labels(blocks) == []


def test_an_alert_of_a_run_without_park_keys_offers_no_buttons():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")

    _, blocks = alert_payload(_alert(gate), content="full", park=None)

    assert _labels(blocks) == []


def test_minimal_content_leaves_out_the_failure_detail_and_keeps_the_next_step():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")

    text, blocks = alert_payload(
        _alert(gate, detail="secret stack trace"), content="minimal", park=_park()
    )

    rendered = json.dumps(blocks) + text
    assert "secret stack trace" not in rendered
    assert "agentic-demo-service" not in rendered
    assert "ADSER-24" in rendered
    assert "gate_paused:security-review" in rendered
    assert "Next step" in rendered


@pytest.mark.parametrize(
    "remedy",
    [
        remedies.code_defect_remedy("secret stack trace"),
        remedies.existing_pr_remedy(
            "ADSER-24", "https://github.com/navapbc/agentic-demo-service/pull/7"
        ),
    ],
)
def test_minimal_content_posts_the_remedy_commands_without_the_remedy_prose(remedy):
    settings = Settings(tenant="alpha")
    alert = replace(
        _alert(remedy, detail="secret stack trace"),
        remedy_text=render_remedy(remedy, settings),
        remedy_commands=render_remedy_commands(remedy, settings),
    )

    text, blocks = alert_payload(alert, content="minimal", park=_park())

    rendered = json.dumps(blocks) + text
    assert "secret stack trace" not in rendered
    assert "agentic-demo-service" not in rendered
    assert "Next step" in rendered
    for command in alert.remedy_commands:
        assert command in rendered


def test_untrusted_detail_cannot_mention_the_channel_or_disguise_a_link():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")

    _, blocks = alert_payload(
        _alert(gate, detail="<!channel> see <https://evil.example|github.com/ok>"),
        content="full",
        park=_park(),
    )

    rendered = json.dumps(blocks)
    assert "<!channel>" not in rendered
    assert "<https://evil.example|" not in rendered
    assert "&lt;!channel&gt;" in rendered


def test_a_park_key_too_long_for_a_button_posts_the_message_without_buttons():
    gates = ",".join(f"gate-{index:03d}-with-a-long-descriptive-name" for index in range(60))
    gate = remedies.gate_decision_remedy("ADSER-24", gates)

    _, blocks = alert_payload(_alert(gate), content="full", park=_park(f"exec-1:policy:{gates}:1"))

    assert _labels(blocks) == []


def test_a_detail_longer_than_a_section_keeps_its_tail_within_the_section_limit():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")
    detail = "\n".join(f"line {index} <&>" for index in range(400)) + "\nfinal failure"

    _, blocks = alert_payload(_alert(gate, detail=detail), content="full", park=_park())

    sections = [block["text"]["text"] for block in blocks if block["type"] == "section"]
    assert all(len(text) <= 3000 for text in sections)
    assert any("final failure" in text for text in sections)


def _resolver():
    return build_single_project_resolver(
        url="https://jira.example.gov/",
        auth_mode="datacenter",
        email="",
        token="t",
        board="1",
        project="ADSER",
    )


def test_minimal_content_links_the_issue_key_to_its_jira_ticket():
    settings = Settings(ticket_backend="jira")
    url = ticket_url("ADSER-24", settings, resolver=_resolver())
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")

    text, blocks = alert_payload(
        replace(_alert(gate), ticket_url=url), content="minimal", park=_park()
    )

    assert url == "https://jira.example.gov/browse/ADSER-24"
    assert "*<https://jira.example.gov/browse/ADSER-24|ADSER-24>*" in json.dumps(blocks) + text


def test_a_rebar_ticket_and_an_unsafe_link_leave_the_issue_key_unlinked():
    gate = remedies.gate_decision_remedy("ADSER-24", "security-review")
    unsafe = replace(_alert(gate), ticket_url="https://jira.example.gov/browse/X|<!channel>")

    _, blocks = alert_payload(unsafe, content="minimal", park=_park())

    assert ticket_url("ADSER-24", Settings(ticket_backend="rebar")) == ""
    assert blocks[0]["text"]["text"].startswith("*ADSER-24* parked")


CONTENT = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Model path unavailable*"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Failing hop:* Bedrock connection"}},
]
BUTTONS = {"type": "actions", "block_id": ACTIONS_BLOCK, "elements": [{"type": "button"}]}
EARLY = datetime(2026, 11, 25, 2, 29, tzinfo=UTC)
LATE = datetime(2026, 11, 25, 3, 5, tzinfo=UTC)


def _line(blocks, state: PostState) -> str:
    [block] = [block for block in blocks if block.get("block_id") == state.block_id]
    return block["elements"][0]["text"]


def test_an_acknowledgement_keeps_the_buttons_under_its_line():
    blocks = status_blocks(
        [*CONTENT, BUTTONS], PostState.ACKNOWLEDGED, actor="slack:E1/U1", at=EARLY
    )

    assert blocks[:2] == CONTENT and blocks[-1] == BUTTONS
    assert blocks[2]["elements"][0]["text"] == "Acknowledged by <@U1> at 2026-11-25 02:29 UTC."


def test_a_later_state_replaces_its_own_line_and_keeps_the_acknowledgement():
    first = status_blocks(
        [*CONTENT, BUTTONS], PostState.ACKNOWLEDGED, actor="slack:E1/U1", at=EARLY
    )
    again = status_blocks(first, PostState.ACKNOWLEDGED, actor="slack:E1/U2", at=LATE)
    resolved = status_blocks(again, PostState.RESOLVED, actor="slack:E1/U1", at=EARLY)
    final = status_blocks(resolved, PostState.RESOLVED, actor="slack:E1/U2", at=LATE)

    assert final[:2] == CONTENT
    assert not any(block["type"] == "actions" for block in final)
    assert _line(final, PostState.ACKNOWLEDGED) == "Acknowledged by <@U2> at 2026-11-25 03:05 UTC."
    assert _line(final, PostState.RESOLVED) == "Resolved by <@U2> at 2026-11-25 03:05 UTC."
    assert len(final) == 4


def test_a_status_line_escapes_its_prose_and_names_a_non_slack_actor_in_plain_words():
    blocks = status_blocks(CONTENT, PostState.RESTARTED, actor="ops@example.com", note="<@U9> & co")

    assert (
        _line(blocks, PostState.RESTARTED) == "Restarted by ops@example.com. &lt;@U9&gt; &amp; co"
    )
