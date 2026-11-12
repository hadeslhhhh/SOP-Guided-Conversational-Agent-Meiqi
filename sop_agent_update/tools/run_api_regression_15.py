"""Run 15 end-to-end conversations through OpenAI NLU and response generation."""
import json
import os
import sys
import time
import uuid
from pathlib import Path
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "generated_conversations_50.jsonl"
OUTPUT = ROOT / "tests" / "api_regression_15_results.txt"


def main():
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required; refusing to run without API")
    if os.getenv("NLU_MODE", "openai").casefold() != "openai":
        raise SystemExit("NLU_MODE=openai is required; local NLU is not used")

    sys.path.insert(0, str(ROOT))
    from app.workflow import ConversationService

    cases = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()][:15]
    service = ConversationService()
    report = ["API-ONLY END-TO-END REGRESSION: 15 CASES", "=" * 72, ""]
    passed = 0
    started = time.monotonic()

    progress = tqdm(
        enumerate(cases, 1),
        total=len(cases),
        desc="API regression",
        unit="case",
        dynamic_ncols=True,
    )
    for index, case in progress:
        session_id = f"api-regression-{uuid.uuid4()}"
        progress.set_postfix_str(f"{case['scenario']} | pass={passed} fail={index - 1 - passed}")
        report.append(f"CASE {index}: {case['scenario']} | expected={case.get('expected_intent')}")
        final_state = None
        case_ok = True
        for turn, text in enumerate(case["turns"], 1):
            try:
                reply, final_state = service.respond(session_id, text)
                report.extend([
                    f"TURN {turn} USER: {text}",
                    f"TURN {turn} BOT: {reply}",
                    f"TURN {turn} STATE: phase={final_state.phase.value}; verified={final_state.verified}; "
                    f"claim={final_state.case_id}; intent={final_state.current_intent}; "
                    f"answered={final_state.answered_fields}",
                ])
            except Exception as exc:
                case_ok = False
                report.append(f"TURN {turn} ERROR: {type(exc).__name__}: {exc}")
                break
        if case_ok:
            passed += 1
        report.append(f"RESULT: {'PASS' if case_ok else 'FAIL'}")
        report.append("-" * 72)
        progress.set_postfix_str(f"{case['scenario']} | pass={passed} fail={index - passed}")

    elapsed = time.monotonic() - started
    report.insert(2, f"RESULT: {passed}/{len(cases)} cases completed; elapsed={elapsed:.1f}s")
    OUTPUT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"API regression complete: {passed}/{len(cases)}; wrote {OUTPUT}")
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
