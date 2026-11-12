import json
import os
from pathlib import Path
import pytest

import app

pytestmark = pytest.mark.api

os.environ.setdefault("ENABLE_NLU_CACHE", "true")

CASES = Path(__file__).with_name("generated_complex_customers_50.jsonl")


def test_50_complex_customer_scenarios_convert():
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("API integration test requires OPENAI_API_KEY")
    rows = [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 50
    assert {row["kind"] for row in rows} == {"multi_question", "emotional", "unrelated", "multiple_case", "human_transfer"}
    failures = []
    for row in rows:
        state = app.blank_state()
        for turn in row["turns"]:
            app.reply(state, turn)
            if state.get("disconnect_scheduled"):
                break
        for _ in range(12):
            if state.get("disconnect_scheduled"):
                break
            app.reply(state, "Yes, please transfer me to a human representative." if state.get("awaiting_human_transfer") else "No")
        ok = state.get("conversation_complete") is True and state.get("phase") == "POST_PROCESS" and state.get("disconnect_scheduled") is True
        if not ok:
            failures.append({"id": row["id"], "kind": row["kind"], "phase": state.get("phase"), "complete": state.get("conversation_complete"), "disconnect": state.get("disconnect_scheduled")})
    assert not failures, json.dumps(failures, ensure_ascii=False)
