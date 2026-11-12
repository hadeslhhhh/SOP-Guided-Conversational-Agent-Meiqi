"""Identity collection, verification, refusal handling, and safety gate."""
import re
from ..models import Phase, Session, EmailConsent
from ..handoff import confirm_handoff, request_handoff
from ..interpreter import is_identity_update, email_consent_kind

REQUIRED_FIELDS = ("name", "dob", "phone", "email", "id_last4")
REFUSAL_PATTERNS = (
    "i refuse", "i don't want to provide", "i do not want to provide",
    "i won't provide", "not giving", "cannot provide",
)
HIGH_RISK_PATTERNS = ("want to die", "kill myself", "end my life")


def _high_risk(text: str) -> bool:
    lowered = text.casefold()
    return any(pattern in lowered for pattern in HIGH_RISK_PATTERNS)


def _refused(text: str) -> bool:
    lowered = text.casefold()
    return any(pattern in lowered for pattern in REFUSAL_PATTERNS)


def _verification_only(state: Session, text: str, parsed) -> bool:
    """Recognize an identity/case reference without a business question."""
    if "?" in text:
        return False
    if not re.search(r"\b(?:name|dob|date of birth|born|ssn|last four|phone|email)\b", text, re.I):
        return False
    if re.search(r"\b(?:status|denied|documents?|payment|deadline|appeal|next steps|why|what|how|tell me|need to know)\b", text, re.I):
        return False
    return _identity_count(state) >= 3 and bool(parsed.hint.case_id)


def _defers_claim_details(text: str) -> bool:
    return bool(re.search(
        r"\b(?:do not|don't|dont|please do not|please don't)\b.*\b(?:tell|show|give|disclose|share)\b.*\b(?:claim|case|account)?\s*(?:details?|information|results?)\b"
        r"|\b(?:not yet|for now)\b.*\b(?:claim|case|account)?\s*(?:details?|information)\b",
        text,
        re.I,
    ))


def _identity_count(state: Session) -> int:
    return sum(bool(state.identity.get(field)) for field in REQUIRED_FIELDS)


def _merge_hint(state, incoming):
    """Merge newly stated case context; PII turns must not erase it."""
    for field in ("case_id", "case_type", "month", "year", "status", "policy_number", "excluded_case_id", "excluded_case_type"):
        value = getattr(incoming, field, None)
        if value is not None:
            setattr(state.hint, field, value)


def _remember(state: Session, text: str, reply: str) -> None:
    state.history.extend([
        {"role": "user", "content": text[:4000]},
        {"role": "assistant", "content": reply[:4000]},
    ])
    state.history = state.history[-12:]


def _lock_after_failed_verification(state: Session) -> None:
    """Remove protected workflow access after an identity failure.

    Keep the case and request context for conversion continuity; the phase
    guard, not destructive state clearing, prevents protected reads.
    """
    state.verified = False
    state.phase = Phase.VERIFY_ID


def _verification_message(state: Session, text: str, *, insufficient: bool, parsed=None) -> str:
    """Natural wording after the guard has made the security decision."""
    emotional = parsed and parsed.emotion_type != "neutral"
    if state.verification_failures >= 5:
        return "I'm still unable to complete the identity check, so I can't access claim details yet. I can help you reach a representative for the next steps."
    if insufficient:
        present = set(state.identity)
        missing = []
        if "name" not in present:
            missing.append("your full name")
        if "dob" not in present:
            missing.append("your date of birth")
        alternatives = []
        if "phone" not in present:
            alternatives.append("phone number")
        if "email" not in present:
            alternatives.append("email")
        if "id_last4" not in present:
            alternatives.append("ID last four digits")
        if len(present) < 3 and alternatives:
            missing.append("one accepted detail such as " + ", ".join(alternatives[:2]))
        requested = ", ".join(missing)
        base = ("Before I look into your claim, I'll need to verify your identity to keep your personal "
                "and claim information secure. Please provide " + requested + ".")
        return ("I know this is an extra step, but " + base[0].lower() + base[1:]) if emotional else base
    return "I wasn't able to verify your identity with the information provided. To protect your account, please double-check the details or provide another accepted verification method."


def run(state: Session, text: str, interpreter, repository, parsed=None):
    # Safety is checked before extraction and before any account lookup.
    if _high_risk(text):
        state.handoff = "requested"
        reply = (
            "I’m really sorry you’re feeling this way. Your immediate safety matters. "
            "I want to connect you with a human representative right away."
        )
        _remember(state, text, reply)
        return reply

    parsed = parsed or interpreter.interpret(text, state)
    if parsed.workflow_action == "request_human" or parsed.human:
        state.handoff_reason = "explicit_human_request_before_verification"
        if state.awaiting_handoff_confirmation:
            return confirm_handoff(state, repository)
        request_handoff(state, state.handoff_reason)
        return confirm_handoff(state, repository)
    if _refused(text):
        state.refusals += 1
        if state.refusals >= 3:
            state.handoff = "offered"
            reply = "I cannot disclose claim details without identity verification. Would you like a human representative?"
            _remember(state, text, reply)
            return reply

    consent_kind = email_consent_kind(text, state)
    if consent_kind == "send_email":
        state.email_consent = EmailConsent.ACCEPTED
        state.email_choice = "requested"
        return "Understood. I’ll prepare the conversation summary request. I have not sent an email."
    if consent_kind == "skip_email":
        state.email_consent = EmailConsent.DECLINED
        state.email_choice = "skipped"
        return "Understood. I will not send an email summary."
    if consent_kind == "revoke_email":
        state.email_consent = EmailConsent.REVOKED
        state.email_choice = "revoked"
        state.pending_email_body = None
        return "Understood. The email summary request has been cancelled."
    # A newly supplied value replaces the prior value for that field. This is
    # intentional: explicit corrections must not leave stale PII in state.
    state.identity.update(parsed.identity)
    # Keep the customer's business question while the identity gate is open.
    # The next turn may contain only a claim ID, but it must still answer the
    # request made in this turn (for example, documents or deadline).
    if parsed.intent and parsed.intent != "unclear":
        state.current_intent = parsed.intent
        state.original_intent = state.original_intent or parsed.intent
        state.intents.append({"intent": parsed.intent, "question": text[:500]})
    if parsed.requested_fields:
        state.requested_fields = list(dict.fromkeys(parsed.requested_fields))
        state.pending_requested_fields = list(state.requested_fields)
    # A caller may provide the claim ID in the same turn as identity data.
    # Preserve it across the identity gate; do not discard NLU hints when the
    # stage changes from VERIFY_ID to RESOLVE_INTENT.
    _merge_hint(state, parsed.hint)
    if _defers_claim_details(text):
        # The customer explicitly asked us not to access/disclose claim data
        # yet. Keep identity data available for the next turn, but do not
        # verify, select, or expose a claim from this turn.
        _lock_after_failed_verification(state)
        reply = "Understood. I will not access or disclose claim details yet. Please provide the remaining identity information when you are ready."
        _remember(state, text, reply)
        return reply
    state.matched_identity_field_count = 0
    verification = repository.verify_detailed(state.identity)
    state.identity_mismatches = verification["mismatches"]
    state.verification_status = verification["status"]
    person = verification["person"]
    if person:
        state.verified = True
        state.verification_status = "verified"
        state.party_id = person["party_id"]
        state.matched_identity_field_count = repository.matched_identity_field_count(state.identity, person)
        if parsed.hint.case_id:
            claim = repository.claim(state.party_id, parsed.hint.case_id)
            if claim:
                state.case_id = claim["case_id"]
                state.phase = Phase.PROCESS_CASE
                if _verification_only(state, text, parsed):
                    reply = f"Verified successfully. Claim {state.case_id} is selected. What would you like to know?"
                elif (parsed.requested_fields or parsed.intent not in (None, "unclear")) and not is_identity_update(text):
                    from . import process_case
                    reply = process_case.run(state, text, interpreter, repository, parsed)
                else:
                    reply = "Your identity information has been corrected and verified successfully."
                _remember(state, text, reply)
                return reply
        # Locate automatically when the verified party has exactly one claim.
        # Multiple claims remain in RESOLVE_INTENT and are narrowed by ID,
        # month, type, status, or the customer's claim description.
        before = repository.claims_for_party(state.party_id)
        state.claim_candidates_before_filter = [c["case_id"] for c in before]
        claims = repository.claims_for(state.party_id, state.hint)
        state.claim_candidates_after_filter = [c["case_id"] for c in claims]
        if len(claims) == 1 and not is_identity_update(text):
            state.case_id = claims[0]["case_id"]
            state.phase = Phase.PROCESS_CASE
            from . import process_case
            reply = process_case.run(state, text, interpreter, repository, parsed)
            _remember(state, text, reply)
            return reply
        state.phase = Phase.RESOLVE_INTENT
        if parsed.hint.case_id:
            reply = f"Thank you. Your identity is verified. I noted claim {parsed.hint.case_id}; I’m checking it now."
        elif len(claims) > 1:
            reply = "Thank you. Your identity is verified. I found multiple claims. Please provide the claim number or describe the claim using its approximate month, type, or status."
        else:
            reply = "Thank you. Your identity is verified. What would you like help with?"
        _remember(state, text, reply)
        return reply

    _lock_after_failed_verification(state)
    missing = 3 - _identity_count(state)
    if missing > 0:
        reply = _verification_message(state, text, insufficient=True, parsed=parsed)
        _remember(state, text, reply)
        return reply

    state.verification_failures += 1
    if state.verification_failures >= 5:
        state.handoff = "offered"
        reply = _verification_message(state, text, insufficient=False, parsed=parsed)
    else:
        reply = _verification_message(state, text, insufficient=False, parsed=parsed)
    _remember(state, text, reply)
    return reply
