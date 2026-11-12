import json
import os
import re
import uuid
from difflib import SequenceMatcher
from threading import Lock, Timer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from prompts import (
    CASE_CLUE_PROMPT,
    IDENTITY_EXTRACTION_PROMPT,
    API_NLU_PROMPT,
    RESPONSE_GENERATION_PROMPT,
    RESPONSE_VALIDATION_PROMPT,
    VALIDATION_POLICY,
)
from guards import LeakGuard, apply_response_guards
from state import advance_phase as _advance_phase, blank_state as _blank_state
import nlu as nlu_service
import verification as verification_service
import case_service
import response_service
import session_service

# The compatibility implementation is registered explicitly so the NLU service
# owns the public boundary while the large legacy implementation is migrated.
nlu_service.configure_dependencies(
    lambda kind, text, state=None: (
        _unified_nlu_impl(text, state) if kind == "unified"
        else _classify_intent_impl(text, state)
    )
)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# Gemini is the only configured provider for this application. If both legacy
# Google and Gemini variables exist in the shell, prevent the SDK from choosing
# the legacy variable implicitly.
if os.getenv("GEMINI_API_KEY"):
    os.environ.pop("GOOGLE_API_KEY", None)

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatPromptTemplate = None
    ChatGoogleGenerativeAI = None

# Gemini is retired. Keep the legacy symbol unavailable so older helper
# functions cannot construct a Google model accidentally. OpenAI is the only
# active LLM provider.
ChatGoogleGenerativeAI = None

ROOT = Path(__file__).parent
FIXTURES = Path(os.getenv("FIXTURES_DIR", str(ROOT / "fixtures")))
CONVERSIONS = Path(os.getenv("CONVERSIONS_DIR", str(ROOT / "conversion")))
CONVERSIONS.mkdir(parents=True, exist_ok=True)
SESSIONS = {}
SESSION_LOCK = Lock()
STAGES = ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")
INTENT_CATEGORIES = (
    "status_inquiry", "denial_question", "missing_documents", "document_submission",
    "document_requirements", "document_alternative", "submission_deadline", "processing_time",
    "receipt_confirmation", "appeal_question", "payment_question", "case_summary",
    "next_steps", "human_transfer", "emotional_support", "unclear",
    "term_explanation",
    "other_claim_inquiry",
)
INTENT_PRIORITY = [
    "human_transfer",
    "denial_question",
    "appeal_question",
    "document_alternative",
    "missing_documents",
    "payment_question",
    "status_inquiry",
    "submission_deadline",
    "processing_time",
    "document_submission",
    "document_requirements",
    "receipt_confirmation",
    "next_steps",
    "case_summary",
    "term_explanation",
    "other_claim_inquiry",
    "emotional_support",
    "unclear",
]
INTENT_PRIORITY_RANK = {intent: index for index, intent in enumerate(INTENT_PRIORITY)}


def normalize_secondary_intents(values=None, legacy=None):
    """Return one canonical, ordered, duplicate-free secondary-intent list."""
    candidates = []
    if isinstance(values, str):
        candidates.append(values)
    elif isinstance(values, list):
        candidates.extend(values)
    if isinstance(legacy, str):
        candidates.insert(0, legacy)
    return list(dict.fromkeys(item for item in candidates if item in INTENT_CATEGORIES))


SEMANTIC_ACTION_TO_INTENT = {
    "check": "status_inquiry",
    "explain": "term_explanation",
    "identify_missing": "missing_documents",
    "submit": "document_submission",
    "confirm_receipt": "receipt_confirmation",
    "ask_deadline": "submission_deadline",
    "ask_timing": "processing_time",
    "ask_payment": "payment_question",
    "appeal": "appeal_question",
    "summarize": "case_summary",
    "next_step": "next_steps",
    "request_human": "human_transfer",
    "review_documents": "document_requirements",
    "emotional_support": "emotional_support",
}


def validate_case_hints(text, hints):
    return nlu_service.validate_case_hints(text, hints)
    """Keep only explicit case clues; reject PII, claim IDs, and false months."""
    cleaned = re.sub(r"\bCL[- ]?\d+\b", " ", text, flags=re.I)
    cleaned = re.sub(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:dob|date of birth|born|birth date)\b[^\n,.;]*", " ", cleaned, flags=re.I)
    normalized = cleaned.lower()
    result = {"status": None, "case_type": None, "month": None, "year": None}
    if hints.get("status") in {"denied", "closed", "open"}:
        result["status"] = hints["status"]
    if hints.get("case_type") in {"healthcare", "dental"}:
        result["case_type"] = hints["case_type"]
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    for name, number in month_names.items():
        month_pattern = rf"(?:\b(?:in|from|during|on|for|claim|case)\s+{name}\b|\b{name}\s+(?:19|20)\d{{2}}\b|\b(?:19|20)\d{{2}}\s*[/ -]\s*{number:02d}\b)"
        short_pattern = rf"(?:\b(?:in|from|during|on|for|claim|case)\s+{name[:3]}\.?\b|\b{name[:3]}\.?\s+(?:19|20)\d{{2}}\b)"
        if re.search(month_pattern, normalized) or re.search(short_pattern, normalized):
            result["month"] = number
            break
    explicit_year = re.search(r"\b(?:19|20)\d{2}\b", normalized)
    if explicit_year:
        result["year"] = int(explicit_year.group())
    return result


def detect_dialogue_act(text):
    return nlu_service.detect_dialogue_act(text)
    # Legacy implementation retained below during migration.
    normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if normalized in {"no that is all", "thats all", "that is all", "nothing else", "no more questions", "no further questions", "i am done", "im done", "done", "bye", "goodbye"}:
        return "end_conversation"
    if normalized in {"ok", "okay", "okie", "okey", "k", "kk", "ty", "thx", "thanks", "thank you", "got it", "understood"}:
        return "acknowledgment"
    if normalized in {"what do you mean", "i dont understand", "i do not understand", "please explain", "can you clarify"}:
        return "clarification"
    return "answer_request"


def infer_requested_field(text, intent=None):
    return nlu_service.infer_requested_field(text, intent)
    # Legacy implementation retained below during migration.
    """Map the exact requested attribute/action to a stable field name."""
    lowered = re.sub(r"\s+", " ", text.lower()).strip()
    if re.search(r"\b(?:what do i owe|amount owed|out of pocket|patient responsibility)\b", lowered):
        return "amount_owed"
    if re.search(r"\b(?:how much will i receive|reimbursement amount|expected reimbursement|how much was paid)\b", lowered):
        return "expected_reimbursement"
    if re.search(r"\b(?:format|file type|file format|pdf|signature|signed|each page)\b", lowered):
        if re.search(r"\b(?:signature|signed|each page)\b", lowered):
            return "signature_requirement"
        return "document_format"
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


def deduplicate_response_sections(answer):
    return nlu_service.deduplicate_response_sections(answer)
    # Legacy implementation retained below during migration.
    """Remove repeated paragraphs/sentences created by multi-intent merging."""
    if not answer:
        return answer
    sections = re.split(r"\n\s*\n", answer.strip())
    seen = set()
    unique = []
    for section in sections:
        normalized = re.sub(r"\s+", " ", section).strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(section.strip())
    result = "\n\n".join(unique)
    # Protect against duplicated wording inside a single merged sentence.
    result = re.sub(r"\b(usually takes)\s+usually\s+", r"\1 ", result, flags=re.I)
    return result

INTENT_PROMPT_POLICY = {
    "primary_rule": "Choose the user's requested business action as the primary intent. Do not replace a business intent with emotional_support merely because the user sounds worried, angry, sad, or frustrated.",
    "secondary_rule": "Treat emotional language as a secondary signal when a concrete business request is present. Use emotional_support as the primary intent only when there is no concrete supported business request.",
    "question_type_rules": {
        "whether_received": "receipt_confirmation",
        "how_or_where_to_send": "document_submission",
        "what_documents_are_needed_or_missing": "missing_documents or document_requirements based on whether the user asks about outstanding versus required documents",
        "what_can_replace_a_document": "document_alternative",
        "when_or_by_what_date": "submission_deadline",
        "how_long": "processing_time",
        "why_rejected": "denial_question",
    },
    "conflict_rules": [
        "Did you receive the documents I uploaded? => receipt_confirmation, not document_submission.",
        "How do I submit the missing documents? => document_submission, not missing_documents.",
        "What documents are still missing? => missing_documents.",
        "What documents are required? => document_requirements.",
        "I cannot get the original report; what alternative can I provide? => document_alternative.",
        "I am angry, but why was my claim denied? => denial_question as primary and emotional_support as secondary.",
        "I am worried and do not know what to do. => emotional_support when no concrete business request is present.",
    ],
    "output_contract": "Return one valid JSON object only with intent, relation, confidence, parent_intent_index, question, case_resolution, next_allowed_action, and needs_clarification.",
}


def llm_content_text(message):
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


'''LEGACY_GUARDS_REMOVED
    """Prevent claim-specific information from leaving the verification gate."""

    CLAIM_ID_PATTERN = re.compile(r"\bCL[- ]?\d+\b", re.I)
    SENSITIVE_PATTERNS = (
        r"\b(?:status|denied|denial|rejected|payment|reimbursement|documents? missing|claim outcome)\b",
    )

    @classmethod
    def check(cls, state, answer):
        if state.get("gates", {}).get("VERIFY_ID") and state.get("verified_party_id"):
            return answer
        lowered = answer.lower()
        leaks_claim_id = cls.CLAIM_ID_PATTERN.search(answer)
        leaks_claim_fact = any(re.search(pattern, lowered) for pattern in cls.SENSITIVE_PATTERNS)
        if leaks_claim_id or leaks_claim_fact:
            return (
                "To protect your claim information, I need to verify your identity first. "
                "Please provide at least three matching identity details: your full name, "
                "date of birth, phone number, email, or SSN last four digits."
            )
        return answer


class GroundingValidator:
    """Ensure claim-specific responses contain only authorized local facts."""

    CLAIM_ID_PATTERN = re.compile(r"\bCL[- ]?\d+\b", re.I)

    @classmethod
    def check(cls, state, answer):
        if state.get("phase") != "PROCESS_CASE" or not state.get("case_access_authorized"):
            return answer
        case_id = state.get("matched_case_id")
        if not case_id:
            return answer
        mentioned_ids = {
            match.upper().replace(" ", "-")
            for match in cls.CLAIM_ID_PATTERN.findall(answer)
        }
        if mentioned_ids and mentioned_ids != {case_id.upper()}:
            return (
                "I’m not able to confirm that detail from the authorized claim documents. "
                "Would you like me to connect you with a human claims representative?"
            )
        claim = next(
            (item for item in load_json("claims.json") if item.get("case_id") == case_id),
            None,
        )
        if not claim:
            return "I could not find the authorized claim details. Would you like me to connect you with a human claims representative?"
        # The current answer functions are document-grounded Python responses.
        # Reject a response that states a different claim status than the local
        # authorized record.
        expected_status = str(claim.get("status") or "").lower()
        status_words = {"open", "closed", "denied", "under review", "processing"}
        stated_statuses = {word for word in status_words if re.search(rf"\b{re.escape(word)}\b", answer.lower())}
        if expected_status and stated_statuses and expected_status not in stated_statuses:
            return (
                "I’m not able to confirm that claim detail from the authorized documents. "
                "Would you like me to connect you with a human claims representative?"
            )
        return answer


def apply_response_guards(state, answer, *_unused):
    """Apply disclosure and grounding checks before returning an answer."""
    answer = LeakGuard.check(state, answer)
    answer = GroundingValidator.check(state, answer, load_json)
    return validate_requested_field_answer(state, answer)


def validate_requested_field_answer(state, answer):
    """Reject answers that clearly provide a related but different field."""
    field = state.get("last_requested_field")
    if not field or not answer:
        return answer
    lowered = answer.lower()
    conflicts = {
        "document_submission_deadline": ("appeal deadline", "appeal due date"),
        "appeal_deadline": ("submission deadline", "upload deadline"),
        "upload_receipt": ("how to upload", "member portal or claim upload link"),
        "document_submission": ("documents currently listed", "documents are missing"),
        "missing_documents": ("how to submit", "member portal or claim upload link"),
    }
    if any(marker in lowered for marker in conflicts.get(field, ())):
        return "I could not confirm the requested information from the authorized claim documents. Would you like me to connect you with a human claims representative?"
    return answer


def should_use_llm_validation(state):
    """Use semantic validation only where code checks cannot judge quality."""
    if os.getenv("ENABLE_LLM_RESPONSE_VALIDATION", "false").lower() != "true":
        return False
    intent = ((state.get("intents") or [{}])[-1]).get("intent")
    return intent in {
        "case_summary", "term_explanation", "unclear", "emotional_support",
        "next_steps", "human_transfer",
    } or len((state.get("intents") or [])) > 1
'''


def _unified_nlu_impl(text, state=None):
    """Run one bounded NLU request and return the shared workflow contract."""
    detected_emotion, detected_level = detect_emotion(text)
    empty = {
        "identity_fields": local_extract(text),
        "raw_message": text,
        "meaning": text,
        "primary_intent": "unclear",
        "requested_field": None,
        "dialogue_act": detect_dialogue_act(text),
        "secondary_intents": [],
        "emotion": detected_emotion,
        "emotion_level": detected_level,
        "emotion_explicit": detected_emotion not in {"Neutral / Calm", "Satisfied / Relieved"},
        "case_hints": {"status": None, "case_type": None, "month": None, "year": None},
    }
    openai_available = bool(OpenAI and os.getenv("OPENAI_API_KEY"))
    if openai_available:
        try:
            prompt_text = API_NLU_PROMPT.format(
                intent_priority=" > ".join(INTENT_PRIORITY),
                semantic_rules=json.dumps(INTENT_SEMANTIC_RULES, ensure_ascii=False),
            )
            context = json.dumps({
                "last_nlu": (state or {}).get("last_nlu", {}),
                "active_intent": (state or {}).get("intents", [])[-1:],
            }, ensure_ascii=False)
            client = OpenAI()
            response = client.responses.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
                instructions=prompt_text,
                input=f"Conversation context:\n{context}\n\nUser message:\n{text}",
            )
            raw_result = response.output_text.strip()
            if raw_result.startswith("```"):
                raw_result = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_result, flags=re.I | re.S).strip()
            result = json.loads(raw_result)
            result["api_provider"] = "OpenAI"
            semantic_intent = SEMANTIC_ACTION_TO_INTENT.get(result.get("action"))
            if semantic_intent and (
                result.get("primary_intent") not in INTENT_CATEGORIES
                or result.get("primary_intent") == "unclear"
                or float(result.get("confidence", 0.0) or 0.0) < 0.7
            ):
                result["primary_intent"] = semantic_intent
                result["needs_clarification"] = False
                result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.9)
            # Continue through the shared normalization and validation below.
        except Exception as exc:
            if os.getenv("OPENAI_DEBUG", "false").lower() == "true":
                print(f"[OpenAI unified NLU unavailable] {type(exc).__name__}: {exc}", flush=True)
            return {"api_error": "OpenAI API is unavailable. Please reconnect the API and try again."}
    if not openai_available:
        return {"api_error": "OpenAI API is unavailable. Please reconnect the API and try again."}
    elif False:
        # Keep the demo and regression suite usable without credentials. The
        # local path performs deterministic extraction/classification only;
        # claim authorization and document grounding remain Python-controlled.
        # Do not let a previous message's cached NLU result override the
        # current local-fallback classification.
        local_state = dict(state or {})
        local_state["last_nlu"] = None
        local_state["_unified_nlu_text"] = None
        local = _classify_intent(text, local_state)
        lowered_local = text.lower().strip()
        if lowered_local in {"status check", "check status", "claim status", "case status"}:
            local.update({"intent": "status_inquiry", "confidence": 0.96})
        local_intent = local.get("intent", "unclear")
        local_secondary = []
        if local_intent == "submission_deadline" and re.search(r"\b(?:how|where)\b.*\b(?:upload|submit|send)\b", text.lower()):
            local_secondary.append("document_submission")
        if detected_emotion not in {"Neutral / Calm", "Satisfied / Relieved"} and local_intent not in {"unclear", "emotional_support"}:
            local_secondary.append("emotional_support")
        return {
            "identity_fields": local_extract(text),
            "raw_message": text,
            "meaning": text.strip(),
            "primary_intent": local_intent,
            "requested_field": infer_requested_field(text, local_intent),
            "secondary_intents": local_secondary,
            "confidence": local.get("confidence", 0.0),
            "needs_clarification": local.get("intent") == "unclear" and detect_dialogue_act(text) != "end_conversation",
            "emotion": detected_emotion,
            "emotion_level": detected_level,
            "emotion_explicit": detected_emotion not in {"Neutral / Calm", "Satisfied / Relieved"},
            "case_hints": validate_case_hints(text, extract_case_nlu(text)),
            "dialogue_act": detect_dialogue_act(text),
            "term": local.get("term"),
            "api_mode": False,
        }
    if openai_available:
        # The OpenAI path has already produced the shared JSON result.
        pass
    else:
      prompt = ChatPromptTemplate.from_messages([
        ("system", "Insurance NLU. Return ONLY valid JSON with exactly: identity_fields, primary_intent, secondary_intents, emotion, emotion_level, case_hints. identity_fields={{full_name,dob,phone,email,ssn_last4,policy_number,case_hint}}; case_hints={{status,case_type,month,year}}; use null when absent and never invent facts. primary_intent must be one of: status_inquiry, denial_question, missing_documents, document_submission, document_requirements, document_alternative, submission_deadline, processing_time, receipt_confirmation, appeal_question, payment_question, case_summary, next_steps, human_transfer, emotional_support, term_explanation, unclear. Choose the concrete business request as primary. If emotion accompanies a business request, add emotional_support to the secondary_intents array; use emotional_support as primary only for a purely emotional message. secondary_intents must be an array with no duplicates. Key boundaries: whether/did you receive=receipt_confirmation; how/where to send= document_submission; what is missing=missing_documents; what is required=document_requirements; replacement/substitute=document_alternative; when/by what date=submission_deadline; how long=processing_time. emotion_level=low|medium|high."),
        ("human", "Conversation context:\n{context}\n\nUser message:\n{text}"),
      ])
    # The canonical prompt text is maintained in prompts.py.
    if not openai_available:
      prompt = ChatPromptTemplate.from_messages([
        ("system", API_NLU_PROMPT),
        ("human", "Conversation context:\n{context}\n\nUser message:\n{text}"),
      ])
      chain = prompt | ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), temperature=0, timeout=30, max_retries=1) | llm_content_text
    try:
        if not openai_available:
          raw_result = chain.invoke({
            "context": json.dumps({
                "last_nlu": (state or {}).get("last_nlu", {}),
                "active_intent": (state or {}).get("intents", [])[-1:],
            }, ensure_ascii=False),
            "text": text,
            "intent_categories": ", ".join(INTENT_CATEGORIES),
            "intent_priority": " > ".join(INTENT_PRIORITY),
            "semantic_rules": json.dumps(INTENT_SEMANTIC_RULES, ensure_ascii=False),
          })
          raw_result = str(raw_result).strip()
          if raw_result.startswith("```"):
              raw_result = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_result, flags=re.I | re.S).strip()
          result = json.loads(raw_result)
        semantic_intent = SEMANTIC_ACTION_TO_INTENT.get(result.get("action"))
        if semantic_intent and (
            result.get("primary_intent") not in INTENT_CATEGORIES
            or result.get("primary_intent") == "unclear"
        ):
            result["primary_intent"] = semantic_intent
            result["needs_clarification"] = False
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.9)
        lowered_message = text.lower()
        is_term_question = any(phrase in lowered_message for phrase in ("what is", "what's", "what does", "meaning of", "explain"))
        if is_term_question and "pathology report" in lowered_message:
            result.update(primary_intent="term_explanation", term="pathology report", needs_clarification=False)
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
        elif is_term_question and "office note" in lowered_message:
            result.update(primary_intent="term_explanation", term="office note", needs_clarification=False)
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
        # Generic help must not be hallucinated into next_steps. If the
        # message is emotional and contains no concrete insurance action,
        # emotional_support is the correct primary intent. A concrete action
        # remains primary and receives emotional_support as a secondary intent.
        emotional_text = bool(re.search(
            r"\b(?:worried|anxious|overwhelmed|scared|concerned|upset|frustrated|distressed|sad|angry|furious|lost|discouraged|hopeless)\b",
            lowered_message,
        ))
        concrete_action = bool(re.search(
            r"\b(?:status|denied|denial reason|why was|missing documents|required documents|how do i submit|how can i submit|where can i submit|upload|receive(?:d)?|receipt|deadline|when should i|how long|processing time|how much|owe|payment|reimbursement|appeal|summarize|summary|what should i do next|what do i do now)\b",
            lowered_message,
        ))
        generic_help = bool(re.search(r"\b(?:help me with this claim|help with this claim|help with my claim|help me with this issue|i need help)\b", lowered_message))
        if emotional_text and (generic_help or not concrete_action):
            result["primary_intent"] = "emotional_support"
            result["secondary_intents"] = []
            result["meaning"] = "The customer is expressing emotional distress and has not stated a specific insurance action."
            result["needs_clarification"] = False
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.92)
        elif emotional_text and concrete_action and result.get("primary_intent") not in {None, "unclear", "emotional_support"}:
            secondary = result.get("secondary_intents") if isinstance(result.get("secondary_intents"), list) else []
            if "emotional_support" not in secondary:
                secondary.append("emotional_support")
            result["secondary_intents"] = secondary
            result["secondary_intents"] = normalize_secondary_intents(
                result.get("secondary_intents"), "emotional_support"
            )
        # Preserve multiple concrete requests in the same message. In
        # particular, "why was it denied and what next step should I take?"
        # must answer denial_question and next_steps in sequence.
        if (
            any(marker in lowered_message for marker in ("why", "denied", "rejected", "turned down"))
            and any(marker in lowered_message for marker in ("next step", "what should i do", "what do i do", "what should i submit"))
            and result.get("primary_intent") != "emotional_support"
        ):
            result["primary_intent"] = "denial_question"
            result["secondary_intents"] = ["next_steps"]
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
        normalized_text = text.strip().lower().strip(" ?!.,")
        previous_intents = (state or {}).get("intents", [])
        active_answer = (previous_intents[-1].get("answer", "").lower() if previous_intents else "")

        # A request can be semantically clear even when the customer does not
        # specify the exact document subtype.  "Check the documents" means
        # review the outstanding documents; when it is paired with a deadline,
        # preserve both requests instead of letting the model's uncertainty
        # about missing-versus-required documents collapse the whole message
        # to unclear.
        asks_documents = bool(re.search(
            r"\b(?:check|review|which|what|any|the)\b[^.!?\n]{0,50}\bdocuments?\b|\bdocuments?\b[^.!?\n]{0,50}\b(?:check|review)\b",
            lowered_message,
        ))
        asks_deadline = bool(re.search(
            r"\b(?:deadline|due date|when should|when do i need|by when|how soon)\b",
            lowered_message,
        ))
        asks_case_check = bool(re.search(
            r"\b(?:check|review|look at)\s+(?:(?:my|the)\s+)?(?:case|claim)\b",
            lowered_message,
        ))
        if asks_case_check and not asks_documents and not asks_deadline and result.get("primary_intent") != "emotional_support":
            result["primary_intent"] = "status_inquiry"
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
            result["needs_clarification"] = False
        if asks_documents and asks_deadline and result.get("primary_intent") != "emotional_support":
            result["primary_intent"] = "missing_documents"
            result["secondary_intents"] = normalize_secondary_intents(
                result.get("secondary_intents"), "submission_deadline"
            )
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
            result["needs_clarification"] = False
        if asks_case_check and asks_documents and result.get("primary_intent") != "emotional_support":
            result["primary_intent"] = "status_inquiry"
            result["secondary_intents"] = normalize_secondary_intents(
                result.get("secondary_intents"), "missing_documents"
            )
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
            result["needs_clarification"] = False
        asks_reason = bool(re.search(
            r"\b(?:check|explain|tell me)\s+(?:the|my|this)?\s*reason\b|\bdenial reason\b",
            lowered_message,
        ))
        if asks_reason and result.get("primary_intent") != "emotional_support":
            result["primary_intent"] = "denial_question"
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.95)
            result["needs_clarification"] = False
        elif asks_documents and result.get("primary_intent") == "unclear":
            result["primary_intent"] = "missing_documents"
            result["confidence"] = max(float(result.get("confidence", 0.0) or 0.0), 0.9)
            result["needs_clarification"] = False
        if normalized_text in {"ok", "okay", "yes", "yes please", "please", "sure", "that would help", "go ahead"}:
            if "documents may help" in active_answer or "explain which documents" in active_answer:
                result["primary_intent"] = "missing_documents"
                result["secondary_intents"] = []
                result["meaning"] = "The customer accepted the offer to review the documents that may help with the next step."
                result["follow_up"] = True
            elif "next step" in active_answer or "what to do next" in active_answer:
                result["primary_intent"] = "next_steps"
                result["secondary_intents"] = []
                result["meaning"] = "The customer accepted the previously offered next-step guidance."
                result["follow_up"] = True
        if result.get("primary_intent") in {None, "unclear"} and normalized_text in {"why", "how come"} and previous_intents:
            if previous_intents[-1].get("intent") == "status_inquiry":
                result["primary_intent"] = "denial_question"
                result["secondary_intents"] = []
                result["meaning"] = "The customer is asking why the claim has its current status, including the denial reason."
        identity = result.get("identity_fields") if isinstance(result.get("identity_fields"), dict) else {}
        identity = {**local_extract(text), **{k: v for k, v in identity.items() if v}}
        hints = result.get("case_hints") if isinstance(result.get("case_hints"), dict) else {}
        hints = validate_case_hints(text, hints)
        try:
            confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        primary_intent = result.get("primary_intent") if result.get("primary_intent") in INTENT_CATEGORIES else "unclear"
        if confidence < 0.7 or result.get("needs_clarification") is True:
            primary_intent = "unclear"
        # Normalize emotion inside the NLU contract itself. This keeps the
        # intermediate NLU state consistent with the final reply: explicit
        # wording, repeated emphasis, and punctuation must not be downgraded
        # by a weaker Gemini classification.
        api_emotion = result.get("emotion") if result.get("emotion") in {
            "Neutral / Calm", "Confused", "Anxious / Worried", "Frustrated",
            "Angry", "Sad / Distressed", "Satisfied / Relieved",
        } else empty["emotion"]
        api_level = result.get("emotion_level") if result.get("emotion_level") in {"low", "medium", "high"} else "low"
        level_rank = {"low": 0, "medium": 1, "high": 2}
        if detected_emotion == "Neutral / Calm":
            # Do not let the model invent concern or escalation for a factual
            # request such as "what documents are missing and what is the
            # deadline". The deterministic detector is authoritative when no
            # explicit emotional wording is present.
            merged_level = detected_level
            merged_emotion = "Neutral / Calm"
        else:
            merged_level = max((detected_level, api_level), key=lambda level: level_rank[level])
            merged_emotion = detected_emotion
        explicit_emotion = (
            detected_emotion not in {"Neutral / Calm", "Satisfied / Relieved"}
            or api_emotion not in {"Neutral / Calm", "Satisfied / Relieved"}
            or merged_level != "low"
        )
        normalized_secondary = normalize_secondary_intents(
            result.get("secondary_intents"), result.get("secondary_intent")
        )
        if merged_emotion not in {"Neutral / Calm", "Satisfied / Relieved"} and primary_intent not in {"unclear", "emotional_support"}:
            normalized_secondary = normalize_secondary_intents(normalized_secondary, "emotional_support")
        return {
            "identity_fields": identity,
            "raw_message": text,
            "meaning": result.get("meaning") or text,
            "dialogue_act": result.get("dialogue_act") or detect_dialogue_act(text),
            "action": result.get("action"),
            "object": result.get("object"),
            "context": result.get("context") if isinstance(result.get("context"), dict) else {},
            "normalized_request": result.get("normalized_request"),
            "requested_field": result.get("requested_field") or infer_requested_field(text, primary_intent),
            "ambiguous": result.get("ambiguous"),
            "uncertain_terms": result.get("uncertain_terms") if isinstance(result.get("uncertain_terms"), list) else [],
            "primary_intent": primary_intent,
            "confidence": confidence,
            "needs_clarification": (primary_intent == "unclear" or bool(result.get("needs_clarification"))) and detect_dialogue_act(text) != "end_conversation",
            "secondary_intents": normalized_secondary,
            "term": result.get("term"),
            "emotion": merged_emotion,
            "emotion_level": merged_level,
            "emotion_explicit": explicit_emotion,
            "case_hints": {key: hints.get(key) for key in ("status", "case_type", "month", "year")},
            "api_mode": True,
        }
    except Exception as exc:
        if os.getenv("GEMINI_DEBUG", "false").lower() == "true":
            print(f"[OpenAI unified NLU unavailable] {type(exc).__name__}: {exc}", flush=True)
        return {"api_error": "OpenAI API is unavailable. Please reconnect the API and try again."}

# Semantic evidence used by the classifier. Positive signals describe what the
# customer is trying to do; exclusion signals prevent closely related intents
# from winning only because they share a word such as "document" or "payment".
INTENT_SEMANTIC_RULES = {
    "status_inquiry": {
        "positive": ["what is the status", "where does my claim stand", "is my claim open or closed", "is it still processing"],
        "exclude": ["why was it denied", "how much will i receive", "what documents are missing", "how do i upload"],
    },
    "denial_question": {
        "positive": ["why was my claim denied", "what is the denial reason", "why was it rejected", "why was the reimbursement turned down"],
        "exclude": ["how much will i receive", "what documents do i need", "how do i appeal"],
    },
    "missing_documents": {
        "positive": ["what documents are missing", "did i miss any documents", "what is still missing", "which documents do i need"],
        "exclude": ["how do i upload", "where can i submit", "did you receive my documents", "what format should they be"],
    },
    "document_submission": {
        "positive": ["how do i upload", "where can i submit", "how do i send the documents", "can i submit by fax or mail"],
        "exclude": ["did you receive", "did you get", "what documents are missing", "what format should they be"],
    },
    "document_requirements": {
        "positive": ["what documents are required", "what format should the documents be", "does each page need a signature", "what information must be visible"],
        "exclude": ["how do i upload", "where can i submit", "did you receive", "what can i submit instead"],
    },
    "document_alternative": {
        "positive": ["what can i submit instead", "i do not have that document", "is there an alternative document", "what can replace it"],
        "exclude": ["what documents are missing", "how do i upload", "what format should they be"],
    },
    "submission_deadline": {
        "positive": ["when should i submit", "when is the deadline", "how soon do i need to send it", "when is it due"],
        "exclude": ["how long will review take", "how do i submit", "did you receive"],
    },
    "processing_time": {
        "positive": ["how long will the review take", "when will it be processed", "how long after submission", "how soon will i hear back"],
        "exclude": ["when is the submission deadline", "what is the claim status", "did you receive"],
    },
    "receipt_confirmation": {
        "positive": ["did you receive", "did you get", "have you received", "was my upload received", "can you confirm receipt"],
        "exclude": ["how do i upload", "where can i submit", "what documents are missing", "what format should they be"],
    },
    "appeal_question": {
        "positive": ["can i appeal", "how do i appeal", "what is the appeal deadline", "how do i challenge the denial"],
        "exclude": ["why was it denied", "how much will i receive", "what documents are missing"],
    },
    "payment_question": {
        "positive": ["how much will i receive", "what is my reimbursement amount", "how much was paid", "what do i owe", "should i pay anything"],
        "exclude": ["why was payment refused", "why was reimbursement denied", "how do i submit", "what documents are missing"],
    },
    "case_summary": {
        "positive": ["summarize my case", "give me an overview", "what happened in my case", "what happened with this claim", "what is the overall result of my case", "tell me the whole story of my claim"],
        "exclude": ["what is the current status", "why was it denied", "what should i do next"],
    },
    "next_steps": {
        "positive": ["what should i do next", "what are the next steps", "what do i do now"],
        "exclude": ["how do i upload", "when is the deadline", "how long will review take"],
    },
    "human_transfer": {
        "positive": ["speak with a person", "connect me with a representative", "transfer me to a human", "i need an agent"],
        "exclude": ["can you explain", "what is the status", "what documents are missing"],
    },
    "other_claim_inquiry": {
        "positive": ["another claim", "different claim", "other claim", "switch claims", "look at my other claim", "check a different claim"],
        "exclude": ["what is the status of this claim", "why was this claim denied"],
    },
    "emotional_support": {
        "positive": ["i am worried", "i am anxious", "i am upset", "i am overwhelmed", "i am scared", "i feel lost", "i am worried about what will happen next", "i feel distressed"],
        "exclude": ["what is the status", "why was my claim denied", "how much will i receive"],
    },
    "term_explanation": {
        "positive": ["what does this term mean", "what is a pathology report", "what is an office note", "can you explain this word"],
        "exclude": ["what is the claim status", "what documents are missing", "how do i submit"],
    },
    "unclear": {
        "positive": ["i do not understand", "what do you mean", "that is unclear", "i am confused"],
        "exclude": ["what is the status", "what documents are missing", "how do i submit"],
    },
}

# Keep at least three explicit near-miss examples for every intent. These are
# exposed in the classifier contract as counterexamples, not merely as loose
# negative keywords, so the LLM must distinguish the requested action.
for _intent_rule in INTENT_SEMANTIC_RULES.values():
    _intent_rule["counterexamples"] = list(_intent_rule["exclude"][:3])


def load_json(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


POLICYHOLDERS = load_json("policyholders.json")


def blank_state():
    return _blank_state()
    return {
        "phase": "VERIFY_ID",
        "gates": {"VERIFY_ID": False, "RESOLVE_INTENT": False, "PROCESS_CASE": False, "POST_PROCESS": False},
        "stage_progress": {"VERIFY_ID": 0.0, "RESOLVE_INTENT": 0.0, "PROCESS_CASE": 0.0, "POST_PROCESS": 0.0},
        "identity": {},
        "verified_fields": [],
        "verified_party_id": None,
        "case_access_authorized": False,
        "intent_hints": [],
        "case_hints": [],
        "attempts": 0,
        "verification_attempts": 0,
        "human_transfer": False,
        "awaiting_human_transfer": False,
        "human_transfer_reason": None,
        "handoff_conditions": {
            "verification_failures": 0,
            "verification_pushbacks": 0,
            "unclear_questions": 0,
            "emotional_intents": 0,
            "triggered_conditions": [],
        },
        "handoff_ticket": None,
        "matched_case_id": None,
        "case_options": [],
        "awaiting_case_selection": False,
        "nlu_hints": {"status": None, "case_type": None, "month": None, "year": None},
        "last_nlu": None,
        "last_user_message": None,
        "last_requested_field": None,
        "last_intent_contract": None,
        "case": None,
        "emotion": "Neutral / Calm",
        "emotion_level": "low",
        "emotional_intent_streak": 0,
        "emotional_intent_count": 0,
        "user_need": None,
        "awaiting_user_need": False,
        "intents": [],
        "potential_intents": [],
        "awaiting_potential_confirmation": False,
        "current_intent_index": None,
        "awaiting_additional_question": False,
        "awaiting_clarification": False,
        "clarification_attempts": 0,
        "followup_sent": False,
        "conversation_complete": False,
        "unrelated_question_count": 0,
        "post_process_step": None,
        "summary_email": None,
        "disconnect_scheduled": False,
        "disconnected": False,
    }


def advance_phase(state, next_phase):
    """Advance only when every preceding stage is complete."""
    return _advance_phase(state, next_phase)
    if next_phase not in STAGES:
        return False
    next_index = STAGES.index(next_phase)
    if any(state["stage_progress"][stage] < 1.0 for stage in STAGES[:next_index]):
        return False
    state["phase"] = next_phase
    return True


def conversion_path(session_id):
    """Return the JSON file used for durable storage of one conversation."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    return CONVERSIONS / f"{safe_id}.json"


def redact_identity(identity):
    """Return a storage-safe identity while keeping the full identity in memory."""
    name = str(identity.get("full_name") or "")
    masked_name = " ".join(f"{part[0]}***" if part else part for part in name.split())
    email = str(identity.get("email") or "")
    if "@" in email:
        local, domain = email.split("@", 1)
        masked_email = f"{local[:1]}***@{domain}"
    else:
        masked_email = "***" if email else ""
    phone_digits = re.sub(r"\D", "", str(identity.get("phone") or ""))
    masked_phone = f"***-***-{phone_digits[-4:]}" if len(phone_digits) >= 4 else ("****" if phone_digits else "")
    dob_digits = re.sub(r"\D", "", str(identity.get("dob") or ""))
    masked_dob = f"***-**-{dob_digits[-2:]}" if len(dob_digits) >= 2 else ("***" if dob_digits else "")
    return {
        "full_name": masked_name,
        "dob": masked_dob,
        "email": masked_email,
        "ssn_last4": "****" if identity.get("ssn_last4") else "",
        "phone": masked_phone,
    }


def redact_session_for_storage(session):
    """Redact PII in the persisted copy; never mutate the live in-memory session."""
    safe = json.loads(json.dumps(session, ensure_ascii=False))
    state = safe.get("state", {})
    original_identity = session.get("state", {}).get("identity", {})
    replacements = {
        str(original_identity.get("full_name")): redact_identity(original_identity).get("full_name"),
        str(original_identity.get("dob")): redact_identity(original_identity).get("dob"),
        str(original_identity.get("email")): redact_identity(original_identity).get("email"),
        str(original_identity.get("phone")): redact_identity(original_identity).get("phone"),
        str(original_identity.get("ssn_last4")): "****",
    }
    def redact_text(value):
        if not isinstance(value, str):
            return value
        for raw, masked in replacements.items():
            if raw and raw != "None":
                value = value.replace(raw, masked)
        return value
    def redact_nested(value):
        if isinstance(value, dict):
            return {key: redact_nested(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_nested(item) for item in value]
        return redact_text(value)
    safe = redact_nested(safe)
    state = safe.get("state", {})
    state["identity"] = redact_identity(original_identity)
    state.pop("verified_fields", None)
    state.pop("verified_party_id", None)
    state.pop("case_access_authorized", None)
    return safe


def has_claim_questions(state):
    """Whether the conversation contains a substantive claim question."""
    excluded = {"unclear", "emotional_support"}
    for item in state.get("intents", []):
        if item.get("intent") not in excluded:
            return True
        if any(followup.get("intent") not in excluded for followup in item.get("followups", [])):
            return True
    return False


def close_without_summary(state):
    """Close an emotional-only/unclear conversation without creating a summary email."""
    state["conversation_complete"] = True
    state["gates"]["POST_PROCESS"] = True
    state["stage_progress"]["POST_PROCESS"] = 1.0
    state["post_process_step"] = "disconnect_scheduled"
    state["disconnect_scheduled"] = True
    return "Understood. No claim-specific question was recorded, so I will not create an insurance case summary. This conversation will close shortly."


def registered_email_for_state(state):
    """Return the registered email from the verified policyholder record only."""
    party_id = state.get("verified_party_id")
    policyholder = next((item for item in POLICYHOLDERS if item.get("party_id") == party_id), None)
    return normalize_email(policyholder.get("email")) if policyholder and policyholder.get("email") else None


def clear_conversion_files():
    """Start each Python run with no previous customer session state."""
    if os.getenv("RESET_CONVERSIONS_ON_START", "true").lower() != "true":
        return
    for path in CONVERSIONS.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            continue


def send_followup_if_idle(session_id):
    session = SESSIONS.get(session_id)
    if not session:
        return
    state = session["state"]
    if state.get("awaiting_additional_question") and not state.get("followup_sent") and not state.get("disconnected"):
        session["messages"].append({"role": "assistant", "content": "Do you have any other questions about this case?"})
        state["followup_sent"] = True
        save_session(session_id, session)


def save_session(session_id, session):
    return session_service.save_session(session_id, session)


def build_conversion_summary(session):
    return session_service.build_conversion_summary(session)


def send_summary_email(session, address):
    return session_service.send_summary_email(session, address)


def load_sessions():
    return session_service.load_sessions()


def disconnect_session(session_id):
    return session_service.disconnect_session(session_id)


def detect_emotion(text):
    return nlu_service.detect_emotion(text)
    # Legacy implementation retained below during migration.
    """Map emotion and intensity using wording, punctuation, urgency, and repetition."""
    groups = {
        "Neutral / Calm": (
            "check my claim status", "claim status", "status update", "just checking",
            "update", "information", "question", "calm", "no rush", "general question",
            "i'd like to know", "i would like to know",
        ),
        "Confused": (
            "confused", "don't understand", "unclear", "i don't understand", "i do not understand", "i'm confused",
            "what do you mean", "i don't follow", "not sure how",
            "which one", "doesn't make sense",
        ),
        "Anxious / Worried": (
            "anxious", "worried", "afraid", "nervous", "concerned", "scared", "panic",
            "urgent", "asap", "as soon as possible", "can't wait", "right now", "immediately", "need this money", "need it soon", "running out of time",
            "what if", "i'm stressed", "i am stressed", "i can't afford", "cannot afford",
            "cannot stop worrying", "can't stop worrying", "worried sick",
        ),
        "Frustrated": (
        "frustrated", "upset", "disappointed", "called three times", "called again", "already called", "again", "no one has",
        "still waiting", "keep waiting", "going in circles", "same question", "repeating myself",
            "no progress", "nothing has happened", "this is taking too long", "i've been trying", "tired of", "waste of time", "r u kidding", "are you kidding",
        ),
        "Angry": (
            "angry", "agary", "mad", "furious", "unacceptable", "outrageous", "ridiculous", "absurd", "incompetent",
            "disgusted", "terrible service", "complaint", "i want to complain", "you people",
            "this is not okay", "how dare", "fed up", "pissed off", "lawsuit",
        ),
        "Sad / Distressed": (
            "sad", "distressed", "helpless", "hopeless", "lost everything", "devastated", "overwhelmed", "crying",
            "heartbroken", "tragic", "terrible accident", "my life is ruined", "i am suffering",
            "can't cope", "cannot cope", "in a difficult situation", "desperate", "lost", "discouraged", "feel lost", "feeling lost", "want to die", "i want to die", "kill myself", "end my life",
        ),
        "Satisfied / Relieved": (
            "thank", "thank you", "grateful", "appreciate", "relieved", "satisfied", "happy",
            "that helps", "much better", "good to know", "sounds good", "great", "perfect",
            "finally resolved", "i understand now", "problem solved", "you've been helpful",
        ),
    }
    lowered = text.lower()
    scores = {emotion: sum(lowered.count(word.lower()) for word in words) for emotion, words in groups.items()}
    emotion, hits = max(scores.items(), key=lambda item: item[1])
    if not hits:
        emotion = "Neutral / Calm"

    lowered = text.lower()
    low_intensity = ("a little", "slightly", "somewhat", "a bit")
    medium_intensity = ("really", "very", "quite", "so")
    high_intensity = ("extremely", "absolutely", "furious", "right now", "immediately")
    urgency_high = ("right now", "immediately", "emergency", "asap")
    urgency_medium = ("need it soon", "as soon as possible", "urgent")
    punctuation_level = 0
    if text.count("!") + text.count("！") >= 3 or text.isupper() and len(text) > 4:
        punctuation_level = 2
    elif text.count("!") + text.count("！") >= 1:
        punctuation_level = 1
    intensity_level = 2 if any(word in lowered for word in high_intensity) else 1 if any(word in lowered for word in medium_intensity) else 0
    if any(word in lowered for word in low_intensity) and intensity_level == 0:
        intensity_level = 0
    urgency_level = 2 if any(word in lowered for word in urgency_high) else 1 if any(word in lowered for word in urgency_medium) else 0
    # Do not treat normal sentence structure ("my", "I", "the", etc.) as
    # emotional repetition. Repetition should reflect repeated emphasis.
    repetition_stopwords = {"i", "my", "me", "the", "a", "an", "is", "are", "was", "were", "of", "and", "to", "for", "in", "on", "from", "this", "that", "it"}
    meaningful_words = [
        word.lower().strip(".,!?;:") for word in text.split()
        if word.lower().strip(".,!?;:") not in repetition_stopwords
    ]
    repeated_words = len(meaningful_words) - len(set(meaningful_words))
    repetition_level = 2 if repeated_words >= 3 else 1 if repeated_words >= 1 else 0
    level_score = max(intensity_level, punctuation_level, urgency_level, repetition_level)
    # An explicit emotional statement such as "I am sad" or "I am worried"
    # deserves at least medium support even without intensifiers or punctuation.
    if emotion not in {"Neutral / Calm", "Satisfied / Relieved"} and level_score == 0:
        level_score = 1
    if emotion == "Neutral / Calm" and punctuation_level >= 2 and not any(word in lowered for word in ("calm", "no rush")):
        emotion = "Frustrated"
    if emotion == "Neutral / Calm":
        return emotion, "low"
    return emotion, ("high" if level_score >= 2 else "medium" if level_score == 1 else "low")


def is_meaningful_user_message(text):
    """Identify messages with claim/identity information that may reset emotion context."""
    lowered = text.lower().strip()
    if re.search(r"\bCL[- ]?\d+\b|\bPOL[- ]?\d+\b|\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|@", text, re.I):
        return True
    meaningful_terms = (
        "claim", "case", "status", "denied", "denial", "rejected", "payment", "reimbursement",
        "document", "pathology", "office note", "submit", "deadline", "appeal", "review",
        "processing", "missing", "received", "next step", "how much", "why", "what is",
    )
    return any(term in lowered for term in meaningful_terms) and len(lowered.split()) >= 2


def is_out_of_scope_question(text):
    """Detect clearly non-insurance requests before generic NLU clarification."""
    lowered = text.lower().strip()
    insurance_terms = (
        "claim", "case", "policy", "insurance", "document", "denied", "denial",
        "appeal", "reimbursement", "payment", "premium", "deductible", "coverage",
        "provider", "upload", "submit", "deadline", "review", "office note",
        "pathology", "medical", "healthcare", "dental",
    )
    if any(term in lowered for term in insurance_terms):
        return False
    patterns = (
        r"\b(?:what is|calculate|solve|add|subtract|multiply|divide|plus|minus|times)\b.*\d",
        r"\b(?:weather|weather forecast|temperature outside|will it rain)\b",
        r"\b(?:recommend|suggest|watch|movie|film|tv show|song|music|restaurant|recipe|cook)\b",
        r"\b(?:joke|riddle|sports? score|who won|stock price|capital of)\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def extract_user_need(text, extracted):
    """Keep a short user-stated need for the conversation record."""
    if extracted.get("case_hint"):
        return extracted["case_hint"]
    return text.strip()[:500] or None


def record_intent(state, text):
    value = text.strip()[:500]
    if not value:
        return False
    classification = classify_intent(value, state)
    # Emotion by itself is not a substantive claim question. Keep the
    # emotional response, but record the primary intent as unclear so it is
    # not treated as a claim topic or included in the case summary.
    if classification.get("intent") == "emotional_support":
        markers = r"\b(?:worried|anxious|scared|upset|frustrated|overwhelmed|distressed|angry|furious|sad|devastated|concerned)\b"
        actionable_text = " ".join(
            part for part in re.split(r"[.!?;]+", value)
            if part.strip() and not re.search(markers, part, re.I)
        ).strip()
        actionable = classify_intent(actionable_text, state) if actionable_text else {"intent": "unclear"}
        if actionable.get("intent") in {"unclear", "emotional_support"}:
            classification["intent"] = "unclear"
    if classification["relation"] == "unclear":
        classification["relation"] = "new_intent"
    if classification["relation"] == "follow_up" and state["intents"]:
        intent = state["intents"][state["current_intent_index"]]
        intent["followups"].append({
            "text": value,
            "intent": classification["intent"],
            "confidence": classification["confidence"],
            "parent_intent_index": classification["parent_intent_index"],
            "term": classification.get("term"),
            "requested_field": classification.get("requested_field"),
            "answered": False,
        })
        intent["answered"] = False
    else:
        state["intents"].append({
            "relation": "new_intent",
            "intent": classification["intent"],
            "confidence": classification["confidence"],
            "parent_intent_index": None,
            "question": classification["question"],
            "term": classification.get("term"),
            "requested_field": classification.get("requested_field"),
            "followups": [],
            "answered": False,
        })
        state["current_intent_index"] = len(state["intents"]) - 1
    state["user_need"] = value
    return True


def mark_current_intent_answered(state):
    if state["current_intent_index"] is None:
        return
    intent = state["intents"][state["current_intent_index"]]
    for followup in intent["followups"]:
        followup["answered"] = True
    intent["answered"] = True


def answer_requires_followup(answer):
    """Keep an intent open when the agent asks for input or offers a next step."""
    lowered = (answer or "").lower()
    return any(phrase in lowered for phrase in (
        "what would you like", "which area", "which word or phrase",
        "would you like", "if you'd like", "if you prefer", "please provide",
        "please choose", "do you have any other questions", "would you like me to",
    ))


def is_conversation_acknowledgment(text):
    normalized = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    return normalized in {
        "ok", "okay", "got it", "understood", "thanks", "thank you", "thank you so much",
        "ty", "thx", "thanks a lot", "thanks a bunch", "much appreciated", "okie", "okey", "k", "kk", "k thx", "alright", "all right", "gotcha", "yep", "yup", "yeah", "yes", "yes please", "that works", "sounds fine", "that helps", "sounds good", "good", "great",
    }


def is_end_request(text):
    normalized = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    return normalized in {
        "i am done", "im done", "done", "finished", "i am finished", "im finished",
        "thats all", "that is all", "nothing else", "no more questions", "no further questions", "never mind", "nvm", "bye", "goodbye", "bye for now", "talk to you later",
    }


def accepts_previous_offer(state, text):
    normalized = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    if normalized not in {"yes", "yes please", "please", "that would help", "sure"}:
        return False
    if state.get("current_intent_index") is None or not state.get("intents"):
        return False
    answer = state["intents"][state["current_intent_index"]].get("answer", "").lower()
    return "if youd like" in answer or "would you like" in answer or "can also explain" in answer


def acknowledges_previous_answer(state, text):
    normalized = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    if normalized not in {"ok", "okay", "okie", "okey", "k", "kk", "got it", "understood", "thanks", "thank you", "ty", "thx", "thanks a lot", "much appreciated", "that helps", "sounds good", "good", "yes", "yes please"}:
        return False
    if state.get("current_intent_index") is None or not state.get("intents"):
        return False
    answer = state["intents"][state["current_intent_index"]].get("answer", "").lower()
    if not answer or "what would you like" in answer or "which area would you like" in answer:
        return False
    return True


def clarification_reprompt(state):
    """Rephrase a pending clarification instead of treating acknowledgment as closure."""
    attempts = state.get("clarification_attempts", 0)
    prompts = (
        "I still need a little more detail. Are you asking why the documents are missing, how to submit them, or whether they were received?",
        "To make sure I answer the right question, would you like an explanation of why the documents are missing, instructions for submitting them, or confirmation that they were received?",
        "Could you tell me which part you want me to check: the reason the documents are missing, the submission process, or receipt confirmation?",
    )
    return prompts[min(attempts, len(prompts) - 1)]


def _answer_intent_content(state, classification, question):
    """Read only the authorized fields needed for the confirmed intent."""
    if classification["intent"] == "unclear":
        if state.get("emotion") not in {None, "Neutral / Calm", "Satisfied / Relieved"} and not classification.get("ambiguous"):
            emotion = state.get("emotion")
            if emotion == "Anxious / Worried":
                return "I understand that this feels worrying. We can take it one step at a time.\n\nWhat would you like help with regarding your claim?"
            if emotion == "Frustrated":
                return "I’m sorry this has been frustrating. I’ll keep this clear and focused.\n\nWhat would you like help with regarding your claim?"
            if emotion == "Sad / Distressed":
                return "I’m sorry you’re dealing with this. We can go through it one step at a time.\n\nWhat would you like help with regarding your claim?"
            return "I understand this is difficult. I’m here to help.\n\nWhat would you like help with regarding your claim?"
        if "emotional_support" in (classification.get("secondary_intents") or []):
            emotion = state.get("emotion", "Neutral / Calm")
            if emotion == "Sad / Distressed":
                return "I’m sorry you’re feeling lost and discouraged. We can take this one step at a time.\n\nWhat would you like help with regarding your claim?"
            if emotion == "Anxious / Worried":
                return "I understand that this feels worrying. We can take it one step at a time.\n\nWhat would you like help with regarding your claim?"
            return "I understand that this process feels difficult. I’m here to help.\n\nWhat would you like help with regarding your claim?"
        clarification_phrases = ("i don't understand", "i do not understand", "i'm confused", "i am confused", "that is unclear", "not clear", "what do you mean")
        if any(phrase in question.lower() for phrase in ("what is", "what does", "what do", "meaning", "what does it mean")):
            return "I can explain the insurance terms in your claim, but I’m not sure which term you mean. Which word or phrase would you like me to explain?"
        if any(phrase in question.lower() for phrase in clarification_phrases):
            return "I may not have explained that clearly.\n\nWhich part would you like me to go over?"
        lowered = question.lower()
        ambiguous = classification.get("ambiguous")
        uncertain_terms = classification.get("uncertain_terms") or []
        if uncertain_terms:
            terms = ", ".join(f'"{str(term)}"' for term in uncertain_terms[:3])
            return f"I want to make sure I understand the term {terms}. Could you clarify what you would like me to check or explain about it?"
        if ambiguous == "document_type":
            return "When you say documents, would you like me to list the documents still missing, explain the document requirements, or explain how to submit them?"
        if ambiguous == "deadline_type":
            return "Are you asking about the deadline for submitting documents or the expected review time?"
        if ambiguous == "requested_action":
            return "Could you tell me whether you want to check the claim status, review documents, submit documents, or ask about a deadline?"
        if re.search(r"\bdocument", lowered):
            return (
                "When you say documents, would you like me to list the documents still missing, "
                "explain the document requirements, or explain how to submit them?"
            )
        if re.search(r"\b(?:deadline|due date|when should|by when|how soon)\b", lowered):
            return "Are you asking about the deadline for submitting documents or the expected review time?"
        state["unrelated_question_count"] += 1
        state.setdefault("handoff_conditions", {})["unclear_questions"] = state["unrelated_question_count"]
        directions = "I can help with claim status, denial reasons, missing documents, document submission, deadlines, processing times, payments, appeals, next steps, or a human claims representative."
        if state["unrelated_question_count"] > 3:
            request_human_transfer(state, "The agent could not determine the user's request after more than three unclear or unrelated questions.", "unclear_questions")
            return f"I’m not able to answer that from the claim documents I have.\n\n{directions}\n\nWould you like me to connect you with a human claims representative?"
        return f"I’m not quite sure I understood what you need yet.\n\n{directions}\n\nWhich area would you like to start with?"
    if classification["intent"] == "emotional_support":
        emotion = state.get("emotion", "Neutral / Calm")
        if emotion == "Anxious / Worried":
            return "I understand that this situation feels worrying. We can take it one step at a time and look at the information available for your claim.\n\nWhat would you like to ask about?"
        if emotion == "Frustrated":
            return "I’m sorry this has been frustrating. I’ll keep the next steps clear and avoid making you repeat information.\n\nWhat would you like to ask about your claim?"
        if emotion == "Angry":
            return "I hear how upsetting this has been. I’ll keep this focused and help identify the next useful step.\n\nWhat would you like to ask about your claim?"
        if emotion == "Sad / Distressed":
            return "I’m sorry you’re dealing with this. We can slow down and go through the claim information one step at a time.\n\nWhat would you like to ask about?"
        return "I understand that you have concerns about your case. I’m here to help you work through the available claim information.\n\nWhat would you like to ask about?"
    if not state.get("case_access_authorized"):
        return "Before I can answer questions about this claim, I need to verify the case information first."
    claims = load_json("claims.json")
    claim = next((item for item in claims if item.get("case_id") == state.get("matched_case_id")), None)
    if not claim or claim.get("party_id") != state.get("verified_party_id"):
        return "I could not find the authorized case information. Would you like me to transfer you to a human claims representative?"
    guidance = load_json("required_document_guideline.json")
    intent = classification["intent"]
    requested_field = classification.get("requested_field") or infer_requested_field(question, intent)
    if intent == "term_explanation":
        term = classification.get("term") or "the requested term"
        normalized_term = re.sub(r"\s+", " ", str(term).lower()).strip()
        if "pathology report" in normalized_term:
            lookup_term, display_term = "original pathology report", "pathology report"
        elif "office note" in normalized_term:
            lookup_term, display_term = "treating provider office note", "office note"
        else:
            lookup_term, display_term = normalized_term, str(term)
        definition = guidance.get("document_guidance", {}).get(lookup_term, {}).get("en")
        if not definition:
            definition = guidance.get("case_type_guidance", {}).get(term, {}).get("en")
        if not definition:
            return f"I could not find an approved explanation for the term \"{display_term}\" in the available insurance guidance. Would you like me to connect you with a human claims representative?"
        plain_definitions = {
            "pathology report": "a medical report that describes the results of laboratory or tissue testing",
            "office note": "a provider's clinical note documenting a visit, assessment, and treatment or follow-up",
        }
        plain_definition = plain_definitions.get(display_term.lower(), "an insurance document used in claim review")
        return f"A {display_term} is {plain_definition}. It is relevant here because the claim lists it as a document requested for review."
    documents = ", ".join(claim.get("documents_needed", [])) or "no additional documents listed"
    if intent == "status_inquiry":
        if not claim.get("status"):
            return "I could not find the claim status in the available documents. Would you like me to transfer you to a human claims representative?"
        return f"Certainly. Your claim {claim['case_id']} is currently {claim['status']}."
    if intent == "denial_question":
        if not claim.get("denial_reason"):
            return "I could not find a denial reason in the available documents. Would you like me to transfer you to a human claims representative?"
        denial_answer = f"The claim was denied because {claim.get('denial_reason', 'the submitted materials did not meet the review requirements')}."
        if classification.get("secondary_intents"):
            return denial_answer
        return f"{denial_answer}\n\nIf you’d like, I can also explain which documents may help with the next step."
    if intent == "missing_documents":
        if not claim.get("documents_needed"):
            return "I could not find a list of missing documents in the available claim records. Would you like me to transfer you to a human claims representative?"
        return f"Certainly. The documents currently listed for claim {claim['case_id']} are: {documents}."
    if intent == "document_submission":
        if requested_field == "fax_submission_allowed":
            fax_guidance = guidance.get("fax_submission_guidance", {}).get("en")
            if not fax_guidance:
                return "I could not confirm whether fax submission is allowed in the available insurance guidance. Would you like me to connect you with a human claims representative?"
            return f"Certainly. {fax_guidance}"
        text = guidance.get("default_guidance", {}).get("en")
        if not text:
            return "I could not find document submission instructions in the available guidelines. Would you like me to transfer you to a human claims representative?"
        return f"Of course. {text}"
    if intent == "document_requirements":
        if requested_field == "document_format":
            format_guidance = guidance.get("document_format_guidance", {}).get("en")
            if not format_guidance:
                return "I could not confirm a file-format requirement in the available insurance guidance. Would you like me to connect you with a human claims representative?"
            return f"Certainly. {format_guidance}"
        if requested_field == "signature_requirement":
            signature_guidance = guidance.get("signature_guidance", {}).get("en")
            if not signature_guidance:
                return "I could not confirm a signature requirement in the available insurance guidance. Would you like me to connect you with a human claims representative?"
            return f"Certainly. {signature_guidance}"
        case_guidance = guidance.get("case_type_guidance", {}).get(claim.get("case_type"), {}).get("en", "")
        return f"Certainly. {case_guidance or guidance['default_guidance']['en']}"
    if intent == "document_alternative":
        alternatives = guidance.get("document_alternative_guidance", {}).get("default", {}).get("en", "")
        if not alternatives:
            return "I could not find an approved document alternative in the available guidelines. Would you like me to transfer you to a human claims representative?"
        return f"I understand. {alternatives}"
    if intent == "submission_deadline":
        if requested_field == "document_submission_deadline":
            return "I could not confirm a document-submission deadline in the available claim records. The recorded deadline is for the appeal, not document submission. Would you like me to connect you with a human claims representative?"
        deadline = claim.get("appeal_deadline")
        if not deadline:
            return "I could not find a submission deadline in the available records. Would you like me to transfer you to a human claims representative?"
        return f"Please submit the requested materials as soon as possible. The current recorded appeal deadline is {deadline}."
    if intent == "processing_time":
        timing = guidance.get("claim_followup_settings", {}).get("average_processing_time_after_submission", {}).get("en")
        if not timing:
            return "I could not find a processing-time estimate in the available guidelines. Would you like me to transfer you to a human claims representative?"
        timing = re.sub(r"\busually\s+", "", timing, count=1, flags=re.I).strip()
        return f"Once the missing files are received and readable, review usually takes {timing}. Another review cycle may still be needed."
    if intent == "receipt_confirmation":
        if requested_field == "upload_receipt_status":
            return "I could not confirm receipt of the uploaded documents from the available claim records. Would you like me to connect you with a human claims representative?"
        return f"After upload, there may be a short intake delay before the claim status changes. If it remains unchanged after a few business days, support can check whether {documents} were attached correctly."
    if intent == "appeal_question":
        deadline = claim.get("appeal_deadline")
        if not deadline:
            return "I could not find an appeal deadline in the available claim records. Would you like me to transfer you to a human claims representative?"
        from datetime import date
        try:
            expired = date.fromisoformat(str(deadline)) < date.today()
        except ValueError:
            expired = False
        deadline_note = " That deadline has passed." if expired else ""
        return f"You may be able to request reconsideration. The recorded appeal deadline is {deadline}.{deadline_note} Please include the missing supporting materials: {documents}."
    if intent == "payment_question":
        if requested_field == "amount_owed":
            amount = claim.get("net_fee")
            if amount is None:
                return "I could not find an amount owed in the available claim records. Would you like me to connect you with a human claims representative?"
            return f"The currently recorded amount owed is ${amount}."
        amount = claim.get("expected_reimbursement_amount")
        if amount is None:
            return "I could not find a reimbursement amount in the available claim records. Would you like me to transfer you to a human claims representative?"
        return f"The currently recorded expected reimbursement is ${amount}."
    if intent == "case_summary":
        if not all(claim.get(field) for field in ("case_id", "case_type", "created_at", "status")):
            return "I could not find enough information to prepare a complete case summary. Would you like me to transfer you to a human claims representative?"
        return f"Certainly. Claim {claim['case_id']} is a {claim.get('case_type', 'claim')} claim created on {claim.get('created_at', 'an unspecified date')}. Its current status is {claim.get('status', 'under review')}."
    if intent == "next_steps":
        if not claim.get("documents_needed"):
            return "I could not find specific next steps in the available claim records. Would you like me to transfer you to a human claims representative?"
        return f"The recommended next step is to provide the requested materials: {documents}. Please use the member portal or claim upload link when possible."
    if intent == "human_transfer":
        state["human_transfer"] = True
        state["awaiting_human_transfer"] = False
        _, ticket_message = get_handoff_ticket_response(state, state.get("human_transfer_reason") or "The user requested a human claims representative.")
        return f"{ticket_message} I’m transferring you to a human claims representative now."
    return "Thank you for your question. I will review the available claim guidance and help with the next appropriate step."


def _answer_intent_legacy(state, classification, question):
    """Apply an emotion-aware response strategy to the grounded answer."""
    state["last_requested_field"] = classification.get("requested_field") or infer_requested_field(
        question, classification.get("intent")
    )
    classification["requested_field"] = state["last_requested_field"]
    answer = _answer_intent_content(state, classification, question)
    level = state.get("emotion_level", "low")
    if classification.get("intent") == "emotional_support":
        state["emotional_intent_streak"] = state.get("emotional_intent_streak", 0) + 1
        state["emotional_intent_count"] = state.get("emotional_intent_count", 0) + 1
        state.setdefault("handoff_conditions", {})["emotional_intents"] = state["emotional_intent_count"]
        if any(phrase in question.lower() for phrase in ("want to die", "kill myself", "end my life")):
            request_human_transfer(state, "The user expressed a potential self-harm concern.", "safety_concern")
            return "I’m really sorry you’re feeling this way. Your immediate safety matters, and I want to connect you with a human representative right away."
        if state.get("emotional_intent_count", 0) >= 3:
            request_human_transfer(state, "The user expressed emotional distress in three or more emotional intents.", "emotional_intents")
            return "I understand this has been very difficult, and we can take this one step at a time. Would you like me to connect you with a human claims representative?"
    else:
        state["emotional_intent_streak"] = 0
    if classification.get("intent") == "emotional_support":
        if level == "high":
            if state.get("emotional_intent_count", 0) >= 3:
                request_human_transfer(state, "The user expressed emotional distress in three consecutive intents without a specific claim question.", "emotional_intents")
                return "I can hear how stressful this feels, and we can take this one step at a time. Let’s start with the part of your claim you most need help understanding.\n\nWould you like me to connect you with a human claims representative?"
            return "I can hear how stressful this feels, and we can take this one step at a time. Let’s start with the part of your claim you most need help understanding."
        if level == "medium":
            return "I understand that this situation is worrying. A helpful next step is to tell me whether you want to review the claim status, denial reason, missing documents, or next steps."
        return "I understand that this situation is worrying. You can ask me about the claim status, denial reason, missing documents, or next steps."
    if level == "high" and classification["intent"] != "human_transfer":
        if state.get("emotional_intent_streak", 0) >= 3:
            request_human_transfer(state, "The user expressed emotional distress in three consecutive intents without a specific claim question.", "emotional_intents")
            return f"I understand this has been very difficult. {answer}\n\nWould you like me to connect you with a human claims representative?"
        return f"I understand this is very important and frustrating. {answer}"
    if level == "medium":
        if state.get("emotion") == "Frustrated":
            return f"I’m sorry this has been frustrating. {answer} I will help you with the next clear step."
        if state.get("emotion") == "Angry":
            return f"I understand how upsetting this is. {answer} I will help you with the next clear step."
        return f"I understand your concern. {answer} I will help you with the next clear step."
    if level == "low" and state.get("emotion_explicit"):
        return f"I understand this is difficult. {answer}"
    return answer


def request_human_transfer(state, reason, condition=None):
    """Register one escalation condition without mixing its counter with others."""
    state["awaiting_human_transfer"] = True
    state["human_transfer_reason"] = reason
    conditions = state.setdefault("handoff_conditions", {})
    if condition:
        triggered = conditions.setdefault("triggered_conditions", [])
        if condition not in triggered:
            triggered.append(condition)


def create_handoff_ticket(state, reason=None):
    """Create a structured handoff record for a human representative."""
    if state.get("handoff_ticket"):
        return state["handoff_ticket"]
    recent_questions = []
    for intent in state.get("intents", [])[-3:]:
        question = intent.get("question")
        if question:
            recent_questions.append(question)
        for followup in intent.get("followups", [])[-2:]:
            if followup.get("text"):
                recent_questions.append(followup["text"])
    verified = bool(state.get("gates", {}).get("VERIFY_ID") and state.get("verified_party_id"))
    state["handoff_ticket"] = {
        "ticket_id": f"HI-{uuid.uuid4().hex[:10].upper()}",
        "reason": reason or state.get("human_transfer_reason") or "The user requested human assistance.",
        "phase": state.get("phase"),
        "verified": verified,
        "case_id": state.get("matched_case_id") if state.get("case_access_authorized") else None,
        "summary": "Recent customer questions: " + "; ".join(recent_questions[-5:]) if recent_questions else "No customer question summary is available.",
    }
    return state["handoff_ticket"]


def get_handoff_ticket_response(state, reason=None):
    """Get the conversation's single handoff ticket and whether it was new."""
    already_exists = bool(state.get("handoff_ticket"))
    ticket = create_handoff_ticket(state, reason)
    if already_exists:
        prefix = f"A human-assistance ticket has already been created for this conversation. Your ticket number is {ticket['ticket_id']}."
    else:
        prefix = f"Your human-assistance ticket {ticket['ticket_id']} has been created."
    return ticket, prefix


def _classify_intent(text, state=None):
    """Classify a user question into one supported claim intent."""
    lowered = text.lower()
    # Tolerate common typing mistakes before intent matching while preserving
    # the original user wording in the recorded conversation.
    lowered = re.sub(r"\bsubmiit\b|\bsubmitt\b|\bsubmt\b", "submit", lowered)
    if lowered.strip(" .!?,") in {"hi", "hello", "hey", "thanks", "thank you", "ty", "thx", "ok", "okay", "okie", "okey", "good", "great"}:
        return {"relation": "new_intent", "intent": "unclear", "confidence": 0.2, "parent_intent_index": None, "question": text.strip()}
    semantic_action = ((state or {}).get("last_nlu") or {}).get("action")
    semantic_primary_intent = ((state or {}).get("last_nlu") or {}).get("primary_intent")
    semantic_requested_field = ((state or {}).get("last_nlu") or {}).get("requested_field")
    if semantic_requested_field == "document_submission_deadline":
        semantic_primary_intent = "submission_deadline"
    if semantic_primary_intent in INTENT_CATEGORIES:
        term = None
        if semantic_primary_intent == "term_explanation":
            lowered_terms = text.lower()
            if "pathology report" in lowered_terms:
                term = "original pathology report"
            elif "office note" in lowered_terms:
                term = "treating provider office note"
        return {
            "relation": "new_intent",
            "intent": semantic_primary_intent,
            "confidence": 0.96,
            "parent_intent_index": None,
            "question": text.strip(),
            "term": term,
        }
    semantic_intent = {
        "check": "status_inquiry", "identify_missing": "missing_documents",
        "submit": "document_submission", "confirm_receipt": "receipt_confirmation",
        "ask_deadline": "submission_deadline", "ask_timing": "processing_time",
        "ask_payment": "payment_question", "appeal": "appeal_question",
        "summarize": "case_summary", "next_step": "next_steps",
        "request_human": "human_transfer",
        "review_documents": "document_requirements",
    }.get(semantic_action)
    if semantic_intent:
        return {"relation": "new_intent", "intent": semantic_intent, "confidence": 0.94, "parent_intent_index": None, "question": text.strip()}
    if state and state.get("intents") and state.get("current_intent_index") is not None:
        previous_answer = state["intents"][state["current_intent_index"]].get("answer", "").lower()
        if "which word or phrase would you like me to explain" in previous_answer:
            guideline = load_json("required_document_guideline.json")
            normalized = re.sub(r"[^a-z ]", " ", lowered)
            for canonical in guideline.get("document_guidance", {}):
                simplified = re.sub(r"^(original|treating provider|supplemental)\s+", "", canonical.lower())
                if simplified in normalized or canonical.lower() in normalized:
                    return {
                        "relation": "follow_up",
                        "intent": "term_explanation",
                        "confidence": 0.98,
                        "parent_intent_index": state.get("current_intent_index"),
                        "question": text.strip(),
                        "term": canonical,
                    }

    # Resolve explicit emotional concerns and supported insurance questions before
    # treating the same wording as a generic terminology request.
    domain_priority_rules = {
        "emotional_support": (
            "i am worried", "i'm worried", "worried about my case",
            "i am concerned", "i'm concerned", "concerned about my case",
            "i am anxious", "i'm anxious", "i am scared", "i'm scared",
        ),
        "appeal_question": ("can i appeal", "how do i appeal", "appeal this denial", "how do i challenge this decision"),
        "submission_deadline": (
            "deadline", "ddl", "when do i need to submit", "when should i send",
            "when should i submit", "when should i submit these files",
            "when should i submit the documents", "when should i send these files",
            "when should i send the documents", "how soon do i need to send",
            "how soon do i need to submit", "when is it due",
        ),
        "status_inquiry": (
            "claim status", "case status", "what is the status", "what's the status",
            "what is the current status", "what's the current status",
            "status update", "where does my claim stand", "is my claim still being processed",
        ),
        "denial_question": (
            "why was my claim denied", "why was the claim denied", "denial reason",
            "reason for denial", "why is my claim denied",
            "turned down", "not approved", "was rejected", "got rejected",
            "claim was refused", "claim was declined", "insurance refused",
            "did not approve", "was not approved",
        ),
        "missing_documents": (
            "what documents are missing", "which documents are missing",
            "what document is missing", "did i miss any document", "what documents do i need",
            "what documents are still missing", "which documents are still missing",
            "what is still missing", "what is missing from my claim", "did i miss any paperwork",
            "which records do i still need",
        ),
        "document_submission": (
            "how do i submit", "how can i submit", "how should i submit",
            "please explain how i can submit", "explain how i can submit",
            "please explain how to submit", "explain how to submit",
            "how do i send", "how can i send", "how should i send",
            "how do i upload", "how can i upload", "where do i upload",
            "where can i submit", "where should i send",
        ),
        "document_alternative": (
            "what can i submit instead", "is there an alternative",
            "what can replace", "what can i use instead",
        ),
        "document_requirements": (
            "what documents are required", "which documents are required",
            "what documentation is required", "which documentation is required",
            "what documents do i need to provide", "what documents do i need to submit",
            "what paperwork is required", "what records are required",
            "does each page need a signature", "what information must be visible",
        ),
        "receipt_confirmation": (
            "did you receive the documents", "did you receive my documents",
            "did you get the documents", "did you get my documents",
            "were my documents received", "have my documents been received",
            "did you receive what i uploaded", "did you get what i uploaded",
            "was my upload received", "can you confirm receipt",
        ),
        "processing_time": (
            "how long will the review take", "how long does the review take",
            "processing time", "review time", "when will the claim be processed",
            "when will it be processed", "how soon will i hear back",
        ),
        "payment_question": (
            "how much will i receive", "payment amount", "reimbursement amount",
            "should i pay", "do i need to pay", "will i have to pay", "do i owe",
            "what do i owe", "out of pocket", "how much does this cost",
            "will this cost me", "what is my responsibility",
        ),
        "next_steps": ("what should i do next", "what are the next steps", "what do i do now"),
        "human_transfer": ("speak with someone", "talk to a person", "human representative"),
        "other_claim_inquiry": (
            "another claim", "different claim", "other claim", "switch claims",
            "look at my other claim", "check a different claim",
        ),
    }
    denial_markers = ("denied", "rejected", "turned down", "not approved", "refused", "declined")
    denial_question_markers = ("why", "reason", "what happened", "what went wrong")
    if any(phrase in lowered for phrase in domain_priority_rules["human_transfer"]):
        return {
            "relation": "new_intent",
            "intent": "human_transfer",
            "confidence": 0.98,
            "parent_intent_index": None,
            "question": text.strip(),
        }
    if (
        any(marker in lowered for marker in denial_markers)
        and (
            any(marker in lowered for marker in denial_question_markers)
            or any(word in lowered for word in ("claim", "reimbursement", "payment"))
        )
    ):
        return {
            "relation": "new_intent",
            "intent": "denial_question",
            "confidence": 0.95,
            "parent_intent_index": None,
            "question": text.strip(),
        }
    if re.search(r"\b(worr+ied|anxious|concerned|scared|nervous|upset|frustrated|distressed|overwhelmed|hopeless|angr+y|agary)\b", lowered) or any(phrase in lowered for phrase in ("want to die", "kill myself", "end my life")):
        return {
            "relation": "new_intent",
            "intent": "emotional_support",
            "confidence": 0.96,
            "parent_intent_index": None,
            "question": text.strip(),
        }
    matched_domain_intents = [
        intent for intent, phrases in domain_priority_rules.items()
        if any(phrase in lowered for phrase in phrases)
    ]
    if matched_domain_intents:
        intent = min(matched_domain_intents, key=lambda item: INTENT_PRIORITY_RANK.get(item, len(INTENT_PRIORITY)))
        return {
            "relation": "new_intent",
            "intent": intent,
            "confidence": 0.96,
            "parent_intent_index": None,
            "question": text.strip(),
        }

    explanation_request = any(phrase in lowered for phrase in ("what is", "what does", "what do", "meaning", "mean", "explain"))
    if explanation_request:
        if re.search(r"\bdeductible\b", lowered):
            return {
                "relation": "new_intent",
                "intent": "term_explanation",
                "confidence": 0.9,
                "parent_intent_index": None,
                "question": text.strip(),
                "term": "deductible",
            }
        guideline = load_json("required_document_guideline.json")
        terms = {}
        for canonical in guideline.get("document_guidance", {}):
            terms[canonical.lower()] = canonical
            simplified = re.sub(r"^(original|treating provider|supplemental)\s+", "", canonical.lower())
            terms[simplified] = canonical
        normalized = re.sub(r"[^a-z ]", " ", lowered).replace("the ", "").replace("a ", "").replace("an ", "")
        matched_term = next((canonical for phrase, canonical in terms.items() if phrase in normalized), None)
        if not matched_term:
            words = normalized.split()
            for phrase, canonical in terms.items():
                candidate = " ".join(word for word in words if word not in {"what", "is", "does", "do", "mean", "meaning", "of", "the"})
                if SequenceMatcher(None, candidate, phrase).ratio() >= 0.65:
                    matched_term = canonical
                    break
        if matched_term:
            return {
                "relation": "new_intent",
                "intent": "term_explanation",
                "confidence": 0.9,
                "parent_intent_index": None,
                "question": text.strip(),
                "term": matched_term,
            }
        return {
            "relation": "new_intent",
            "intent": "unclear",
            "confidence": 0.3,
            "parent_intent_index": None,
            "question": text.strip(),
            "term": None,
        }
    if state and state.get("intents") and lowered.strip(" ?!.,") in {"yes", "yes please", "please", "that would help", "sure"}:
        parent_index = state.get("current_intent_index")
        parent = state["intents"][parent_index]
        previous_answer = parent.get("answer", "").lower()
        if "documents may help" in previous_answer or "explain which documents" in previous_answer:
            return {
                "relation": "follow_up",
                "intent": "missing_documents",
                "confidence": 0.98,
                "parent_intent_index": parent_index,
                "question": text.strip(),
            }
        if "human claims representative" in previous_answer or "connect you" in previous_answer:
            return {
                "relation": "follow_up",
                "intent": "human_transfer",
                "confidence": 0.98,
                "parent_intent_index": parent_index,
                "question": text.strip(),
            }
    if state and state.get("intents") and lowered.strip(" ?!.") in {"why", "how come"}:
        parent = state["intents"][state["current_intent_index"]]
        followup_intent = "denial_question" if parent.get("intent") == "status_inquiry" else parent.get("intent", "denial_question")
        return {
            "relation": "follow_up",
            "intent": followup_intent,
            "confidence": 0.95,
            "parent_intent_index": state.get("current_intent_index"),
            "question": text.strip(),
        }
    high_priority_rules = {
        "emotional_support": (
            "i am worried", "i'm worried", "worried about my case", "concerned about my case",
            "i am concerned", "i'm concerned", "i am anxious", "i'm anxious", "i am scared",
            "i'm scared", "i feel worried", "i feel anxious", "i need reassurance",
        ),
        "term_explanation": (
            "what is pathology report", "what is a pathology report", "what is the pathology report", "what does pathology report mean", "what does the pathology report mean",
            "what is office note", "what is an office note", "what is the office note", "what does office note mean", "what does the office note mean",
            "what is repair estimate", "what is a repair estimate", "what is the repair estimate", "what does repair estimate mean", "what does the repair estimate mean",
            "what is an accident scene photo", "what does this term mean", "what is a deductible",
        ),
        "unclear": (
            "i don't understand", "i do not understand", "i'm confused", "i am confused",
            "that is unclear", "not clear", "what do you mean", "i don't follow",
        ),
        "missing_documents": (
            "did i miss any document", "did i miss a document", "did i miss any documents",
            "what document is missing", "what documents are missing", "which document is missing",
            "which documents are missing", "missing document", "missing documents",
            "what else do i need", "what else is needed",
        ),
        "document_alternative": (
            "what can i submit instead", "what else can i submit", "alternative document",
            "another document", "do i need a replacement",
        ),
        "status_inquiry": ("where does my claim stand", "where is my claim at", "claim update"),
        "processing_time": (
            "how long will the review take", "how long does the review take",
            "how long will processing take", "how long does processing take",
            "how long after i submit", "how soon after i submit",
        ),
    }
    matched_high_priority = [
        intent for intent, phrases in high_priority_rules.items()
        if any(phrase in lowered for phrase in phrases)
    ]
    if matched_high_priority:
        intent = min(matched_high_priority, key=lambda item: INTENT_PRIORITY_RANK.get(item, len(INTENT_PRIORITY)))
        is_followup = bool(state and state.get("intents") and any(marker in lowered for marker in ("also", "what about", "follow up", "additionally")))
        return {
            "relation": "follow_up" if is_followup else "new_intent",
            "intent": intent,
            "confidence": 0.95,
            "parent_intent_index": state.get("current_intent_index") if is_followup else None,
            "question": text.strip(),
            "term": next((term for term in ("pathology report", "office note", "repair estimate", "supplemental accident scene photos") if term in lowered), "the requested term"),
        }
    if ChatPromptTemplate and ChatGoogleGenerativeAI and os.getenv("GEMINI_API_KEY"):
        semantic_rules = json.dumps({"prompt_policy": INTENT_PROMPT_POLICY, "intent_rules": INTENT_SEMANTIC_RULES}, ensure_ascii=False)
        semantic_rules = semantic_rules.replace("{", "{{").replace("}", "}}")
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Understand the user's meaning, including paraphrases, synonyms, abbreviations, and natural conversational wording; do not require exact keywords. First determine what the user is asking for: when means deadline or processing time; how or where means document submission; whether something was received means receipt confirmation; what documents are needed means missing documents or document requirements; why something was rejected means denial question; how much or what is owed means payment question. Do not classify based on a shared keyword alone. Classify based on the user's requested action and question type. Interpret question words such as why, when, how, what, where, and whether before matching topic keywords. For example, 'When is the deadline to submit the documents?' is submission_deadline, not document_submission, because the requested action is asking when. 'How do I submit the missing documents?' is document_submission, not missing_documents, because the requested action is asking how. Use the following positive and exclusion evidence for each intent: " + semantic_rules + " If positive and exclusion evidence conflict, prefer the intent whose positive evidence describes the user's actual requested action, then apply the configured intent priority. Identify all distinct requests in the same message. Return the primary intent in intent and any additional supported request in secondary_intent or secondary_intents. Each distinct intent must be answered separately. Emotional support may be secondary to a substantive insurance question and must not replace the substantive answer. Interpret these as denial_question: 'why was my claim denied', 'my claim was rejected', 'the reimbursement was turned down', 'the insurer did not approve it', and 'why was payment refused'. Interpret these as payment_question: 'how much will I receive', 'what is my reimbursement amount', 'how much was paid', and 'what do I owe'. Priority rule: if a sentence contains both a reimbursement or payment term and a denial signal such as rejected, denied, refused, declined, turned down, or not approved, classify it as denial_question unless the user is explicitly asking for the amount. You are only a classifier inside a bounded workflow. Never access claim facts, authorize identity, match ownership, change phases, or answer the user. Return ONLY valid JSON with keys intent, secondary_intent, secondary_intents, relation, confidence, parent_intent_index, question, case_resolution, next_allowed_action, needs_clarification. relation must be new_intent, follow_up, or unclear. intent and every secondary intent must be one of: " + ", ".join(INTENT_CATEGORIES) + ". case_resolution must be one of: not_provided, provided, matched, not_found, ownership_mismatch, not_required. next_allowed_action must be one of: verify_identity, request_case_id, request_intent, confirm_potential_intent, answer_from_documents, ask_clarification, offer_human_transfer, start_post_process. confidence must be a number from 0 to 1. needs_clarification must be true or false. Preserve the user's question in question. The Python workflow controller will validate and override authorization-related fields."),
            ("human", "Existing intents:\n{intents}\n\nCurrent question:\n{text}"),
        ])
        chain = prompt | ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), temperature=0) | llm_content_text
        try:
            existing = json.dumps((state or {}).get("intents", []), ensure_ascii=False)
            llm_output = chain.invoke({"intents": existing, "text": text})
            cleaned_output = re.sub(r"^```(?:json)?\s*|\s*```$", "", llm_output.strip(), flags=re.I)
            result = json.loads(cleaned_output)
            if result.get("intent") in INTENT_CATEGORIES and result.get("relation") in {"new_intent", "follow_up", "unclear"}:
                confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
                if confidence < 0.7:
                    result["intent"] = "unclear"
                    result["relation"] = "new_intent"
                return {
                    "relation": result["relation"],
                    "intent": result["intent"],
                    "confidence": confidence,
                    "parent_intent_index": result.get("parent_intent_index"),
                    "question": result.get("question") or text.strip(),
                }
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            if os.getenv("REQUIRE_LLM_INTENT", "false").lower() == "true":
                raise RuntimeError(f"LLM intent classification failed; local fallback is disabled. Output: {str(locals().get('llm_output', ''))[:500]}")
            pass

    lowered = text.lower()
    rules = {
        "status_inquiry": ("status", "where is my claim", "claim update"),
        "appeal_question": ("appeal", "appealing", "challenge the denial"),
        "denial_question": ("denied", "denial", "rejected", "why was"),
        "missing_documents": ("what documents", "missing document", "what do i need"),
        "document_alternative": ("instead", "alternative", "do not have", "don't have", "replacement"),
        "document_submission": ("submit", "send", "upload", "portal", "fax", "mail"),
        "document_requirements": ("format", "file type", "pdf", "scan", "clear enough"),
        "submission_deadline": ("deadline", "when do i need", "how soon do i need"),
        "processing_time": ("how long", "processing time", "review time", "how soon"),
        "receipt_confirmation": ("received", "receipt", "confirmation", "did you get"),
        "payment_question": ("how much", "payment", "pay", "owe", "cost", "out of pocket", "reimbursement", "paid", "amount"),
        "case_summary": ("summarize", "summary", "overview", "what happened"),
        "next_steps": ("what should i do", "next step", "what do i do now"),
        "human_transfer": ("representative", "human", "agent", "speak with someone", "supervisor"),
    }
    for intent, keywords in rules.items():
        if any(keyword in lowered for keyword in keywords):
            relation = "new_intent"
            parent_index = None
            markers = ("also", "what about", "another question", "follow up", "additionally")
            if state and state.get("intents") and any(marker in lowered for marker in markers):
                relation = "follow_up"
                parent_index = state.get("current_intent_index")
            return {"relation": relation, "intent": intent, "confidence": 0.7, "parent_intent_index": parent_index, "question": text.strip()}
    return {"relation": "new_intent", "intent": "unclear", "confidence": 0.35, "parent_intent_index": None, "question": text.strip()}


WORKFLOW_CASE_RESOLUTIONS = {"not_provided", "provided", "matched", "not_found", "ownership_mismatch", "not_required"}
WORKFLOW_ACTIONS = {
    "verify_identity", "request_case_id", "request_intent", "confirm_potential_intent",
    "answer_from_documents", "ask_clarification", "offer_human_transfer", "start_post_process",
}


def validate_workflow_contract(contract):
    """Validate the LLM/classifier result before the workflow can use it."""
    if contract.get("intent") not in INTENT_CATEGORIES:
        return None
    if contract.get("relation") not in {"new_intent", "follow_up", "unclear"}:
        return None
    try:
        confidence = float(contract.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    if contract.get("case_resolution") not in WORKFLOW_CASE_RESOLUTIONS:
        return None
    if contract.get("next_allowed_action") not in WORKFLOW_ACTIONS:
        return None
    contract["confidence"] = confidence
    contract["needs_clarification"] = bool(contract.get("needs_clarification"))
    return contract


def decide_next_action(state, contract):
    """Deterministically enforce gates; the LLM cannot advance a phase."""
    if state.get("emotion_level") == "high" or state.get("awaiting_human_transfer"):
        return "offer_human_transfer"
    if not state["gates"].get("VERIFY_ID"):
        return "verify_identity"
    if not state["gates"].get("RESOLVE_INTENT"):
        if contract["case_resolution"] in {"not_provided", "not_found", "ownership_mismatch"}:
            return "request_case_id"
        return "request_intent"
    if contract["needs_clarification"] or contract["intent"] == "unclear":
        return "ask_clarification"
    if state.get("phase") == "POST_PROCESS":
        return "start_post_process"
    return "answer_from_documents"


def _classify_intent_impl(text, state=None):
    """Return a validated bounded workflow contract."""
    cached = state.get("last_nlu") if state and state.get("_unified_nlu_text") == text else None
    if cached:
        raw = {
            "relation": "new_intent",
            "intent": cached.get("primary_intent", "unclear"),
            "confidence": 0.95,
            "parent_intent_index": None,
            "question": text.strip(),
            "ambiguous": cached.get("ambiguous"),
            "uncertain_terms": cached.get("uncertain_terms", []),
            "requested_field": cached.get("requested_field"),
            "secondary_intents": normalize_secondary_intents(
                cached.get("secondary_intents", []), cached.get("secondary_intent")
            ),
            "term": cached.get("term"),
        }
    else:
        # All intent interpretation now goes through the unified Gemini NLU
        # contract. Do not re-enter the legacy keyword classifier here.
        nlu = unified_nlu(text, state)
        if nlu.get("api_error"):
            raw = {
                "relation": "new_intent",
                "intent": "unclear",
                "confidence": 0.0,
                "parent_intent_index": None,
                "question": text.strip(),
            }
        else:
            # Preserve clear multi-request wording even if the API asks for
            # clarification about the exact document subtype.
            lowered = text.lower()
            asks_document_review = bool(re.search(
                r"\b(?:check|review)\b[^.!?\n]{0,50}\bdocuments?\b|\bdocuments?\b[^.!?\n]{0,50}\b(?:check|review)\b",
                lowered,
            ))
            asks_deadline = bool(re.search(
                r"\b(?:deadline|due date|when should|when do i need|by when|how soon)\b",
                lowered,
            ))
            if asks_document_review and asks_deadline:
                nlu["primary_intent"] = "missing_documents"
                nlu["secondary_intents"] = normalize_secondary_intents(
                    nlu.get("secondary_intents"), "submission_deadline"
                )
                nlu["confidence"] = max(float(nlu.get("confidence", 0.0) or 0.0), 0.95)
                nlu["needs_clarification"] = False
            asks_case_check = bool(re.search(
                r"\b(?:check|review|look at)\s+(?:(?:my|the)\s+)?(?:case|claim)\b",
                lowered,
            ))
            if asks_case_check and asks_document_review:
                nlu["primary_intent"] = "status_inquiry"
                nlu["secondary_intents"] = normalize_secondary_intents(
                    nlu.get("secondary_intents"), "missing_documents"
                )
                nlu["confidence"] = max(float(nlu.get("confidence", 0.0) or 0.0), 0.95)
                nlu["needs_clarification"] = False
            elif asks_case_check and not asks_document_review and not asks_deadline:
                nlu["primary_intent"] = "status_inquiry"
                nlu["confidence"] = max(float(nlu.get("confidence", 0.0) or 0.0), 0.95)
                nlu["needs_clarification"] = False
            if re.search(r"\b(?:check|explain|tell me)\s+(?:the|my|this)?\s*reason\b|\bdenial reason\b", lowered):
                nlu["primary_intent"] = "denial_question"
                nlu["confidence"] = max(float(nlu.get("confidence", 0.0) or 0.0), 0.95)
                nlu["needs_clarification"] = False
            raw = {
                "relation": "follow_up" if nlu.get("follow_up") else "new_intent",
                "intent": nlu.get("primary_intent", "unclear"),
                "confidence": nlu.get("confidence", 0.0),
                "parent_intent_index": state.get("current_intent_index") if nlu.get("follow_up") else None,
                "question": text.strip(),
                "ambiguous": nlu.get("ambiguous"),
                "uncertain_terms": nlu.get("uncertain_terms", []),
                "requested_field": nlu.get("requested_field"),
                "secondary_intents": normalize_secondary_intents(
                    nlu.get("secondary_intents", []), nlu.get("secondary_intent")
                ),
                "term": nlu.get("term"),
            }
    case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
    if not state or not state.get("gates", {}).get("VERIFY_ID"):
        case_resolution = "provided" if case_ids else "not_required"
    elif state.get("case_access_authorized") and state.get("matched_case_id"):
        case_resolution = "matched"
    elif case_ids:
        case_resolution = "provided"
    else:
        case_resolution = "not_provided"
    intent = raw.get("intent", "unclear")
    if raw.get("requested_field") == "document_submission_deadline":
        raw["intent"] = intent = "submission_deadline"
    contract = {
        **raw,
        "case_resolution": case_resolution,
        "next_allowed_action": "ask_clarification" if intent == "unclear" else "answer_from_documents",
        "needs_clarification": intent == "unclear" or float(raw.get("confidence", 0.0)) < 0.7,
    }
    contract = validate_workflow_contract(contract)
    if not contract:
        contract = {
            "relation": "new_intent", "intent": "unclear", "confidence": 0.0,
            "parent_intent_index": None, "question": text.strip(),
            "case_resolution": case_resolution, "next_allowed_action": "ask_clarification",
            "needs_clarification": True,
        }
    contract["next_allowed_action"] = decide_next_action(state or {"gates": {}}, contract)
    return contract


clear_conversion_files()
load_sessions()


def local_extract(text):
    return nlu_service.local_extract(text, POLICYHOLDERS)


def model_extract(text):
    return nlu_service.model_extract(text, POLICYHOLDERS)


def _model_extract_legacy(text):
    lowered = text.lower()
    has_extractable_input = bool(
        re.search(r"\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", text)
        or "@" in text
        or re.search(r"\b(?:ssn|social|national id|government id|phone|telephone|last four)\b", lowered)
        or re.search(r"\bCL[- ]?\d+\b|\bPOL[- ]?\d+\b", text, re.I)
    )
    if not has_extractable_input:
        return local_extract(text)
    if not (ChatPromptTemplate and ChatGoogleGenerativeAI and os.getenv("GEMINI_API_KEY")):
        return local_extract(text)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Extract identity fields and useful case hints from the caller message. Understand aliases such as SSN, social security number, national ID, government ID, ID last four, and last four digits. Return ONLY valid JSON with keys full_name,dob,phone,email,ssn_last4,policy_number,case_hint. Map a verified national or government ID last-four value to ssn_last4 only when the user explicitly provides its last four digits. Use null for absent fields. Never infer missing values."),
        ("human", "{text}"),
    ])
    chain = prompt | ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), temperature=0) | (lambda m: m.content)
    raw = chain.invoke({"text": text})
    try:
        model_fields = {k: v for k, v in json.loads(raw).items() if v}
        # Merge deterministic fields so a model omission cannot hide a failed identity attempt.
        local_fields = local_extract(text)
        for key, value in local_fields.items():
            model_fields.setdefault(key, value)
        return model_fields
    except (json.JSONDecodeError, TypeError):
        return local_extract(text)


def unified_nlu(text, state=None):
    """Compatibility entry point; NLU service owns the public boundary."""
    return nlu_service.unified_nlu(text, state)


def classify_intent(text, state=None):
    """Compatibility entry point; NLU service owns the public boundary."""
    return nlu_service.classify_intent(text, state)


def normalize_phone(value):
    """Compare phone numbers by digits while preserving country-code semantics."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def normalize_email(value):
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def email_variants(value):
    normalized = normalize_email(value)
    variants = {normalized} if normalized else set()
    if "@" in normalized:
        local, domain = normalized.split("@", 1)
        # Support common provider aliases without changing non-provider addresses.
        if domain in {"gmail.com", "googlemail.com"}:
            variants.add(local.replace(".", "") + "@gmail.com")
            variants.add(local.split("+", 1)[0].replace(".", "") + "@gmail.com")
    return variants


def normalize_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def normalize_dob(value):
    return re.sub(r"\D", "", str(value or ""))


def name_variants(value):
    raw = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    tokens = [token for token in raw.split() if token]
    variants = {normalize_name(value)} if tokens else set()
    if tokens:
        variants.add(normalize_name(" ".join(reversed(tokens))))
        variants.add(normalize_name(" ".join(sorted(tokens))))
    return variants


def identity_field_matches(field, supplied, expected, record):
    return verification_service.identity_field_matches(field, supplied, expected, record)
    # Legacy implementation retained below during migration.
    if field == "phone":
        aliases = [*record.get("phone_aliases", []), *record.get("phones", [])]
        return normalize_phone(supplied) in {normalize_phone(item) for item in [expected, *aliases]}
    if field == "email":
        aliases = [*record.get("email_aliases", []), *record.get("emails", [])]
        expected_values = set()
        for item in [expected, *aliases]:
            expected_values.update(email_variants(item))
        return bool(email_variants(supplied) & expected_values)
    if field == "full_name":
        aliases = [*record.get("name_aliases", []), *record.get("full_name_aliases", [])]
        expected_values = set()
        for item in [expected, *aliases]:
            expected_values.update(name_variants(item))
        return bool(name_variants(supplied) & expected_values)
    if field == "dob":
        return normalize_dob(supplied) == normalize_dob(expected)
    return str(supplied or "").strip().lower() == str(expected or "").strip().lower()


def verify(state, extracted):
    return verification_service.verify(state, extracted)
    # Legacy implementation retained below during migration.
    state["identity"].update({k: v for k, v in extracted.items() if k not in ("case_hint",)})
    if extracted.get("case_hint"):
        if extracted["case_hint"] not in state["case_hints"]:
            state["case_hints"].append(extracted["case_hint"])
    # Policy number is only a lookup hint; it cannot count toward identity verification.
    verification_fields = ("full_name", "dob", "phone", "email", "ssn_last4")
    candidates = []
    for p in POLICYHOLDERS:
        matches = 0
        expected = {
            "full_name": p["name"], "dob": p["dob"], "phone": p["phone"],
            "email": p["email"], "ssn_last4": p["id_last4"],
        }
        for key in verification_fields:
            value = expected[key]
            if state["identity"].get(key) and identity_field_matches(key, state["identity"][key], value, p):
                matches += 1
        if matches >= 3:
            candidates.append(p)
    if len(candidates) == 1:
        candidate = candidates[0]
        state["verified_fields"] = [k for k in verification_fields if state["identity"].get(k)]
        state["verified_party_id"] = candidate["party_id"]
        state["gates"]["VERIFY_ID"] = True
        state["stage_progress"]["VERIFY_ID"] = 1.0
        advance_phase(state, "RESOLVE_INTENT")
        return True, candidate
    return False, None


def authorize_case_access(state):
    return verification_service.authorize_case_access(state)
    # Legacy implementation retained below during migration.
    """Authorize claim access only after all identity and ownership checks pass."""
    if not state["gates"]["VERIFY_ID"] or not state.get("verified_party_id"):
        return False, "Before I can access claim information, please provide at least three matching identity details: your full name, date of birth, phone number, email, or SSN last four digits."
    case_id = state.get("matched_case_id")
    if not case_id:
        return False, "Before I can access claim information, please provide the case ID you want to access."
    claims = load_json("claims.json")
    claim = next((item for item in claims if item.get("case_id") == case_id), None)
    if not claim:
        return False, "I could not locate that case. Please provide a valid case ID, such as CL-2048."
    if claim.get("party_id") != state["verified_party_id"]:
        return False, "I could not match that case to the identity you verified. Please provide a case ID associated with that identity."
    state["case_access_authorized"] = True
    return True, claim


def extract_case_nlu(text):
    return nlu_service.extract_case_nlu(text)


def _extract_case_nlu_legacy(text):
    """Use the LLM to understand case clues, with deterministic fallback."""
    lowered = text.lower()
    has_case_language = any(word in lowered for word in (
        "claim", "case", "denied", "denial", "rejected", "closed", "open", "medical",
        "healthcare", "dental", "dentist", "hospital", "january", "february", "march",
        "april", "may", "june", "july", "august", "september", "october", "november", "december",
        "jan", "janerary", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    )) or bool(re.search(r"\b(19|20)\d{2}\b", lowered))
    if not has_case_language:
        return {"status": None, "case_type": None, "month": None, "year": None}
    if ChatPromptTemplate and ChatGoogleGenerativeAI and os.getenv("GEMINI_API_KEY"):
        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an insurance case-clue NLU extractor. Understand the meaning of natural language, synonyms, paraphrases, abbreviations, spelling variations, and conversational context. Extract only clues explicitly stated or clearly implied by the user's wording. Do not invent missing values. Return ONLY valid JSON with exactly these keys: status, case_type, month, year. status must be one of denied, closed, open, or null. case_type must be one of healthcare, dental, or null. month must be an integer from 1 to 12 or null. year must be an integer or null. Examples: 'They rejected my medical claim from January 2026' means status=denied, case_type=healthcare, month=1, year=2026. 'The claim was turned down' means status=denied. 'It was for my teeth' means case_type=dental."),
            ("human", "User message and available case context:\n{text}"),
        ])
        chain = prompt | ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), temperature=0) | (lambda message: message.content)
        try:
            result = json.loads(chain.invoke({"text": text}))
            valid_statuses = {"denied", "closed", "open"}
            valid_types = {"healthcare", "dental"}
            status = result.get("status") if result.get("status") in valid_statuses else None
            case_type = result.get("case_type") if result.get("case_type") in valid_types else None
            month = result.get("month")
            year = result.get("year")
            month = int(month) if month is not None and 1 <= int(month) <= 12 else None
            year = int(year) if year is not None and 1900 <= int(year) <= 2100 else None
            return {"status": status, "case_type": case_type, "month": month, "year": year}
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            if os.getenv("GEMINI_DEBUG", "false").lower() == "true":
                print(f"[Gemini NLU fallback] {type(exc).__name__}: {exc}", flush=True)
            pass

    # Deterministic fallback for local/demo mode without an API key.
    lowered = text.lower()
    hints = {"status": None, "case_type": None, "month": None, "year": None}
    status_aliases = {
        "denied": ("denied", "denial", "rejected"),
        "closed": ("closed", "settled", "resolved"),
        "open": ("open", "active", "in progress", "processing"),
    }
    for status, aliases in status_aliases.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            hints["status"] = status
            break
    case_type_aliases = {
        "healthcare": ("healthcare", "health care", "medical", "hospital"),
        "dental": ("dental", "dentist", "teeth"),
    }
    for case_type, aliases in case_type_aliases.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            hints["case_type"] = case_type
            break
    months = {
        name: index for index, name in enumerate(
            ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1
        )
    }
    month_aliases = {
        "jan": 1, "janerary": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }
    for name, number in months.items():
        if re.search(rf"\b{name}\b", lowered):
            hints["month"] = number
            break
    if hints["month"] is None:
        for alias, number in month_aliases.items():
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                hints["month"] = number
                break
    if hints["month"] is None:
        # Accept a close, single-token misspelling such as "janerary" while
        # avoiding broad matches against unrelated words.
        for token in re.findall(r"[a-z]+", lowered):
            closest = max(months, key=lambda month: SequenceMatcher(None, token, month).ratio())
            if len(token) >= 4 and SequenceMatcher(None, token, closest).ratio() >= 0.72:
                hints["month"] = months[closest]
                break
    numeric_month = re.search(r"\b(0?[1-9]|1[0-2])\s*/\s*(?:19|20)\d{2}\b", lowered)
    if numeric_month:
        hints["month"] = int(numeric_month.group(1))
    numeric_year_month = re.search(r"\b(?:19|20)\d{2}\s*/\s*(0?[1-9]|1[0-2])\b", lowered)
    if numeric_year_month:
        hints["month"] = int(numeric_year_month.group(1))
        hints["year"] = int(re.search(r"\b(?:19|20)\d{2}\b", numeric_year_month.group(0)).group(0))
    year = re.search(r"\b(19|20)\d{2}\b", lowered)
    if year:
        hints["year"] = int(year.group(0))
    return hints


def interpret_user_message(text, state=None):
    return nlu_service.interpret_user_message(text, state)


def _interpret_user_message_impl(text, state=None):
    """Convert natural language into normalized semantic features before intent classification."""
    lowered = re.sub(r"\s+", " ", text.lower()).strip()
    emotion = detect_emotion(text)
    llm_required = os.getenv("REQUIRE_LLM", "true").lower() == "true"
    if llm_required and not (ChatPromptTemplate and ChatGoogleGenerativeAI and os.getenv("GEMINI_API_KEY")):
        return {"api_error": "Gemini API is unavailable. Please reconnect the API and try again."}
    if ChatPromptTemplate and ChatGoogleGenerativeAI and os.getenv("GEMINI_API_KEY"):
        semantic_prompt = ChatPromptTemplate.from_messages([
            ("system", API_NLU_PROMPT.format(
                intent_priority=json.dumps(INTENT_PRIORITY, ensure_ascii=False),
                semantic_rules=json.dumps(INTENT_SEMANTIC_RULES, ensure_ascii=False),
            )),
            ("human", "Conversation context:\n{context}\n\nCurrent user message:\n{text}"),
        ])
        chain = semantic_prompt | ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), temperature=0) | (lambda message: message.content)
        try:
            raw = chain.invoke({"context": json.dumps((state or {}).get("intents", [])[-5:], ensure_ascii=False), "text": text})
            result = nlu_service.api_semantic_interpretation(
                text, state, lambda _text, _state: raw, INTENT_CATEGORIES
            )
            if result:
                return result
        except Exception as exc:
            if os.getenv("GEMINI_DEBUG", "false").lower() == "true":
                print(f"[Gemini semantic NLU fallback] {type(exc).__name__}: {exc}", flush=True)
            if llm_required:
                return {"api_error": "Gemini API is unavailable. Please reconnect the API and try again."}
            pass


def resolve_intent(state, text, extracted):
    """Resolve a case from an explicit ID or structured NLU case hints."""
    if text not in state["case_hints"]:
        state["case_hints"].append(text)
    case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
    if case_ids:
        state["matched_case_id"] = case_ids[-1].upper().replace(" ", "-")
        state["gates"]["RESOLVE_INTENT"] = True
        state["stage_progress"]["RESOLVE_INTENT"] = 1.0
        return True
    if not state.get("verified_party_id"):
        return False
    case_context = " ".join(state.get("case_hints", []))
    # Do not let identity values, especially a DOB year, become claim filters.
    case_context = re.sub(r"(?:date of birth|dob)\s*(?:is|:)?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", case_context, flags=re.I)
    case_context = re.sub(r"(?:ssn|social(?: security)?|national id|government id|id last four|last four)[^0-9]{0,40}\d{4}", "", case_context, flags=re.I)
    nlu = extract_case_nlu(case_context)
    state["nlu_hints"] = {key: value for key, value in nlu.items() if value is not None}
    claims = [c for c in load_json("claims.json") if c.get("party_id") == state["verified_party_id"]]
    excluded_case_id = state.get("excluded_case_id")
    if excluded_case_id:
        claims = [c for c in claims if c.get("case_id") != excluded_case_id]
    if nlu.get("status"):
        claims = [c for c in claims if c.get("status") == nlu["status"]]
    if nlu.get("case_type"):
        claims = [c for c in claims if c.get("case_type") == nlu["case_type"]]
    if nlu.get("month"):
        claims = [c for c in claims if c.get("created_at", "")[5:7] == f"{nlu['month']:02d}"]
    if nlu.get("year"):
        claims = [c for c in claims if c.get("created_at", "")[:4] == str(nlu["year"])]
    # Use the customer's natural-language clue as additional search evidence.
    # This supports clues such as "the denied medical claim about the pathology
    # report" rather than limiting matching to month and year only. Search is
    # always performed after restricting candidates to the verified party.
    if len(claims) > 1:
        stopwords = {
            "the", "a", "an", "my", "me", "i", "is", "in", "on", "for", "about",
            "claim", "case", "please", "help", "find", "check", "this", "that", "from",
            "with", "and", "or", "of", "to", "was", "were", "has", "have", "had",
        }
        clue_tokens = {
            token for token in re.findall(r"[a-z0-9]+", case_context.lower())
            if len(token) >= 3 and token not in stopwords
        }
        if clue_tokens:
            scored = []
            for claim in claims:
                haystack = " ".join(str(claim.get(field, "")) for field in (
                    "case_id", "case_type", "status", "created_at", "summary",
                    "denial_reason", "documents_needed",
                )).lower()
                score = sum(1 for token in clue_tokens if token in haystack)
                if score:
                    scored.append((score, claim))
            if scored:
                best_score = max(score for score, _ in scored)
                best_claims = [claim for score, claim in scored if score == best_score]
                if len(best_claims) == 1:
                    claims = best_claims
    if len(claims) == 1:
        state["matched_case_id"] = claims[0]["case_id"]
        state["case_options"] = []
        state["awaiting_case_selection"] = False
        state["gates"]["RESOLVE_INTENT"] = True
        state["stage_progress"]["RESOLVE_INTENT"] = 1.0
        return True
    if len(claims) > 1:
        state["case_options"] = [claim["case_id"] for claim in claims]
        state["awaiting_case_selection"] = True
    return False


def case_selection_prompt(state):
    # Do not disclose claim details or enumerate claim options before the
    # customer identifies which authorized case they want to discuss. A claim
    # ID is preferred, but a safe time/status/type clue can also be used for
    # internal matching.
    hints = state.get("nlu_hints", {})
    if hints.get("month") and not hints.get("year"):
        return (
            "I found more than one claim matching that month. To securely identify the right one, "
            "could you provide the claim year, claim type, or current status? You may also provide "
            "the claim ID if you have it."
        )
    if hints.get("year") and not hints.get("month"):
        return (
            "I found more than one claim matching that year. To securely identify the right one, "
            "could you provide the claim month, claim type, or current status? You may also provide "
            "the claim ID if you have it."
        )
    options = state.get("case_options", [])
    if options:
        numbered = " ".join(f"{index}. {case_id}" for index, case_id in enumerate(options, 1))
        return (
            "I closed the previous claim for this conversation. I found multiple claims associated "
            "with your verified identity. Please choose the claim you would like to discuss: "
            f"{numbered} Reply with the claim ID or its number."
        )
    return (
        "I found multiple claims associated with your verified identity. "
        "Please provide the claim ID you would like to discuss so I can confirm it. "
        "If you do not know the claim ID, you can instead tell me the approximate "
        "month or year of the claim, and I can try to match it securely."
    )


def process_case(state):
    """Validate case ownership without exposing or storing case details."""
    if not (state["gates"]["VERIFY_ID"] and state["gates"]["RESOLVE_INTENT"]):
        return False, "Please complete identity verification and case intent confirmation first."
    authorized, match = authorize_case_access(state)
    if not authorized:
        state["matched_case_id"] = None
        state["case_access_authorized"] = False
        state["gates"]["RESOLVE_INTENT"] = False
        state["stage_progress"]["RESOLVE_INTENT"] = 0.0
        state["phase"] = "RESOLVE_INTENT"
        return False, match
    # Do not expose claim contents before the user states what they need.
    state["case"] = match
    state["stage_progress"]["PROCESS_CASE"] = 0.0
    state["phase"] = "RESOLVE_INTENT"
    state["awaiting_user_need"] = True
    return True, match


def answer_potential_intent(state):
    """Promote the latest pre-case question after verification and answer it once."""
    if not state.get("potential_intents"):
        return None
    potential = state["potential_intents"].pop()
    state["potential_intents"] = []
    state["awaiting_potential_confirmation"] = False
    state["awaiting_user_need"] = False
    classification = {
        "relation": "new_intent",
        "intent": potential["intent"],
        "confidence": potential["confidence"],
        "parent_intent_index": None,
        "question": potential["question"],
        "requested_field": potential.get("requested_field"),
    }
    state["intents"].append({
        "relation": "new_intent",
        "intent": classification["intent"],
        "confidence": classification["confidence"],
        "parent_intent_index": None,
        "question": classification["question"],
        "term": potential.get("term"),
        "requested_field": potential.get("requested_field"),
        "followups": [],
        "answered": False,
    })
    state["current_intent_index"] = len(state["intents"]) - 1
    answer = answer_intent(state, classification, classification["question"])
    state["intents"][-1]["answer"] = answer
    mark_current_intent_answered(state)
    state["gates"]["PROCESS_CASE"] = True
    state["stage_progress"]["PROCESS_CASE"] = 1.0
    state["awaiting_additional_question"] = True
    advance_phase(state, "PROCESS_CASE")
    return answer


def answer_intent(state, classification, question):
    """Compatibility entry point; response service owns routing."""
    return response_service.answer_intent(state, classification, question)


def answer_stored_nlu_intents(state):
    """Compatibility entry point; response service owns stored-intent handling."""
    return response_service.answer_stored_nlu_intents(state)


def _answer_stored_nlu_intents_impl(state):
    """Answer intents already identified by NLU when the case becomes available."""
    nlu = state.get("last_nlu") or {}
    primary = nlu.get("primary_intent")
    if primary not in INTENT_CATEGORIES or primary == "unclear":
        return None
    intents_to_answer = [primary]
    secondary = normalize_secondary_intents(nlu.get("secondary_intents"))
    intents_to_answer.extend(item for item in secondary if item in INTENT_CATEGORIES and item not in intents_to_answer)
    answers = []
    for intent_name in intents_to_answer:
        classification = {
            "relation": "new_intent",
            "intent": intent_name,
            "confidence": 0.96,
            "parent_intent_index": None,
            "question": state.get("_unified_nlu_text", state.get("user_need") or ""),
            "requested_field": nlu.get("requested_field"),
        }
        state["intents"].append({
            "relation": "new_intent", "intent": intent_name, "confidence": 0.96,
            "parent_intent_index": None, "question": classification["question"],
            "requested_field": (nlu.get("requested_field") if intent_name == primary else infer_requested_field(classification["question"], intent_name)),
            "followups": [], "answered": False,
        })
        state["current_intent_index"] = len(state["intents"]) - 1
        answer = answer_intent(state, classification, classification["question"])
        state["intents"][-1]["answer"] = answer
        mark_current_intent_answered(state)
        answers.append(answer)
    if answers:
        state["gates"]["PROCESS_CASE"] = True
        state["stage_progress"]["PROCESS_CASE"] = 1.0
        state["awaiting_additional_question"] = True
        advance_phase(state, "PROCESS_CASE")
        return deduplicate_response_sections("\n\n".join(answers))
    return None


def handle_phase_out_of_scope(state, classification, text, extracted=None):
    """Apply the same unrelated-question threshold with phase-specific guidance."""
    extracted = extracted or {}
    intent = classification.get("intent")
    lowered = text.lower()
    if (state.get("last_nlu") or {}).get("domain") == "unknown" or is_out_of_scope_question(text):
        state["unrelated_question_count"] += 1
        state.setdefault("handoff_conditions", {})["unclear_questions"] = state["unrelated_question_count"]
        if state["unrelated_question_count"] > 3:
            request_human_transfer(state, f"The user asked more than three out-of-scope questions during {state['phase']}.", "unclear_questions")
            return "I can only assist with insurance claims-related questions. Would you like me to connect you with a human claims representative?"
        return "I'm sorry, but I can only assist with insurance claims-related questions."
    if intent != "unclear":
        return None
    uncertain_terms = classification.get("uncertain_terms") or []
    if uncertain_terms:
        terms = ", ".join(f'"{str(term)}"' for term in uncertain_terms[:3])
        return f"I want to make sure I understand the term {terms}. Could you clarify what you would like me to check or explain about it?"
    has_case_id = bool(re.search(r"\bCL[- ]?\d+\b", text, re.I))
    has_case_clue = bool(re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|janerary|19\d{2}|20\d{2}|denied|denial|rejected|closed|open|healthcare|medical|dental)\b",
        lowered,
    ))
    cannot_find_case_id = any(phrase in lowered for phrase in (
        "do not know the case id", "don't know the case id", "cannot find the case id",
        "can't find the case id", "do not have the case id", "don't have the case id",
        "i do not know", "i don't know", "not sure", "no idea",
    ))
    has_identity = any(extracted.get(field) for field in ("full_name", "dob", "phone", "email", "ssn_last4"))
    if state["phase"] == "VERIFY_ID" and has_identity:
        return None
    if state["phase"] == "RESOLVE_INTENT" and (has_case_id or has_case_clue or cannot_find_case_id):
        return None
    state["unrelated_question_count"] += 1
    state.setdefault("handoff_conditions", {})["unclear_questions"] = state["unrelated_question_count"]
    if state["unrelated_question_count"] > 3:
        request_human_transfer(state, f"The user asked more than three unclear or out-of-scope questions during {state['phase']}.", "unclear_questions")
        return "I’m unable to determine how this relates to your current claim task. Would you like me to connect you with a human claims representative?"
    if state["phase"] == "VERIFY_ID":
        return "I can help with your claim, but I first need at least three matching identity details: your full name, date of birth, phone number, email, or SSN last four digits."
    if state["phase"] == "RESOLVE_INTENT":
        return case_selection_prompt(state)
    if state["phase"] == "POST_PROCESS":
        return "Please provide the email address for the summary, or ask a question related to your claim if you would like to continue."
    return "I can help with claim status, denial reasons, missing documents, document submission, deadlines, processing times, payments, appeals, next steps, or a human claims representative."


def _prepare_turn(state, text):
    """Apply turn-wide bookkeeping and deterministic emotion detection.

    This runs before any LangGraph stage so every stage receives the same
    conversation metadata without duplicating emotion handling.
    """
    state["last_user_message"] = text
    if isinstance(state.get("last_nlu"), dict):
        state["last_nlu"]["raw_message"] = text
    state["attempts"] += 1
    previous_emotion = state.get("emotion", "Neutral / Calm")
    previous_level = state.get("emotion_level", "low")
    current_emotion, current_level = detect_emotion(text)
    state["emotion"] = current_emotion
    state["emotion_level"] = current_level
    if (
        current_emotion == "Neutral / Calm"
        and previous_level in {"medium", "high"}
        and not is_meaningful_user_message(text)
    ):
        state["emotion"] = previous_emotion
        state["emotion_level"] = previous_level


def _verify_id_stage_logic(state, text, extracted):
    """Handle identity verification without changing the shared controller."""
    if any(phrase in text.lower() for phrase in ("i refuse", "i don't want to provide", "i do not want to provide", "i won't provide", "not giving", "i cannot provide")):
        state.setdefault("handoff_conditions", {})["verification_pushbacks"] = state.get("handoff_conditions", {}).get("verification_pushbacks", 0) + 1
    state["verification_attempts"] += 1
    state.setdefault("handoff_conditions", {})["verification_failures"] = state["verification_attempts"]
    if state["verification_attempts"] > 5:
        request_human_transfer(state, "Identity verification was unsuccessful after more than five attempts.", "verification_failures")
        return "I’m unable to verify your identity after several attempts. Would you like me to connect you with a human claims representative?", extracted
    ok, _ = verify(state, extracted)
    if ok:
        remembered = classify_intent(" ".join(state.get("case_hints", [])), state)
        if remembered["intent"] == "unclear" and any(word in " ".join(state.get("case_hints", [])).lower() for word in ("denied", "denial", "rejected")):
            remembered.update({"intent": "denial_question", "confidence": 0.8, "question": "Why was this claim denied?"})
        if resolve_intent(state, text, extracted):
            authorized, result = process_case(state)
            if authorized:
                automatic_answer = answer_potential_intent(state)
                automatic_answer = automatic_answer or answer_stored_nlu_intents(state)
                if automatic_answer:
                    return automatic_answer, extracted
                message = f"I found case {result['case_id']} and confirmed that it belongs to your verified identity. Before I access any case details, what would you like help with?"
                if state["emotion_level"] == "high":
                    message = f"I understand this has been stressful, and I’ll keep the next step clear. {message}"
                return message, extracted
            return result, extracted
        if state.get("awaiting_case_selection"):
            return case_selection_prompt(state), extracted
        message = "Thank you. I verified your identity. Please provide the case ID or describe the claim you need help with."
        if state["emotion_level"] == "high":
            message = f"I understand this has been stressful, and I’ll keep the next step clear. {message}"
        return message, extracted
    provided_fields = [field for field in ("full_name", "dob", "phone", "email", "ssn_last4") if state["identity"].get(field)]
    if len(provided_fields) >= 3:
        return "The identity information could not be verified. Please enter the correct information and make sure at least three fields match our records.", extracted
    return None


def _process_turn_impl(state, text):
    # Record the exact current user turn before any early workflow return.
    # This prevents POST_PROCESS acknowledgments and closing responses from
    # inheriting the previous NLU raw_message.
    _prepare_turn(state, text)
    if state.get("disconnected"):
        return "This conversation has been closed. Please start a new conversation if you need further assistance.", {}
    if is_end_request(text) and (state.get("human_transfer") or state.get("awaiting_human_transfer") or state.get("conversation_complete")):
        state["conversation_complete"] = True
        return "Understood. Goodbye, and take care.", {}
    if any(phrase in text.lower() for phrase in ("want to die", "kill myself", "end my life")):
        request_human_transfer(state, "The user expressed a potential self-harm concern.", "safety_concern")
        return "I’m really sorry you’re feeling this way. Your immediate safety matters, and I want to connect you with a human representative right away. Would you like me to arrange that?", {}
    preliminary_fields = local_extract(text) if state["phase"] in {"VERIFY_ID", "RESOLVE_INTENT"} else {}
    has_identity_input = any(preliminary_fields.get(field) for field in ("full_name", "dob", "phone", "email", "ssn_last4"))
    has_case_input = bool(re.search(r"\bCL[- ]?\d+\b", text, re.I))
    _, current_emotion_level = detect_emotion(text)
    if current_emotion_level == "high" and state["phase"] in {"VERIFY_ID", "RESOLVE_INTENT"} and not state.get("awaiting_human_transfer") and not has_identity_input and not has_case_input:
        if state["phase"] == "VERIFY_ID":
            return "I understand this is important and frustrating. We can take this one step at a time. Please provide at least three matching identity details so I can protect your claim information.", {}
        return "I understand this is important and frustrating. I will keep the next step clear. Please provide the case ID so I can match the claim to your verified identity.", {}
    normalized_reply = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    if state.get("awaiting_human_transfer") and state["phase"] != "POST_PROCESS" and normalized_reply in {"yes", "yes please", "please do", "transfer me", "sure"}:
        state["human_transfer"] = True
        state["awaiting_human_transfer"] = False
        _, ticket_message = get_handoff_ticket_response(state)
        state["conversation_complete"] = True
        state["gates"]["POST_PROCESS"] = True
        state["stage_progress"]["POST_PROCESS"] = 1.0
        advance_phase(state, "POST_PROCESS")
        state["post_process_step"] = "ask_summary"
        registered_email = registered_email_for_state(state)
        if registered_email:
            summary = build_conversion_summary({"state": state, "intents": state.get("intents", [])})
            return f"{ticket_message} I’m transferring you to a human claims representative now.\n\n{summary}\n\nYour registered email address is {registered_email}. Would you like me to send this summary there?", {}
        return f"{ticket_message} I’m transferring you to a human claims representative now. I could not find a registered email address for your verified account.", {}
    if state["phase"] == "POST_PROCESS":
        normalized = re.sub(r"[^a-z0-9@._ -]", "", text.strip().lower()).strip()
        post_classification = classify_intent(text, state)
        if not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text) and normalized not in {"yes", "yes please", "sure", "please do", "no", "no thanks", "no thank you"}:
            out_of_scope = handle_phase_out_of_scope(state, post_classification, text)
            if out_of_scope:
                return out_of_scope, {}
        if state.get("post_process_step") == "ask_summary":
            if normalized in {"yes", "yes please", "sure", "please do"}:
                registered_email = registered_email_for_state(state)
                if not registered_email:
                    state["post_process_step"] = "email_retry"
                    return "I could not find a registered email address for your verified account. Would you like me to connect you with a human claims representative?", {}
                state["summary_email"] = registered_email
                if send_summary_email({"state": state, "intents": state.get("intents", [])}, registered_email):
                    state["post_process_step"] = "disconnect_scheduled"
                    state["disconnect_scheduled"] = True
                    return f"Your summary was sent successfully to your registered email address ({redact_identity({'email': registered_email}).get('email')}). I will close this conversation shortly.", {}
                state["post_process_step"] = "email_retry"
                return f"I could not send the summary to your registered email address ({registered_email}). Would you like me to try again?", {}
            if normalized in {"no", "no thanks", "no thank you"}:
                state["post_process_step"] = "disconnect_scheduled"
                state["disconnect_scheduled"] = True
                return "Understood. I will close this conversation shortly.", {}
        if state.get("post_process_step") == "email_retry" and normalized in {"no", "no thanks", "no thank you", "not now"}:
            state["post_process_step"] = "disconnect_scheduled"
            state["disconnect_scheduled"] = True
            return "Understood. I will not retry the email. This conversation will close in 30 seconds.", {}
        if state.get("post_process_step") == "email_retry" and normalized in {"yes", "yes please", "sure", "please do", "try again"}:
            registered_email = registered_email_for_state(state)
            if registered_email and send_summary_email({"state": state, "intents": state.get("intents", [])}, registered_email):
                state["summary_email"] = registered_email
                state["post_process_step"] = "disconnect_scheduled"
                state["disconnect_scheduled"] = True
                return f"Your summary was sent successfully to your registered email address ({redact_identity({'email': registered_email}).get('email')}). I will close this conversation shortly.", {}
            state["post_process_step"] = "disconnect_scheduled"
            state["disconnect_scheduled"] = True
            return "I still could not send the summary to your registered email address. This conversation will close in 30 seconds.", {}
        if state.get("post_process_step") in {"ask_summary", "email_retry"} and re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text):
            return "For your protection, the summary can only be sent to the registered email address on the verified account. Please reply yes if you would like me to use that address.", {}
        # A new question during post-processing reopens case processing.
        state["phase"] = "PROCESS_CASE"
        state["gates"]["PROCESS_CASE"] = False
        state["gates"]["POST_PROCESS"] = False
        state["stage_progress"]["PROCESS_CASE"] = 0.0
        state["stage_progress"]["POST_PROCESS"] = 0.0
        state["conversation_complete"] = False
        state["post_process_step"] = None
    unified = unified_nlu(text, state)
    state["last_nlu"] = unified
    state["_unified_nlu_text"] = text
    if unified.get("api_error"):
        return unified["api_error"], {}
    extracted = unified["identity_fields"]
    state["last_nlu"]["raw_message"] = text
    # Keep the deterministic safety/emotion signal authoritative. Gemini may
    # describe the message as low even when explicit wording or punctuation
    # clearly indicates medium/high intensity; it must not downgrade that
    # signal or erase an ongoing escalation on a non-meaningful turn.
    api_emotion = unified.get("emotion", "Neutral / Calm")
    api_level = unified.get("emotion_level", "low")
    local_emotion, local_level = detect_emotion(text)
    level_rank = {"low": 0, "medium": 1, "high": 2}
    previous_level = state.get("emotion_level", "low")
    if local_emotion != "Neutral / Calm":
        state["emotion"] = local_emotion
    else:
        # The deterministic detector is authoritative for factual messages;
        # do not let the API invent concern from the claim context alone.
        state["emotion"] = "Neutral / Calm"
    # A new meaningful claim request starts a fresh emotion assessment. Do not
    # carry an earlier high level into factual wording such as a case/status
    # or document question. Non-meaningful acknowledgments may retain the
    # previous escalation context.
    if is_meaningful_user_message(text):
        if local_emotion == "Neutral / Calm":
            state["emotion_level"] = "low"
        else:
            state["emotion_level"] = max(
                (local_level, api_level),
                key=lambda level: level_rank.get(level, 0),
            )
    else:
        state["emotion_level"] = max(
            (local_level, api_level, previous_level),
            key=lambda level: level_rank.get(level, 0),
        )
    state["emotion_explicit"] = (
        local_emotion not in {"Neutral / Calm", "Satisfied / Relieved"}
    )
    state["nlu_hints"] = {key: value for key, value in unified.get("case_hints", {}).items() if value is not None}
    lowered = re.sub(r"[^a-z0-9@._ -]", "", text.strip().lower()).strip()
    # Once a claim has been matched, the next message is the user's request
    # about that claim. Do not run the pre-case out-of-scope gate against it;
    # otherwise valid requests such as "What is the current status?" are
    # incorrectly sent back to case-ID matching.
    if state["phase"] in {"VERIFY_ID", "RESOLVE_INTENT"} and not state.get("awaiting_user_need"):
        if state["phase"] == "RESOLVE_INTENT" and state.get("awaiting_case_selection") and re.search(r"\b(?:i\s+)?(?:cannot|can not|can't|dont|don\x27t|do not)\s+(?:remember|recall|know)\b", text.lower()):
            return (
                "No problem. You do not need to remember the claim number. "
                "Please choose one of the other claim IDs below: "
                + " ".join(f"{index}. {case_id}" for index, case_id in enumerate(state.get("case_options", []), 1)),
                extracted,
            )
        phase_classification = classify_intent(text, state)
        out_of_scope = handle_phase_out_of_scope(state, phase_classification, text, extracted)
        if out_of_scope:
            return out_of_scope, extracted
    if state["phase"] == "PROCESS_CASE":
        state["user_need"] = extract_user_need(text, extracted)
    if state["phase"] == "VERIFY_ID":
        stage_result = _verify_id_stage_logic(state, text, extracted)
        if stage_result is not None:
            return stage_result
    elif state["phase"] == "RESOLVE_INTENT":
        # When the customer cannot remember the claim ID, keep the verified
        # identity and present safe claim-ID choices instead of repeating the
        # case-ID request. Only identifiers are disclosed; no claim facts are
        # exposed before the customer selects one.
        if re.search(r"\b(?:i\s+)?(?:cannot|can not|can't|dont|don\x27t|do not)\s+(?:remember|recall|know)\b", text.lower()):
            claims = [
                {"case_id": case_id}
                for case_id in case_service.other_claim_ids(
                    state.get("verified_party_id"), state.get("matched_case_id"), load_json("claims.json")
                )
            ]
            state["excluded_case_id"] = state.get("matched_case_id")
            state["matched_case_id"] = None
            state["case_access_authorized"] = False
            state["case_options"] = [claim["case_id"] for claim in claims]
            state["awaiting_case_selection"] = len(state["case_options"]) > 0
            if state["awaiting_case_selection"]:
                return (
                    "No problem. Since your identity is already verified, I can show you the other claim IDs linked to your account. "
                    + case_selection_prompt(state), extracted
                )
            return "No problem. I could not find another claim linked to your verified identity.", extracted
        # The customer may repeat the claim number after it has already been
        # matched. Treat that as an acknowledgment, not as a new unclear
        # question, and keep the customer at the intent-entry step.
        repeated_case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
        text_without_case_id = re.sub(r"\bCL[- ]?\d+\b", " ", text, flags=re.I)
        text_without_case_id = re.sub(r"\b(?:my\s+)?claim\s+(?:number|id)\s*(?:is|:)?\b|\bfor\s+(?:the\s+)?claim\b", " ", text_without_case_id, flags=re.I)
        repeated_case_only = not text_without_case_id.strip(" .,!?;:-")
        if (
            state.get("matched_case_id")
            and repeated_case_ids
            and any(case_id.upper().replace(" ", "-") == state["matched_case_id"] for case_id in repeated_case_ids)
            and state.get("awaiting_user_need")
            and repeated_case_only
        ):
            return (
                "Your case has already been verified. I can help with claim status, denial reasons, missing documents, "
                "document submission, deadlines, processing times, payments, appeals, next steps, or a human claims representative. "
                "Which area would you like to start with?",
                extracted,
            )
        if state.get("awaiting_case_selection"):
            if re.search(r"\b(?:i\s+)?(?:cannot|can not|can't|dont|don\x27t|do not)\s+(?:remember|recall|know)\b", text.lower()):
                return (
                    "No problem. You do not need to remember the claim number. "
                    "Please choose one of the other claim IDs below: "
                    + " ".join(f"{index}. {case_id}" for index, case_id in enumerate(state.get("case_options", []), 1)),
                    extracted,
                )
            selected_case = case_service.select_case_id(text, state.get("case_options", []))
            case_ids = re.findall(r"\bCL[- ]?\d+\b", text, re.I)
            selection_match = re.fullmatch(r"\s*(\d+)\s*", text)
            if not selected_case:
                # The customer may refine the selection with a month, year,
                # claim type, or status instead of supplying the ID. Re-run
                # secure matching before asking for another clarification.
                if not case_ids and not selection_match:
                    if resolve_intent(state, text, extracted):
                        authorized, result = process_case(state)
                        if authorized:
                            automatic_answer = answer_potential_intent(state)
                            automatic_answer = automatic_answer or answer_stored_nlu_intents(state)
                            if automatic_answer:
                                return automatic_answer, extracted
                            return f"I found the matching case and confirmed that it belongs to your verified identity. Before I access any case details, what would you like help with?", extracted
                        return result, extracted
                options = "\n".join(f"{index}. {case_id}" for index, case_id in enumerate(state.get("case_options", []), 1))
                return case_selection_prompt(state), extracted
            state["matched_case_id"] = selected_case
            state.pop("excluded_case_id", None)
            state["case_options"] = []
            state["awaiting_case_selection"] = False
            state["gates"]["RESOLVE_INTENT"] = True
            state["stage_progress"]["RESOLVE_INTENT"] = 1.0
            ok, result = process_case(state)
            if ok:
                automatic_answer = answer_potential_intent(state)
                automatic_answer = automatic_answer or answer_stored_nlu_intents(state)
                if automatic_answer:
                    return automatic_answer, extracted
                return f"I found case {result['case_id']} and confirmed that it belongs to your verified identity. Before I access any case details, what would you like help with?", extracted
            return result, extracted
        if False and state.get("awaiting_potential_confirmation"):
            if lowered in {"yes", "yes please", "correct", "that is right", "that's right"}:
                potential = state["potential_intents"][-1]
                state["user_need"] = potential["question"]
                state["awaiting_potential_confirmation"] = False
                state["awaiting_user_need"] = False
                classification = {
                    "relation": "new_intent",
                    "intent": potential["intent"],
                    "confidence": potential["confidence"],
                    "parent_intent_index": None,
                    "question": potential["question"],
                }
                state["intents"].append({
                    "relation": "new_intent",
                    "intent": classification["intent"],
                    "confidence": classification["confidence"],
                    "parent_intent_index": None,
                    "question": classification["question"],
                    "followups": [],
                    "answered": False,
                })
                state["current_intent_index"] = len(state["intents"]) - 1
                answer = answer_intent(state, classification, state["user_need"])
                state["intents"][-1]["answer"] = answer
                if not answer_requires_followup(answer):
                    mark_current_intent_answered(state)
                state["gates"]["PROCESS_CASE"] = True
                state["stage_progress"]["PROCESS_CASE"] = 1.0
                state["awaiting_additional_question"] = True
                advance_phase(state, "PROCESS_CASE")
                return answer, extracted
            if lowered in {"no", "no thanks", "not that", "different question"}:
                state["potential_intents"] = []
                state["awaiting_potential_confirmation"] = False
                state["awaiting_user_need"] = True
                return "Understood. What would you like help with regarding this case?", extracted
            return "I noted a possible request. Is that the question you would like help with? Please answer yes or no.", extracted
        if state.get("awaiting_user_need"):
            state["user_need"] = text.strip()[:500] or state["user_need"]
            if not state["user_need"]:
                return "What would you like help with? Please describe your request before I process the case.", extracted
            state["awaiting_user_need"] = False
            record_intent(state, state["user_need"])
            classification = classify_intent(state["user_need"], state)
            answer = answer_intent(state, classification, state["user_need"])
            secondary_intents = normalize_secondary_intents(classification.get("secondary_intents"))
            seen_secondary = set()
            for secondary_name in secondary_intents:
                if secondary_name in seen_secondary or secondary_name in {classification.get("intent"), "emotional_support", "unclear"}:
                    continue
                seen_secondary.add(secondary_name)
                secondary_answer = answer_intent(state, {
                    "relation": "follow_up",
                    "intent": secondary_name,
                    "confidence": classification.get("confidence", 0.9),
                    "parent_intent_index": state.get("current_intent_index"),
                    "question": state["user_need"],
                }, state["user_need"])
                answer = f"{answer}\n\n{secondary_answer}"
            if classification["intent"] == "emotional_support":
                emotional_markers = r"\b(?:worried|anxious|scared|upset|frustrated|overwhelmed|distressed|angry|furious|sad|devastated|concerned)\b"
                actionable_text = " ".join(
                    part for part in re.split(r"[.!?;]+", state["user_need"])
                    if part.strip() and not re.search(emotional_markers, part, re.I)
                ).strip()
                secondary = classify_intent(actionable_text, state) if actionable_text else {"intent": "unclear"}
                if secondary.get("intent") not in {"unclear", "emotional_support"}:
                    answer = f"{answer}\n\n{answer_intent(state, secondary, actionable_text)}"
            state["intents"][state["current_intent_index"]]["answer"] = answer
            if not answer_requires_followup(answer):
                mark_current_intent_answered(state)
            state["awaiting_additional_question"] = True
            state["gates"]["PROCESS_CASE"] = True
            state["stage_progress"]["PROCESS_CASE"] = 1.0
            advance_phase(state, "PROCESS_CASE")
            return deduplicate_response_sections(answer), extracted
        if resolve_intent(state, text, extracted):
            ok, result = process_case(state)
            if ok:
                automatic_answer = answer_potential_intent(state)
                automatic_answer = automatic_answer or answer_stored_nlu_intents(state)
                if automatic_answer:
                    return automatic_answer, extracted
                return f"I found case {result['case_id']} and confirmed that it belongs to your verified identity. Before I access any case details, what would you like help with?", extracted
            return result, extracted
        if state.get("awaiting_case_selection"):
            return case_selection_prompt(state), extracted
        classification = classify_intent(text, state)
        return "Please provide the case ID first. I need to match it to your verified identity before I can address your request.", extracted
    elif state["phase"] == "PROCESS_CASE":
        lowered = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
        if state.get("awaiting_additional_question") and lowered in {"no", "nope", "none", "nothing else", "thats all", "that is all", "no that is all", "no thats all"}:
            state["awaiting_additional_question"] = False
            state["conversation_complete"] = True
            state["gates"]["POST_PROCESS"] = True
            state["stage_progress"]["POST_PROCESS"] = 1.0
            advance_phase(state, "POST_PROCESS")
            state["post_process_step"] = "ask_summary"
            if not has_claim_questions(state):
                return close_without_summary(state), extracted
            registered_email = registered_email_for_state(state)
            if registered_email:
                summary = build_conversion_summary({"state": state, "intents": state.get("intents", [])})
                return f"{summary}\n\nYour registered email address is {registered_email}. Would you like me to send this summary there?", extracted
            return "Understood. I could not find a registered email address for your verified account. Would you like me to connect you with a human claims representative?", extracted
        if state.get("awaiting_additional_question") and is_end_request(text):
            mark_current_intent_answered(state)
            return "Understood. Do you have any other questions about this case?", extracted
        if state.get("awaiting_clarification") and is_conversation_acknowledgment(text):
            state["clarification_attempts"] = state.get("clarification_attempts", 0) + 1
            return clarification_reprompt(state), extracted
        if state.get("awaiting_human_transfer") and lowered in {"yes", "yes please", "please do", "transfer me", "sure"}:
            state["human_transfer"] = True
            state["awaiting_human_transfer"] = False
            _, ticket_message = get_handoff_ticket_response(state)
            state["conversation_complete"] = True
            state["gates"]["POST_PROCESS"] = True
            state["stage_progress"]["POST_PROCESS"] = 1.0
            advance_phase(state, "POST_PROCESS")
            state["post_process_step"] = "ask_summary"
            if not has_claim_questions(state):
                return close_without_summary(state), extracted
            registered_email = registered_email_for_state(state)
            if registered_email:
                summary = build_conversion_summary({"state": state, "intents": state.get("intents", [])})
                return f"{ticket_message} I’m transferring you to a human claims representative now.\n\n{summary}\n\nYour registered email address is {registered_email}. Would you like me to send this summary there?", extracted
            return f"{ticket_message} I’m transferring you to a human claims representative now. I could not find a registered email address for your verified account.", extracted
        if state.get("awaiting_additional_question") and is_conversation_acknowledgment(text) and not accepts_previous_offer(state, text) and not acknowledges_previous_answer(state, text):
            return "I’m not quite sure what you mean by that. Could you clarify what you would like help with?", extracted
        if state.get("awaiting_additional_question") and accepts_previous_offer(state, text):
            parent = state.get("current_intent_index")
            suggestion_intent = (state.get("last_nlu") or {}).get("primary_intent") or "missing_documents"
            state["intents"][parent]["followups"].append({
                "text": text.strip()[:500], "intent": suggestion_intent,
                "parent_intent_index": parent, "answered": False,
            })
            answer = answer_intent(state, {
                "relation": "follow_up", "intent": suggestion_intent,
                "confidence": 0.96, "parent_intent_index": parent,
                "question": text.strip(),
            }, text)
            state["intents"][parent]["followups"][-1]["answer"] = answer
            state["intents"][parent]["followups"][-1]["answered"] = True
            return answer, extracted
        if state.get("awaiting_additional_question") and acknowledges_previous_answer(state, text):
            mark_current_intent_answered(state)
            return "You’re welcome. I’m glad I could clarify that. Do you have any other questions about this case?", extracted
        state["awaiting_additional_question"] = False
        state["awaiting_clarification"] = False
        state["clarification_attempts"] = 0
        state["followup_sent"] = False
        if is_out_of_scope_question(text):
            state["unrelated_question_count"] += 1
            state.setdefault("handoff_conditions", {})["unclear_questions"] = state["unrelated_question_count"]
            return "I'm sorry, but I can only assist with insurance claims-related questions.", extracted
        if not record_intent(state, text):
            return "Are you asking a follow-up question about the current topic, or do you have a different question about your claim?", extracted
        classification = classify_intent(text, state)
        # A correction such as "no, I mean the appeal ddl" supersedes the
        # previous document-deadline interpretation for this turn.
        lowered_request = text.lower()
        if re.search(r"\b(?:appeal|appealing)\b", lowered_request) and re.search(r"\b(?:deadline|ddl|due date|when|by what date)\b", lowered_request):
            classification["intent"] = "appeal_question"
            classification["requested_field"] = "appeal_deadline"
            classification["secondary_intents"] = []
            classification["needs_clarification"] = False
        if classification.get("intent") == "other_claim_inquiry":
            state["phase"] = "RESOLVE_INTENT"
            state["gates"]["RESOLVE_INTENT"] = False
            state["gates"]["PROCESS_CASE"] = False
            state["case_access_authorized"] = False
            state["excluded_case_id"] = state.get("matched_case_id")
            state["matched_case_id"] = None
            state["case_options"] = []
            state["awaiting_case_selection"] = False
            if resolve_intent(state, text, extracted):
                authorized, result = process_case(state)
                if authorized:
                    return f"I found case {result['case_id']} and confirmed that it belongs to your verified identity. What would you like help with regarding this claim?", extracted
            if state.get("awaiting_case_selection"):
                return case_selection_prompt(state), extracted
            return "I can help you look up another claim. Please provide the claim ID, or tell me the approximate month, year, claim type, or status.", extracted
        answer = answer_intent(state, classification, text)
        # Answer every concrete intent returned by the shared NLU contract in
        # order. The primary answer is followed by each distinct secondary
        # business answer, while emotional support remains an accompaniment.
        secondary_intents = normalize_secondary_intents(classification.get("secondary_intents"))
        # Preserve every clearly expressed request even if the provider's
        # normalized response omits one of the secondary intents.
        lowered_request = text.lower()
        if re.search(r"\b(?:missing documents|documents? still missing|check my documents)\b", lowered_request):
            if classification.get("intent") != "missing_documents" and "missing_documents" not in secondary_intents:
                secondary_intents.append("missing_documents")
        if (
            re.search(r"\b(?:deadline|ddl|due date|by when|when should|how soon)\b", lowered_request)
            and not re.search(r"\b(?:appeal|appealing)\b", lowered_request)
        ):
            if "submission_deadline" not in secondary_intents and classification.get("intent") != "submission_deadline":
                secondary_intents.append("submission_deadline")
        if re.search(r"\b(?:check|review|look at)\s+(?:my|the)\s+(?:case|claim)\b", lowered_request):
            if classification.get("intent") != "status_inquiry" and "status_inquiry" not in secondary_intents:
                secondary_intents.insert(0, "status_inquiry")
        seen_secondary = set()
        for secondary_name in secondary_intents:
            if secondary_name in seen_secondary:
                continue
            seen_secondary.add(secondary_name)
            if secondary_name in {classification.get("intent"), "emotional_support", "unclear"}:
                continue
            secondary_answer = answer_intent(state, {
                "relation": "follow_up", "intent": secondary_name,
                "confidence": classification.get("confidence", 0.9),
                "parent_intent_index": state.get("current_intent_index"),
                "question": text.strip(),
            }, text)
            answer = f"{answer}\n\n{secondary_answer}"
        # A customer can express emotion and ask a substantive claim question
        # in the same message. Preserve the emotional response, but also answer
        # the actionable request instead of stopping at reassurance.
        if classification["intent"] == "emotional_support":
            emotional_markers = r"\b(?:worried|anxious|scared|upset|frustrated|overwhelmed|distressed|angry|furious|sad|devastated|concerned)\b"
            actionable_text = " ".join(
                part for part in re.split(r"[.!?;]+", text)
                if part.strip() and not re.search(emotional_markers, part, re.I)
            ).strip()
            secondary = classify_intent(actionable_text, state) if actionable_text else {"intent": "unclear"}
            if secondary.get("intent") not in {"unclear", "emotional_support"}:
                answer = f"{answer}\n\n{answer_intent(state, secondary, actionable_text)}"
        intent = state["intents"][state["current_intent_index"]]
        if intent["followups"] and intent["followups"][-1].get("text") == text.strip()[:500]:
            intent["followups"][-1]["answer"] = answer
        else:
            intent["answer"] = answer
        if not answer_requires_followup(answer):
            mark_current_intent_answered(state)
        state["awaiting_additional_question"] = True
        return deduplicate_response_sections(answer), extracted
    missing = [x for x in ("full_name", "dob", "phone", "email", "ssn_last4") if x not in state["identity"]]
    return ("I understand that you want this handled quickly. To protect your claim information, I need to verify at least three identity fields. You can provide your name, date of birth, phone, email, or the last four digits of your SSN or ID." + (f" You can still provide: {', '.join(missing[:3])}." if missing else "")), extracted


def verify_id_stage(state, text):
    """Execute exactly one VERIFY_ID turn."""
    if state.get("phase", "VERIFY_ID") != "VERIFY_ID":
        raise ValueError(f"SOP stage mismatch: expected VERIFY_ID, got {state.get('phase')}")
    return _process_turn_impl(state, text)


def resolve_intent_stage(state, text):
    """Execute exactly one RESOLVE_INTENT turn.

    This is the stable migration boundary for case selection and intent
    resolution. The legacy implementation remains behind the boundary until
    its behavior is fully covered by the stage-level tests.
    """
    if state.get("phase") != "RESOLVE_INTENT":
        raise ValueError(f"SOP stage mismatch: expected RESOLVE_INTENT, got {state.get('phase')}")
    if not state.get("gates", {}).get("VERIFY_ID") or not state.get("verified_party_id"):
        return (
            "Before we select a claim, I need to verify your identity using at least three matching details.",
            {},
        )
    return _process_turn_impl(state, text)


def process_case_stage(state, text):
    """Execute exactly one authorized PROCESS_CASE turn.

    The authorization contract is enforced at the stage boundary so the
    LangGraph node cannot process claim content for an unverified identity.
    """
    if state.get("phase") != "PROCESS_CASE":
        raise ValueError(f"SOP stage mismatch: expected PROCESS_CASE, got {state.get('phase')}")
    if not state.get("gates", {}).get("VERIFY_ID") or not state.get("case_access_authorized"):
        return (
            "Before I can access claim information, I need to verify your identity and match the case to that identity.",
            {},
        )
    return _process_turn_impl(state, text)


def post_process_stage(state, text):
    """Execute exactly one post-processing turn after case handling."""
    if state.get("phase") != "POST_PROCESS":
        raise ValueError(f"SOP stage mismatch: expected POST_PROCESS, got {state.get('phase')}")
    if not state.get("gates", {}).get("PROCESS_CASE"):
        return (
            "I need to finish processing the claim before I can close the conversation or send a summary.",
            {},
        )
    return _process_turn_impl(state, text)


def _process_turn(state, text):
    """Compatibility dispatcher for callers outside LangGraph."""
    stage = {
        "VERIFY_ID": verify_id_stage,
        "RESOLVE_INTENT": resolve_intent_stage,
        "PROCESS_CASE": process_case_stage,
        "POST_PROCESS": post_process_stage,
    }.get(state.get("phase", "VERIFY_ID"), verify_id_stage)
    return stage(state, text)


def api_runtime_config():
    """Report the effective OpenAI runtime mode."""
    configured = bool(OpenAI and os.getenv("OPENAI_API_KEY"))
    return {
        "model": os.getenv("OPENAI_MODEL") or "gpt-5.6",
        "api_provider": "OpenAI" if configured else "OpenAI unavailable",
        "api_mode": configured,
    }


def reply(state, text):
    """Public compatibility wrapper routed through the LangGraph workflow."""
    from workflow import run_turn

    return run_turn(state, text)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            self.send_json(api_runtime_config())
            return
        if self.path == "/api/state":
            sid = self.headers.get("X-Session-Id")
            self.send_json(SESSIONS.get(sid, {"state": blank_state()})); return
        page = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)

    def do_POST(self):
        if self.path == "/api/reset":
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            sid = payload.get("session_id")
            if sid:
                SESSIONS.pop(sid, None)
                path = conversion_path(sid)
                if path.exists():
                    path.unlink()
            self.send_json({"reset": True})
            return
        if self.path != "/api/chat": self.send_json({"error": "not found"}, 404); return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        sid = payload.get("session_id") or str(uuid.uuid4())
        if sid in SESSIONS and SESSIONS[sid]["state"].get("disconnected"):
            sid = str(uuid.uuid4())
        session = SESSIONS.setdefault(sid, {"state": blank_state(), "messages": []})
        message = payload.get("message", "")
        answer, extracted = reply(session["state"], message)
        answer = apply_response_guards(session["state"], answer, load_json)
        session["messages"].extend([{"role": "user", "content": message}, {"role": "assistant", "content": answer}])
        save_session(sid, session)
        if session["state"].get("awaiting_additional_question") and not session["state"].get("followup_sent"):
            # Give the customer five minutes to continue before prompting once.
            Timer(300.0, send_followup_if_idle, args=(sid,)).start()
        if session["state"].get("disconnect_scheduled") and not session["state"].get("disconnected"):
            Timer(30.0, disconnect_session, args=(sid,)).start()
        runtime = api_runtime_config()
        self.send_json({
            "session_id": sid,
            "answer": answer,
            "extracted": extracted,
            "model": runtime["model"],
            "api_mode": runtime["api_mode"],
            "api_provider": runtime["api_provider"],
            "state": session["state"],
        })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Insurance SOP demo: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
