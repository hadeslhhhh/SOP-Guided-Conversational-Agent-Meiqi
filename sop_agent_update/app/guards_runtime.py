"""Runtime response guards for grounding, leakage, requested fields, and uncertainty."""
import re
from .identity_guard import IdentityVerificationGuard
from .factual_guard import FactualAccuracyGuard


class GroundingValidator:
    def validate(self, answer, claim):
        allowed = {str(value) for value in claim.values() if value is not None}
        factual_markers = ("recorded", "claim", "deadline", "reimbursement", "denial")
        # Customer-facing replies intentionally redact internal claim IDs as
        # "your claim". That remains grounded because the authorized claim is
        # already supplied to this validator.
        redacted_claim = "your claim" in answer.casefold() and bool(claim.get("case_id"))
        return (not any(marker in answer.casefold() for marker in factual_markers)
                or redacted_claim
                or any(value in answer for value in allowed))


class LeakGuard:
    def validate(self, answer, identity):
        sensitive = [str(value) for value in identity.values() if value]
        return not any(value in answer for value in sensitive)


class RequestedFieldValidator:
    def validate(self, answer, parsed, state=None):
        fields = parsed.requested_fields or parsed.requested_field
        if not fields and state is not None:
            fields = state.requested_fields
        if not fields:
            return True
        if state is not None:
            covered = set(state.answered_fields) | set(state.unavailable_fields) | set(state.needs_clarification_fields)
            return set(fields) <= covered
        return True


def guarded_answer(answer, state, claim, parsed, response_service):
    identity = IdentityVerificationGuard().check(state)
    if not identity["passed"]:
        return "I need to verify your identity before sharing protected claim information."
    factual = FactualAccuracyGuard().validate(answer, claim)
    state.factual_guard_passed = factual.passed
    state.factual_guard_failure_reason = None if factual.passed else "; ".join(factual.contradictions + factual.unsupported_claims)
    if not factual.passed:
        # Rebuild from trusted field-level data before falling back. The
        # factual guard validates assertions; it does not choose the user's
        # requested field or substitute an intent-level answer.
        corrected = response_service.compose(state, claim, parsed, claim)
        corrected_result = FactualAccuracyGuard().validate(corrected, claim)
        if corrected_result.passed:
            state.factual_guard_passed = True
            state.factual_guard_failure_reason = None
            return response_service.deduplicate(corrected)
        return FactualAccuracyGuard().safe_fallback(claim, corrected_result)
    grounding = GroundingValidator()
    leak = LeakGuard()
    requested = RequestedFieldValidator()
    if not leak.validate(answer, state.identity) or not grounding.validate(answer, claim) or not requested.validate(answer, parsed, state):
        return "The available record does not contain enough authorized information to answer that question."
    return response_service.deduplicate(answer)
