"""NLU service boundary and dependency-light semantic helpers."""

import json
import os
import re
from difflib import SequenceMatcher


class NLUDependencies:
    """Runtime dependencies supplied by the application without a module cycle."""

    def __init__(self, implementation):
        self.implementation = implementation

    def run_unified(self, text, state=None):
        if os.getenv("ENABLE_NLU_CACHE", "false").lower() == "true":
            active = ((state or {}).get("intents") or [{}])[-1].get("intent")
            key = (text.strip(), (state or {}).get("phase"), (state or {}).get("matched_case_id"), active, bool((state or {}).get("awaiting_human_transfer")))
            if key in _UNIFIED_API_CACHE:
                return json.loads(json.dumps(_UNIFIED_API_CACHE[key], ensure_ascii=False))
            result = self.implementation("unified", text, state)
            if not result.get("api_error"):
                _UNIFIED_API_CACHE[key] = result
            return result
        return self.implementation("unified", text, state)

    def run_classification(self, text, state=None):
        return self.implementation("classification", text, state)


_DEPENDENCIES = None
_UNIFIED_API_CACHE = {}


def configure_dependencies(implementation):
    """Configure the temporary compatibility adapter used during migration."""
    global _DEPENDENCIES
    _DEPENDENCIES = NLUDependencies(implementation)


def local_extract(text, policyholders=None):
    """Extract identity and case hints without requiring an LLM."""
    found = {}
    normalized_text = re.sub(r"[^a-z0-9]", "", text.lower())
    for person in policyholders or []:
        names = [person.get("name"), *person.get("name_aliases", []), *person.get("full_name_aliases", [])]
        for name in names:
            normalized_name = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
            if normalized_name and normalized_name in normalized_text:
                found["full_name"] = person.get("name")
                break
    dob = re.search(r"\b(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}\b", text)
    if dob:
        found["dob"] = dob.group(0).replace("/", "-")
    ssn = re.search(r"(?:ssn|social(?: security)?|national id|government id|government identification|id last four|last four)[^0-9]{0,40}(\d{4})", text, re.I)
    if ssn:
        found["ssn_last4"] = ssn.group(1)
    phone = re.search(r"(?<!\d)(?:\+?1[\s().-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)", text)
    if phone:
        found["phone"] = re.sub(r"[^0-9+]", "", phone.group(0))
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    if email:
        found["email"] = email.group(0).lower()
    policy = re.search(r"\bPOL[- ]?\d+\b", text, re.I)
    if policy:
        found["policy_number"] = policy.group(0).upper().replace(" ", "-")
    if any(word in text.lower() for word in ("claim", "denied", "healthcare", "medical")):
        found["case_hint"] = text.strip()
    return found


def model_extract(text, policyholders=None, model_runner=None):
    """Extract identity fields with an optional model runner and safe fallback."""
    local_fields = local_extract(text, policyholders)
    if model_runner is None:
        return local_fields


def extract_case_nlu(text):
    """Extract only explicit, validated case clues for secure matching."""
    lowered = text.lower()
    if not (any(word in lowered for word in ("claim", "case", "denied", "denial", "rejected", "closed", "open", "medical", "healthcare", "dental", "dentist", "hospital", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "jan", "janerary", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")) or re.search(r"\b(?:19|20)\d{2}\b", lowered)):
        return {"status": None, "case_type": None, "month": None, "year": None}
    hints = {"status": None, "case_type": None, "month": None, "year": None}
    for status, aliases in {"denied": ("denied", "denial", "rejected"), "closed": ("closed", "settled", "resolved"), "open": ("open", "active", "in progress", "processing")}.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            hints["status"] = status
            break
    for case_type, aliases in {"healthcare": ("healthcare", "health care", "medical", "hospital"), "dental": ("dental", "dentist", "teeth")}.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            hints["case_type"] = case_type
            break
    months = {name: index for index, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
    aliases = {"jan": 1, "janerary": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
    for name, number in months.items():
        if re.search(rf"\b{name}\b", lowered):
            hints["month"] = number
            break
    if hints["month"] is None:
        for alias, number in aliases.items():
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                hints["month"] = number
                break
    if hints["month"] is None:
        for token in re.findall(r"[a-z]+", lowered):
            closest = max(months, key=lambda month: SequenceMatcher(None, token, month).ratio())
            if len(token) >= 4 and SequenceMatcher(None, token, closest).ratio() >= 0.72:
                hints["month"] = months[closest]
                break
    numeric = re.search(r"\b(0?[1-9]|1[0-2])\s*/\s*(?:19|20)\d{2}\b", lowered)
    reverse = re.search(r"\b((?:19|20)\d{2})\s*/\s*(0?[1-9]|1[0-2])\b", lowered)
    if numeric:
        hints["month"] = int(numeric.group(1))
    if reverse:
        hints["year"], hints["month"] = int(reverse.group(1)), int(reverse.group(2))
    year = re.search(r"\b(?:19|20)\d{2}\b", lowered)
    if year:
        hints["year"] = int(year.group(0))
    return hints


def local_semantic_interpretation(text):
    """Deterministic semantic fallback used when the API is unavailable."""
    lowered = re.sub(r"\s+", " ", text.lower()).strip()
    emotion, emotion_level = detect_emotion(text)
    semantic = {
        "meaning": text.strip(),
        "domain": "insurance_claim" if any(word in lowered for word in ("claim", "case", "insurance", "document", "deductible", "reimbursement", "denied", "appeal", "payment", "policy")) else "unknown",
        "action": None, "object": None, "context": {},
        "emotion": emotion, "emotion_level": emotion_level,
        "normalized_request": None, "primary_intent": None, "secondary_intents": [],
    }
    patterns = {
        "other_claim": ("another claim", "different claim", "other claim", "switch claims", "look at my other claim", "check a different claim", "forgot my other claim", "show my other claims", "list my other claims", "what other claims"),
        "check": ("status", "where does my claim stand", "is it open", "is it closed"),
        "explain": ("why", "explain", "what does", "what is", "meaning"),
        "identify_missing": ("missing", "still need", "did i miss", "required"),
        "submit": ("submit", "upload", "send", "fax", "mail"),
        "confirm_receipt": ("did you receive", "did you get", "received", "confirm receipt"),
        "ask_deadline": ("deadline", "when should", "when do i need", "when is it due", "ddl"),
        "ask_timing": ("how long", "how soon", "when will it be processed"),
        "ask_payment": ("how much", "reimbursement", "payment", "pay", "owe"),
        "appeal": ("appeal", "challenge the decision"),
        "summarize": ("summarize", "summary", "overview", "what happened with this claim"),
        "next_step": ("what should i do next", "next steps", "what do i do now"),
        "request_human": ("human", "representative", "speak with someone"),
        "review_documents": ("check the documents", "check my documents", "review the documents", "review my documents"),
    }
    priority = ("confirm_receipt", "ask_deadline", "ask_timing", "ask_payment", "appeal", "check", "summarize", "next_step", "request_human", "review_documents", "explain", "submit", "identify_missing")
    if any(phrase in lowered for phrase in patterns["other_claim"]):
        semantic["action"] = "other_claim"
        semantic["primary_intent"] = "other_claim_inquiry"
        semantic["object"] = "another_claim"
        semantic["normalized_request"] = {"action": "other_claim", "object": "another_claim", "domain": semantic["domain"]}
        return semantic
    for action in priority:
        if any(phrase in lowered for phrase in patterns[action]):
            semantic["action"] = action
            break
    action_to_intent = {
        "check": "status_inquiry", "explain": "term_explanation", "identify_missing": "missing_documents",
        "submit": "document_submission", "confirm_receipt": "receipt_confirmation", "ask_deadline": "submission_deadline",
        "ask_timing": "processing_time", "ask_payment": "payment_question", "appeal": "appeal_question",
        "summarize": "case_summary", "next_step": "next_steps", "request_human": "human_transfer",
        "review_documents": "document_requirements", "other_claim": "other_claim_inquiry",
    }
    semantic["primary_intent"] = action_to_intent.get(semantic["action"])
    if semantic["action"] in {"submit", "identify_missing", "review_documents"}:
        semantic["object"] = "claim_documents"
    elif semantic["action"] == "confirm_receipt":
        semantic["object"] = "submitted_documents"
    elif semantic["action"] == "ask_payment":
        semantic["object"] = "claim_payment"
    elif semantic["action"] == "check":
        semantic["object"] = "claim_status"
    if emotion != "Neutral / Calm" and semantic["primary_intent"]:
        semantic["secondary_intents"] = ["emotional_support"]
    if "pathology report" in lowered:
        semantic["object"] = "pathology_report"
    elif "office note" in lowered:
        semantic["object"] = "office_note"
    case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
    if case_ids:
        semantic["context"]["case_id"] = case_ids[-1].upper().replace(" ", "-")
    semantic["normalized_request"] = {"action": semantic["action"], "object": semantic["object"], "domain": semantic["domain"]}
    return semantic


def normalize_api_semantic_result(result, text, intent_categories):
    """Validate and normalize one API semantic-NLU response."""
    if not isinstance(result, dict):
        return None
    allowed_actions = {"check", "explain", "identify_missing", "submit", "confirm_receipt", "ask_deadline", "ask_timing", "ask_payment", "appeal", "summarize", "next_step", "request_human", "emotional_support", "review_documents", "unclear"}
    action = result.get("action")
    if action not in allowed_actions:
        return None
    result["meaning"] = result.get("meaning") or text.strip()
    result["context"] = result.get("context") if isinstance(result.get("context"), dict) else {}
    result["normalized_request"] = result.get("normalized_request") or {
        "action": action, "object": result.get("object"), "domain": result.get("domain", "unknown")
    }
    result["secondary_actions"] = result.get("secondary_actions") if isinstance(result.get("secondary_actions"), list) else []
    result["primary_intent"] = result.get("primary_intent") if result.get("primary_intent") in intent_categories else None
    result["secondary_intents"] = [item for item in (result.get("secondary_intents") or []) if item in intent_categories and item != result["primary_intent"]]
    return result


def api_semantic_interpretation(text, state, model_runner, intent_categories):
    """Run an injected API semantic-NLU call and validate its contract."""
    try:
        raw = model_runner(text, state)
        result = json.loads(raw) if isinstance(raw, str) else raw
        return normalize_api_semantic_result(result, text, intent_categories)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None


def interpret_user_message(text, state=None):
    """Run semantic interpretation through the NLU service boundary."""
    import app
    return app._interpret_user_message_impl(text, state)
    try:
        model_fields = model_runner(text)
        if not isinstance(model_fields, dict):
            return local_fields
        merged = {key: value for key, value in model_fields.items() if value}
        for key, value in local_fields.items():
            merged.setdefault(key, value)
        return merged
    except (ValueError, TypeError, json.JSONDecodeError):
        return local_fields


def detect_emotion(text):
    groups = {
        "Neutral / Calm": ("check my claim status", "claim status", "status update", "just checking", "update", "information", "question", "calm", "no rush", "general question", "i'd like to know", "i would like to know"),
        "Confused": ("confused", "don't understand", "unclear", "i don't understand", "i do not understand", "i'm confused", "what do you mean", "i don't follow", "not sure how", "which one", "doesn't make sense"),
        "Anxious / Worried": ("anxious", "worried", "afraid", "nervous", "concerned", "scared", "panic", "urgent", "asap", "as soon as possible", "can't wait", "right now", "immediately", "need this money", "need it soon", "running out of time", "what if", "i'm stressed", "i am stressed", "i can't afford", "cannot afford", "cannot stop worrying", "can't stop worrying", "worried sick"),
        "Frustrated": ("frustrated", "upset", "disappointed", "called three times", "called again", "already called", "again", "no one has", "still waiting", "keep waiting", "going in circles", "same question", "repeating myself", "no progress", "nothing has happened", "this is taking too long", "i've been trying", "tired of", "waste of time", "r u kidding", "are you kidding"),
        "Angry": ("angry", "agary", "mad", "furious", "unacceptable", "outrageous", "ridiculous", "absurd", "incompetent", "disgusted", "terrible service", "complaint", "i want to complain", "you people", "this is not okay", "how dare", "fed up", "pissed off", "lawsuit"),
        "Sad / Distressed": ("sad", "distressed", "helpless", "hopeless", "lost everything", "devastated", "overwhelmed", "crying", "heartbroken", "tragic", "terrible accident", "my life is ruined", "i am suffering", "can't cope", "cannot cope", "in a difficult situation", "desperate", "lost", "discouraged", "feel lost", "feeling lost", "want to die", "i want to die", "kill myself", "end my life"),
        "Satisfied / Relieved": ("thank", "thank you", "grateful", "appreciate", "relieved", "satisfied", "happy", "that helps", "much better", "good to know", "sounds good", "great", "perfect", "finally resolved", "i understand now", "problem solved", "you've been helpful"),
    }
    lowered = text.lower()
    scores = {emotion: sum(lowered.count(word.lower()) for word in words) for emotion, words in groups.items()}
    emotion, hits = max(scores.items(), key=lambda item: item[1])
    if not hits:
        emotion = "Neutral / Calm"
    low = ("a little", "slightly", "somewhat", "a bit")
    medium = ("really", "very", "quite", "so")
    high = ("extremely", "absolutely", "furious", "right now", "immediately")
    urgent_high = ("right now", "immediately", "emergency", "asap")
    urgent_medium = ("need it soon", "as soon as possible", "urgent")
    punctuation = 2 if text.count("!") + text.count("！") >= 3 or text.isupper() and len(text) > 4 else 1 if text.count("!") + text.count("！") >= 1 else 0
    intensity = 2 if any(word in lowered for word in high) else 1 if any(word in lowered for word in medium) else 0
    urgency = 2 if any(word in lowered for word in urgent_high) else 1 if any(word in lowered for word in urgent_medium) else 0
    stop = {"i", "my", "me", "the", "a", "an", "is", "are", "was", "were", "of", "and", "to", "for", "in", "on", "from", "this", "that", "it"}
    words = [word.lower().strip(".,!?;:") for word in text.split() if word.lower().strip(".,!?;:") not in stop]
    repeated = len(words) - len(set(words))
    repetition = 2 if repeated >= 3 else 1 if repeated >= 1 else 0
    level = max(intensity, punctuation, urgency, repetition)
    if emotion not in {"Neutral / Calm", "Satisfied / Relieved"} and level == 0:
        level = 1
    if emotion == "Neutral / Calm" and punctuation >= 2 and not any(word in lowered for word in ("calm", "no rush")):
        emotion = "Frustrated"
    if emotion == "Neutral / Calm":
        return emotion, "low"
    return emotion, ("high" if level >= 2 else "medium" if level == 1 else "low")


def unified_nlu(text, state=None):
    if _DEPENDENCIES is None:
        import app
        return app._unified_nlu_impl(text, state)
    return _DEPENDENCIES.run_unified(text, state)


def classify_intent(text, state=None):
    if _DEPENDENCIES is None:
        import app
        return app._classify_intent_impl(text, state)
    return _DEPENDENCIES.run_classification(text, state)


def infer_requested_field(text, intent=None):
    lowered = re.sub(r"\s+", " ", text.lower()).strip()
    if re.search(r"\b(?:what do i owe|amount owed|out of pocket|patient responsibility)\b", lowered):
        return "amount_owed"
    if re.search(r"\b(?:how much will i receive|reimbursement amount|expected reimbursement|how much was paid)\b", lowered):
        return "expected_reimbursement"
    if re.search(r"\b(?:format|file type|file format|pdf|signature|signed|each page)\b", lowered):
        return "signature_requirement" if re.search(r"\b(?:signature|signed|each page)\b", lowered) else "document_format"
    if re.search(r"\b(?:did you receive|did you get|have you received|upload received|receipt)\b", lowered):
        return "upload_receipt_status"
    if re.search(r"\b(?:appeal|appeal deadline)\b", lowered) and re.search(r"\b(?:when|deadline|ddl|due|by what date)\b", lowered):
        return "appeal_deadline"
    if re.search(r"\b(?:when|deadline|ddl|due date|by what date|how soon)\b", lowered) and re.search(r"\b(?:submit|send|upload|document|file)\b", lowered):
        return "document_submission_deadline"
    if re.search(r"\b(?:fax|faxing)\b", lowered) and re.search(r"\b(?:can|allowed|submit|send)\b", lowered):
        return "fax_submission_allowed"
    if re.search(r"\b(?:how|where|upload|submit|send|mail)\b", lowered) and re.search(r"\b(?:document|file|paperwork)\b", lowered):
        return "document_submission_method"
    if intent == "missing_documents" or re.search(r"\b(?:what documents|which documents|documents? (?:are )?missing|what is missing)\b", lowered):
        return "missing_document_list"
    if intent == "status_inquiry" or re.search(r"\b(?:check|current) (?:my )?(?:case|claim|status)\b", lowered):
        return "claim_status"
    if intent == "case_summary":
        return "case_summary"
    if intent == "next_steps":
        return "next_steps"
    return None


def detect_dialogue_act(text):
    normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if normalized in {"no that is all", "thats all", "that is all", "nothing else", "no more questions", "no further questions", "i am done", "im done", "done", "bye", "goodbye"}:
        return "end_conversation"
    if normalized in {"ok", "okay", "okie", "okey", "k", "kk", "ty", "thx", "thanks", "thank you", "got it", "understood"}:
        return "acknowledgment"
    if normalized in {"what do you mean", "i dont understand", "i do not understand", "please explain", "can you clarify"}:
        return "clarification"
    return "answer_request"


def deduplicate_response_sections(answer):
    if not answer:
        return answer
    sections = re.split(r"\n\s*\n", answer.strip())
    seen, unique = set(), []
    for section in sections:
        normalized = re.sub(r"\s+", " ", section).strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(section.strip())
    return re.sub(r"\b(usually takes)\s+usually\s+", r"\1 ", "\n\n".join(unique), flags=re.I)


def validate_case_hints(text, hints):
    cleaned = re.sub(r"\bCL[- ]?\d+\b", " ", text, flags=re.I)
    cleaned = re.sub(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:dob|date of birth|born|birth date)\b[^\n,.;]*", " ", cleaned, flags=re.I)
    normalized = cleaned.lower()
    result = {"status": None, "case_type": None, "month": None, "year": None}
    if hints.get("status") in {"denied", "closed", "open"}:
        result["status"] = hints["status"]
    if hints.get("case_type") in {"healthcare", "dental"}:
        result["case_type"] = hints["case_type"]
    months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
              "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
    for name, number in months.items():
        if re.search(rf"\b(?:in|from|during|on|for|claim|case)\s+{name}\b", normalized) or re.search(rf"\b{name}\s+(?:19|20)\d{{2}}\b", normalized) or re.search(rf"\b(?:19|20)\d{{2}}\s*/\s*{number:02d}\b", normalized):
            result["month"] = number
            break
    explicit_year = re.search(r"\b(?:19|20)\d{2}\b", normalized)
    if explicit_year:
        result["year"] = int(explicit_year.group())
    return result


__all__ = ["NLUDependencies", "configure_dependencies", "local_extract", "model_extract", "extract_case_nlu", "normalize_api_semantic_result", "api_semantic_interpretation", "interpret_user_message", "unified_nlu", "classify_intent", "detect_emotion", "infer_requested_field", "detect_dialogue_act", "deduplicate_response_sections", "validate_case_hints"]
