"""Run the generated 30-customer SOP harness with OpenAI API only."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "tests" / "generated_sop_harness_30_customers.jsonl"
OUTPUT = PROJECT / "tests" / "generated_sop_harness_30_conversion.txt"


def main():
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required; refusing to run without API")
    if os.getenv("NLU_MODE", "openai").lower() != "openai":
        raise SystemExit("Set NLU_MODE=openai; local fallback is not allowed")
    # Import only the canonical implementation in this project directory.
    sys.path.insert(0, str(PROJECT))
    from app.workflow import ConversationService
    from app.models import Session

    rows = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    service = ConversationService()
    report = ["SOP HARNESS 30 CUSTOMER API-ONLY CONVERSION", "=" * 60, ""]
    started = time.monotonic()
    total = len(rows)
    print(f"API mode: OpenAI Structured NLU | customers: {total}", flush=True)

    for index, row in enumerate(rows, 1):
        sid = str(uuid.uuid4())
        state = Session()
        report += [f"CUSTOMER {row['id']} | {row['customer']} | {row['kind']}", f"EXPECTED: {row['expected_intent']}", "-"]
        print(f"[{index}/{total}] {row['id']} {row['kind']}", flush=True)
        for turn_no, text in enumerate(row["turns"], 1):
            print(f"  turn {turn_no}/{len(row['turns'])} API call...", flush=True)
            try:
                reply, state = service.respond(sid, text)
            except Exception as exc:
                phase = service.store.get(sid).phase.value if service.store.get(sid) else "VERIFY_ID"
                report += [f"USER: {text}", f"FAILURE: phase={phase}; error={type(exc).__name__}: {exc}", ""]
                print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)
                break
            report += [f"USER: {text}", f"BOT: {reply}", f"STATE: phase={state.phase.value}; verified={state.verified}; case_id={state.case_id}; intent={state.current_intent}", ""]
        report += ["CONVERSION: " + json.dumps({"case_id": state.case_id, "phase": state.phase.value, "verified": state.verified, "intent": state.current_intent}, ensure_ascii=False), "", "=" * 60, ""]
        elapsed = time.monotonic() - started
        average = elapsed / index
        remaining = average * (total - index)
        print(f"  completed {index}/{total} | elapsed {elapsed:.1f}s | ETA {remaining:.1f}s | phase={state.phase.value}", flush=True)

    OUTPUT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"DONE: wrote {OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
