"""Canonical per-conversation customer state JSON.

Session remains the in-memory/domain model; this file is its single external
snapshot. Workflow nodes must not create parallel state documents.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Session


def _directory() -> Path:
    value = os.getenv("CUSTOMER_STATE_DIR")
    path = Path(value) if value else Path(__file__).resolve().parents[1] / "runtime" / "customer_states"
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(session_id: str) -> Path:
    safe_id = "".join(char for char in session_id if char.isalnum() or char in "-_")
    return _directory() / f"{safe_id}.json"


def document(session_id: str, state: Session) -> dict:
    nlu = state.last_nlu.model_dump(mode="json") if state.last_nlu else None
    return {
        "session_id": session_id,
        "customer": {
            "identity": state.identity,
            "party_id": state.party_id,
            "verified": state.verified,
            "verification_status": state.verification_status,
            "matched_identity_field_count": state.matched_identity_field_count,
            "identity_mismatches": state.identity_mismatches,
        },
        "conversion": {
            "phase": state.phase.value,
            "case_id": state.case_id,
            "active_claim_id": state.active_claim_id,
            "case_history": state.claim_history,
            "claim_history": state.claim_history,
            "case_options": state.case_options,
            "claim_candidates_before_filter": state.claim_candidates_before_filter,
            "claim_candidates_after_filter": state.claim_candidates_after_filter,
            "awaiting_case_selection": state.awaiting_case_selection,
            "hint": state.hint.model_dump(mode="json", exclude_none=True),
        },
        "intent": {
            "primary": state.current_intent or state.original_intent,
            "original": state.original_intent,
            "original_requested_fields": (
                list(state.pending_requested_fields)
                or list(state.requested_fields)
            ),
            "secondary": list(nlu.get("secondary_intents", [])) if nlu else [],
            "request_history": list(state.intents),
            "requested_fields": state.requested_fields,
            "pending_requested_fields": state.pending_requested_fields,
            "retrieved_fields": state.retrieved_fields,
            "answered_fields": state.answered_fields,
            "unavailable_fields": state.unavailable_fields,
            "needs_clarification_fields": state.needs_clarification_fields,
            "superseded_fields": state.superseded_fields,
            "last_nlu": nlu,
        },
        "emotion": {
            "value": state.emotion,
            "type": state.emotion_type,
            "level": state.emotion_level,
            "last": state.last_emotion,
            "distress_turns": state.distress_turns,
        },
        "handoff": {
            "requested": state.handoff_status != "not_requested",
            "status": state.handoff_status,
            "execution_status": state.handoff_execution_status,
            "ticket_out": bool(state.handoff_ticket),
            "ticket_number": state.handoff_ticket,
            "awaiting_confirmation": state.awaiting_handoff_confirmation,
            "reason": state.handoff_reason,
        },
        "email": {
            "consent": state.email_consent.value,
            "choice": state.email_choice,
            "dispatch_status": state.email_dispatch_status,
            "sent": state.email_dispatch_status in {"confirmed", "sent", "demo_sent"},
            "summary_email": state.summary_email,
            "post_process_step": state.post_process_step,
        },
        "guards": {
            "answer_status": state.answer_status,
            "factual_guard_passed": state.factual_guard_passed,
            "factual_guard_failure_reason": state.factual_guard_failure_reason,
            "escalation_required": state.escalation_required,
            "escalation_reason": state.escalation_reason,
        },
        "conversation": {
            "conversation_complete": state.conversation_complete,
            "turn_revision": state.revision,
            "refusals": state.refusals,
            "irrelevant": state.irrelevant,
            "verification_failures": state.verification_failures,
            "history": state.history,
            "next_steps": state.next_steps,
        },
        "audit": {
            "last_nlu": nlu or {},
            "turn_number": state.revision,
            "last_intent": state.current_intent,
            "last_workflow_action": nlu.get("workflow_action", "none") if nlu else "none",
            "response_mode": state.response_mode,
            "factual_guard_passed": state.factual_guard_passed,
        },
    }


def save(session_id: str, state: Session) -> Path:
    target = path_for(session_id)
    payload = json.dumps(document(session_id, state), ensure_ascii=False, indent=2)
    with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as tmp:
        tmp.write(payload)
        temporary = Path(tmp.name)
    temporary.replace(target)
    return target


def delete(session_id: str) -> None:
    path_for(session_id).unlink(missing_ok=True)
