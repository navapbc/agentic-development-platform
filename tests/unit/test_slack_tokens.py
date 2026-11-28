from __future__ import annotations

import time
from dataclasses import replace

import pytest
from services.integration.slack.tokens import (
    ActionToken,
    TokenRefused,
    sign_action_token,
    verify_action_token,
)

KEY = b"alpha-action-key"


def _token(**overrides) -> ActionToken:
    fields = {
        "tenant": "alpha",
        "execution_id": "intake-1a2b3c4d",
        "workflow_id": "draft-ADSER-24-intake-1a2b3c4d",
        "park_key": "intake-1a2b3c4d:review:security-review,acceptance:2",
        "kind": "gate",
        "actions": ("approve",),
        "expires_at": int(time.time()) + 3600,
    }
    return ActionToken(**{**fields, **overrides})


def test_a_signed_token_verifies_to_the_same_bindings():
    token = _token()

    assert verify_action_token(sign_action_token(token, KEY), KEY) == token


def test_signing_twice_gives_the_same_value_so_an_alert_retry_posts_the_same_button():
    assert sign_action_token(_token(), KEY) == sign_action_token(_token(), KEY)


def test_the_token_names_the_audit_event_of_the_park_it_decides():
    gate = _token()
    repair = _token(park_key="intake-1a2b3c4d:repair:3:4", kind="repair", actions=("retry",))

    assert gate.stage == "review"
    assert gate.decision_event_id == (
        "intake-1a2b3c4d:HUMAN_INTERVENTION_DECISION:review:security-review,acceptance:2"
    )
    assert repair.decision_event_id == "intake-1a2b3c4d:HUMAN_INTERVENTION_DECISION:repair:3:4"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (lambda: sign_action_token(_token(), b"beta-action-key"), "invalid"),
        (lambda: sign_action_token(_token(), KEY)[:-4] + "AAAA", "invalid"),
        (lambda: "v1.not-a-token", "invalid"),
        (lambda: "", "invalid"),
        (lambda: sign_action_token(_token(expires_at=int(time.time()) - 1), KEY), "expired"),
        (lambda: sign_action_token(_token(park_key=""), KEY), "keyless"),
        (lambda: sign_action_token(_token(kind="restart"), KEY), "unknown_kind"),
        (lambda: sign_action_token(_token(actions=("retry",)), KEY), "unknown_kind"),
    ],
)
def test_a_token_that_is_altered_expired_keyless_or_of_an_unknown_kind_is_refused(value, reason):
    with pytest.raises(TokenRefused) as refused:
        verify_action_token(value(), KEY)

    assert refused.value.reason == reason


def test_a_token_for_a_long_gate_list_fits_a_slack_button_value():
    gates = ",".join(f"gate-number-{index:02d}-with-a-long-descriptive-name" for index in range(20))
    token = _token(park_key=f"intake-1a2b3c4d:policy:{gates}:1", actions=("approve",))

    assert len(sign_action_token(token, KEY)) <= 2000


def test_a_token_too_long_for_a_button_value_is_not_signed():
    gates = ",".join(f"gate-{index:03d}-with-a-long-descriptive-name" for index in range(60))

    with pytest.raises(ValueError):
        sign_action_token(replace(_token(), park_key=f"intake-1:policy:{gates}:1"), KEY)
