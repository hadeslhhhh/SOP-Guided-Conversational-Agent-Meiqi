"""Response safety checks for claim disclosure and grounding."""

import re


class LeakGuard:
    """Prevent claim-specific information from leaving the verification gate."""

    CLAIM_ID_PATTERN = re.compile(r"\bCL[- ]?\d+\b", re.I)
    SENSITIVE_PATTERNS = (
        r"\b(?:status|denied|denial|rejected|payment|reimbursement|documents? missing|claim outcome)\b",
    )

    @classmethod
    def check(cls, state, answer):
        # LeakGuard applies only during the identity-verification stage.
        # Once verification succeeds, later stages are protected by the
        # authorization and grounding checks instead.
        if state.get("phase") != "VERIFY_ID" or (
            state.get("gates", {}).get("VERIFY_ID") and state.get("verified_party_id")
        ):
            return answer
        lowered = answer.lower()
        if cls.CLAIM_ID_PATTERN.search(answer) or any(re.search(pattern, lowered) for pattern in cls.SENSITIVE_PATTERNS):
            return (
                "To protect your claim information, I need to verify your identity first. "
                "Please provide at least three matching identity details: your full name, "
                "date of birth, phone number, email, or SSN last four digits."
            )
        return answer


class GroundingValidator:
    """Ensure claim-specific responses contain only authorized local facts."""

    CLAIM_ID_PATTERN = re.compile(r"\bCL[- ]?\d+\b", re.I)

    @classmethod
    def check(cls, state, answer, load_json):
        if state.get("phase") != "PROCESS_CASE" or not state.get("case_access_authorized"):
            return answer
        case_id = state.get("matched_case_id")
        if not case_id:
            return answer
        mentioned_ids = {match.upper().replace(" ", "-") for match in cls.CLAIM_ID_PATTERN.findall(answer)}
        if mentioned_ids and mentioned_ids != {case_id.upper()}:
            return cls._uncertain_response()
        # GroundingValidator is intentionally narrow: normal claim guidance,
        # status text, document explanations, and workflow messages are not
        # blocked. Only an explicit claim number is validated here.
        if not mentioned_ids:
            return answer
        claim = next((item for item in load_json("claims.json") if item.get("case_id") == case_id), None)
        if not claim:
            return "I could not find the authorized claim details. Would you like me to connect you with a human claims representative?"
        return answer

    @staticmethod
    def _uncertain_response():
        return (
            "I’m not able to confirm that claim detail from the authorized documents. "
            "Would you like me to connect you with a human claims representative?"
        )


def _validate_requested_field(state, answer):
    field = state.get("last_requested_field")
    if not field or not answer:
        return answer
    lowered = answer.lower()
    conflicts = {
        "document_submission_deadline": ("appeal deadline", "appeal due date"),
        "appeal_deadline": ("submission deadline", "upload deadline"),
        "upload_receipt_status": ("how to upload", "member portal or claim upload link"),
        "document_submission_method": ("documents currently listed", "documents are missing"),
        "missing_document_list": ("how to submit", "member portal or claim upload link"),
    }
    if any(marker in lowered for marker in conflicts.get(field, ())):
        return "I could not confirm the requested information from the authorized claim documents. Would you like me to connect you with a human claims representative?"
    return answer


def apply_response_guards(state, answer, load_json):
    """Apply the active response guards before returning an answer.

    LeakGuard remains active for pre-verification privacy protection.
    GroundingValidator is intentionally disabled for now because it was
    over-intercepting otherwise valid claim responses.
    """
    answer = LeakGuard.check(state, answer)
    answer = GroundingValidator.check(state, answer, load_json)
    return _validate_requested_field(state, answer)
