import app
import pytest

pytestmark = pytest.mark.api


def test_requested_fields_are_specific():
    cases = {
        "What do I owe?": "amount_owed",
        "How much will I receive?": "expected_reimbursement",
        "What format should the files be in?": "document_format",
        "Does each page need a signature?": "signature_requirement",
        "Did you receive my upload?": "upload_receipt_status",
        "When must I submit the documents?": "document_submission_deadline",
        "When is my appeal deadline?": "appeal_deadline",
        "Can I fax the documents?": "fax_submission_allowed",
    }
    for message, expected in cases.items():
        result = app.unified_nlu(message, app.blank_state())
        assert result["requested_field"] == expected, (message, result)


def test_case_hints_ignore_dob_claim_id_and_bare_may():
    assert app.validate_case_hints(
        "My DOB is 1985-03-15 and claim CL-2048; I may have missed it",
        {"month": 5, "year": 2048},
    ) == {"status": None, "case_type": None, "month": None, "year": None}


def test_case_hints_accept_explicit_claim_time():
    assert app.validate_case_hints(
        "The claim was filed in January 2025", {"month": 1, "year": 2025}
    ) == {"status": None, "case_type": None, "month": 1, "year": 2025}


def test_neutral_document_explanation_is_not_confused_emotion():
    emotion, level = app.detect_emotion("Can you explain the office note?")
    assert emotion == "Neutral / Calm"
    assert level == "low"


def test_indirect_worry_is_detected():
    emotion, level = app.detect_emotion("I cannot stop worrying about this claim.")
    assert emotion == "Anxious / Worried"
    assert level in {"medium", "high"}


def test_end_conversation_does_not_need_clarification():
    result = app.unified_nlu("No, that is all.", app.blank_state())
    assert result["dialogue_act"] == "end_conversation"
    assert result["needs_clarification"] is False


def test_raw_message_is_current_message():
    message = "What do I owe?"
    result = app.unified_nlu(message, app.blank_state())
    assert result["raw_message"] == message


def test_response_validation_rejects_related_deadline_field():
    state = app.blank_state()
    state["last_requested_field"] = "document_submission_deadline"
    answer = "The appeal deadline is March 18, 2026."
    checked = app.apply_response_guards(state, answer, app.load_json)
    assert "could not confirm" in checked.lower()


def _authorized_state():
    state = app.blank_state()
    state.update({
        "phase": "PROCESS_CASE",
        "case_access_authorized": True,
        "matched_case_id": "CL-2048",
        "verified_party_id": "P9",
        "emotion": "Neutral / Calm",
        "emotion_level": "low",
        "emotion_explicit": False,
        "gates": {
            "VERIFY_ID": True,
            "RESOLVE_INTENT": True,
            "PROCESS_CASE": True,
            "POST_PROCESS": False,
        },
    })
    return state


def test_answer_uses_requested_field_for_deadline():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "submission_deadline",
        "requested_field": "document_submission_deadline",
    }, "When must I submit the documents?")
    assert "could not confirm a document-submission deadline" in answer.lower()
    assert "deadline is for the appeal" in answer.lower()
    assert "2026-03-18" not in answer


def test_answer_does_not_substitute_upload_guidance_for_receipt_status():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "receipt_confirmation",
        "requested_field": "upload_receipt_status",
    }, "Did you receive my upload?")
    assert "could not confirm receipt" in answer.lower()
    assert "member portal" not in answer.lower()


def test_answer_uses_amount_owed_not_reimbursement():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "payment_question",
        "requested_field": "amount_owed",
    }, "What do I owe?")
    assert "amount owed" in answer.lower()
    assert "$1450.00" in answer
    assert "expected reimbursement" not in answer.lower()


def test_standalone_emotion_gets_empathy_without_generic_unclear_prompt():
    state = _authorized_state()
    state["emotion"] = "Frustrated"
    answer = app.answer_intent(state, {"intent": "unclear"}, "I cannot stop worrying about this.")
    assert "frustrating" in answer.lower() or "difficult" in answer.lower()
    assert "i’m not quite sure i understood" not in answer.lower()


def test_processing_time_does_not_duplicate_usually():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "processing_time",
        "requested_field": "processing_time",
    }, "How long will the review take?")
    assert "usually takes usually" not in answer.lower()


def test_case_63_document_deadline_never_uses_appeal_date():
    message = "When do I need to submit the required report?"
    nlu = app.unified_nlu(message, app.blank_state())
    assert nlu["primary_intent"] == "submission_deadline"
    assert nlu["requested_field"] == "document_submission_deadline"
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": nlu["primary_intent"],
        "requested_field": nlu["requested_field"],
    }, message)
    assert "could not confirm" in answer.lower()
    assert "2026-03-18" not in answer


def test_case_64_appeal_deadline_uses_appeal_date():
    message = "When is my appeal deadline?"
    nlu = app.unified_nlu(message, app.blank_state())
    assert nlu["requested_field"] == "appeal_deadline"
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "appeal_question",
        "requested_field": nlu["requested_field"],
    }, message)
    assert "appeal deadline" in answer.lower()
    assert "2026-03-18" in answer


def test_case_54_business_intent_keeps_emotional_secondary():
    message = "I am really frustrated. Why was my claim denied?"
    nlu = app.unified_nlu(message, app.blank_state())
    assert nlu["primary_intent"] == "denial_question"
    assert "emotional_support" in nlu["secondary_intents"]
    assert nlu["emotion"] == "Frustrated"
    assert nlu["emotion_level"] == "medium"
    assert nlu["emotion_explicit"] is True
    state = _authorized_state()
    state["emotion"] = nlu["emotion"]
    state["emotion_level"] = nlu["emotion_level"]
    state["emotion_explicit"] = nlu["emotion_explicit"]
    answer = app.answer_intent(state, {
        "intent": "denial_question",
        "requested_field": "denial_reason",
        "secondary_intents": ["emotional_support"],
    }, message)
    assert "sorry" in answer.lower()
    assert "denied" in answer.lower()


def test_neutral_business_question_has_no_emotional_secondary():
    nlu = app.unified_nlu("Can you explain the office note?", app.blank_state())
    assert nlu["primary_intent"] == "term_explanation"
    assert nlu["secondary_intents"] == []
    assert nlu["emotion"] == "Neutral / Calm"


def test_case_11_math_is_out_of_scope_after_case_authorization():
    state = _authorized_state()
    answer, _ = app.reply(state, "What is 17 times 24?")
    assert answer == "I'm sorry, but I can only assist with insurance claims-related questions."
    assert state["matched_case_id"] == "CL-2048"
    assert state["phase"] == "PROCESS_CASE"


def test_case_12_weather_is_out_of_scope_without_case_id_prompt():
    state = _authorized_state()
    answer, _ = app.reply(state, "What will the weather be tomorrow?")
    assert answer == "I'm sorry, but I can only assist with insurance claims-related questions."
    assert "case id" not in answer.lower()


def test_case_13_movie_recommendation_is_out_of_scope():
    state = _authorized_state()
    answer, _ = app.reply(state, "Can you recommend a movie for tonight?")
    assert answer == "I'm sorry, but I can only assist with insurance claims-related questions."
    assert state["case_access_authorized"] is True


def test_multi_intent_followup_appeal_ddl_is_answered_once():
    state = _authorized_state()
    first, _ = app.reply(state, "what is ddl and how to upload the files")
    second, _ = app.reply(state, "the appeal ddl")
    assert "2026-03-18" in second
    assert second.count("2026-03-18") == 1
    assert second.lower().count("appeal deadline") <= 1
    assert "member portal" not in second.lower()
    assert first


def test_expired_appeal_deadline_is_explicitly_marked():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "appeal_question",
        "requested_field": "appeal_deadline",
    }, "What is my appeal deadline?")
    assert "2026-03-18" in answer
    assert "has passed" in answer.lower()


def test_requested_field_is_inferred_when_classifier_omits_it():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "submission_deadline",
        "requested_field": None,
    }, "When must I submit the required report?")
    assert state["last_requested_field"] == "document_submission_deadline"
    assert "document-submission deadline" in answer.lower()
    assert "appeal deadline" not in answer.lower()
    assert "2026-03-18" not in answer


def test_term_explanation_is_definition_before_claim_relevance():
    state = _authorized_state()
    answer = app.answer_intent(state, {
        "intent": "term_explanation",
        "requested_field": None,
        "term": "original pathology report",
    }, "What does pathology report mean?")
    assert answer.startswith("A pathology report is")
    assert "document requested for review" in answer
    assert "patient name" not in answer


def test_correction_and_angry_followup_do_not_duplicate_answer():
    state = _authorized_state()
    first, _ = app.reply(state, "What is my appeal deadline?")
    second, _ = app.reply(state, "No, that is not what I asked!!!")
    assert first
    assert state["emotion"] in {"Frustrated", "Angry"}
    assert second.count("2026-03-18") <= 1


def test_switch_from_submission_deadline_to_appeal_deadline():
    state = _authorized_state()
    first, _ = app.reply(state, "When must I submit the documents?")
    second, _ = app.reply(state, "What is my appeal deadline?")
    assert "document-submission deadline" in first.lower()
    assert "2026-03-18" in second
    assert second.count("2026-03-18") == 1
    assert "document-submission deadline" not in second.lower()


def test_other_claim_inquiry_reopens_verified_claim_selection():
    state = _authorized_state()
    answer, _ = app.reply(state, "I want to check another claim")
    assert "multiple claims" in answer.lower()
    assert state["phase"] == "RESOLVE_INTENT"
    assert state["verified_party_id"] == "P9"
    assert state["case_access_authorized"] is False
    assert state["case_options"] == ["CL-2011", "CL-1899", "CL-2102"]

    answer, _ = app.reply(state, "CL-2011")
    assert "CL-2011" in answer
    assert state["matched_case_id"] == "CL-2011"
    assert state["case_access_authorized"] is True

    answer, _ = app.reply(state, "check status")
    assert "CL-2011" in answer
    assert "closed" in answer.lower()


def test_other_claim_selection_lists_only_other_claims():
    state = _authorized_state()
    answer, _ = app.reply(state, "I forgot my other claim options")
    assert "CL-2048" not in answer
    assert "CL-2011" in answer
    assert "CL-1899" in answer
    assert "CL-2102" in answer
    assert state["matched_case_id"] is None
    assert state["case_access_authorized"] is False


def test_yes_after_clarification_repeats_clarification_naturally():
    state = _authorized_state()
    first, _ = app.reply(state, "why their missing")
    second, _ = app.reply(state, "yes")
    assert "clarify" in first.lower() or "understood" in first.lower()
    assert "still need" in second.lower() or "which part" in second.lower() or "would you like" in second.lower()
    assert "do you have any other questions" not in second.lower()


def test_appeal_deadline_correction_is_answered_once():
    state = _authorized_state()
    app.reply(state, "Check my documents")
    answer, _ = app.reply(state, "no, I am asking the appeal ddl")
    assert "recorded appeal deadline" in answer.lower()
    assert answer.count("2026-03-18") == 1
    assert answer.count("Please include the missing supporting materials") == 1
    assert "document-submission deadline" not in answer.lower()


def test_cannot_remember_claim_id_shows_safe_other_claim_options():
    state = _authorized_state()
    app.reply(state, "Can I check my other claims?")
    answer, _ = app.reply(state, "I cannot remember")
    assert "No problem" in answer
    assert "CL-2011" in answer
    assert "CL-2048" not in answer
    assert state["case_access_authorized"] is False
