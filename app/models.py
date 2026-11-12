from __future__ import annotations

from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class Phase(str, Enum):
    VERIFY_ID = 'VERIFY_ID'
    RESOLVE_INTENT = 'RESOLVE_INTENT'
    PROCESS_CASE = 'PROCESS_CASE'
    POST_PROCESS = 'POST_PROCESS'
    COMPLETE = 'COMPLETE'

class EmailConsent(str, Enum):
    UNKNOWN = 'unknown'
    ACCEPTED = 'accepted'
    DECLINED = 'declined'
    REVOKED = 'revoked'

class Hint(BaseModel):
    case_id: str | None = None
    case_type: Literal['healthcare', 'dental', 'auto'] | None = None
    month: int | None = Field(default=None, ge=1, le=12)
    year: int | None = Field(default=None, ge=1900, le=2200)
    status: Literal['denied', 'closed', 'open'] | None = None
    policy_number: str | None = None
    excluded_case_id: str | None = None
    excluded_case_type: Literal['healthcare', 'dental', 'auto'] | None = None

class Parsed(BaseModel):
    identity: dict[str, str] = Field(default_factory=dict)
    hint: Hint = Field(default_factory=Hint)
    intent: str | None = None
    raw_intent: str | None = None
    classification_status: str = "classified"
    workflow_action: str = "none"
    secondary_intents: list[str] = Field(default_factory=list)
    requested_field: list[str] = Field(default_factory=list)
    requested_fields: list[str] = Field(default_factory=list)
    requested_terms: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    dialogue_act: str = "answer_request"
    needs_clarification: bool = False
    ambiguous: str | None = None
    uncertain_terms: list[str] = Field(default_factory=list)
    normalized_request: str | None = None
    context: dict[str, str] = Field(default_factory=dict)
    correction: bool = False
    emotion: str | None = None
    emotion_type: str = "neutral"
    emotion_level: str = "low"
    emotion_evidence: list[str] = Field(default_factory=list)
    human: bool = False
    unrelated: bool = False
    refusal: bool = False

class Session(BaseModel):
    phase: Phase = Phase.VERIFY_ID
    identity: dict[str, str] = Field(default_factory=dict)
    verified: bool = False
    verification_status: Literal['pending', 'verified', 'failed'] = 'pending'
    identity_mismatches: list[str] = Field(default_factory=list)
    matched_identity_field_count: int = 0
    party_id: str | None = None
    hint: Hint = Field(default_factory=Hint)
    case_id: str | None = None
    active_claim_id: str | None = None
    claim_history: list[str] = Field(default_factory=list)
    case_options: list[str] = Field(default_factory=list)
    claim_candidates_before_filter: list[str] = Field(default_factory=list)
    claim_candidates_after_filter: list[str] = Field(default_factory=list)
    awaiting_case_selection: bool = False
    excluded_case_id: str | None = None
    original_intent: str | None = None
    original_decision_topics: list[str] = Field(default_factory=list)
    current_intent: str | None = None
    last_nlu: Parsed | None = None
    requested_fields: list[str] = Field(default_factory=list)
    pending_requested_fields: list[str] = Field(default_factory=list)
    retrieved_fields: list[str] = Field(default_factory=list)
    answered_fields: list[str] = Field(default_factory=list)
    unavailable_fields: list[str] = Field(default_factory=list)
    needs_clarification_fields: list[str] = Field(default_factory=list)
    superseded_fields: list[str] = Field(default_factory=list)
    intents: list[dict[str, str]] = Field(default_factory=list)
    emotional_intent_count: int = 0
    emotional_intent_streak: int = 0
    unresolved_intent_streak: int = 0
    out_of_scope_count: int = 0
    unresolved_requests: list[dict[str, str | int]] = Field(default_factory=list)
    answer_status: str = "unknown"
    factual_guard_passed: bool = True
    factual_guard_failure_reason: str | None = None
    escalation_required: bool = False
    escalation_reason: str | None = None
    escalation_offered: bool = False
    handoff_status: str = "not_requested"
    connection_failure_since: float | None = None
    connection_failure_started_at: float | None = None
    emotion: str | None = None
    emotion_type: str = "neutral"
    emotion_level: str = "low"
    refusals: int = 0
    irrelevant: int = 0
    verification_failures: int = 0
    alternative_attempts: int = 0
    email_consent: EmailConsent = EmailConsent.UNKNOWN
    email_choice: str | None = None
    email_dispatch_status: str = "not_configured"
    discussed: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    handoff: Literal['offered', 'requested'] | None = None
    handoff_reason: str | None = None
    awaiting_handoff_confirmation: bool = False
    history: list[dict[str, str]] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    last_emotion: str = 'neutral'
    distress_turns: int = 0
    finish_offered: bool = False
    response_mode: Literal['model'] = 'model'
    revision: int = 0
    pending_email_body: str | None = Field(default=None, exclude=True)
    summary_email: str | None = None
    handoff_ticket: str | None = None
    handoff_execution_status: str = "not_configured"
    post_process_step: str | None = None
    followup_sent: bool = False
    conversation_complete: bool = False
