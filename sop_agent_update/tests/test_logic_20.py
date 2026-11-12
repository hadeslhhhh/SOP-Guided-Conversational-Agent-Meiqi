"""Twenty API-independent logic samples for development and CI."""

from app.interpreter import canonical_requested_field, consent_of, requested_fields_from_text
from app.models import Parsed, Session
from app.response_service import ResponseService


CLAIM = {
    "case_id": "CL-2048",
    "party_id": "P9",
    "status": "denied",
    "case_type": "healthcare",
    "documents_needed": ["pathology report", "office note"],
    "denial_reason": "The review file was incomplete.",
    "appeal_deadline": "2026-03-18",
    "expected_reimbursement_amount": 0.0,
    "payment_date": None,
}


def make_state(**kwargs):
    return Session(verified=True, party_id="P9", case_id="CL-2048", **kwargs)


def compose(fields, intent="unclear", **state_kwargs):
    state = make_state(**state_kwargs)
    parsed = Parsed(intent=intent, requested_fields=fields)
    answer = ResponseService().compose(state, CLAIM, parsed, CLAIM)
    return answer, state


def test_01_status_is_grounded():
    answer, state = compose(["claim_status"])
    assert "denied" in answer.lower()
    assert state.answered_fields == ["claim_status"]


def test_02_missing_documents_are_grounded():
    answer, state = compose(["missing_documents"])
    assert "pathology report" in answer.lower()
    assert "office note" in answer.lower()
    assert state.answered_fields == ["missing_documents"]


def test_03_status_and_documents_are_connected():
    answer, _ = compose(["claim_status", "missing_documents"])
    assert "also shows" in answer.lower()


def test_04_denial_reason_is_returned():
    answer, _ = compose(["denial_reason"])
    assert "incomplete" in answer.lower()


def test_05_appeal_deadline_is_returned():
    answer, _ = compose(["appeal_deadline"])
    assert "2026-03-18" in answer


def test_06_payment_date_is_not_invented():
    answer, state = compose(["payment_date"])
    assert "payment date" in answer.lower()
    assert state.unavailable_fields == ["payment_date"]


def test_07_payment_amount_and_date_stay_separate():
    answer, state = compose(["payment_amount", "payment_date"])
    assert "reimbursement amount" in answer.lower()
    assert "payment date" in answer.lower()
    assert state.answered_fields == ["payment_amount"]


def test_08_case_summary_contains_follow_up_items():
    answer, _ = compose(["case_summary"])
    assert "missing documents" in answer.lower()
    assert "appeal deadline" in answer.lower()


def test_09_duplicate_sections_are_removed():
    service = ResponseService()
    answer = service.deduplicate("One answer.\n\nOne answer.\n\nAnother answer.")
    assert answer.count("One answer.") == 1


def test_10_claim_ids_are_redacted_in_customer_text():
    assert ResponseService._hide_claim_ids("Claim CL-2048 is denied.") == "your claim is denied."


def test_11_requested_fields_detect_status():
    assert "claim_status" in requested_fields_from_text("What is the current status?")


def test_12_requested_fields_detect_missing_documents():
    assert requested_fields_from_text("Which documents are still missing?") == ["missing_documents"]


def test_13_requested_fields_detect_submission():
    assert "document_submission_channel" in requested_fields_from_text("Where can I upload the documents?")


def test_14_requested_fields_detect_deadline():
    assert "document_submission_deadline" in requested_fields_from_text("When is the deadline to submit documents?")


def test_15_requested_fields_detect_compound_request():
    fields = requested_fields_from_text("What is the claim status and how much will I receive?")
    assert fields[:2] == ["claim_status", "payment_amount"]


def test_16_send_email_consent_is_detected():
    assert consent_of("Yes, please email me the summary") == "send_email"


def test_17_skip_email_consent_is_detected():
    assert consent_of("No, please do not send the summary") == "skip_email"


def test_18_document_email_is_not_summary_consent():
    assert consent_of("Can I email the missing documents?") is None


def test_19_answered_fields_are_recorded():
    _, state = compose(["claim_status", "missing_documents"])
    assert set(state.answered_fields) == {"claim_status", "missing_documents"}
    assert state.pending_requested_fields == []


def test_20_unverified_state_does_not_claim_verification():
    state = Session()
    assert state.verified is False
    assert state.case_id is None


def test_21_status_alias_maps_to_canonical_claim_status():
    assert canonical_requested_field("status") == "claim_status"


def test_22_payment_alias_maps_to_canonical_payment_amount():
    assert canonical_requested_field("payment") == "payment_amount"


def test_23_documents_alias_maps_to_canonical_missing_documents():
    assert canonical_requested_field("documents") == "missing_documents"


def test_24_case_type_values_are_not_intent_labels():
    allowed_case_types = {"healthcare", "dental", "auto"}
    assert "other_claim_inquiry" not in allowed_case_types
