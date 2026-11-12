"""Deterministic response composition; models may propose, never authorize."""
import re
from datetime import date


class ResponseService:
    def __init__(self, guidance=None):
        self.guidance = guidance or {}

    def _guidance(self, *path):
        value = self.guidance
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value.get("en") if isinstance(value, dict) else value

    def deduplicate(self, answer: str) -> str:
        sections, seen = [], set()
        for section in re.split(r"\n\s*\n", answer.strip()):
            key = re.sub(r"\s+", " ", section).casefold()
            if key and key not in seen:
                seen.add(key)
                sections.append(section.strip())
        return "\n\n".join(sections)

    @staticmethod
    def _hide_claim_ids(answer: str) -> str:
        """Keep internal case IDs out of customer-facing business answers."""
        answer = re.sub(r"\bclaim\s+CL-\d+\b", "your claim", answer, flags=re.I)
        answer = re.sub(r"\bCL-\d+\b", "your claim", answer, flags=re.I)
        return answer

    def _naturalize(self, answer: str, requested_fields, parsed) -> str:
        """Improve conversational flow without changing approved facts.

        This is deliberately deterministic.  The SOP/field answer remains
        the source of truth; this helper only adds connective language and
        avoids making a multi-field response sound like a form dump.
        """
        answer = self.deduplicate(answer)
        sections = [part.strip() for part in answer.split("\n\n") if part.strip()]
        if len(sections) > 1 and requested_fields:
            lead = "Here’s what I found for your request:"
            # Status and missing-document answers are commonly requested
            # together. Join them into one explanation so the second fact is
            # clearly connected to the first instead of sounding like a
            # duplicated form response.
            if (len(sections) == 2
                    and re.match(r"^Claim .* is recorded as denied\.", sections[0], re.I)
                    and re.match(r"^The recorded missing documents", sections[1], re.I)):
                status = re.sub(
                    r"^Claim (.+) is recorded as denied\.",
                    r"I found that \1 is currently recorded as denied.",
                    sections[0],
                    flags=re.I,
                )
                documents = re.sub(
                    r"^The recorded missing documents for .* are:\s*",
                    "The record also shows that the following documents are still needed: ",
                    sections[1],
                    flags=re.I,
                )
                answer = lead + "\n\n" + status + " " + documents
            else:
                answer = lead + "\n\n" + "\n\n".join(sections)
        elif len(sections) == 1 and requested_fields:
            text = sections[0]
            text = re.sub(r"^Claim ([A-Z]{2}-\d+) is recorded as ", r"I found that claim \1 is ", text)
            text = re.sub(r"^The recorded reimbursement for claim ", r"For claim ", text)
            answer = text
        if parsed.emotion_type != "neutral" and not answer.startswith("I understand"):
            answer = "I understand this may be stressful. " + answer
        return answer

    def compose(self, state, claim, parsed, facts):
        parts = []
        state.retrieved_fields = []
        state.answered_fields = []
        state.unavailable_fields = []
        state.needs_clarification_fields = []
        # requested_fields is the field-level contract.  Intent is only a
        # fallback for legacy/ambiguous NLU output.
        requested_fields = list(dict.fromkeys(parsed.requested_fields or parsed.requested_field or []))
        if not requested_fields:
            requested_fields = list(getattr(state, "pending_requested_fields", []))
        # Hard coverage loop: the response layer must never trust a partial
        # requested_fields list. Always add fields implied by every primary
        # and secondary intent, then answer each distinct field below.
        intent_fields = self._fields_for_intents([parsed.intent, *parsed.secondary_intents])
        for field in intent_fields:
            if field not in requested_fields:
                requested_fields.append(field)
        requested = set(requested_fields)
        retrieved, answered, unavailable = [], [], []

        def add(field, text, available=True):
            parts.append(text)
            retrieved.append(field)
            (answered if available else unavailable).append(field)

        for field in requested_fields:
            if field == "claim_status":
                status_text = f"Claim {claim['case_id']} is recorded as {claim['status']}."
                if claim.get("status") == "closed" and not claim.get("closure_reason"):
                    status_text += " The claim record does not include a separate reason for the closure."
                add(field, status_text)
            elif field == "missing_documents":
                docs = claim.get("documents_needed")
                if docs is not None:
                    add(field, f"The recorded missing documents for claim {claim['case_id']} are: {', '.join(docs) or 'none listed'}.")
                else:
                    add(field, f"The claim record does not include a confirmed missing-document list for {claim['case_id']}.", False)
            elif field == "document_submission_channel":
                guidance = self._guidance("default_guidance")
                add(field, f"For claim {claim['case_id']}, the approved guidance is: {guidance}" if guidance else f"The claim record does not specify whether email or another submission channel is accepted for claim {claim['case_id']}.", bool(guidance))
            elif field == "document_submission_deadline":
                value = claim.get("document_submission_deadline")
                guidance = self._guidance("claim_followup_settings", "average_processing_time_after_submission")
                if value:
                    add(field, f"The recorded document-submission deadline for claim {claim['case_id']} is {value}.")
                elif guidance:
                    add(field, f"The claim record does not list a separate document-submission deadline for claim {claim['case_id']}. Approved guidance recommends submitting the requested documents within a week.", False)
                else:
                    add(field, f"The claim record does not include a document-submission deadline for claim {claim['case_id']}.", False)
            elif field == "appeal_deadline":
                value = claim.get("appeal_deadline")
                if value:
                    suffix = " This deadline has passed." if date.fromisoformat(value) < date.today() else ""
                    add(field, f"The recorded appeal deadline for claim {claim['case_id']} is {value}.{suffix}")
                else:
                    add(field, f"The claim record does not include an appeal deadline for claim {claim['case_id']}.", False)
            elif field == "payment_amount":
                value = claim.get("expected_reimbursement_amount")
                add(field, f"The recorded reimbursement amount for claim {claim['case_id']} is {value}." if value is not None else f"The claim record does not include a payment amount for claim {claim['case_id']}.", value is not None)
            elif field == "allowed_amount":
                value = claim.get("allowed_max_amount")
                add(field, f"The recorded allowed amount for claim {claim['case_id']} is {value}." if value is not None else f"The claim record does not include an allowed amount for claim {claim['case_id']}.", value is not None)
            elif field == "payment_date":
                value = claim.get("payment_date")
                add(field, f"The recorded payment date for claim {claim['case_id']} is {value}." if value else f"The claim record does not include a payment date for claim {claim['case_id']}.", bool(value))
            elif field == "denial_reason":
                value = claim.get("denial_reason")
                add(field, f"The recorded denial reason for claim {claim['case_id']} is: {value}." if value else f"The claim record does not include a denial reason for claim {claim['case_id']}.", bool(value))
            elif field == "next_steps":
                add(field, f"Next steps for claim {claim['case_id']} are to provide any listed missing documents and contact the insurer if you need to appeal or confirm submission requirements.")
            elif field == "processing_time":
                guidance = self._guidance("claim_followup_settings", "average_processing_time_after_submission")
                add(field, f"Approved guidance says review usually takes {guidance} after submission for claim {claim['case_id']}; this is not a guarantee." if guidance else f"The available record does not include a processing-time guarantee for claim {claim['case_id']}.", bool(guidance))
            elif field == "document_receipt_status":
                add(field, f"The available record does not include an upload receipt for claim {claim['case_id']}, so I cannot confirm whether the documents were received.", False)
            elif field == "document_alternative":
                guidance = self._guidance("document_alternative_guidance", "default")
                add(field, f"Approved guidance says: {guidance}" if guidance else f"The approved guidance does not identify an alternative for claim {claim['case_id']}.", bool(guidance))
            elif field == "document_requirements":
                guidance = self._guidance("default_guidance")
                add(field, f"Approved document guidance says: {guidance}" if guidance else f"The approved guidance does not specify document requirements for claim {claim['case_id']}.", bool(guidance))
            elif field == "case_summary":
                parts.extend([
                    f"Claim {claim['case_id']} is recorded as {claim.get('status', 'not available')}.",
                    f"Recorded denial reason: {claim.get('denial_reason', 'not available in the record.')}",
                    f"Recorded missing documents: {', '.join(claim.get('documents_needed') or []) or 'none listed'}.",
                    f"Recorded appeal deadline: {claim.get('appeal_deadline', 'not available in the record.')}.",
                ])
                retrieved.append(field)
                answered.append(field)
            else:
                parts.append(f"I need clarification about the requested field '{field}' for claim {claim['case_id']}.")
                retrieved.append(field)
                state.needs_clarification_fields.append(field)

        # Legacy intent responses remain available only when NLU supplied no
        # field-level request. They must never override an explicit field.
        for intent in [] if requested_fields else dict.fromkeys([parsed.intent, *parsed.secondary_intents]):
            if intent == "status":
                parts.append(f"Claim {claim['case_id']} is recorded as {claim['status']}.")
            elif intent == "documents":
                docs = ', '.join(claim.get('documents_needed') or []) or 'No missing documents are listed.'
                parts.append(f"The recorded missing documents for claim {claim['case_id']} are: {docs}.")
            elif intent == "submission":
                if "submission_channels" in requested:
                    parts.append(f"The claim record does not specify whether email submission is accepted for claim {claim['case_id']}.")
                else:
                    parts.append(f"The claim record does not specify the upload channel for claim {claim['case_id']}.")
            elif intent == "format":
                guidance = self._guidance("default_guidance")
                parts.append(f"Approved document guidance says: {guidance}" if guidance else f"The claim record does not specify a required file format or signature rule for claim {claim['case_id']}. Please confirm the current document requirements with the insurer.")
            elif intent == "alternatives":
                guidance = self._guidance("document_alternative_guidance", "default")
                parts.append(f"Approved guidance says: {guidance}" if guidance else f"The claim record does not confirm whether a replacement document is acceptable for claim {claim['case_id']}. A claims representative should confirm an approved alternative before submission.")
            elif intent in {"denial", "denial_details"}:
                parts.append(f"The recorded denial reason for claim {claim['case_id']} is: {claim.get('denial_reason', 'not available in the record.')}" )
            elif intent == "payment":
                amount = claim.get('expected_reimbursement_amount', 'not available in the record.')
                explanation = " The record does not include a more detailed explanation for the amount." if "payment_explanation" in requested else ""
                parts.append(f"The recorded reimbursement for claim {claim['case_id']} is {amount}.{explanation}")
            elif intent == "appeal":
                deadline = claim.get('appeal_deadline')
                if deadline:
                    suffix = " This deadline has passed." if date.fromisoformat(deadline) < date.today() else ""
                    parts.append(f"The recorded appeal deadline for claim {claim['case_id']} is {deadline}.{suffix}")
                else:
                    parts.append(f"The claim record does not include an appeal deadline for claim {claim['case_id']}.")
            elif intent == "receipt":
                parts.append(f"The available record does not include an upload receipt for claim {claim['case_id']}, so I cannot confirm whether the documents were received.")
            elif intent == "case_summary":
                parts.extend([
                    f"Claim {claim['case_id']} is recorded as {claim.get('status', 'not available')}.",
                    f"Recorded denial reason: {claim.get('denial_reason', 'not available in the record.')}",
                    f"Recorded missing documents: {', '.join(claim.get('documents_needed') or []) or 'none listed'}.",
                    f"Recorded appeal deadline: {claim.get('appeal_deadline', 'not available in the record.')}.",
                ])
            elif intent == "next_steps":
                parts.append(f"Next steps for claim {claim['case_id']} are to provide any listed missing documents and contact a representative if you need to appeal or confirm submission requirements.")
            elif intent == "out_of_scope":
                parts.append("I can only assist with insurance claim-related questions.")
            elif intent == "unsupported_intent":
                parts.append("I can help with claim status, documents, submission, payment, denial reasons, appeals, or next steps. Which claim question would you like to ask?")
            elif intent == "appeal":
                parts.append(f"The recorded appeal deadline for claim {claim['case_id']} is {claim.get('appeal_deadline', 'not available in the record.')}")
                parts.append("The available record does not include the complete appeal submission procedure. Please use the insurer's documented appeal channel or contact a claims representative.")
            elif intent == "timing":
                parts.append(f"The available record does not include a processing-time guarantee for claim {claim['case_id']}.")
            elif intent == "term_explanation":
                definitions = {
                    "diagnosis report": "a report describing the diagnosis made by the treating provider",
                    "pathology report": "a clinical report describing findings from examined tissue or samples",
                    "office note": "a provider's clinical note documenting an office visit",
                }
                term = (parsed.requested_terms or [None])[0]
                definition = definitions.get(term.casefold()) if term else None
                guidance = self._guidance("document_guidance", term.casefold() if term else "") if term else None
                parts.append(f"A {term} is {definition}. Approved guidance also says: {guidance}" if definition and guidance else f"A {term} is {definition}." if definition else "I do not have an exact definition for that term. Could you clarify the term?")
            elif intent == "next_steps":
                parts.append("The next steps are to review the recorded documents, appeal information, and any available payment details.")
        state.retrieved_fields = retrieved
        state.answered_fields = answered
        state.unavailable_fields = unavailable
        state.needs_clarification_fields = list(dict.fromkeys(state.needs_clarification_fields))
        # Unavailable fields remain pending: they were explicitly addressed,
        # but not answered from the record. A later turn may supply a source,
        # correct the request, or ask for a representative.
        state.pending_requested_fields = [f for f in requested_fields if f not in answered]
        # requested_fields represents the active contract, not a transcript
        # of completed questions. Completed fields must not reappear on the
        # next unrelated turn.
        state.requested_fields = list(state.pending_requested_fields)
        state.answer_status = "answered" if not unavailable and not state.needs_clarification_fields else "unavailable" if unavailable else "needs_clarification"
        if not parts:
            parts.append(f"I can review the status, denial reason, documents, appeal, payment, or next steps for claim {claim['case_id']}.")
        if parsed.emotion_type != "neutral":
            parts.insert(0, "I understand this has been stressful. We can take it one step at a time.")
        if parsed.dialogue_act == "clarification":
            parts.append("Which part would you like me to clarify first?")
        return self._hide_claim_ids(self._naturalize("\n\n".join(parts), requested_fields, parsed))

    @staticmethod
    def _fields_for_intents(intents):
        mapping = {
            "status": ["claim_status"], "documents": ["missing_documents"],
            "submission": ["document_submission_channel"], "denial": ["denial_reason"],
            "denial_details": ["denial_reason"], "payment": ["payment_amount"],
            "appeal": ["appeal_deadline"], "receipt": ["document_receipt_status"],
            "timing": ["processing_time"], "next_steps": ["next_steps"],
            "case_summary": ["case_summary"],
        }
        result = []
        for intent in intents:
            for field in mapping.get(intent, []):
                if field not in result:
                    result.append(field)
        return result
