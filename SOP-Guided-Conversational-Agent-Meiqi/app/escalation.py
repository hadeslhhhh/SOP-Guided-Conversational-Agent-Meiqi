"""Policy-only escalation evaluation, separate from NLU and business answers."""
import time


def _key(parsed, claim_id):
    field = ",".join(sorted(set(parsed.requested_fields or []) | set(parsed.requested_field or [])))
    return f"{parsed.intent or 'unknown'}|{field}|{claim_id or 'none'}"


def evaluate(state, parsed, answer_status, *, explicit_human=False, connection_failure_duration=None):
    """Evaluate exactly the five supported escalation triggers."""
    state.answer_status = answer_status
    if explicit_human:
        state.escalation_required = True
        state.escalation_reason = "explicit_human_request"
        state.handoff_status = "requested"
        return True
    if parsed.emotion_level == "high":
        state.escalation_required = True
        state.escalation_reason = "high_emotion"
    elif connection_failure_duration is not None and connection_failure_duration >= 300:
        state.escalation_required = True
        state.escalation_reason = "connection_failure_5_minutes"
    elif answer_status in {"unavailable", "unsupported"}:
        key = _key(parsed, state.case_id)
        existing = next((item for item in state.unresolved_requests if item.get("key") == key), None)
        if existing:
            existing["count"] = int(existing.get("count", 0)) + 1
        else:
            existing = {"key": key, "count": 1}
            state.unresolved_requests.append(existing)
        state.unresolved_intent_streak = int(existing["count"])
        if state.unresolved_intent_streak >= 3:
            state.escalation_required = True
            state.escalation_reason = "repeated_unresolved_intent"
    elif answer_status == "answered":
        # Only a verified, complete answer resolves the matching request.
        key = _key(parsed, state.case_id)
        state.unresolved_requests = [item for item in state.unresolved_requests if item.get("key") != key]
        state.unresolved_intent_streak = 0
        state.escalation_required = False
        state.escalation_reason = None
    if state.escalation_required and not state.escalation_offered:
        state.escalation_offered = True
        state.handoff_status = "offered" if state.handoff_status != "requested" else state.handoff_status
    return state.escalation_required


def connection_started(state):
    return state.connection_failure_since or time.monotonic()
