"""Structured claim-fact validation before a response reaches the customer."""
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("sop_agent.factual_guard")


@dataclass
class FactualGuardResult:
    passed: bool
    unsupported_claims: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    missing_required_fields: list[str] = field(default_factory=list)
    corrected_response_required: bool = False
    source_fields_used: list[str] = field(default_factory=list)


class FactualAccuracyGuard:
    def validate(self, answer, claim, approved_sources=None):
        answer = answer or ""
        unsupported, contradictions = [], []
        missing_fields = []
        source_fields = [field for field in ("case_id", "status", "expected_reimbursement_amount",
                         "payment_date", "document_submission_deadline", "appeal_deadline",
                         "documents_needed", "denial_reason") if field in claim]
        if "documents_needed" not in claim:
            missing_fields.append("documents_needed")
        case_id = str(claim.get("case_id", ""))
        ids = re.findall(r"\bCL-\d+\b", answer, re.I)
        if ids and any(value.upper() != case_id.upper() for value in ids):
            contradictions.append("claim_id")
        amount = claim.get("expected_reimbursement_amount")
        # Claim IDs contain digits; do not treat CL-2011 as a payment amount
        # just because a generic menu sentence mentions reimbursement.
        answer_without_claim_ids = re.sub(r"\bCL-\d+\b", "", answer, flags=re.I)
        if amount is not None and re.search(r"\b(?:reimbursement|payment|receive|amount)\b.{0,80}\b\d+(?:\.\d{1,2})?\b", answer_without_claim_ids, re.I):
            if str(amount) not in answer:
                contradictions.append("reimbursement_amount")
        status = claim.get("status")
        status_assertion = re.search(r"\b(?:status\s*(?:is|:)?|recorded\s+as)\s+(?:the\s+)?(?:open|closed|denied)\b", answer, re.I)
        if status and status_assertion and status.casefold() not in answer.casefold():
            contradictions.append("claim_status")
        trusted_dates = {
            str(value) for field, value in claim.items()
            if field.endswith("_date") or field in {"created_at", "appeal_deadline", "document_submission_deadline"}
        }
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", answer)
        if dates and trusted_dates and any(value not in trusted_dates for value in dates):
            contradictions.append("date")
        unavailable_language = re.search(r"(?:not available|does not specify|not listed|cannot confirm|can't confirm|no record|unable to confirm)", answer, re.I)
        if "documents_needed" not in claim:
            # A limitation statement is safe. Only a concrete document claim
            # is unsupported when no approved source contains document data.
            concrete_document = re.search(r"(?:missing documents are|documents needed are|requires? the)\s*:?\s*([^.;]+)", answer, re.I)
            approved_docs = set((approved_sources or {}).get("documents_needed", []))
            if concrete_document and not unavailable_language:
                stated = concrete_document.group(1).casefold()
                if not approved_docs or not any(doc.casefold() in stated for doc in approved_docs):
                    unsupported.append("documents_needed")
        concrete_denial = re.search(r"\b(?:denial reason|denied because|rejected because)\s*(?:is|:)", answer, re.I)
        if "denial_reason" not in claim and concrete_denial and not unavailable_language:
            unsupported.append("denial_reason")
        passed = not unsupported and not contradictions
        result = FactualGuardResult(passed, unsupported, contradictions, missing_fields, not passed, source_fields)
        if not passed:
            logger.warning("factual_guard_failed statements=%r unsupported=%s contradictions=%s missing_fields=%s sources=%s values=%s",
                           answer[:500], unsupported, contradictions, missing_fields, source_fields,
                           {field: claim.get(field) for field in source_fields})
        else:
            logger.info("factual_guard_passed missing_fields=%s sources=%s", missing_fields, source_fields)
        return result

    def safe_fallback(self, claim, result):
        case_id = claim.get("case_id", "the claim")
        return f"I can only confirm information supported by the record for {case_id}. " \
               "The requested detail could not be safely confirmed from the available claim data."
