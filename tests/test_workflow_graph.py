"""Structural tests for the LangGraph SOP stage boundaries."""

import app
import verification
from state import transition_phase
from workflow import (
    post_process_node,
    process_case_node,
    resolve_intent_node,
    verify_id_node,
)


def _payload(state):
    return {"state": state, "text": "test turn"}


def test_state_transition_requires_adjacent_completed_stage():
    state = app.blank_state()
    state["gates"]["VERIFY_ID"] = True
    assert transition_phase(state, "RESOLVE_INTENT") is True
    assert state["phase"] == "RESOLVE_INTENT"
    assert transition_phase(state, "POST_PROCESS") is False
    assert state["phase"] == "RESOLVE_INTENT"


def test_unverified_identity_cannot_enter_case_stage():
    state = app.blank_state()

    try:
        process_case_node(_payload(state))
    except ValueError as exc:
        assert "expected PROCESS_CASE" in str(exc)
    else:
        raise AssertionError("PROCESS_CASE accepted an unverified VERIFY_ID state")


def test_verified_identity_moves_to_intent_stage(monkeypatch):
    state = app.blank_state()

    def fake_turn(current, text):
        current["gates"]["VERIFY_ID"] = True
        current["phase"] = "RESOLVE_INTENT"
        return "identity verified", {}

    monkeypatch.setattr(app, "verify_id_stage", fake_turn)
    result = verify_id_node(_payload(state))

    assert result["answer"] == "identity verified"
    assert state["gates"]["VERIFY_ID"] is True
    assert state["phase"] == "RESOLVE_INTENT"


def test_resolve_intent_stage_accepts_only_resolve_phase(monkeypatch):
    state = app.blank_state()
    state["phase"] = "RESOLVE_INTENT"

    def fake_turn(current, text):
        current["gates"]["RESOLVE_INTENT"] = True
        current["phase"] = "PROCESS_CASE"
        return "case selected", {}

    monkeypatch.setattr(app, "resolve_intent_stage", fake_turn)
    result = resolve_intent_node(_payload(state))

    assert result["answer"] == "case selected"
    assert state["gates"]["RESOLVE_INTENT"] is True
    assert state["phase"] == "PROCESS_CASE"

    wrong_state = app.blank_state()
    try:
        resolve_intent_node(_payload(wrong_state))
    except ValueError as exc:
        assert "expected RESOLVE_INTENT" in str(exc)
    else:
        raise AssertionError("RESOLVE_INTENT accepted a VERIFY_ID state")


def test_mismatched_case_ownership_is_rejected():
    state = app.blank_state()
    state["gates"]["VERIFY_ID"] = True
    state["verified_party_id"] = "P9"
    state["matched_case_id"] = "CL-3001"  # belongs to P12

    authorized, message = verification.authorize_case_access(state)

    assert authorized is False
    assert "identity you verified" in message
    assert state["case_access_authorized"] is False


def test_authorized_case_can_enter_process_case_stage(monkeypatch):
    state = app.blank_state()
    state.update({
        "phase": "PROCESS_CASE",
        "gates": {"VERIFY_ID": True, "RESOLVE_INTENT": True, "PROCESS_CASE": False, "POST_PROCESS": False},
        "verified_party_id": "P9",
        "matched_case_id": "CL-2048",
        "case_access_authorized": True,
    })

    def fake_turn(current, text):
        current["gates"]["PROCESS_CASE"] = True
        current["stage_progress"]["PROCESS_CASE"] = 1.0
        current["phase"] = "POST_PROCESS"
        return "claim answer", {}

    monkeypatch.setattr(app, "process_case_stage", fake_turn)
    result = process_case_node(_payload(state))

    assert result["answer"] == "claim answer"
    assert state["case_access_authorized"] is True
    assert state["gates"]["PROCESS_CASE"] is True
    assert state["phase"] == "POST_PROCESS"


def test_completed_case_moves_to_post_process(monkeypatch):
    state = app.blank_state()
    state["phase"] = "POST_PROCESS"
    state["gates"].update({
        "VERIFY_ID": True,
        "RESOLVE_INTENT": True,
        "PROCESS_CASE": True,
    })

    def fake_turn(current, text):
        current["gates"]["POST_PROCESS"] = True
        current["stage_progress"]["POST_PROCESS"] = 1.0
        current["conversation_complete"] = True
        return "conversation complete", {}

    monkeypatch.setattr(app, "post_process_stage", fake_turn)
    result = post_process_node(_payload(state))

    assert result["answer"] == "conversation complete"
    assert state["gates"]["POST_PROCESS"] is True
    assert state["conversation_complete"] is True
