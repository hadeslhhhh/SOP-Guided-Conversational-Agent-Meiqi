"""Typed SOP session state and phase-transition helpers."""

from typing import Any, TypedDict

STAGES = ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")


class SessionState(TypedDict, total=False):
    """Shared state contract used by LangGraph and the HTTP session layer.

    This is intentionally a ``TypedDict`` rather than a runtime model so the
    existing dictionary-based persistence and response code remain compatible.
    """

    phase: str
    gates: dict[str, bool]
    stage_progress: dict[str, float]
    identity: dict[str, Any]
    verified_fields: list[str]
    verified_party_id: str | None
    case_access_authorized: bool
    matched_case_id: str | None
    case_options: list[str]
    case: dict[str, Any] | None
    intents: list[dict[str, Any]]
    last_nlu: dict[str, Any] | None
    user_need: str | None
    emotion: str
    emotion_level: str
    conversation_complete: bool
    disconnected: bool


def blank_state() -> SessionState:
    return {
        "phase": "VERIFY_ID",
        "gates": {stage: False for stage in STAGES},
        "stage_progress": {stage: 0.0 for stage in STAGES},
        "identity": {}, "verified_fields": [], "verified_party_id": None,
        "case_access_authorized": False, "intent_hints": [], "case_hints": [],
        "attempts": 0, "verification_attempts": 0, "human_transfer": False,
        "awaiting_human_transfer": False, "human_transfer_reason": None,
        "handoff_conditions": {"verification_failures": 0, "verification_pushbacks": 0,
                                "unclear_questions": 0, "emotional_intents": 0,
                                "triggered_conditions": []},
        "handoff_ticket": None, "matched_case_id": None, "case_options": [],
        "awaiting_case_selection": False,
        "nlu_hints": {"status": None, "case_type": None, "month": None, "year": None},
        "last_nlu": None, "last_user_message": None, "last_requested_field": None,
        "last_intent_contract": None, "case": None, "emotion": "Neutral / Calm",
        "emotion_level": "low", "emotional_intent_streak": 0,
        "emotional_intent_count": 0, "user_need": None, "awaiting_user_need": False,
        "intents": [], "potential_intents": [], "awaiting_potential_confirmation": False,
        "current_intent_index": None, "awaiting_additional_question": False,
        "followup_sent": False, "conversation_complete": False,
        "unrelated_question_count": 0, "post_process_step": None,
        "summary_email": None, "disconnect_scheduled": False, "disconnected": False,
    }


def advance_phase(state: SessionState, next_phase: str) -> bool:
    if next_phase not in STAGES:
        return False
    index = STAGES.index(next_phase)
    if any(state["stage_progress"][stage] < 1.0 for stage in STAGES[:index]):
        return False
    state["phase"] = next_phase
    return True


def transition_phase(state: SessionState, next_phase: str) -> bool:
    """Single guarded phase-transition entry point for new stage code.

    The legacy ``advance_phase`` behavior is preserved for compatibility;
    newly migrated stages should use this stricter adjacent-transition API.
    """
    current = state.get("phase", "VERIFY_ID")
    if current == next_phase:
        return True
    try:
        current_index = STAGES.index(current)
        next_index = STAGES.index(next_phase)
    except ValueError:
        return False
    if next_index != current_index + 1:
        return False
    if any(not state.get("gates", {}).get(stage, False) for stage in STAGES[:current_index + 1]):
        return False
    state["phase"] = next_phase
    return True
