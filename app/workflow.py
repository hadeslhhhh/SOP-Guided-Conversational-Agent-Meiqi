"""ConversationService and LangGraph orchestration."""
from typing import Any, TypedDict
import re
from .models import Parsed, Phase, Session
import os
import logging
from .interpreter import (
    OpenAIStructuredInterpreter,
    RuleInterpreter,
    email_consent_kind,
    _explicit_human_request,
)
from .repository import Repository
from .store import ConcurrentTurnError, Store
from .stages import verify_identity, resolve_intent, process_case, post_process
from . import customer_state

logger = logging.getLogger("sop_agent.workflow")


class GraphState(TypedDict, total=False):
    session: Session
    text: str
    nlu: Parsed
    reply: str
    event: str


def route(data: GraphState) -> str:
    session = data["session"]
    phase = session.phase.value
    # Handoff is globally dominant: an explicit transfer request must never
    # fall through to the previous claim answer, regardless of current phase.
    action = getattr(data.get("nlu"), "workflow_action", "none")
    if action == "request_human":
        phase = "POST_PROCESS"
    # Explicit claim-switch language must re-enter claim resolution even when
    # the current phase is PROCESS_CASE. This prevents new hints from being
    # attached to the old active case without re-matching candidates.
    claim_switch = bool(re.search(
        r"\b(?:switch|change|use|check)\b.{0,35}\b(?:claim|case)\b.{0,25}\b(?:instead|rather)\b"
        r"|\b(?:another|different|other)\s+(?:claim|case)\b",
        data["text"], re.I,
    ))
    if action == "switch_claim" or claim_switch:
        phase = "RESOLVE_INTENT"
    # Hard authorization boundaries: model output can never bypass these.
    if action != "request_human" and not session.verified and phase in {"RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"}:
        phase = "VERIFY_ID"
    elif action != "request_human" and phase == "PROCESS_CASE" and not session.case_id:
        phase = "RESOLVE_INTENT"
    if action in {"send_email", "skip_email", "revoke_email"} and phase not in {"COMPLETE", "POST_PROCESS"}:
        phase = "POST_PROCESS"
    if phase != session.phase.value:
        session.phase = Phase(phase)
    logger.info("router_decision phase=%s reason=phase_gate case_selected=%s verified=%s",
                phase, bool(data["session"].case_id), data["session"].verified)
    return phase


def _event_for(session: Session) -> str:
    return {
        Phase.VERIFY_ID: "need_identity",
        Phase.RESOLVE_INTENT: "identity_verified",
        Phase.PROCESS_CASE: "claim_selected",
        Phase.POST_PROCESS: "continue_processing",
        Phase.COMPLETE: "conversation_complete",
    }[session.phase]


def _sync_turn_contract(session: Session, parsed: Parsed | None) -> None:
    """Synchronize the current NLU contract before every workflow stage.

    Stages may advance the phase, select a claim, or enforce authorization,
    but they must not lose a concrete request already identified for this
    turn. Unclear follow-up turns intentionally preserve the last actionable
    request until a new one replaces it.
    """
    if parsed is None:
        return
    session.last_nlu = parsed
    if parsed.intent not in (None, "unclear"):
        session.current_intent = parsed.intent
        session.original_intent = session.original_intent or parsed.intent
    from .interpreter import canonical_requested_field
    fields = list(dict.fromkeys(
        canonical_requested_field(field)
        for field in (parsed.requested_fields or parsed.requested_field or [])
        if canonical_requested_field(field)
    ))
    if fields:
        session.requested_fields = fields
        session.pending_requested_fields = list(fields)
    if parsed.emotion_type != "neutral":
        session.emotion_type = parsed.emotion_type
        session.emotion = parsed.emotion_type
        session.emotion_level = parsed.emotion_level


def _enforce_authorization_boundary(session: Session) -> None:
    """Prevent any unverified session from remaining in a protected phase."""
    if session.verified or session.phase in {Phase.VERIFY_ID, Phase.COMPLETE}:
        return
    session.phase = Phase.VERIFY_ID


def _prepare_turn(data: GraphState, interpreter) -> GraphState:
    """Interpret the current customer turn exactly once before stage routing."""
    session_before = data["session"].model_dump(exclude={"identity", "history", "last_nlu"})
    pending_intent = data["session"].current_intent or data["session"].original_intent
    pending_fields_before = list(data["session"].pending_requested_fields)
    parsed = interpreter.interpret(data["text"], data["session"])
    data["session"].last_nlu = parsed
    # OpenAI remains the semantic NLU, but this narrow deterministic scope
    # guard prevents an obvious unrelated question from being mislabeled as
    # ``unclear`` and bypassing the out-of-scope counter.
    from .interpreter import is_obvious_out_of_scope
    if is_obvious_out_of_scope(data["text"]):
        parsed.intent = "out_of_scope"
        parsed.raw_intent = "out_of_scope"
        parsed.unrelated = True
        parsed.requested_fields = []
        parsed.requested_field = []
        parsed.secondary_intents = []
    session = data["session"]
    # Acknowledgements are conversation acts, not new claim questions. Keep
    # the completed field ledger intact even if the model happens to classify
    # "thank you" as the previous status/documents intent.
    if (re.fullmatch(r"\s*(?:ok(?:ay)?|thanks|thank you|thank you so much|much appreciated)\s*[!.]?\s*",
                     data["text"], re.I)
            and session.verified and session.case_id
            and session.answered_fields and not session.pending_requested_fields):
        parsed.intent = "unclear"
        parsed.raw_intent = "acknowledgement"
        parsed.requested_fields = []
        parsed.requested_field = []
        parsed.secondary_intents = []
        parsed.workflow_action = "none"
    # Identity evidence is not a replacement business request. Restore the
    # request that caused the verification gate so the verification turn can
    # continue the original workflow deterministically.
    if parsed.workflow_action == "verify_identity" and pending_intent:
        parsed.intent = pending_intent
        parsed.requested_fields = list(pending_fields_before)
        parsed.requested_field = list(pending_fields_before)
        session.current_intent = pending_intent
    # Identity is a verification action, never a claim-information field.
    # If the API emits requested_fields=["identity"] for a PII-only turn,
    # remove it so successful verification does not produce a fake
    # "clarify identity" business response.
    if (parsed.workflow_action == "verify_identity"
            and not pending_fields_before
            and set(parsed.requested_fields or parsed.requested_field or []) == {"identity"}):
        parsed.requested_fields = []
        parsed.requested_field = []
    # The API model remains authoritative for intent, but deterministic hint
    # extraction guarantees that case context stated in any turn reaches the
    # same LangGraph state even when structured NLU omits optional hint fields.
    # API-only runtime: local RuleInterpreter is disabled unless explicitly
    # opted into for offline diagnostics.
    trusted = RuleInterpreter().interpret(data["text"], session) if os.getenv("ALLOW_LOCAL_RULES") == "1" else parsed
    # The model owns business intent; safety-sensitive facts come only from
    # explicit text extraction. This prevents invented identity/handoff data
    # from changing authorization or routing.
    parsed.identity = dict(trusted.identity)
    # A model-provided claim ID is never trusted. It must be present in the
    # current text and pass the deterministic claim-ID parser.
    parsed.hint.case_id = trusted.hint.case_id
    # Human transfer is a safety-critical explicit action.  The API remains
    # authoritative for business intent, but a clear phrase such as
    # "transfer me to a person" must not be lost when the model omits the
    # structured workflow flag on a later turn.
    explicit_human = _explicit_human_request(data["text"])
    if explicit_human:
        parsed.human = True
        parsed.intent = "human_transfer"
        parsed.raw_intent = "human_transfer"
        parsed.workflow_action = "request_human"
        parsed.requested_fields = []
    # A request for a missing diagnosis/medical/document report is an
    # authorized document inquiry, not a request for private adjuster notes.
    # Keep this narrow correction at the routing boundary so an over-broad
    # model classification cannot reuse the previous unsupported reply.
    document_request = bool(re.search(
        r"\b(?:what|which|any|the)?\s*(?:document|report|paperwork|record)s?\b.{0,40}\b(?:missing|needed|required|submit|provide)\b"
        r"|\b(?:missing|needed|required)\b.{0,30}\b(?:document|diagnosis|medical|report|paperwork)\b",
        data["text"], re.I,
    ))
    if document_request and parsed.intent == "unsupported_intent":
        parsed.intent = "documents"
        parsed.raw_intent = "documents"
        parsed.workflow_action = "none"
        parsed.requested_fields = ["missing_documents"]
    if not explicit_human and not trusted.human and parsed.workflow_action == "request_human":
        parsed.human = False
        parsed.workflow_action = "none"
        if parsed.intent == "human_transfer":
            parsed.intent = "unclear"
    deterministic_hint = trusted.hint
    for field in ("case_id", "case_type", "month", "year", "status", "policy_number", "excluded_case_type"):
        value = getattr(deterministic_hint, field, None)
        if value is not None:
            setattr(session.hint, field, value)
    claim_id_only = bool(re.fullmatch(r"\s*CL[- ]?\d+\s*", data["text"], re.I))
    from .interpreter import requested_fields_from_text, email_consent_kind
    consent_kind = email_consent_kind(data["text"], session)
    if consent_kind == "send_email":
        from .models import EmailConsent
        session.email_consent = EmailConsent.ACCEPTED
    elif consent_kind == "skip_email":
        from .models import EmailConsent
        session.email_consent = EmailConsent.DECLINED
    elif consent_kind == "revoke_email":
        from .models import EmailConsent
        session.email_consent = EmailConsent.REVOKED
    if consent_kind:
        # Consent is a safety-sensitive workflow signal. The API cannot
        # downgrade or reinterpret it as a business intent.
        parsed.workflow_action = consent_kind
    if parsed.workflow_action in {"send_email", "skip_email", "revoke_email"}:
        session.phase = Phase.POST_PROCESS
    # Identity updates are handled by VERIFY_ID independently of business
    # intent. Some model responses attach a stale document field to a PII
    # turn; do not let that field enter the current-turn request queue.
    deterministic_text_fields = (requested_fields_from_text(data["text"], parsed.intent)
                                 if os.getenv("ALLOW_LOCAL_RULES") == "1" else [])
    logger.info(
        "api_nlu_guard local_structured_fields=%s parsed_fields_after_guard=%s pending_before=%s",
        deterministic_text_fields,
        [str(value) for value in (parsed.requested_fields or [])],
        [str(value) for value in session.pending_requested_fields],
    )
    # Consent is orthogonal to insurance/document intents. Never let a
    # summary consent utterance populate document submission fields.
    if consent_kind:
        parsed.requested_fields = []
        parsed.requested_field = []
        parsed.secondary_intents = [i for i in parsed.secondary_intents if i != "submission"]
        session.requested_fields = []
        session.pending_requested_fields = []
        session.retrieved_fields = []
        session.answered_fields = []
        session.unavailable_fields = []
        session.needs_clarification_fields = []
    # Only literal wording in the current turn counts as an explicit local
    # fallback. Do not infer a new field from the API's broad intent when a
    # prior request is still pending.
    # This is a hard field-level inquiry extractor, not Local Rules NLU. It
    # supplements the API result in API-only mode so compound questions are
    # never reduced to a single primary intent.
    explicit_fields = requested_fields_from_text(data["text"], None)
    from .interpreter import canonical_requested_field
    fields = list(dict.fromkeys(
        canonical_requested_field(field)
        for field in (parsed.requested_fields or parsed.requested_field)
        if canonical_requested_field(field)
    ))
    for field in explicit_fields:
        if field not in fields:
            fields.append(field)
    if consent_kind:
        fields = []
    elif session.pending_requested_fields and explicit_fields:
        # A concrete structural signal in the current wording (for example,
        # "sent last Friday" => receipt status) may correct a broad API field,
        # but only when it matches or continues the pending request.
        if set(explicit_fields) & set(session.pending_requested_fields):
            fields = list(dict.fromkeys(explicit_fields))
    elif claim_id_only or (session.pending_requested_fields and not explicit_fields):
        fields = []
    elif explicit_fields and not fields:
        # Local rules are only a narrow structural fallback. They never
        # override requested fields returned by the API NLU.
        fields = list(dict.fromkeys(explicit_fields))
    elif explicit_fields and not consent_kind:
        # The API owns the semantic primary intent, but explicit field-level
        # requests in the same sentence must not be dropped. This preserves
        # compound requests such as "explain the denial and tell me what to do
        # next" as one synchronized multi-field contract.
        fields = list(dict.fromkeys([*fields, *explicit_fields]))
    if parsed.workflow_action == "verify_identity" and session.pending_requested_fields:
        fields = list(session.pending_requested_fields)
    if fields:
        # A newly extracted field list is a replacement request. This is what
        # makes corrections such as "I mean the payment date" authoritative,
        # while preserving the order returned by NLU.
        session.superseded_fields.extend(f for f in session.pending_requested_fields if f not in fields)
        session.superseded_fields = list(dict.fromkeys(session.superseded_fields))
        session.requested_fields = fields
        session.pending_requested_fields = list(fields)
        parsed.requested_fields = list(fields)
        parsed.requested_field = list(fields)
        session.retrieved_fields = []
        session.answered_fields = []
        session.unavailable_fields = []
        session.needs_clarification_fields = []
    elif parsed.intent in {"out_of_scope", "unsupported_intent", "finish"} or parsed.unrelated:
        # Explicitly leaving the claim topic withdraws the active request.
        session.requested_fields = []
        session.pending_requested_fields = []
        session.retrieved_fields = []
        session.answered_fields = []
        session.unavailable_fields = []
        session.needs_clarification_fields = []
    elif not consent_kind and parsed.intent not in (None, "unclear") and not session.pending_requested_fields:
        # Legacy/ambiguous NLU may provide only the broad intent. Derive fields
        # once, but never use this branch to override explicit fields.
        from .response_service import ResponseService
        fields = ResponseService._fields_for_intents([parsed.intent, *parsed.secondary_intents])
        session.requested_fields = list(fields)
        session.pending_requested_fields = list(fields)
    logger.info("nlu phase=%s claim=%s primary_intent=%s requested_fields=%s pending_requested_fields=%s secondary=%s emotion=%s/%s confidence=%.2f",
                session.phase.value, session.case_id,
                parsed.intent, session.requested_fields, session.pending_requested_fields, parsed.secondary_intents, parsed.emotion_type,
                parsed.emotion_level, parsed.confidence)
    logger.info("state_transition_safe before_phase=%s before_intent=%s after_phase=%s after_intent=%s after_fields=%s",
                session_before.get("phase"), session_before.get("current_intent"), session.phase.value,
                session.current_intent, session.requested_fields)
    return {
        "session": data["session"],
        "text": data["text"],
        "nlu": parsed,
        "event": "turn_prepared",
    }


def _node(handler, interpreter, repository):
    def execute(data: GraphState) -> GraphState:
        previous = data["session"].phase.value
        _sync_turn_contract(data["session"], data.get("nlu"))
        reply = handler(data["session"], data["text"], interpreter, repository, data.get("nlu"))
        _enforce_authorization_boundary(data["session"])
        logger.info("node=%s phase=%s->%s claim=%s primary_intent=%s requested_fields=%s pending_requested_fields=%s retrieved_fields=%s answered_fields=%s unavailable_fields=%s needs_clarification_fields=%s final_response=%r factual_guard_result=%s answer_status=%s escalation_reason=%s",
                    handler.__module__, previous, data["session"].phase.value,
                    data["session"].case_id, getattr(data.get("nlu"), "intent", None),
                    data["session"].requested_fields, data["session"].pending_requested_fields,
                    data["session"].retrieved_fields, data["session"].answered_fields, data["session"].unavailable_fields,
                    data["session"].needs_clarification_fields, reply,
                    data["session"].factual_guard_passed, data["session"].answer_status,
                    data["session"].escalation_reason)
        return {
            "session": data["session"],
            "text": data["text"],
            "nlu": data.get("nlu"),
            "reply": reply,
            "event": _event_for(data["session"]),
        }
    return execute


class ConversationService:
    def __init__(self, repo=None, interpreter=None, store=None):
        self.repo = repo or Repository()
        if interpreter:
            self.interpreter = interpreter
        else:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is required; Local Rules NLU has been removed.")
            if os.getenv("NLU_MODE", "openai").casefold() != "openai":
                raise RuntimeError("NLU_MODE must be 'openai'; Local Rules NLU has been removed.")
            self.interpreter = OpenAIStructuredInterpreter()
        self.store = store or Store()
        self.graph = self._build_graph()

    def _build_graph(self):
        from langgraph.graph import END, START, StateGraph
        graph = StateGraph(GraphState)
        # Bind dependencies once; every node receives and returns GraphState.
        handlers = {
            "VERIFY_ID": verify_identity.run,
            "RESOLVE_INTENT": resolve_intent.run,
            "PROCESS_CASE": process_case.run,
            "POST_PROCESS": post_process.run,
        }
        for name, handler in handlers.items():
            graph.add_node(name, _node(handler, self.interpreter, self.repo))
        graph.add_node("PREPARE_TURN", lambda d: _prepare_turn(d, self.interpreter))
        graph.add_node("COMPLETE", lambda d: {
            "session": d["session"], "text": d["text"],
            "reply": "This conversation is complete. Please start a new conversation.",
            "event": "conversation_complete",
        })
        destinations = {phase: phase for phase in ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "COMPLETE")}
        graph.add_edge(START, "PREPARE_TURN")
        graph.add_conditional_edges("PREPARE_TURN", route, destinations)
        for phase in destinations:
            graph.add_edge(phase, END)
        return graph.compile()

    def respond(self, session_id: str, text: str):
        restored = self.store.get(session_id)
        state = restored or Session()
        revision = state.revision
        phase_before = state.phase.value
        verified_before = state.verified
        case_id_before = state.case_id
        handoff_ticket_before = state.handoff_ticket
        # Create the canonical customer-state document at conversion start;
        # every node reads/writes the same Session-backed snapshot.
        customer_state.save(session_id, state)
        graph_state: GraphState = {"session": state, "text": text, "event": "turn_started"}
        result = self.graph.invoke(graph_state)
        updated = result["session"]
        reply = result["reply"]
        # A verification transition must be visible to the customer before
        # any protected claim answer. Some one-claim paths go straight from
        # VERIFY_ID to PROCESS_CASE, so enforce this at the single workflow
        # response boundary instead of relying on individual stages.
        reply_lower = reply.casefold()
        if (not verified_before and updated.verified
                and "verified successfully" not in reply_lower
                and "identity is verified" not in reply_lower):
            reply = "Your identity has been verified successfully. " + reply
        # A completed handoff remains addressable by its ticket number. Do
        # not replace this with the generic COMPLETE message on repeat turns.
        if (handoff_ticket_before
                and updated.handoff_ticket
                and updated.handoff_execution_status in {"confirmed", "demo_recorded"}
                and getattr(updated.last_nlu, "workflow_action", "none") == "request_human"):
            reply = (
                "Your human-assistance request is already on file under ticket "
                f"{updated.handoff_ticket}. No additional transfer was created."
            )
        updated.active_claim_id = updated.case_id
        updated.connection_failure_since = None
        updated.connection_failure_started_at = None
        with self.store.turn():
            if updated.pending_email_body and updated.email_choice == "queued_demo":
                self.store.queue(session_id, updated.summary_email, updated.pending_email_body)
                updated.pending_email_body = None
            self.store.save(session_id, updated, expected_revision=revision)
        # The JSON snapshot is the one canonical external customer state. It
        # exists only during an active conversion and is removed at closure.
        if updated.phase == Phase.COMPLETE or updated.conversation_complete:
            customer_state.delete(session_id)
        else:
            customer_state.save(session_id, updated)
        logger.info(
            "turn_diagnostic thread_id=%s phase_before=%s phase_after=%s "
            "verified_before=%s verified_after=%s matched_identity_field_count=%d "
            "case_id_before=%s case_id_after=%s primary_intent=%s "
            "requested_fields=%s checkpoint_restored=%s",
            session_id, phase_before, updated.phase.value, verified_before,
            updated.verified, updated.matched_identity_field_count,
            case_id_before, updated.case_id,
            getattr(updated.last_nlu, "intent", None),
            updated.requested_fields, restored is not None,
        )
        return reply, updated
