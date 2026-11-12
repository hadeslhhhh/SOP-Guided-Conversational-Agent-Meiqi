"""Regenerate readable conversation reports from the current application code."""
import json
import os
import time
from pathlib import Path

import app

ROOT = Path(__file__).parent
SETS = {
    "latest_75_customer_conversations": [ROOT / "tests/generated_customers_50.jsonl", ROOT / "tests/generated_edge_customers_25.jsonl"],
    "latest_new_75_customer_conversations": [ROOT / "tests/generated_new_customers_75.jsonl"],
    "latest_complex_50_customer_conversations": [ROOT / "tests/generated_complex_customers_50.jsonl"],
    "latest_complex_25_customer_conversations": [ROOT / "tests/generated_complex_customers_25.jsonl"],
}


def main():
    selected = os.getenv("REPORT_SET")
    report_sets = {stem: files for stem, files in SETS.items() if not selected or (selected == "complex" and stem == "latest_complex_50_customer_conversations") or (selected == "complex25" and stem == "latest_complex_25_customer_conversations") or (selected == "new" and stem == "latest_new_75_customer_conversations") or (selected == "original" and stem == "latest_75_customer_conversations")}
    for stem, files in report_sets.items():
        api_only = stem in {"latest_complex_50_customer_conversations", "latest_complex_25_customer_conversations"}
        if api_only and os.getenv("USE_LOCAL_FALLBACK", "false").lower() == "true":
            raise RuntimeError("Complex customer report is API-only. Run with USE_LOCAL_FALLBACK=false.")
        rows = []
        for file in files:
            rows += [json.loads(x) for x in file.read_text(encoding="utf-8").splitlines() if x.strip()]
        saved, text = [], []
        total = len(rows)
        started = time.monotonic()
        for index, row in enumerate(rows, 1):
            state = app.blank_state()
            messages = []

            def send(message):
                answer, _ = app.reply(state, message)
                messages.append({"customer": message, "assistant": answer, "nlu": dict(state.get("last_nlu") or {})})

            for message in row["turns"]:
                send(message)
                if state.get("disconnect_scheduled"):
                    break
            for _ in range(12):
                if state.get("disconnect_scheduled"):
                    break
                followup = "Yes, please transfer me to a human representative." if state.get("awaiting_human_transfer") else "No"
                send(followup)
            item = {"id": row["id"], "customer": row["customer"], "kind": row["kind"], "expected_intent": row["expected_intent"], "messages": messages,
                    "conversion": {"case_id": state.get("matched_case_id"), "phase": state.get("phase"), "conversation_complete": state.get("conversation_complete"), "disconnect_scheduled": state.get("disconnect_scheduled")}}
            saved.append(item)
            elapsed = time.monotonic() - started
            average = elapsed / index
            remaining = max(0.0, average * (total - index))
            print(
                f"[{index:02d}/{total}] {index / total:6.1%} | "
                f"已用 {elapsed:6.1f}s | 平均 {average:5.1f}s/条 | "
                f"预计剩余 {remaining:6.1f}s | {row['kind']} #{row['id']}",
                flush=True,
            )
            mode = "API" if messages and messages[0]["nlu"].get("api_mode") else "LOCAL"
            text += ["=" * 88, f"顾客 {row['id']} | 类型：{row['kind']}", f"运行模式：{mode}", f"预期意图：{row['expected_intent']}", "=" * 88]
            for index, message in enumerate(messages, 1):
                text += ["", f"【第 {index} 轮】", "顾客：", message["customer"], "", "系统：", message["assistant"], "", "NLU：", json.dumps(message["nlu"], ensure_ascii=False, indent=2)]
            text += ["", "Conversion 结果：", f"  案件：{state.get('matched_case_id') or '-'}", f"  阶段：{state.get('phase')}", f"  完成：{'是' if state.get('conversation_complete') else '否'}", f"  已安排断开：{'是' if state.get('disconnect_scheduled') else '否'}", ""]
        (ROOT / f"tests/{stem}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in saved) + "\n", encoding="utf-8")
        (ROOT / f"tests/{stem}.txt").write_text("\n".join(text), encoding="utf-8")
        total_elapsed = time.monotonic() - started
        print(f"完成 {stem}: {len(saved)} conversations，总耗时 {total_elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
