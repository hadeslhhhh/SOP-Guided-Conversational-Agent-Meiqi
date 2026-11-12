import json
from pathlib import Path

import app
import pytest

pytestmark = pytest.mark.api

CASES = Path(__file__).with_name("generated_new_customers_75.jsonl")


def replay(row):
    state = app.blank_state()
    detected = None
    for index, turn in enumerate(row["turns"]):
        app.reply(state, turn)
        if index == 2:
            detected = state.get("intents", [{}])[-1].get("intent") if state.get("intents") else None
            if not detected and state.get("potential_intents"):
                detected = state["potential_intents"][-1].get("intent")
        if state.get("disconnect_scheduled"):
            break
    for _ in range(12):
        if state.get("disconnect_scheduled"):
            break
        if state.get("awaiting_human_transfer"):
            followup = "Yes, please transfer me to a human representative."
        elif state.get("phase") == "POST_PROCESS" and state.get("post_process_step") in {"ask_summary", "email_retry"}:
            followup = "No"
        else:
            followup = "No, that is all."
        app.reply(state, followup)
    if not detected:
        detected = app.classify_intent(row["turns"][2], app.blank_state()).get("intent")
    return detected, state


def test_new_75_realistic_scenarios_match_and_convert():
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 75
    assert {row["kind"] for row in rows} == {"unrelated", "followup", "upset", "polite", "human_transfer"}
    failures = []
    for row in rows:
        detected, state = replay(row)
        if row["kind"] == "unrelated":
            intent_ok = detected == "unclear"
        elif row["kind"] == "followup":
            intent_ok = detected == row["expected_intent"]
        elif row["kind"] == "human_transfer":
            intent_ok = detected in {"human_transfer", "unclear"}
        else:
            intent_ok = detected in {"status_inquiry", "denial_question", "missing_documents", "document_submission", "document_requirements", "document_alternative", "submission_deadline", "processing_time", "receipt_confirmation", "appeal_question", "payment_question", "case_summary", "next_steps", "term_explanation", "emotional_support", "unclear"}
        ok = intent_ok and state.get("conversation_complete") is True and state.get("phase") == "POST_PROCESS" and state.get("disconnect_scheduled") is True
        if not ok:
            failures.append({"id": row["id"], "kind": row["kind"], "expected": row["expected_intent"], "actual": detected,
                             "phase": state.get("phase"), "complete": state.get("conversation_complete"), "disconnect": state.get("disconnect_scheduled")})
    assert not failures, json.dumps(failures, ensure_ascii=False)
