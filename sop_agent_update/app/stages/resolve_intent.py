"""Claim selection and intent resolution after identity verification."""
import re
from ..models import Hint, Phase
from ..handoff import confirm_handoff, request_handoff


def _continue_pending(state, text, interpreter, repository, parsed):
    """Answer the intent captured before claim selection in the same turn."""
    # Prefer the current turn's structured request. After identity correction
    # the session may still contain the old ``unclear`` intent even though the
    # verified caller has now asked a concrete question such as claim status.
    current_intent = parsed.intent if parsed and parsed.intent not in (None, "unclear") else state.current_intent
    if current_intent not in (None, "unclear"):
        state.current_intent = current_intent
        if parsed and parsed.requested_fields:
            state.requested_fields = list(dict.fromkeys(parsed.requested_fields))
            state.pending_requested_fields = list(state.requested_fields)
        from . import process_case
        return process_case.run(state, text, interpreter, repository, parsed)
    return "I found and verified your claim. What would you like help with?"


def _other_claim_request(text: str) -> bool:
    return bool(re.search(
        r"\b(?:another|different|other|switch|change)\s+(?:to\s+)?(?:my\s+)?(?:claim|case)\b"
        r"|\b(?:switch|change|use|check)\b.{0,35}\b(?:claim|case)\b"
        r"|\b(?:claim|case)\b.{0,25}\b(?:instead|rather)\b",
        text, re.I,
    ))


def _previous_claim_request(text: str) -> bool:
    return bool(re.search(r"(?:previous|last|earlier|back to)\s+(?:claim|case)|回到上一个(?:claim|case|案件)|上一个(?:claim|case|案件)", text, re.I))


def _selection_prompt(options):
    return ("I found multiple claims linked to your verified account. "
            "Please provide the claim number or describe the claim using its "
            "approximate month, type, or status.")


def run(state, text, interpreter, repository, parsed=None):
    if not state.verified or not state.party_id:
        parsed = parsed or interpreter.interpret(text, state)
        if parsed.human or parsed.workflow_action == "request_human":
            state.handoff_reason = "explicit_human_request_before_claim_selection"
            if state.awaiting_handoff_confirmation:
                return confirm_handoff(state, repository)
            request_handoff(state, state.handoff_reason)
            return confirm_handoff(state, repository)
        return "Before selecting a claim, I need to verify your identity."
    parsed = parsed or interpreter.interpret(text, state)
    # The structured model may normalize an unrelated definition question to
    # ``unclear``. Re-check only the deterministic scope signal at the router
    # boundary so non-claim questions bypass selection; insurance terminology
    # remains supported because RuleInterpreter marks it as related.
    from ..interpreter import RuleInterpreter
    scope_check = (RuleInterpreter().interpret(text, state)
                   if __import__("os").getenv("ALLOW_LOCAL_RULES") == "1" else parsed)
    identity_only = bool(scope_check.identity) and not scope_check.requested_fields and not re.search(
        r"\b(?:status|claim|case|document|payment|appeal|denied|summary|next steps?)\b",
        text, re.I,
    )
    if identity_only:
        # Keep an identity-only turn on the verification path. This updates
        # and rechecks PII without turning the turn into claim selection.
        if state.verified:
            return "Your identity has already been verified successfully. What would you like help with?"
        from . import verify_identity
        parsed.human = False
        parsed.workflow_action = "none"
        parsed.intent = "unclear"
        reply = verify_identity.run(state, text, interpreter, repository, parsed)
        if state.verified and not state.case_id:
            return "I noted the identity information. I will not access claim details until you choose a claim."
        return reply
    if scope_check.unrelated and not scope_check.identity and not scope_check.human:
        parsed.intent = "out_of_scope"
        parsed.unrelated = True
        parsed.requested_fields = []
        parsed.requested_field = []
    if scope_check.intent == "finish" and not scope_check.human:
        parsed.intent = "finish"
        parsed.human = False
        parsed.workflow_action = "finish"
    # Out-of-scope and explicit completion turns do not require claim
    # selection. They are handled before any claim lookup or selection prompt.
    if parsed.unrelated or parsed.intent == "out_of_scope":
        return "I can only assist with insurance claim-related questions."
    if parsed.intent == "finish":
        state.phase = Phase.COMPLETE
        state.conversation_complete = True
        return "Understood. I’ll close this conversation. Thank you for contacting us."
    if parsed.human or parsed.workflow_action == "request_human":
        state.handoff_reason = "explicit_human_request_before_claim_selection"
        if state.awaiting_handoff_confirmation:
            return confirm_handoff(state, repository)
        request_handoff(state, state.handoff_reason)
        return confirm_handoff(state, repository)

    if _previous_claim_request(text) and state.claim_history:
        previous = state.claim_history[-1]
        if repository.claim(state.party_id, previous):
            if state.case_id and state.case_id != previous:
                state.claim_history.append(state.case_id)
            state.case_id = previous
            state.phase = Phase.PROCESS_CASE
            state.awaiting_case_selection = False
            return f"I returned to your previous verified claim {state.case_id}. What would you like help with?"
        return "I could not restore the previous claim for this verified account."

    # A verified caller may switch claims without re-verifying identity.
    if _other_claim_request(text):
        state.excluded_case_id = state.case_id
        if state.case_id:
            state.claim_history.append(state.case_id)
        # A claim switch starts a fresh claim-resolution context. Preserve
        # identity/verification, but remove every fact tied to the old claim.
        state.active_claim_id = None
        state.case_id = None
        state.case_options = []
        state.claim_candidates_before_filter = []
        state.claim_candidates_after_filter = []
        state.retrieved_fields = []
        state.answered_fields = []
        state.unavailable_fields = []
        state.needs_clarification_fields = []
        state.pending_requested_fields = []
        state.requested_fields = []
        state.superseded_fields = []
        state.answer_status = "unknown"
        state.current_intent = None
        # Do not carry old month/type/status/policy hints into the new match.
        state.hint = Hint()
        # Hard-extract explicit switch clues so a model omission cannot leave
        # the old hint active (e.g. "check my February claim instead").
        month_match = re.search(
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
            text, re.I,
        )
        if month_match:
            state.hint.month = {
                "january": 1, "february": 2, "march": 3, "april": 4,
                "may": 5, "june": 6, "july": 7, "august": 8,
                "september": 9, "october": 10, "november": 11, "december": 12,
            }[month_match.group(1).casefold()]
        # Never carry the previous claim ID through a switch. The new claim
        # must be found from the current turn's fresh clues.
        parsed.hint.case_id = None
        for field in ("case_type", "month", "year", "status", "policy_number", "excluded_case_type"):
            value = getattr(parsed.hint, field, None)
            if value is not None:
                setattr(state.hint, field, value)
        # The current turn's explicit month has priority over any stale hint
        # echoed by the API from the previously active claim.
        if month_match:
            state.hint.month = {
                "january": 1, "february": 2, "march": 3, "april": 4,
                "may": 5, "june": 6, "july": 7, "august": 8,
                "september": 9, "october": 10, "november": 11, "december": 12,
            }[month_match.group(1).casefold()]
        # Re-run matching with the new hints; do not merely list every other
        # claim, otherwise the old active claim can remain authoritative.
        state.case_options = [
            claim["case_id"] for claim in repository.claims_for(state.party_id, state.hint)
            if claim.get("case_id") != state.excluded_case_id
        ]
        state.awaiting_case_selection = bool(state.case_options)
        if len(state.case_options) == 1:
            state.case_id = state.case_options[0]
            state.active_claim_id = state.case_id
            state.excluded_case_id = None
            state.case_options = []
            state.awaiting_case_selection = False
            state.phase = Phase.PROCESS_CASE
            # A pure claim-switch turn is not a request to replay the prior
            # claim's intent. Do not answer the old denial/details field or
            # trigger an unavailable-field human escalation here.
            state.current_intent = None
            state.requested_fields = []
            state.pending_requested_fields = []
            parsed.intent = "unclear"
            parsed.requested_fields = []
            parsed.requested_field = []
            return "I switched to the requested claim. What would you like to know about it?"
        if state.case_options:
            return _selection_prompt(state.case_options)
        return "I could not find another claim linked to your verified account."

    # Resolve a pending list selection by number or explicit claim ID.
    if state.awaiting_case_selection:
        selected = repository.select_case_id(text, state.case_options)
        if selected:
            if state.case_id and state.case_id != selected:
                state.claim_history.append(state.case_id)
            state.case_id = selected
            state.active_claim_id = selected
            state.excluded_case_id = None
            state.awaiting_case_selection = False
            state.case_options = []
            state.phase = Phase.PROCESS_CASE
            return _continue_pending(state, text, interpreter, repository, parsed)
        return _selection_prompt(state.case_options)

    parsed_case_id = parsed.hint.case_id
    if parsed_case_id:
        for field in ("case_id", "case_type", "month", "year", "status", "policy_number", "excluded_case_type"):
            value = getattr(parsed.hint, field, None)
            if value is not None:
                setattr(state.hint, field, value)
        # Never trust a supplied ID without checking ownership in Repository.
        claim = repository.claim(state.party_id, parsed_case_id)
        if claim and claim.get('case_id') != state.excluded_case_id:
            if state.case_id and state.case_id != claim['case_id']:
                state.claim_history.append(state.case_id)
            state.case_id = claim['case_id']
            state.active_claim_id = state.case_id
            state.excluded_case_id = None
            state.phase = Phase.PROCESS_CASE
            return _continue_pending(state, text, interpreter, repository, parsed)
        return "I could not find that claim for your verified account. Please provide another claim ID or describe the claim."

    for field in ("case_id", "case_type", "month", "year", "status", "policy_number", "excluded_case_type"):
        value = getattr(parsed.hint, field, None)
        if value is not None:
            setattr(state.hint, field, value)
    before = repository.claims_for_party(state.party_id)
    state.claim_candidates_before_filter = [c["case_id"] for c in before]
    claims = repository.claims_for(state.party_id, state.hint)
    claims = [claim for claim in claims if claim.get('case_id') != state.excluded_case_id]
    state.claim_candidates_after_filter = [c["case_id"] for c in claims]
    if len(claims) == 1:
        state.case_id = claims[0]["case_id"]
        state.active_claim_id = state.case_id
        state.excluded_case_id = None
        state.phase = Phase.PROCESS_CASE
        return _continue_pending(state, text, interpreter, repository, parsed)
    if len(claims) > 1:
        state.case_options = [claim['case_id'] for claim in claims]
        state.awaiting_case_selection = True
        return _selection_prompt(state.case_options)
    return "Please provide the claim ID, or describe its approximate month, year, type, or status."
