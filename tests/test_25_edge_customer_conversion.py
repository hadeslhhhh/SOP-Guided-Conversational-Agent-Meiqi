import json
from pathlib import Path

import app
import pytest

pytestmark = pytest.mark.api


CASES = Path(__file__).with_name("generated_edge_customers_25.jsonl")


def replay(row):
    state = app.blank_state()
    for turn in row["turns"][:3]:
        app.reply(state, turn)
    detected = state.get("intents", [{}])[-1].get("intent") if state.get("intents") else None
    if not detected and state.get("potential_intents"):
        detected = state["potential_intents"][-1].get("intent")
    app.reply(state, row["turns"][3])
    for _ in range(10):
        if state.get("disconnect_scheduled"):
            break
        if state.get("phase") == "POST_PROCESS" and state.get("post_process_step") in {"ask_summary", "email_retry"}:
            followup = "No"
        elif state.get("awaiting_human_transfer"):
            followup = "No"
        elif state.get("phase") == "RESOLVE_INTENT":
            followup = row["turns"][1] if not state.get("matched_case_id") else row["turns"][2]
        else:
            followup = "No, that is all."
        app.reply(state, followup)
    if not detected:
        detected = app.classify_intent(row["turns"][2], app.blank_state()).get("intent")
    return detected, state


def test_25_realistic_edge_customers_match_and_convert():
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 25
    failures = []
    for row in rows:
        detected, state = replay(row)
        ok = (
            detected == row["expected_intent"]
            and state.get("conversation_complete") is True
            and state.get("phase") == "POST_PROCESS"
            and state.get("disconnect_scheduled") is True
        )
        if not ok:
            failures.append({"id": row["id"], "scenario": row["scenario"], "expected": row["expected_intent"],
                             "actual": detected, "phase": state.get("phase"),
                             "complete": state.get("conversation_complete"),
                             "disconnect": state.get("disconnect_scheduled")})
    assert not failures, json.dumps(failures, ensure_ascii=False)
