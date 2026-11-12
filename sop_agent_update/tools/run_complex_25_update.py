"""Run the copied 25-case complex corpus against the new architecture."""
import json
from pathlib import Path
from app.workflow import ConversationService

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "generated_complex_customers_25.jsonl"
REPORT = ROOT / "tests" / "latest_complex_25_update_conversations.txt"
EXPECTED_MAP = {
    "receipt_confirmation": "receipt", "missing_documents": "documents",
    "denial_question": "denial_details", "document_requirements": "format",
    "document_alternative": "alternatives", "appeal_question": "appeal",
    "payment_question": "payment", "case_summary": "case_summary",
    "next_steps": "next_steps", "processing_time": "timing",
    "submission_deadline": "timing", "status_inquiry": "status",
    "emotional_support": "emotional_support", "unclear": "unclear",
}


def main():
    rows = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    results, counts = [], {"verified": 0, "completed": 0, "intent_match": 0, "errors": 0}
    for row in rows:
        service = ConversationService()
        sid = f"complex-{row['id']}"
        turns = []
        error = None
        state = None
        for index, text in enumerate(row.get("turns", []), 1):
            try:
                reply, state = service.respond(sid, text)
                nlu = state.last_nlu
                turns.append({
                    "number": index, "user": text, "assistant": reply,
                    "phase": state.phase.value, "event": "", "verified": state.verified,
                    "intent": nlu.intent if nlu else None,
                    "secondary": nlu.secondary_intents if nlu else [],
                    "requested_field": nlu.requested_field if nlu else None,
                    "emotion": state.emotion, "handoff": state.handoff,
                })
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                counts["errors"] += 1
                break
        if state and state.verified:
            counts["verified"] += 1
        if state and state.conversation_complete:
            counts["completed"] += 1
        intents = [intent for turn in turns for intent in ([turn["intent"]] + turn["secondary"]) if intent]
        expected = EXPECTED_MAP.get(row.get("expected_intent"), row.get("expected_intent"))
        if expected in intents:
            counts["intent_match"] += 1
        results.append((row, turns, state, error))

    lines = [
        "NEW SOP_AGENT_UPDATE - COMPLEX 25 CONVERSATION REPORT",
        "=" * 72, "",
        f"Cases: {len(rows)}",
        f"Verified: {counts['verified']}",
        f"Normalized intent observed: {counts['intent_match']}/{len(rows)}",
        f"Conversation complete: {counts['completed']}",
        f"Runtime errors: {counts['errors']}", "",
    ]
    for row, turns, state, error in results:
        lines.extend(["-" * 72, f"CASE {row['id']} | {row.get('customer')} | {row.get('kind')}",
                      f"Expected intent: {row.get('expected_intent')}", ""])
        for turn in turns:
            lines.extend([
                f"TURN {turn['number']} | phase={turn['phase']} | verified={turn['verified']} | intent={turn['intent']} | secondary={turn['secondary']} | requested_field={turn['requested_field']} | emotion={turn['emotion']} | handoff={turn['handoff']}",
                f"USER: {turn['user']}", f"ASSISTANT: {turn['assistant']}", "",
            ])
        if error:
            lines.extend([f"ERROR: {error}", ""])
        if state:
            lines.extend([f"FINAL: phase={state.phase.value}, verified={state.verified}, case_id={state.case_id}, email_choice={state.email_choice}, handoff={state.handoff}", ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(REPORT)
    print(counts)


if __name__ == "__main__":
    main()
