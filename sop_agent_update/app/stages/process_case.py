"""Authorized claim question handling. Facts come only from Repository."""
import re
from ..models import Phase
from ..response_service import ResponseService
from ..guards_runtime import guarded_answer
from ..escalation import evaluate
from ..handoff import confirm_handoff, request_handoff


def _guard_response(state, claim, answer):
    """Ground every answer in the authorized claim and block identity leakage."""
    if claim.get('case_id') not in answer and not answer.startswith(('I can', 'I’m sorry', "I'm sorry")):
        return "The available record does not contain enough information to answer that question."
    identity_values = [str(value) for value in state.identity.values() if value]
    sensitive = identity_values + [str(claim.get('party_id', ''))]
    if any(value and value in answer for value in sensitive):
        return "I can answer questions about the claim, but I cannot repeat private identity details."
    return answer


def _answer_for_intent(intent, claim):
    if intent == 'status':
        return f"Claim {claim['case_id']} is recorded as {claim['status']}."
    if intent in {'documents', 'submission', 'alternatives'}:
        docs = ', '.join(claim.get('documents_needed') or []) or 'No missing documents are listed.'
        return f"The recorded missing documents for claim {claim['case_id']} are: {docs}."
    if intent in {'denial', 'denial_details'}:
        return f"The recorded denial reason for claim {claim['case_id']} is: {claim.get('denial_reason', 'not available in the record.')}"
    if intent == 'payment':
        return f"The recorded reimbursement for claim {claim['case_id']} is {claim.get('expected_reimbursement_amount', 'not available in the record.')}"
    if intent == 'appeal':
        return f"The recorded appeal deadline for claim {claim['case_id']} is {claim.get('appeal_deadline', 'not available in the record.')}"
    if intent == 'timing':
        return f"The available record does not include a processing-time guarantee for claim {claim['case_id']}."
    return f"I can review the status, denial reason, documents, appeal, payment, or next steps for claim {claim['case_id']}."


def run(state, text, interpreter, repository, parsed=None):
    parsed = parsed or interpreter.interpret(text, state)
    # Current-turn explicit transfer requests take precedence over identity
    # corrections and any stale claim/request state. The request is routed to
    # the existing handoff mechanism without disclosing claim details.
    if parsed.human or parsed.workflow_action == "request_human":
        state.handoff_reason = "explicit_human_request_during_claim_processing"
        if state.awaiting_handoff_confirmation:
            return confirm_handoff(state, repository)
        request_handoff(state, state.handoff_reason)
        return confirm_handoff(state, repository)
    if (re.fullmatch(r"(?:ok(?:ay)?|thanks|thank you|thank you so much|much appreciated)", text.strip(), re.I)
            and state.verified and state.case_id and state.answered_fields):
        state.phase = Phase.POST_PROCESS
        state.post_process_step = "ask_followup"
        state.followup_sent = True
        return "You’re welcome. I’m glad I could clarify that. Do you have any other questions about your claim?"
    from ..interpreter import is_identity_update, extract_identity
    if is_identity_update(text):
        # Route explicit PII corrections through the existing identity guard;
        # they are never claim-field questions. Keep the authorized claim
        # available only if the corrected evidence verifies successfully.
        if not parsed.identity:
            parsed.identity.update(extract_identity(text))
        previous_case = state.case_id
        from . import verify_identity
        reply = verify_identity.run(state, text, interpreter, repository, parsed)
        if state.verified and previous_case and repository.claim(state.party_id, previous_case):
            state.case_id = previous_case
            state.phase = Phase.PROCESS_CASE
        state.requested_fields = []
        state.pending_requested_fields = []
        state.retrieved_fields = []
        state.answered_fields = []
        state.unavailable_fields = []
        state.needs_clarification_fields = []
        return reply
    claim = repository.authorized_claim(state)
    if not claim:
        return "Before I can access claim information, I need to verify your identity and select your claim."
    if state.awaiting_handoff_confirmation:
        lowered = text.strip().casefold()
        if lowered in {"yes", "yes please", "sure", "please do", "transfer me"}:
            return confirm_handoff(state, repository)
        if lowered in {"no", "no thanks", "not now", "skip"}:
            state.awaiting_handoff_confirmation = False
            state.escalation_required = False
            state.escalation_reason = None
            state.handoff_status = "declined"
            state.handoff = None
            return "Understood. I’ll continue helping with your claim here."
        return "Would you like me to connect you with a human representative? Reply yes or no."
    if re.search(r"(?:previous|last|earlier|back to)\s+(?:claim|case)|回到上一个(?:claim|case|案件)|上一个(?:claim|case|案件)", text, re.I):
        if state.claim_history:
            previous = state.claim_history[-1]
            if repository.claim(state.party_id, previous):
                state.claim_history.append(state.case_id)
                state.case_id = previous
                return f"I returned to your previous verified claim {state.case_id}. What would you like help with?"
        return "I could not find a previous claim in this conversation."
    if re.search(r"\b(?:another|different|other|switch|change)\s+(?:claim|case)\b", text, re.I):
        current_case_id = state.case_id
        options = repository.other_claim_ids(state.party_id, current_case_id)
        if options:
            state.claim_history.append(current_case_id)
            state.excluded_case_id = current_case_id
            state.case_id = None
            state.case_options = options
            state.awaiting_case_selection = True
            state.phase = Phase.RESOLVE_INTENT
            return "I found these other claims: " + " ".join(
                f"{index}. {case_id}" for index, case_id in enumerate(options, 1)
            ) + ". Please choose one."
        # Keep the current authorized claim active when switching is impossible.
        # A failed switch must not force the caller to identify the same claim again.
        return f"I could not find another claim linked to your verified account. I’ll keep claim {current_case_id} selected."
    claim_id_only = bool(re.fullmatch(r"\s*CL[- ]?\d+\s*", text, re.I))
    from ..interpreter import requested_fields_from_text
    explicit_fields = requested_fields_from_text(text, parsed.intent)
    requested_fields = list(dict.fromkeys(parsed.requested_fields or parsed.requested_field))
    if claim_id_only or (state.pending_requested_fields and not explicit_fields):
        requested_fields = list(state.pending_requested_fields)
    elif explicit_fields:
        requested_fields = list(dict.fromkeys(explicit_fields))
    if not requested_fields:
        requested_fields = list(state.pending_requested_fields)
    if not requested_fields and parsed.intent not in (None, "unclear"):
        requested_fields = ResponseService._fields_for_intents([parsed.intent, *parsed.secondary_intents])
    else:
        # A structured NLU response may populate only the primary field even
        # when secondary_intents contains additional customer requests. Merge
        # the intent-derived fields so every distinct request is answered.
        for field in ResponseService._fields_for_intents([parsed.intent, *parsed.secondary_intents]):
            if field not in requested_fields:
                requested_fields.append(field)
    # The business node is authoritative about the fields it will answer.
    # Keep Parsed and State aligned so ResponseService and coverage validation
    # consume the same current-turn contract instead of stale model fields.
    parsed.requested_fields = list(requested_fields)
    parsed.requested_field = list(requested_fields)
    import logging
    logging.getLogger("sop_agent.workflow").info(
        "process_case_final_fields fields=%s parsed_intent=%s pending_state=%s",
        [str(value) for value in requested_fields],
        parsed.intent,
        [str(value) for value in state.pending_requested_fields],
    )
    if requested_fields:
        state.superseded_fields.extend(f for f in state.pending_requested_fields if f not in requested_fields)
        state.superseded_fields = list(dict.fromkeys(state.superseded_fields))
        state.requested_fields = requested_fields
        state.pending_requested_fields = list(requested_fields)
    if state.awaiting_handoff_confirmation and text.strip().casefold() in {"no", "no thanks", "not now", "skip"}:
        state.awaiting_handoff_confirmation = False
        state.escalation_required = False
        state.escalation_reason = None
        state.handoff_status = "declined"
        state.handoff = None
        return "Understood. I’ll continue helping with your claim here."
    if parsed.emotion_type != "neutral":
        state.emotional_intent_count += 1
        state.emotional_intent_streak += 1
        state.emotion = parsed.emotion_type
        state.emotion_type = parsed.emotion_type
        state.emotion_level = parsed.emotion_level or ("high" if state.emotional_intent_streak >= 3 else "medium")
    else:
        state.emotional_intent_streak = 0
        state.emotion_type = "neutral"
        state.emotion = None
        state.emotion_level = "low"
    if parsed.human or parsed.workflow_action == "request_human":
        state.handoff = "requested"
        state.handoff_reason = "explicit_human_request"
        state.handoff_status = "requested"
        state.escalation_required = True
        state.escalation_reason = "explicit_human_request"
        return confirm_handoff(state, repository)
    if parsed.intent == 'finish' or re.search(r"\b(?:done|finished|that's all|that is all|no more questions|goodbye)\b", text, re.I):
        state.phase = Phase.POST_PROCESS
        state.post_process_step = 'ask_summary'
        return "Would you like a summary of this claim conversation sent to your registered email address? Reply send or skip."
    # Claim selection turns often contain no business question. Reuse the
    # pending intent captured before verification instead of losing it. This
    # must happen before the unrelated-input guard because "1" is a valid
    # selection response, not an unrelated request.
    effective_intent = parsed.intent
    if effective_intent in (None, "unclear") and state.current_intent not in (None, "unclear"):
        parsed.intent = state.current_intent
        effective_intent = state.current_intent
    state.current_intent = effective_intent
    state.intents.append({"intent": effective_intent or "unclear", "question": text[:500]})
    if parsed.unrelated or effective_intent == "out_of_scope":
        state.out_of_scope_count += 1
        state.unresolved_intent_streak = state.out_of_scope_count
        if state.out_of_scope_count >= 4:
            state.escalation_required = True
            state.escalation_reason = "repeated_out_of_scope"
            state.escalation_offered = True
            state.handoff_status = "offered"
            state.handoff_reason = "The agent could not identify a claim-related request after repeated out-of-scope questions."
            state.awaiting_handoff_confirmation = True
            return "I can only assist with insurance claim-related questions. We have not been able to identify a claim-related request after several attempts. Would you like me to connect you with a human representative? Reply yes or no."
        return "I can only assist with insurance claim-related questions."
    state.out_of_scope_count = 0
    if effective_intent == "unsupported_intent":
        restricted_request = bool(re.search(
            r"\b(?:internal|staff[- ]only|adjuster|reviewer|private|confidential)\b.{0,45}"
            r"\b(?:notes?|comments?|annotations?|remarks?)\b|\b(?:notes?|comments?|annotations?)\b.{0,45}"
            r"\b(?:internal|staff[- ]only|adjuster|reviewer|private|confidential)\b",
            text, re.I,
        ))
        if restricted_request:
            answer = ("I can’t provide internal adjuster notes, staff-only review comments, or other restricted "
                      "internal claim annotations. I can help with the authorized claim status, documents, "
                      "payment, denial reason, appeal information, or next steps.")
        else:
            answer = ("I can only help with insurance claim-related questions. I can assist with claim status, "
                      "documents, payment, denial reason, appeal information, or next steps.")
        evaluate(state, parsed, "unsupported")
        if state.escalation_required and state.escalation_offered:
            answer += " If this remains unresolved, I can offer a human representative."
        return answer
    if effective_intent in (None, "unclear") and not state.requested_fields:
        state.unresolved_intent_streak += 1
        month = state.hint.month
        case_type = state.hint.case_type
        if month and case_type:
            import calendar
            context = f"your {calendar.month_name[month]} {case_type} claim"
            return f"I can help with {context}. What specifically would you like to know about it?"
        if state.case_id:
            return "I can help with this claim. What specifically would you like to know about it?"
        return "I can help with your claim. What specifically would you like to know?"
    response_service = ResponseService(repository.guidance)
    deterministic_draft = response_service.compose(state, claim, parsed, claim)
    # API-only response layer: Python selects and authorizes facts; OpenAI
    # turns those facts into the final conversational answer. There is no
    # local-language fallback in the production path.
    if not hasattr(interpreter, "generate_response"):
        raise RuntimeError("The API response generator is not configured.")
    answer = interpreter.generate_response(
        text,
        deterministic_draft,
        {
            "claim": claim,
            "requested_fields": parsed.requested_fields,
            "answered_fields": state.answered_fields,
            "unavailable_fields": state.unavailable_fields,
            "guidance": repository.guidance,
        },
        state,
    )
    answer = guarded_answer(answer, state, claim, parsed, response_service)
    if state.unavailable_fields:
        answer += ("\n\nI could not confirm every requested detail from the authorized record. "
                   "I can connect you with a human representative to review the remaining information.")
    # compose() records field-level coverage.  A related answer is not a
    # successful answer and must remain pending for the next turn.
    answer_status = "answered" if not state.unavailable_fields and not state.pending_requested_fields else "unavailable"
    evaluate(state, parsed, answer_status)
    if state.escalation_required and state.escalation_reason == "high_emotion":
        answer += " I can also arrange human support because this situation sounds highly distressing."
    state.intents[-1]["answer"] = answer
    return answer
