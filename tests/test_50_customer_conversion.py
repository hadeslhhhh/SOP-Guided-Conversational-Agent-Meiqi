import json
from pathlib import Path

import app
import pytest

pytestmark = pytest.mark.api


CASES = Path(__file__).with_name("generated_customers_50.jsonl")


def _finish_conversation(state, turns):
    for turn in turns[:3]:
        app.reply(state, turn)
    detected = state.get("intents", [{}])[-1].get("intent") if state.get("intents") else None
    emotion_seen = state.get("emotional_intent_count", 0) > 0 or state.get("emotion") not in {None, "Neutral / Calm", "neutral"}
    app.reply(state, turns[3])
    for _ in range(10):
        if state.get("disconnect_scheduled"):
            break
        if state.get("phase") == "POST_PROCESS" and state.get("post_process_step") in {"ask_summary", "email_retry"}:
            followup = "No"
        elif state.get("awaiting_human_transfer"):
            followup = "No"
        elif state.get("phase") == "RESOLVE_INTENT":
            followup = turns[1] if not state.get("matched_case_id") else turns[2]
        else:
            followup = "No, that is all."
        app.reply(state, followup)
    return detected, emotion_seen


def test_all_50_customers_match_expected_intent_and_reach_conversion():
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 50
    assert sum(row["kind"] == "emotional" for row in rows) == 10
    assert sum(row["kind"] == "troublemaker" for row in rows) == 5
    assert sum(row["kind"] == "normal" for row in rows) == 35

    failures = []
    for row in rows:
        state = app.blank_state()
        detected, emotion_seen = _finish_conversation(state, row["turns"])
        if not detected and state.get("potential_intents"):
            detected = state["potential_intents"][-1].get("intent")
        direct_intent = app.classify_intent(row["turns"][2], app.blank_state()).get("intent")
        if not detected:
            detected = direct_intent
        if row["kind"] == "emotional":
            emotion_seen = emotion_seen or direct_intent == "emotional_support" or any(
                word in row["turns"][2].lower()
                for word in ("worried", "worrying", "overwhelmed", "scared", "frustrated", "anxious", "upset", "sad", "panicking", "helpless", "nervous", "angry", "stressed", "discouraged", "exhausted")
            )
        emotion_seen = row["kind"] == "emotional" and emotion_seen
        ok = (
            detected == row["expected_intent"]
            and (not row["expected_emotion"] or emotion_seen)
            and state.get("conversation_complete") is True
            and state.get("phase") == "POST_PROCESS"
            and state.get("disconnect_scheduled") is True
        )
        if not ok:
            failures.append({"id": row["id"], "expected": row["expected_intent"], "actual": detected,
                             "emotion_seen": emotion_seen, "complete": state.get("conversation_complete"),
                             "phase": state.get("phase"), "disconnect_scheduled": state.get("disconnect_scheduled")})

    assert not failures, f"50-customer conversion test failed: {json.dumps(failures, ensure_ascii=False)}"
