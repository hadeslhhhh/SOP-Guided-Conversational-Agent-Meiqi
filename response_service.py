"""Grounded response-generation service boundary."""


def answer_intent(state, classification, question):
    import app
    requested_field = classification.get("requested_field") or app.infer_requested_field(
        question, classification.get("intent")
    )
    if not requested_field and classification.get("relation") == "follow_up":
        requested_field = state.get("last_requested_field")
    state["last_requested_field"] = requested_field
    classification["requested_field"] = requested_field
    answer = app._answer_intent_content(state, classification, question)
    if intent_is_unclear_with_pending_detail(classification):
        state["awaiting_clarification"] = True
        state["clarification_attempts"] = 0
    level = state.get("emotion_level", "low")
    intent = classification.get("intent")
    if intent == "emotional_support":
        state["emotional_intent_streak"] = state.get("emotional_intent_streak", 0) + 1
        state["emotional_intent_count"] = state.get("emotional_intent_count", 0) + 1
        state.setdefault("handoff_conditions", {})["emotional_intents"] = state["emotional_intent_count"]
        if any(term in question.lower() for term in ("want to die", "kill myself", "end my life")):
            app.request_human_transfer(state, "The user expressed a potential self-harm concern.", "safety_concern")
            return "I’m really sorry you’re feeling this way. Your immediate safety matters, and I want to connect you with a human representative right away."
        if state["emotional_intent_count"] >= 3:
            app.request_human_transfer(state, "The user expressed emotional distress in three or more emotional intents.", "emotional_intents")
            return "I understand this has been very difficult, and we can take this one step at a time. Would you like me to connect you with a human claims representative?"
    else:
        state["emotional_intent_streak"] = 0
    if intent == "emotional_support":
        if level == "high":
            return "I can hear how stressful this feels, and we can take this one step at a time. Let’s start with the part of your claim you most need help understanding."
        if level == "medium":
            return "I understand that this situation is worrying. A helpful next step is to tell me whether you want to review the claim status, denial reason, missing documents, or next steps."
        return "I understand that this situation is worrying. You can ask me about the claim status, denial reason, missing documents, or next steps."
    if level == "high" and intent != "human_transfer":
        return f"I understand this is very important and frustrating. {answer}"
    if level == "medium":
        prefix = "I’m sorry this has been frustrating." if state.get("emotion") == "Frustrated" else "I understand your concern."
        return f"{prefix} {answer} I will help you with the next clear step."
    if level == "low" and state.get("emotion_explicit"):
        return f"I understand this is difficult. {answer}"
    return answer


def intent_is_unclear_with_pending_detail(classification):
    # Even a broad unclear response leaves an unanswered clarification task.
    # A short acknowledgment such as "yes" must continue that task rather
    # than be treated as approval of the previous business answer.
    return classification.get("intent") == "unclear"


def answer_stored_nlu_intents(state):
    import app
    nlu = state.get("last_nlu") or {}
    primary = nlu.get("primary_intent")
    if primary not in app.INTENT_CATEGORIES or primary == "unclear":
        return None
    intent_names = [primary]
    intent_names.extend(
        item for item in app.normalize_secondary_intents(nlu.get("secondary_intents"))
        if item in app.INTENT_CATEGORIES and item not in intent_names
    )
    question = state.get("_unified_nlu_text", state.get("user_need") or "")
    answers = []
    for intent_name in intent_names:
        classification = {
            "relation": "new_intent", "intent": intent_name, "confidence": 0.96,
            "parent_intent_index": None, "question": question,
            "requested_field": nlu.get("requested_field") if intent_name == primary else app.infer_requested_field(question, intent_name),
        }
        state["intents"].append({
            "relation": "new_intent", "intent": intent_name, "confidence": 0.96,
            "parent_intent_index": None, "question": question,
            "requested_field": classification["requested_field"], "followups": [], "answered": False,
        })
        state["current_intent_index"] = len(state["intents"]) - 1
        answer = answer_intent(state, classification, question)
        state["intents"][-1]["answer"] = answer
        app.mark_current_intent_answered(state)
        answers.append(answer)
    if not answers:
        return None
    state["gates"]["PROCESS_CASE"] = True
    state["stage_progress"]["PROCESS_CASE"] = 1.0
    state["awaiting_additional_question"] = True
    app.advance_phase(state, "PROCESS_CASE")
    return deduplicate_response_sections("\n\n".join(answers))


def deduplicate_response_sections(answer):
    import nlu
    return nlu.deduplicate_response_sections(answer)


__all__ = ["answer_intent", "answer_stored_nlu_intents", "deduplicate_response_sections"]
