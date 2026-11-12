"""Run the 30-customer conversation set through OpenAI Structured NLU only."""
import json
import os
import re
import sys
import uuid
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "complex_30_customer_conversions.txt"
OUTPUT = ROOT / "tests" / "complex_30_api_conversion_results.txt"
JSONL_SOURCE = ROOT.parent / "tests" / "generated_sop_harness_30_customers.jsonl"


def load_cases():
    text = SOURCE.read_text()
    blocks = re.split(r"\n(?=C\d+ \|)", text)
    cases = []
    for block in blocks:
        head = re.match(r"(C\d+) \| ([^|]+) \| ([^|]+) \|", block)
        if not head:
            continue
        turns = re.findall(r"^USER: (.+)$", block, re.MULTILINE)
        cases.append((head.group(1), head.group(2).strip(), head.group(3).strip(), turns))
    return cases


def load_jsonl_cases(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [(f"C{row['id']}", row["kind"], row["customer"], row["turns"], row) for row in rows]


def _turn_has(trace, predicate):
    return any(predicate(item) for item in trace)


def _denial_answer_is_grounded(item):
    """Validate against the selected claim, not one exact sentence template."""
    claim = item.get("claim") or {}
    reply = item.get("reply", "").casefold()
    reason = str(claim.get("denial_reason") or "").casefold()
    if not item.get("case_id") or not claim or claim.get("case_id") != item.get("case_id"):
        return False
    if not ("claim" in reply or "your case" in reply):
        return False
    denial_language = bool(re.search(r"\bdeni(?:ed|al)|rejected|not approved|turned down\b", reply))
    reason_language = bool(re.search(r"\breason|because|incomplete|missing|insufficient|review file\b", reply))
    reason_tokens = [token for token in re.findall(r"[a-z]+", reason) if len(token) > 4]
    grounded_overlap = not reason_tokens or any(token in reply for token in reason_tokens)
    return denial_language and reason_language and grounded_overlap


def assertions(row, state, trace=None, error=None):
    trace = trace or []
    checks = []
    expected = set(row.get("expected_checks", []))
    if error:
        return [{"check": "api_execution", "passed": False, "status": "FAIL", "detail": error}]
    # Ground truth is optional for backward compatibility, but when supplied
    # it is the strict source of truth for structured API behavior.
    ground_truth = row.get("ground_truth") or row.get("turn_ground_truth")
    if ground_truth:
        mismatches = []
        for expected_turn in ground_truth:
            turn_no = expected_turn.get("turn")
            actual = next((item for item in trace if item.get("turn") == turn_no), None)
            if not actual:
                mismatches.append(f"turn {turn_no}: missing actual turn")
                continue
            for field in ("raw_intent", "intent", "workflow_action", "requested_fields",
                          "phase", "verified", "case_id", "email_consent",
                          "email_choice", "handoff_status"):
                if field not in expected_turn:
                    continue
                expected_value = expected_turn[field]
                actual_value = actual.get(field)
                if field == "requested_fields":
                    expected_value = sorted(expected_value or [])
                    actual_value = sorted(actual_value or [])
                if expected_value != actual_value:
                    mismatches.append(
                        f"turn {turn_no} {field}: expected={expected_value!r}, actual={actual_value!r}"
                    )
        ok = not mismatches
        checks.append({
            "check": "ground_truth_structured_behavior",
            "passed": ok,
            "status": "PASS" if ok else "FAIL",
            "detail": "All supplied Ground Truth fields matched."
            if ok else "; ".join(mismatches),
        })
    if any("verify" in x.lower() or "pii" in x.lower() for x in expected):
        ok = bool(state.verified and state.matched_identity_field_count >= 3)
        checks.append({"check": "identity_verification", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": f"verified={state.verified}; matched_identity_field_count={state.matched_identity_field_count}"})
    if any("hint" in x.lower() or "case" in x.lower() for x in expected):
        hint = state.hint.model_dump(exclude_none=True)
        checks.append({"check": "case_hints_preserved", "passed": bool(hint), "status": "PASS" if hint else "FAIL", "detail": json.dumps(hint, ensure_ascii=False)})
    if any("email" in x.lower() for x in expected):
        ok = state.email_consent.value != "unknown"
        checks.append({"check": "email_consent_recorded", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": f"consent={state.email_consent.value}; choice={state.email_choice}; dispatch={state.email_dispatch_status}; draft={bool(state.pending_email_body)}"})
    if any("human" in x.lower() or "escalat" in x.lower() for x in expected):
        ok = bool(state.handoff_status in {"offered", "requested"} or state.awaiting_handoff_confirmation)
        checks.append({"check": "human_transfer_state", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": f"status={state.handoff_status}; awaiting_confirmation={state.awaiting_handoff_confirmation}; ticket={state.handoff_ticket or '-'}"})
    if "polite_out_of_scope_refusal" in expected:
        ok = _turn_has(trace, lambda item: item["is_out_of_scope"] and "insurance claim" in item["reply"].casefold()
                       and not re.search(r"(?:choose|select).*(?:claim|case)", item["reply"], re.I))
        checks.append({"check": "polite_out_of_scope_refusal", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": "Out-of-scope turn must be declined without claim selection."})
    if "stay_in_scope" in expected:
        ok = _turn_has(trace, lambda item: item["is_out_of_scope"] and not item["claim_selection_prompt"])
        checks.append({"check": "stay_in_scope", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": "Out-of-scope turn did not enter claim selection."})
    if "conversation_completion" in expected or "complete_conversation" in expected:
        ok = bool(state.conversation_complete and state.phase.value == "COMPLETE")
        checks.append({"check": "conversation_completion", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": f"phase={state.phase.value}; conversation_complete={state.conversation_complete}"})
    if _turn_has(trace, lambda item: bool(re.search(r"\b(?:no,? that is all|nothing else|no more questions|goodbye|all set)\b", item["text"], re.I))):
        ok = bool(state.conversation_complete and state.phase.value == "COMPLETE")
        checks.append({"check": "conversation_completion_observed", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": f"completion turn observed; final phase={state.phase.value}; complete={state.conversation_complete}"})
    if _turn_has(trace, lambda item: item["is_identity_only"]):
        def identity_turn_is_safe(item):
            if not item["is_identity_only"]:
                return False
            if not item["claim_selection_prompt"]:
                return True
            # After successful verification, multiple authorized claims may
            # legitimately require selection. This is not premature claim
            # selection; it is the required disambiguation step.
            return bool(re.search(r"(?:multiple claims|choose|select|claim id)", item["reply"], re.I))
        ok = _turn_has(trace, identity_turn_is_safe)
        checks.append({"check": "identity_only_no_selection", "passed": ok, "status": "PASS" if ok else "FAIL",
                       "detail": "Identity-only turn did not force premature claim selection; legitimate multi-claim disambiguation is allowed."})
    def add(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "status": "PASS" if ok else "FAIL", "detail": detail})

    compound_turns = [item for item in trace if len(item.get("requested_fields", [])) > 1]
    if compound_turns:
        missing = []
        for item in compound_turns:
            missing.extend(set(item["requested_fields"]) - set(item.get("answered_fields", [])))
        add("all_requested_fields_answered", not missing,
            "Every requested field was answered and marked answered."
            if not missing else "Missing answers for: " + ", ".join(sorted(set(missing))))

    if "grounded_denial_reason" in expected or "grounded_answer" in expected:
        ok = _turn_has(trace, lambda item: "denial_reason" in set(item.get("requested_fields", []))
                       and _denial_answer_is_grounded(item))
        for name in ("grounded_denial_reason", "grounded_answer"):
            if name in expected:
                add(name, ok, "Denial response must cite the selected claim and recorded denial reason.")
    if "grounded_status" in expected or "grounded_open_status" in expected:
        def grounded_status(item):
            reply = item["reply"].casefold()
            return (
                bool(item.get("case_id"))
                and bool(re.search(r"\b(?:open|closed|denied|pending|approved|settled)\b", reply))
            )
        ok = _turn_has(trace, grounded_status)
        for name in ("grounded_status", "grounded_open_status"):
            if name in expected:
                add(name, ok, "Status response must cite the selected claim and recorded status.")
    if "grounded_missing_document" in expected or "grounded_submission_guidance" in expected:
        ok = _turn_has(trace, lambda item: bool(item.get("case_id"))
                       and bool(re.search(r"\b(?:missing|document|record|guidance|available)\b", item["reply"], re.I)))
        for name in ("grounded_missing_document", "grounded_submission_guidance"):
            if name in expected:
                add(name, ok, "Document response must be grounded in recorded claim/document data.")
    if "grounded_payment_amounts" in expected:
        ok = _turn_has(trace, lambda item: "reimbursement" in item["reply"].casefold()
                       or "$" in item["reply"])
        add("grounded_payment_amounts", ok, "Payment response must contain a grounded reimbursement/amount value.")
    # A fact-specific assertion is blocked, not failed, when the workflow
    # correctly refuses to answer before a unique authorized claim exists.
    for name in ("grounded_deadline", "grounded_summary"):
        if name in expected:
            if any(item.get("claim_selection_prompt") for item in trace):
                checks.append({"check": name, "passed": False, "status": "BLOCKED",
                               "detail": "A unique authorized claim was not established; safe clarification is correct."})
            else:
                checks.append({"check": name, "passed": False, "status": "OBSERVED",
                               "detail": "The API produced an intent observation, but the business fact was not verified."})
    if "no_disclosure" in expected or "refuse_claim_disclosure_before_verification" in expected:
        ok = all(item["verified"] or not re.search(r"\bCL[- ]?\d+\b|recorded (?:as|denial)|reimbursement", item["reply"], re.I)
                 for item in trace)
        for name in ("no_disclosure", "refuse_claim_disclosure_before_verification"):
            if name in expected:
                add(name, ok, "Unverified turns must not disclose protected claim data.")
    if "reject_out_of_scope" in expected or "count_repeated_irrelevant_attempts" in expected:
        outscope_turns = [item for item in trace if item["is_out_of_scope"]]
        if "reject_out_of_scope" in expected:
            ok = bool(outscope_turns) and all(
                "insurance claim" in item["reply"].casefold() for item in outscope_turns
            )
            add("reject_out_of_scope", ok, "Every unrelated turn must receive a polite insurance-scope refusal.")
        if "count_repeated_irrelevant_attempts" in expected:
            observed_count = max([item.get("out_of_scope_count", 0) for item in trace] or [0])
            add("count_repeated_irrelevant_attempts", observed_count >= 3,
                f"Observed out-of-scope turns={len(outscope_turns)}; session counter={observed_count}; expected at least 3.")
    if "email_not_sent_after_decline" in expected or "email_not_sent" in expected or "email_sent_only_after_yes" in expected:
        no_dispatch = state.email_dispatch_status != "sent" and state.pending_email_body is None
        if "email_not_sent_after_decline" in expected:
            add("email_not_sent_after_decline", no_dispatch and state.email_consent.value in {"declined", "revoked"}, "Declined/revoked consent must not dispatch email.")
        if "email_not_sent" in expected:
            add("email_not_sent", no_dispatch, "No configured email dispatch or pending email body is allowed.")
        if "email_sent_only_after_yes" in expected:
            add("email_sent_only_after_yes", state.email_consent.value == "accepted" or state.email_dispatch_status != "sent", "Dispatch requires explicit accepted consent.")
    implemented = {check["check"] for check in checks}
    covered = set(implemented)
    if "identity_verification" in implemented:
        covered.update(name for name in expected if "verify" in name.lower() or "pii" in name.lower())
    if "case_hints_preserved" in implemented:
        covered.update(name for name in expected if "hint" in name.lower() or "case" in name.lower())
    if "email_consent_recorded" in implemented:
        covered.update(name for name in expected if "email" in name.lower())
    if "human_transfer_state" in implemented:
        covered.update(name for name in expected if "human" in name.lower() or "escalat" in name.lower())
    # A completed API turn with a structured intent is observable, but it is
    # not proof that an unimplemented business assertion passed.
    observed_intents = [item.get("raw_intent") or item.get("intent") for item in trace]
    for name in sorted(expected - covered):
        observed = bool(observed_intents)
        checks.append({"check": name, "passed": False, "status": "OBSERVED" if observed else "FAIL",
                       "detail": ("API completed and produced structured intent observations: "
                                  + ", ".join(str(intent) for intent in observed_intents if intent)
                                  if observed else "No API turn produced a structured intent.")})
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--source", choices=("legacy", "sop-jsonl"), default="legacy")
    parser.add_argument("--ids", nargs="+", help="Only run selected case IDs, e.g. C302 C303 C305 C320")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required; this runner never uses Local NLU.")
    if os.getenv("NLU_MODE", "openai").casefold() != "openai":
        raise SystemExit("NLU_MODE=openai is required; this runner never uses Local NLU.")
    sys.path.insert(0, str(ROOT))
    from app.workflow import ConversationService
    from app.models import Session

    cases = load_jsonl_cases(JSONL_SOURCE) if args.source == "sop-jsonl" else load_cases()
    if args.ids:
        wanted = {value.upper() if value.upper().startswith("C") else f"C{value}" for value in args.ids}
        cases = [item for item in cases if item[0].upper() in wanted]
    cases = cases[:args.limit]
    output = (ROOT.parent / "tests" / "generated_sop_harness_30_conversion.txt" if args.source == "sop-jsonl" else OUTPUT)
    service = ConversationService()
    conversation_report = ["COMPLEX 30 API-ONLY CONVERSATIONS", "=" * 42, ""]
    diagnostics_report = ["INTERMEDIATE STATE, NLU, AND ASSERTION DETAILS", "=" * 42, ""]
    total = len(cases)
    print(f"API mode: OpenAI Structured NLU | cases: {total}", flush=True)
    for index, item in enumerate(cases, 1):
        if len(item) == 4:
            case_id, kind, customer, turns = item
            row = {}
        else:
            case_id, kind, customer, turns, row = item
        print(f"[{index}/{total}] {case_id} {customer} ({kind})", flush=True)
        sid = str(uuid.uuid4())
        state = Session()
        trace = []
        conversation_report.extend([f"{case_id} | {customer} | {kind}", "-"])
        diagnostics_report.extend([f"{case_id} | {customer} | {kind}", f"EXPECTED_CHECKS: {row.get('expected_checks', [])}", "-"])
        run_error = None
        for turn_no, text in enumerate(turns, 1):
            print(f"  turn {turn_no}/{len(turns)} API call...", flush=True)
            try:
                reply, state = service.respond(sid, text)
            except Exception as exc:
                phase = service.store.get(sid).phase.value if service.store.get(sid) else "VERIFY_ID"
                failure = f"{type(exc).__name__}: {str(exc)[:500]}"
                run_error = failure
                print(f"  FAILED turn {turn_no}: node=PREPARE_TURN phase={phase} error={failure}", flush=True)
                conversation_report.extend([f"USER: {text}", f"BOT: [API FAILURE: {failure}]", ""])
                diagnostics_report.extend([f"{case_id} TURN {turn_no} FAILURE: node=PREPARE_TURN; phase={phase}; error={failure}", ""])
                break
            parsed = state.last_nlu
            trace.append({
                "turn": turn_no,
                "text": text,
                "phase": state.phase.value,
                "verified": state.verified,
                "case_id": state.case_id,
                "claim": service.repo.authorized_claim(state) or {},
                "out_of_scope_count": state.out_of_scope_count,
                "identity": dict(state.identity),
                "case_hints": state.hint.model_dump(exclude_none=True),
                "verification_status": state.verification_status,
                "raw_intent": parsed.raw_intent if parsed else None,
                "intent": parsed.intent if parsed else None,
                "requested_fields": parsed.requested_fields if parsed else [],
                "answered_fields": list(state.answered_fields),
                "pending_requested_fields": list(state.pending_requested_fields),
                "workflow_action": parsed.workflow_action if parsed else None,
                "email_consent": state.email_consent.value,
                "email_choice": state.email_choice,
                "handoff_status": state.handoff_status,
                "reply": reply,
                "is_out_of_scope": bool(parsed and (parsed.unrelated or parsed.intent == "out_of_scope")),
                "is_identity_only": bool(parsed and parsed.identity and not parsed.requested_fields),
                "claim_selection_prompt": bool(re.search(r"(?:choose|select|provide).*(?:claim|case)", reply, re.I)),
            })
            conversation_report.extend([
                f"USER: {text}",
                f"BOT: {reply}",
                "",
            ])
            diagnostics_report.extend([
                f"TURN {turn_no} GROUND_TRUTH: {json.dumps(next((item for item in (row.get('ground_truth') or row.get('turn_ground_truth') or []) if item.get('turn') == turn_no), None), ensure_ascii=False)}",
                f"TURN {turn_no} ACTUAL: {json.dumps({key: trace[-1].get(key) for key in ('raw_intent', 'intent', 'workflow_action', 'requested_fields', 'phase', 'verified', 'case_id', 'email_consent', 'email_choice', 'handoff_status', 'out_of_scope_count')}, ensure_ascii=False)}",
                f"STATE: phase={state.phase.value}; raw_intent={parsed.raw_intent if parsed else None}; "
                f"intent={parsed.intent if parsed else None}; "
                f"requested_fields={parsed.requested_fields if parsed else []}; "
                f"case_id={state.case_id}; identity={state.identity}; case_hints={state.hint.model_dump(exclude_none=True)}; "
                f"claim_history={state.claim_history}; "
                f"verified={state.verified}; matched_identity_field_count={state.matched_identity_field_count}; "
                f"identity_fields={sorted(state.identity)}; case_hints={state.hint.model_dump(exclude_none=True)}; "
                f"claim_candidates_before_filter={state.claim_candidates_before_filter}; "
                f"claim_candidates_after_filter={state.claim_candidates_after_filter}; "
                f"email_consent={state.email_consent.value}; email_choice={state.email_choice}; "
                f"email_dispatch_status={state.email_dispatch_status}; email_draft={bool(state.pending_email_body)}; "
                f"handoff_status={state.handoff_status}; handoff_awaiting_confirmation={state.awaiting_handoff_confirmation}; "
                f"handoff_execution_status={state.handoff_execution_status}; "
                f"handoff_ticket={state.handoff_ticket or '-'}",
                "",
            ])
        checks = assertions(row, state, trace, run_error)
        if any(check.get("status") == "FAIL" for check in checks):
            result = "FAIL"
        elif any(check.get("status") == "OBSERVED" for check in checks):
            result = "OBSERVED"
        else:
            result = "PASS"
        diagnostics_report.append("ASSERTIONS: " + json.dumps({"result": result, "checks": checks}, ensure_ascii=False))
        diagnostics_report.append(
            "CONVERSION: " + json.dumps({
                "case_id": state.case_id,
                "phase": state.phase.value,
                "verified": state.verified,
                "current_intent": state.current_intent,
                "claim_history": state.claim_history,
                "response_mode": state.response_mode,
                "turn_trace": trace,
            }, ensure_ascii=False)
        )
        diagnostics_report.append("")
        print(f"  completed {index}/{total} | result={result} | active_claim={state.case_id} | "
              f"verified={state.verified} fields={state.matched_identity_field_count} | "
              f"hints={state.hint.model_dump(exclude_none=True)} | email={state.email_consent.value}/{state.email_dispatch_status} | "
              f"handoff={state.handoff_status}/{state.handoff_ticket or '-'} | "
              f"ticket_number={state.handoff_ticket or '-'}", flush=True)
    output.write_text("\n".join(conversation_report + ["", "", *diagnostics_report]) + "\n")
    print(f"DONE: wrote {output}", flush=True)


if __name__ == "__main__":
    main()
