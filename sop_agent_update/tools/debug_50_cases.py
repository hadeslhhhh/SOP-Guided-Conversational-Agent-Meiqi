import json
from pathlib import Path

import app


ROOT = Path(__file__).parent
CASES = ROOT / "tests" / "generated_cases_50.jsonl"
REPORT = ROOT / "tests" / "generated_cases_50_report.md"


def detect_intent(state):
    if state.get("intents"):
        return state["intents"][-1].get("intent")
    if state.get("potential_intents"):
        return state["potential_intents"][-1].get("intent")
    return "未识别"


def diagnose(row, state, actual_intent, answer):
    expected_verify = "VERIFY_ID" in row.get("expected_checks", [])
    verified = state["gates"]["VERIFY_ID"]
    if expected_verify and not verified:
        return "需要补充身份信息（至少三项匹配资料）"
    if not expected_verify and verified:
        return "身份核验结果与预期不一致"
    if state["phase"] == "RESOLVE_INTENT":
        if actual_intent == row.get("expected_intent"):
            return "意图已识别，但还需要案件编号才能继续处理"
        return "系统已核验身份，但还需要案件编号或更明确的案件描述"
    if actual_intent == row.get("expected_intent"):
        return "通过"
    return "意图识别与预期不一致，需要检查分类规则"


def main():
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = [
        "# 50 条顾客对话 Debug 报告",
        "",
        "这份报告自动运行每条测试输入，并用简单中文说明结果。",
        "",
    ]
    counts = {"通过": 0, "需要补充案件信息": 0, "其他问题": 0}
    for row in rows:
        state = app.blank_state()
        answer, _ = app.reply(state, row["message"])
        actual_intent = detect_intent(state)
        diagnosis = diagnose(row, state, actual_intent, answer)
        if diagnosis == "通过":
            counts["通过"] += 1
        elif "案件编号" in diagnosis:
            counts["需要补充案件信息"] += 1
        else:
            counts["其他问题"] += 1
        report.extend([
            f"## 顾客 {row['id']}：{row['scenario']}",
            "",
            f"- 顾客说：{row['message']}",
            f"- 预期意图：`{row['expected_intent']}`",
            f"- 实际意图：`{actual_intent}`",
            f"- 当前阶段：`{state['phase']}`",
            f"- 身份核验：{'通过' if state['gates']['VERIFY_ID'] else '未通过'}",
            f"- 系统回复：{answer.replace(chr(10), ' ')}",
            f"- Debug 结论：**{diagnosis}**",
            "",
        ])
    report[3:3] = [
        f"汇总：通过 {counts['通过']} 条；需要补充案件信息 {counts['需要补充案件信息']} 条；其他问题 {counts['其他问题']} 条。",
        "",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")
    print(f"已生成报告：{REPORT}")
    print(f"通过 {counts['通过']} 条；需要补充案件信息 {counts['需要补充案件信息']} 条；其他问题 {counts['其他问题']} 条。")


if __name__ == "__main__":
    main()
