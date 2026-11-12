import app
import pytest

pytestmark = pytest.mark.api


def test_sample_message_verifies_resolves_and_reuses_intent():
    state = app.blank_state()
    answer, _ = app.reply(state, "I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.")
    assert state["gates"]["VERIFY_ID"] is True
    assert len(state["verified_fields"]) == 3
    assert state["matched_case_id"] == "CL-2048"
    assert state["phase"] == "PROCESS_CASE"
    assert "denied" in answer.lower()


def test_policy_number_alone_does_not_count_as_pii():
    state = app.blank_state()
    app.reply(state, "My name is Margaret Chen, DOB 1985-03-15, policy POL-9921.")
    assert state["gates"]["VERIFY_ID"] is False


def test_summary_contains_outcome_and_next_steps():
    state = app.blank_state()
    state["case"] = {"case_id": "CL-2048", "status": "denied", "case_type": "healthcare", "documents_needed": ["pathology report"]}
    summary = app.build_conversion_summary({"state": state, "intents": []})
    assert "Status/outcome: denied" in summary
    assert "pathology report" in summary


def test_handoff_ticket_is_created_once_and_reused():
    state = app.blank_state()

    first_ticket, first_message = app.get_handoff_ticket_response(state, "Customer requested a representative.")
    second_ticket, second_message = app.get_handoff_ticket_response(state, "Customer confirmed the transfer.")

    assert second_ticket is first_ticket
    assert second_ticket["ticket_id"] == first_ticket["ticket_id"]
    assert "has been created" in first_message
    assert "already been created" in second_message
    assert second_ticket["reason"] == "Customer requested a representative."


def test_unclear_questions_are_not_in_case_summary():
    state = app.blank_state()
    state["intents"] = [{
        "intent": "unclear",
        "question": "Can you tell me a joke about cooking?",
        "answer": "I can only help with insurance claims.",
        "followups": [],
    }]

    summary = app.build_conversion_summary({"state": state, "intents": state["intents"]})

    assert "cooking" not in summary
    assert app.has_claim_questions(state) is False


def test_emotional_only_message_is_recorded_as_unclear_primary_intent():
    state = app.blank_state()

    app.record_intent(state, "I am worried about this claim")

    assert state["intents"][-1]["intent"] == "unclear"
