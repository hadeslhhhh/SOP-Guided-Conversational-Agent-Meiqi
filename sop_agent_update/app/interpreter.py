"""Model suggestions never authorize identity, consent, or phase changes."""
import re
import os
import json
import logging
from datetime import datetime
from typing import Protocol
from .models import Parsed, Hint, Phase

logger = logging.getLogger("sop_agent.nlu")

MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
BUSINESS_INTENTS = {
    'status', 'documents', 'submission', 'format', 'receipt', 'alternatives',
    'denial_details', 'appeal', 'payment', 'timing', 'next_steps', 'case_summary',
    'term_explanation', 'other_claim_inquiry', 'human_transfer', 'emotional_support',
    'out_of_scope', 'unclear', 'unsupported_intent',
}

CANONICAL_INTENTS = BUSINESS_INTENTS

INTENT_ALIASES = {
    "check_status": "status", "check_claim_status": "status", "inquire_status": "status",
    "claim_status": "status", "status_inquiry": "status", "check status": "status",
    "claim inquiry": "unclear", "claim_inquiry": "unclear",
    "missing_documents": "documents", "document_requirements": "documents",
    "document_requirement": "documents", "documents_required": "documents",
    "document_submission": "submission", "document_submission_method": "submission",
    "submission_deadline": "timing", "processing_time": "timing", "deadline": "timing",
    "inquire_payment_date": "payment", "payment_date": "payment",
    "speed_up_process": "next_steps", "expedite_claim": "next_steps",
    "check_deadline": "timing", "document_deadline": "timing", "ddl": "timing",
    "appeal_deadline": "appeal", "appeal_question": "appeal",
    "denial_question": "denial_details", "denial_reason": "denial_details",
    "request_denial_reason": "denial_details", "why_denied": "denial_details",
    "inquire_appeal_deadline": "appeal",
    "inquire_document_acceptability": "format", "document_acceptability": "format",
    "why_denied": "denial_details", "payment_question": "payment", "payment_amount": "payment",
    "receipt_confirmation": "receipt", "case_summary": "case_summary",
    "other_claim": "other_claim_inquiry", "switch_claim": "other_claim_inquiry",
    "human": "human_transfer", "unrelated": "out_of_scope", "out_of_scope": "out_of_scope",
}

REQUESTED_FIELD_ALIASES = {
    "appeal_deadline": "appeal_deadline", "last_day_to_appeal": "appeal_deadline",
    "document_submission_deadline": "document_submission_deadline", "submission_deadline": "document_submission_deadline",
    "payment_date": "payment_date", "payment_amount": "payment_amount",
    "processing_time": "processing_time", "missing_documents": "missing_documents",
    "document_receipt_status": "document_receipt_status", "upload_receipt_status": "document_receipt_status",
    "document_submission_channel": "document_submission_channel", "submission_channels": "document_submission_channel",
    "claim_status": "claim_status", "denial_reason": "denial_reason", "next_steps": "next_steps",
}


def canonical_requested_field(value):
    if not isinstance(value, str):
        return None
    key = re.sub(r"[-\s]+", "_", value.strip().casefold())
    return REQUESTED_FIELD_ALIASES.get(key)


def canonical_intent(value):
    if not isinstance(value, str):
        return value
    key = re.sub(r"[-\s]+", "_", value.strip().casefold())
    if key in INTENT_ALIASES:
        return INTENT_ALIASES[key]
    if key in BUSINESS_INTENTS:
        return key
    logger.warning("intent_unmapped raw=%r normalized=%r; routing to unsupported_intent", value, key)
    return "unsupported_intent"

def _secondary_intents(text, primary):
    """Preserve independent requests expressed in one customer turn."""
    rules = (
        ('status', r'\b(?:status|progress|update|where does|still being processed|still processing|under review|being reviewed|looked at)\b'),
        ('receipt', r'\b(?:did you receive|have you received|was .* received|did .* arrive|upload.*received)\b'),
        ('documents', r'\b(?:documents?|paperwork|missing|pathology|office note)\b'),
        ('submission', r'\b(?:upload|submit|send|portal|fax|mail)\b'),
        ('timing', r'\b(?:deadline|ddl|due date|how soon|how long|when)\b'),
        ('appeal', r'\b(?:appeal|challenge)\b'),
        ('payment', r'\b(?:paid|payment|reimbursement|amount|owe)\b'),
        ('next_steps', r'\b(?:what can i do next|what should i do next|what i can do next|what i should do next|what do i do now|next steps?)\b'),
        ('case_summary', r'\b(?:summarize|summary|overview|what happened)\b'),
    )
    found = [name for name, pattern in rules if re.search(pattern, text, re.I)]
    return [name for name in dict.fromkeys(found) if name != primary]


def requested_fields_from_text(text, intent=None):
    t = text.casefold()
    patterns = (
        ('claim_status', r'\b(?:status|progress|where.*stand|under review|being reviewed|looked at)\b|\bwhy\s+(?:is|was)\s+(?:it|the claim)\s+marked\s+(?:closed|open|denied)\b'),
        ('denial_reason', r'\b(?:why|reason).*(?:denied|rejected)|\bdenial(?: reason)?\b'),
        # Include common conversational misspellings such as "doucments";
        # this is a narrow field-level recovery, not a replacement for NLU.
        ('missing_documents', r'\b(?:missing|which|what).*(?:documents?|doucments?|paperwork|record)\b'),
        ('document_requirements', r'\b(?:format|file type|pdf|signature|scanned?)\b|\b(?:what|required|count as|required document)\b.*\b(?:document|file|paperwork|note|report|signature|required)\b'),
        # An email address is identity evidence.  It is a submission channel
        # only when the customer explicitly connects it to documents or
        # paperwork; otherwise identity turns must not become document turns.
        ('document_submission_channel',
         r'\b(?:upload|submit|portal|fax|mail)\b'
         r'|\b(?:send|email)\b.*\b(?:document|documents|paperwork|claim form|records?)\b'
         r'|\b(?:document|documents|paperwork|claim form|records?)\b.*\b(?:send|email)\b'),
        ('appeal_deadline', r'\b(?:appeal).*(?:deadline|when|date)|\bdeadline.*appeal|\blast day to appeal\b|\bdeadline\b(?=.*\b(?:denial|denied|rejected)\b)'),
        ('document_submission_deadline', r'\b(?:deadline|due|when).*(?:submit|send|upload|document)\b'),
        ('processing_time', r'\b(?:how long|how soon|processing time|review time)\b'),
        ('document_receipt_status', r'\b(?:did you receive|have you received|was .* received|receipt|did .* arrive|upload.*received|sent\s+(?:last|yesterday|today|\d+\s+days?))\b'),
        ('payment_amount', r'\b(?:how much (?:will|did) i (?:receive|get|owe)|reimbursement|paid|payment amount|net pay|what happened with (?:the )?payment|what about (?:the )?payment)\b'),
        ('allowed_amount', r'\b(?:what|how much).{0,30}\b(?:was|is)\s+allowed\b|\ballowed\s+(?:amount|maximum|max)\b'),
        ('payment_date', r'\b(?:payment|paid|reimbursement).*(?:date|when)|\bwhen was .*paid\b|\bhow much.*\bwhen\b'),
        ('next_steps', r'\b(?:next steps?|what should i do|what i should do|what can i do|what i can do|what do i do)\b'),
        ('case_summary', r'\b(?:summarize|summary|plain[- ]language summary|overview|what happened)\b'),
    )
    fields = [name for name, pattern in patterns if re.search(pattern, t)]
    if re.search(r'\bwhat exactly is missing\b', t) and 'missing_documents' not in fields:
        fields.append('missing_documents')
    # Preserve the semantic order for the common compound requests whose
    # second field is expressed only as "deadline" or "when".
    if re.search(r'\b(?:denial|denied|rejected)\b', t) and re.search(r'\bdeadline\b', t) and 'appeal_deadline' not in fields:
        fields.append('appeal_deadline')
    if re.search(r'\bhow much\b', t) and re.search(r'\bwhen\b', t) and 'payment_date' not in fields:
        fields.append('payment_date')
    # Compound questions such as "status and payment" often omit the
    # payment verb. Keep both requested fields so response coverage does not
    # depend on the model choosing a secondary intent.
    if re.search(r'\bstatus\b', t) and re.search(r'\bpayment\b', t) and 'payment_amount' not in fields:
        fields.append('payment_amount')
    return fields


def requested_field(text, intent=None):
    fields = requested_fields_from_text(text, intent)
    return fields[0] if fields else None


def dialogue_act(text):
    t = re.sub(r"[^a-z ]", "", text.casefold()).strip()
    if t in {"bye", "goodbye", "done", "thats all", "nothing else"}:
        return "end_conversation"
    if t in {"yes", "yes please", "sure", "okay", "ok", "no", "no thanks"}:
        return "acknowledgment"
    if re.search(r"\b(?:what do you mean|please clarify|i don't understand|can you explain)\b", t):
        return "clarification"
    return "answer_request"


def _contextual_intent(text, state, intent):
    """Resolve short follow-ups using the last stored NLU result."""
    t = text.casefold()
    previous = getattr(state, "last_nlu", None) if state else None
    if re.search(r"\b(?:why|how come)\b", t) and previous and previous.intent == "status":
        return "denial_details"
    if re.search(r"\b(?:actually|correction|no,? i mean|instead)\b", t):
        corrected = intent_of(re.sub(r"\b(?:actually|correction|no,? i mean|instead)\b", "", t), None)
        return corrected or intent
    if not intent and previous and dialogue_act(text) == "clarification":
        return "term_explanation"
    return intent

class Interpreter(Protocol):
    def interpret(self, text: str, state=None) -> Parsed: ...

def consent_of(text, context=None):
    t = re.sub(r"\s+", " ", text.lower().replace('’', "'")).strip(' .,!?:;')
    document_context = bool(re.search(r"\b(?:claim|document|documents|file|files|paperwork|upload|submit|submission|portal|fax|mailing)\b", t))
    in_summary_context = bool(
        context and (
            getattr(getattr(context, 'phase', None), 'value', getattr(context, 'phase', None)) == 'POST_PROCESS'
            or getattr(context, 'email_consent', None) is not None
            and getattr(getattr(context, 'email_consent', None), 'value', getattr(context, 'email_consent', None))
            in {'accepted', 'declined', 'revoked'}
        )
    )
    if (re.fullmatch(r"(?:no(?: thanks| thank you)?|no email(?: summary)?(?:,? thank you)?|skip(?: (?:it|the email|the summary))?|decline|not now)", t)
            or re.fullmatch(r"no,? do not send (?:an )?email(?: summary)?", t)
            or re.search(r"\b(?:no|without)\b.*\b(?:summary|recap)\b.*\bemail\b|\b(?:summary|recap)\b.*\bemail\b.*\b(?:no|without)\b", t)
            or re.search(r"\b(?:don't|do not|never|please don't)\b\s+(?:send|email|mail)(?:\s+me)?(?:\s+(?:the|a))?\s*(?:conversation\s+)?(?:summary|recap|email)\b", t)
            or re.search(r"\bno need to\s+(?:send|email|mail)(?:\s+me)?(?:\s+(?:the|a))?\s*(?:conversation\s+)?(?:summary|recap)\b", t)
            or ((in_summary_context or getattr(context, 'case_id', None))
                and not document_context
                and re.fullmatch(r"(?:skip|do not|don't|please don't)\s+(?:send|sending)\s+(?:anything|it)", t))
            or re.fullmatch(r"(?:don't|do not|please don't) email me", t)
            or (not document_context and re.search(
                r"\bno\b.*\b(?:email|send|mail)\b|\b(?:don't|do not|please don't|no need to|skip)\b.*\b(?:email|send|mail)\b",
                t,
            ))):
        return 'skip_email'
    if re.fullmatch(r"(?:wait,? )?cancel(?: that| the)? email(?: summary)?|revoke(?: the)? email consent", t):
        return 'revoke_email'
    # Full utterance matching intentionally leaves questions/qualifications unresolved.
    if (re.fullmatch(r'(yes(?: please)?(?:,? send (?:it|the summary)(?: by email)?|,? email it|,? email me (?:the )?(?:conversation )?summary(?: by email)?)?|yes,? send me (?:the )?(?:conversation )?summary(?: by email)?|send(?: (?:it|the summary|the email))(?: by email)?(?: please)?|please send(?: (?:it|the summary|the email))(?: by email)?|please email me (?:the )?(?:conversation )?summary|email me (?:the )?(?:conversation )?summary|sure|okay|ok|please do)', t)
            or (not document_context and re.search(
                r"\b(?:yes|sure|okay|ok|please do)\b.*\b(?:email|send|mail)\b.*\b(?:summary|recap|next steps|follow[- ]?up|outcome|conversation)\b"
                r"|\b(?:email|send|mail)\b.*\b(?:summary|recap|next steps|follow[- ]?up|outcome)\b",
                t,
            ))):
        return 'send_email'
    return None


def email_consent_kind(text, context=None):
    """Classify summary consent independently from claim/document intent."""
    return consent_of(text, context)


def _explicit_human_request(text):
    # Do not treat a bare "human"/"representative" mention as a transfer.
    # Match the meaning of a request for a live person, not only one keyword.
    # These patterns are deliberately narrow safety routing, while the API
    # remains authoritative for ordinary business intent.
    return bool(re.search(
        r"\b(?:speak|talk|connect|transfer|put)\b\s+(?:me\s+)?(?:through\s+)?(?:with\s+|to\s+)?(?:a\s+)?(?:human|person|representative|supervisor|live\s+agent|real\s+person)\b"
        r"|\b(?:human\s+(?:representative|transfer)|transfer\s+to\s+(?:a\s+)?human|live\s+(?:agent|person)|real\s+person)\b"
        r"|\b(?:transfer|put)\s+me\b|\bconnect\s+me\s+(?:with|to)\s+(?:a\s+)?(?:human|person|representative|agent)\b"
        r"|\bswitch\s+to\s+(?:a\s+)?(?:human|person|representative|live\s+agent)\b"
        r"|\b(?:want|need|would\s+like)\s+(?:a\s+)?(?:human|person|representative|supervisor|live\s+agent)\b"
        r"|\b(?:can|could|may)\s+I\s+(?:speak|talk)\s+(?:to|with)\s+(?:someone|a\s+person|an?\s+agent|customer\s+service)\b"
        r"|\b(?:let|please\s+let)\s+me\s+(?:speak|talk)\s+(?:to|with)\s+(?:someone|a\s+person|an?\s+agent)\b"
        r"|\b(?:get|move|escalate)\s+(?:this|me)\s+(?:to|over\s+to)\s+(?:a\s+)?(?:person|human|supervisor|manager|agent|customer\s+service)\b"
        r"|\b(?:stop|remove)\s+(?:the\s+)?(?:bot|automated\s+system)\b.{0,25}\b(?:person|agent|someone|customer\s+service)\b"
        r"|\b(?:I\s+need|give\s+me)\s+(?:a\s+)?(?:live|real)\s+(?:person|agent|operator)\b",
        text, re.I,
    ) and not re.search(r"\b(?:no|don't|do not)\b.{0,20}\b(?:transfer|connect|put me through)\b", text, re.I))


def _identity_only_turn(text, parsed):
    text_fields = requested_fields_from_text(text)
    # An email address is identity evidence, not a document-submission
    # request. Treat that single deterministic artifact as empty for the
    # identity-only decision, while preserving real document wording.
    email_field_artifact = (
        text_fields == ["document_submission_channel"]
        and not re.search(
            r"\b(?:document|documents|paperwork|upload|submit|submission|portal|fax|mail)\b",
            text,
            re.I,
        )
    )
    return bool(parsed.identity) and (
        is_identity_update(text)
        or ((not text_fields or email_field_artifact)
            and not re.search(r"\b(?:status|claim|case|document|payment|appeal|denied|summary|next steps?)\b", text, re.I))
    )


def is_identity_update(text):
    """Recognize an explicit identity correction, not a claim-field request."""
    return bool(re.search(
        r"\b(?:actually|correct(?:ly)?|correction|sorry|i meant)\b.*\b(?:ssn|social security|national id|id)\b.*\b(?:last\s*(?:four|4)|digits?|ends?)\b"
        r"|\b(?:ssn|social security|national id|id)\b.*\b(?:last\s*(?:four|4)|digits?|ends?)\b.*\b(?:actually|correct|sorry|meant)\b"
        r"|\b(?:actually|correct(?:ly)?|correction|sorry|i meant)\b.*\blast\s*(?:four|4)\b",
        text.replace('’', "'"), re.I,
    ))

def decision_topics(text, previous=None):
    """Collect independent questions instead of choosing one keyword winner."""
    t = text.lower().replace('’', "'")
    denial = bool(re.search(r"\b(denied|denial|rejected|refused|turned down|not approved|didn't (?:you )?approve|did not (?:you )?approve)\b", t))
    notification = bool(re.search(r"\b(notified|notification|notify|notice|wasn't (?:i |we )?told|was (?:i |we )?not told|was not told|weren't (?:we )?told|never (?:told|tell)|didn't tell|did not tell)\b", t))
    date_question = bool(re.search(r"\b(when|what date|which date)\b", t))
    context = denial or previous in ('denial', 'appeal', 'denial_details')
    topics = []
    if denial:
        topics.append('reason')
        if re.search(r'\bappeal\b', t):
            topics.append('appeal_decision')
    if date_question and context and (denial or not notification):
        topics.append('decision_date')
    if notification:
        topics.append('notification')
    return topics


def intent_of(text, previous=None):
    t = text.lower().replace('’', "'")
    if not re.search(r"\b(not|don't|do not|never)\b", t) and re.search(r"\b(done|finished|goodbye|bye|nothing else|no more questions|all set|that's (?:all|everything)|that is (?:all|everything))\b", t):
        return 'finish'
    topics = decision_topics(t, previous)
    if topics and (any(topic != 'reason' for topic in topics) or not re.search(r'\b(documents?|paperwork|pathology|office note|missing)\b', t)):
        return 'denial_details'
    rules = (
        ('other_claim_inquiry', r'\b(?:another|different|other|switch|change)\s+(?:claim|case)\b'),
        ('human_transfer', r'\b(?:human|representative|supervisor|live agent|transfer me)\b'),
        ('term_explanation', r'\b(?:what does .* mean|explain|definition|what is a deductible|pathology report mean|office note mean)\b'),
        ('case_summary', r'\b(?:summarize|summary|overview|what happened)\b'),
        ('alternatives', r"\b(can't|cannot|unable|don't have|do not have|replacement|substitute|alternative)\b"),
        ('timing', r'\b(deadline|ddl|due date|how long|how soon|processing time|review time|when.*submit)\b'),
        ('receipt', r'\b(?:did you receive|have you received|was .* received|confirm(?:ed)? receipt|did .* arrive|upload.*received|received.*files)\b'),
        ('format', r'\b(format|file type|scan|pdf|clear enough)\b'),
        ('submission', r'\b(upload|submit|send|portal|fax|mail)\b'),
        ('appeal', r'\b(appeal|deadline|expired|missed)\b'),
        ('documents', r'\b(documents?|paperwork|pathology|office note|missing)\b'),
        ('denial', r'\b(denial|denied|reason|rejected)\b'),
        ('payment', r'\b(paid|payment|reimbursement|net pay|amount)\b'),
        ('payment', r'\bhow much will i receive\b'),
        ('next_steps', r'\b(?:what can i do next|what should i do next|what i can do next|what i should do next|what do i do now|next steps?)\b'),
        ('case_summary', r'\b(?:summarize|summary|overview|what happened)\b'),
        ('status', r'\b(status|progress|update|still being processed|still processing|under review|being reviewed|looked at|where does .* stand)\b'),
        ('next_steps', r'\b(next steps?|what (?:can|should) i do|options)\b'),
    )
    return next((name for name, pattern in rules if re.search(pattern, t)), None)

def normalize_dob(value):
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%m/%d/%Y', '%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(' '.join(value.replace(',', '').split()), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract_identity(text):
    """Extract only values explicitly present in this utterance."""
    identity = {}
    t = text.replace('’', "'")
    # Accept an opening verification tuple ending at a sentence boundary.
    # Later claim context must not invalidate it or become identity evidence.
    month_pattern = '|'.join(month + '|' + month[:3] for month in MONTHS)
    date_pattern = rf'(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{8}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}|(?:{month_pattern})\s+\d{{1,2}},?\s+\d{{4}})'
    compact = re.match(
        rf"\s*(?:(?P<name>[A-Za-z]+(?:[ '-][A-Za-z]+){{0,3}}?)[,;\s]+)?"
        rf'(?P<dob>{date_pattern})(?:[,;\s]+(?P<last4>\d{{4}}))?\s*(?=$|[.!?](?:\s|$))',
        t, re.I,
    )
    if compact and not re.search(r'\b(claim|filed|incident|accident|policy|payment|paid|report)\b', compact.group('name') or '', re.I):
        dob = normalize_dob(compact.group('dob'))
        if dob:
            identity['dob'] = dob
        if compact.group('last4'):
            identity['id_last4'] = compact.group('last4')
        full_name = compact.group('name') or ''
        if len(full_name.split()) >= 2 and not re.search(r'\b(?:dob|date|birth|ssn|phone|email|policy)\b', full_name, re.I):
            identity['name'] = full_name
    # Bound a name at a clause boundary rather than greedily consuming the next field.
    name = re.search(r"\b(?:my name is|name is)\s+(.+?)(?=\s+(?:and|policy|dob|ssn|phone|email|calling)\b|[,.;!?]|$)", t, re.I)
    if not name:
        name = re.search(r"\b(?:i am|i'm|this is|it is)\s+(.+?)(?=\s+(?:and|policy|dob|ssn|phone|email|calling)\b|[,.;!?]|$)", t, re.I)
    candidate = name.group(1).strip() if name else ''
    if name and not re.search(r'\b(?:my name is|name is|it is)\b', t, re.I) and not all(part[0].isupper() for part in candidate.split()):
        candidate = ''
    if not candidate and re.match(r'^[A-Za-z]+(?:[ -][A-Za-z]+){1,3}\s*,\s*\d{4}-', t):
        candidate = t.split(',')[0].strip()
    if not candidate:
        leading_name = re.match(r'^\s*([A-Za-z]+(?:[ -][A-Za-z]+){1,3})\s*,\s*(?:dob|date of birth|born)\b', t, re.I)
        if leading_name:
            candidate = leading_name.group(1).strip()
    if re.fullmatch(r"[A-Za-z]+(?:[ '-][A-Za-z]+){1,3}", candidate) and not re.search(r'\b(not|upset|worried|angry|calling|policyholder|confused|done|terrified|very|refusing|dob|date|birth|ssn|phone|email|policy)\b', candidate, re.I):
        identity['name'] = candidate
    date_match = re.search(r'\b(?:dob|date of birth|born(?: on)?|birthday)\s*(?:is|:)?\s*(\d{8}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]+ \d{1,2},? \d{4})', t, re.I)
    if not date_match:
        date_match = re.search(r'\b\d{4}-\d{2}-\d{2}\b', t) if re.fullmatch(r'[A-Za-z ,\d-]+', t) and not re.search(r'claim|filed|incident', t, re.I) else None
    if date_match:
        value = date_match.group(1) if date_match.lastindex else date_match.group()
        dob = normalize_dob(value)
        if dob:
            identity['dob'] = dob
    patterns = {
        'phone': r'\b(?:phone|mobile)(?: number)?\s*(?:is|:)?\s*(\+?\d[\d ()-]{8,}\d)',
        'id_last4': r'\b(?:ssn|social security|national id|id)\s*(?:(?:last\s*(?:four|4)(?:\s*digits)?)|(?:ends?|ending)\s*in)?\s*(?:of\s+my\s+)?(?:is|are|:)?\s*(\d{4})\b',
        'email': r'\b([\w.+-]+@[\w.-]+\.[a-z]{2,})\b',
        'policy_number': r'\b(POL-\d+)\b',
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, t, re.I)
        if match:
            identity[field] = match.group(1)
    last4 = re.search(r'\blast\s*(?:four|4)\s*(?:digits)?\s*(?:is|are|:)?\s*(\d{4})\b', t, re.I)
    if last4:
        identity['id_last4'] = last4.group(1)
    if re.fullmatch(r'(?:[A-Za-z]+(?:[ -][A-Za-z]+){1,3},\s*)?\d{4}-\d{2}-\d{2}\s*[,;]\s*\d{4}\s*', t):
        identity['id_last4'] = t.strip()[-4:]
    return identity

class RuleInterpreter:
    def interpret(self, text: str, state=None):
        t = text.replace('’', "'")
        identity = extract_identity(t)
        # A full-name answer can complete an earlier compact partial answer.
        if (state and state.phase == Phase.VERIFY_ID and 'name' not in state.identity
                and {'dob', 'id_last4'} <= state.identity.keys()
                and re.fullmatch(r"[A-Za-z]+(?:[ '-][A-Za-z]+){1,3}", t.strip())
                and not re.search(r"\b(no|not|thanks|thank|please|help|angry|upset|worried|refuse|don't|skip|human|representative)\b", t, re.I)):
            identity['name'] = t.strip()
        hint_text = t
        if identity.get('dob'):
            hint_text = re.sub(
                r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})\b',
                lambda match: '' if normalize_dob(match.group()) == identity['dob'] else match.group(),
                t,
            )
        hint = Hint()
        case = re.search(r'\bCL-\d+\b', t, re.I)
        if case:
            hint.case_id = case.group().upper()
        policy = re.search(r'\bPOL-\d+\b', t, re.I)
        if policy:
            hint.policy_number = policy.group().upper()
        for kind in ('healthcare', 'dental', 'auto'):
            if re.search(rf'\b{kind}\b', t, re.I):
                hint.case_type = kind
        excluded_type = re.search(r'\b(?:not|instead of|rather than)\s+(?:the\s+)?(healthcare|dental|auto)\b', t, re.I)
        if excluded_type:
            hint.excluded_case_type = excluded_type.group(1).casefold()
        for status in ('denied', 'closed', 'open'):
            if re.search(rf'\b{status}\b', t, re.I):
                hint.status = status
        if re.search(r'\b(?:completed|settled|resolved)\b', t, re.I):
            hint.status = 'closed'
        if 'dob' not in identity or re.search(r'\b(claim|filed|incident)\b', t, re.I):
            for number, month in enumerate(MONTHS, 1):
                if re.search(rf'\b{month[:3]}(?:{month[3:]})?\b', hint_text, re.I):
                    hint.month = number
            year = re.search(r'\b(?:claim|filed|from|in)\s+(?:\w+\s+)?(20\d{2})\b', t, re.I)
            if year:
                hint.year = int(year.group(1))
        intent = intent_of(t, state.current_intent if state else None)
        intent = _contextual_intent(t, state, intent)
        if state and state.phase == Phase.POST_PROCESS:
            intent = consent_of(t) or (intent if intent in BUSINESS_INTENTS else None)
        emotion_patterns = {
            "angry": r"\b(?:angry|furious|ridiculous|unfair|fed up|sick of)\b",
            "frustrated": r"\b(?:frustrated|frustrating|ignored|taking forever|already told you|exhausting|exhausted|stuck)\b",
            "anxious": r"\b(?:anxious|worried|worrying|stressed|scared|nervous|panicking|panic|cannot cope|can't cope)\b",
            "confused": r"\b(?:confused|confusing|unclear|nobody has explained|do not understand|don't understand)\b",
            "distressed": r"\b(?:terrified|overwhelmed|distressed|helpless|unbearable|want to die|kill myself|end my life)\b",
        }
        emotion_hits = [kind for kind, pattern in emotion_patterns.items() if re.search(pattern, t, re.I)]
        emotion = bool(emotion_hits)
        emotion_type = "neutral"
        for preferred in ("distressed", "angry", "anxious", "frustrated", "confused"):
            if preferred in emotion_hits:
                emotion_type = preferred
                break
        strong_emotion = bool(re.search(r"\b(?:want to die|kill myself|end my life|cannot cope|can't cope|panicking|unbearable)\b", t, re.I))
        emotion_level = "high" if strong_emotion or len(emotion_hits) >= 2 else "medium" if emotion else "low"
        refusal = bool(re.search(r"\b(refuse|refusing|won't provide|will not provide|don't want to (?:give|share|provide)|do not want to (?:give|share|provide))\b", t, re.I))
        # A transfer requires an explicit request, not a bare mention of a
        # human/representative in an identity or support sentence.
        human = bool(re.search(
            r"\b(?:speak|talk|connect|transfer|put)\b\s+(?:me\s+)?(?:through\s+)?(?:with\s+|to\s+)?(?:a\s+)?(?:human|person|representative|supervisor|live\s+agent|real\s+person)\b"
            r"|\b(?:human\s+(?:representative|transfer)|transfer\s+me|live\s+(?:agent|person)|real\s+person)\b"
            r"|\b(?:want|need|would\s+like)\s+(?:a\s+)?(?:human|person|representative|supervisor|live\s+agent)\b",
            t, re.I,
        )) and not re.search(r"\b(?:no|don't|do not)\b.{0,20}\b(?:transfer|connect|put me through)\b", t, re.I)
        related = bool(identity or hint.model_dump(exclude_none=True) or intent or emotion or refusal or human or re.search(r'\b(insurance|claim|verification|identity|consent|email|hello|hi|thanks|thank you|yes|no|okay|ok|sure|why|help|correct|forget|remove)\b', t, re.I))
        explicit_offtopic = bool(re.search(
            r'\b(?:reinforcement learning|what is rl|what rl is|what does rl mean|'
            r'quantum mechanics|neural network|python tutorial|write code|weather|'
            r'sports scores?|recipe|joke|capital of france)\b', t, re.I))
        act = dialogue_act(t)
        correction = bool(re.search(r'\b(?:actually|correction|no,? i mean|instead)\b', t, re.I))
        secondary = _secondary_intents(t, intent)
        if emotion and intent not in {'emotional_support', 'unclear'}:
            secondary.append('emotional_support')
        if not intent and act not in {'acknowledgment', 'end_conversation'}:
            intent = 'unclear'
        inferred_fields = requested_fields_from_text(t, intent)
        return Parsed(identity=identity, hint=hint, intent=intent, raw_intent=intent,
                      secondary_intents=list(dict.fromkeys(secondary)),
                      requested_field=inferred_fields,
                      requested_fields=inferred_fields,
                      dialogue_act=act,
                      needs_clarification=intent == 'unclear' or act == 'clarification',
                      ambiguous='requested_action' if intent == 'unclear' else None,
                      correction=correction,
                      normalized_request=t[:500],
                      context={'previous_intent': state.last_nlu.intent} if state and state.last_nlu else {},
                      confidence=0.9 if intent not in {'unclear', None} else 0.35,
                      emotion=emotion_type if emotion else None,
                      emotion_type=emotion_type,
                      emotion_level=emotion_level,
                      emotion_evidence=emotion_hits,
                      workflow_action="request_human" if human else "none",
                      refusal=refusal, human=human, unrelated=explicit_offtopic or not related)


class OpenAIStructuredInterpreter:
    """OpenAI-only structured interpreter; there is no local NLU fallback."""
    def __init__(self, model=None, client=None):
        from openai import OpenAI
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.client = client or OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=float(os.getenv("OPENAI_TIMEOUT", "30")),
            max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "1")),
        )

    def interpret(self, text: str, state=None) -> Parsed:
        prior = {
            "phase": getattr(getattr(state, "phase", None), "value", None),
            "identity": getattr(state, "identity", {}),
            "verified": getattr(state, "verified", False),
            "party_id": getattr(state, "party_id", None),
            "current_intent": getattr(state, "current_intent", None),
            "case_id": getattr(state, "case_id", None),
            "claim_history": getattr(state, "claim_history", []),
        }
        context = json.dumps(prior, ensure_ascii=False)
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": (
                    "Classify the current insurance support message. Return only valid JSON matching the Parsed schema. "
                    "Every response must include an identity object, a hint object, intent, secondary_intents, "
                    "requested_field and requested_fields (requested_fields must include every distinct field in the message), "
                    "Tolerate minor spelling errors and infer the intended supported term from context; for example, "
                    "doucments means documents and procssing means processing. "
                    "confidence as a number from 0 to 1, dialogue_act, needs_clarification, correction, emotion, "
                    "emotion_type as exactly one of neutral, worried, anxious, frustrated, angry, sad, distressed, or confused; "
                    "emotion_level as exactly one of low, medium, or high; emotion_evidence as a list of exact customer-language "
                    "The hint object may contain case_id, case_type (healthcare, auto, or dental), month (1-12), year, status, "
                    "policy_number, and excluded_case_type (healthcare, auto, or dental). Preserve every explicit hint. "
                    "or conversation-behavior signals; human, unrelated, refusal, "
                    "classification_status, and workflow_action. workflow_action must be one of none, "
                    "switch_claim, verify_identity, request_human, send_email, skip_email, revoke_email, or finish. "
                    "Canonical business intents are exactly: status, documents, submission, format, receipt, "
                    "alternatives, denial_details, appeal, payment, timing, next_steps, case_summary, "
                    "term_explanation, other_claim_inquiry, human_transfer, emotional_support, unsupported_intent, out_of_scope, "
                    "or unclear. Prefer these canonical values directly; do not invent labels. "
                    "Canonical requested_fields are exactly: appeal_deadline, document_submission_deadline, payment_date, "
                    "payment_amount, processing_time, missing_documents, document_receipt_status, "
                    "document_submission_channel, claim_status, denial_reason, next_steps. "
                    "Never classify requests for internal adjuster notes, internal review notes, staff-only comments, "
                    "fraud-review notes, or internal claim annotations as verify_identity. These are sensitive unsupported "
                    "information requests and must use primary_intent=unsupported_intent and workflow_action=none. "
                    "Extract identity fields explicitly stated "
                    "in the current message: name, dob, phone, email, id_last4, and policy_number. "
                    "Extract claim IDs such as CL-2048 into hint.case_id. Never invent identity, claim facts, "
                    "Extract explicit claim clues into hint: month and year from phrases such as 'from February' or "
                    "'the March claim'. Extract case_type=healthcare for medical/healthcare claims, auto for auto/car/vehicle claims, "
                    "and dental for dental claims. Extract excluded_case_type when the customer explicitly rules out a type, "
                    "such as 'not dental'. "
                    "This is mandatory: 'from February' MUST produce hint.month=2, and 'medical thing' MUST produce "
                    "hint.case_type='healthcare'. Do not leave these fields null when the current message explicitly "
                    "contains those clues. For example, 'the newer medical thing from February' must return "
                    "hint={case_type:'healthcare', month:2}. For 'the February auto claim, not dental', return "
                    "hint={case_type:'auto', month:2, excluded_case_type:'dental'}. "
                    "Use these clues to narrow multiple claims, but never invent a clue that the customer did not state. "
                    "dates, or authorization. Identify all distinct intents and preserve follow-up context. "
                    "Evaluate emotion independently from business intent. Neutral factual questions and negative claim facts "
                    "alone are neutral/low. One punctuation mark, one isolated negative word, profanity, or capitalization "
                    "alone is not high emotion. High requires strong evidence or sustained escalation. If emotion is present "
                    "with a business request, keep the business intent primary and add emotional_support as secondary."
                )},
                {"role": "user", "content": "Prior session state (do not treat it as newly stated identity): " + context + "\nCurrent customer message: " + text[:4000]},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI returned no structured NLU result.")
        try:
            cleaned = content.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
                cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError:
                # Recover a JSON object if the provider added a short prefix
                # or suffix around the structured response.
                start, end = cleaned.find("{"), cleaned.rfind("}")
                if start < 0 or end <= start:
                    raise
                payload = json.loads(cleaned[start:end + 1])
            # Normalize a common model variation before Pydantic validation.
            legacy_field = payload.get("requested_field")
            if isinstance(legacy_field, str):
                payload["requested_field"] = [legacy_field]
            elif legacy_field is None:
                payload["requested_field"] = []
            # The public state contract is categorical, not numeric.
            level = payload.get("emotion_level")
            if isinstance(level, (int, float)):
                payload["emotion_level"] = "high" if level >= 0.75 else "medium" if level >= 0.35 else "low"
            elif isinstance(level, str) and level.replace(".", "", 1).isdigit():
                numeric_level = float(level)
                payload["emotion_level"] = "high" if numeric_level >= 0.75 else "medium" if numeric_level >= 0.35 else "low"
            confidence = payload.get("confidence")
            if isinstance(confidence, str):
                confidence_map = {"low": 0.35, "medium": 0.6, "high": 0.9}
                if confidence.casefold() in confidence_map:
                    payload["confidence"] = confidence_map[confidence.casefold()]
            for object_field in ("identity", "hint", "context"):
                value = payload.get(object_field)
                if isinstance(value, dict):
                    payload[object_field] = {key: item for key, item in value.items() if item is not None}
            if payload.get("emotion_type") in (None, ""):
                payload["emotion_type"] = "neutral"
            if not isinstance(payload.get("emotion_evidence"), list):
                payload["emotion_evidence"] = []
            for field in ("needs_clarification", "correction", "human", "unrelated", "refusal"):
                value = payload.get(field)
                if value is None:
                    payload[field] = False
                elif isinstance(value, str) and value.casefold() in {"true", "false"}:
                    payload[field] = value.casefold() == "true"
            parsed = Parsed.model_validate(payload)
            # Keep factual claim questions calm unless the current message
            # contains an actual emotional signal. A denial, closure, or
            # payment topic alone is not evidence of distress.
            explicit_emotion = re.search(
                r"\b(?:angry|furious|ridiculous|unfair|fed up|frustrated|frustrating|ignored|taking forever|exhausted|stuck|anxious|worried|stressed|scared|nervous|panicking|confused|confusing|overwhelmed|distressed|helpless|unbearable)\b",
                text,
                re.I,
            )
            if not explicit_emotion:
                parsed.emotion_type = "neutral"
                parsed.emotion_level = "low"
                parsed.emotion = None
                parsed.emotion_evidence = []
            raw_intent = parsed.intent
            # The prompt exposes the semantic action separately from the
            # business intent. Normalize it into the workflow contract before
            # routing; otherwise email consent is silently treated as a claim
            # summary or document request.
            action_to_workflow = {
                "request_human": "request_human",
                "send_email": "send_email",
                "skip_email": "skip_email",
                "revoke_email": "revoke_email",
            }
            if payload.get("action") in action_to_workflow and parsed.workflow_action == "none":
                parsed.workflow_action = action_to_workflow[payload["action"]]
            parsed.raw_intent = raw_intent
            logger.info(
                "structured_nlu_fields raw_intent=%s raw_requested_fields=%s raw_requested_field_count=%d",
                raw_intent,
                [str(value) for value in (parsed.requested_fields or [])],
                len(parsed.requested_fields or []),
            )
            # Workflow actions are not business intents. Keep them out of
            # canonical_intent() so valid identity/transfer/completion turns
            # cannot be downgraded to unsupported_intent.
            workflow_by_intent = {
                "verify_identity": "verify_identity",
                "request_human": "request_human",
                "human_transfer": "request_human",
                "finish": "finish",
                "send_email": "send_email",
                "skip_email": "skip_email",
                "revoke_email": "revoke_email",
            }
            workflow_signal = raw_intent if raw_intent in workflow_by_intent else parsed.workflow_action
            if workflow_signal in workflow_by_intent:
                parsed.workflow_action = workflow_by_intent[workflow_signal]
                parsed.intent = "human_transfer" if workflow_signal in {"request_human", "human_transfer"} else (
                    "finish" if workflow_signal == "finish" else "unclear"
                )
            else:
                parsed.intent = canonical_intent(raw_intent)
            explicit_human = _explicit_human_request(text)
            consent_kind = email_consent_kind(text, state)
            # Consent is a workflow action, not a claim intent.  The API
            # sometimes returns case_summary/other_claim_inquiry for these
            # short utterances, so the narrow consent recognizer normalizes
            # the action contract without changing ordinary business NLU.
            if consent_kind and not explicit_human:
                parsed.workflow_action = consent_kind
                parsed.raw_intent = consent_kind
                parsed.intent = "unclear"
            if not explicit_human and (parsed.human or parsed.workflow_action == "request_human"
                                       or parsed.intent == "human_transfer"):
                parsed.human = False
                parsed.workflow_action = "none"
                if parsed.intent == "human_transfer":
                    parsed.intent = "unclear"
                    parsed.raw_intent = "verify_identity" if parsed.identity else "unclear"
            parsed.requested_fields = [canonical_requested_field(item) for item in parsed.requested_fields]
            # Identity evidence is never a business request.  Some API
            # responses echo fields such as dob/email/id_last4 in
            # requested_fields; carrying those into workflow state creates
            # false clarification requests after verification.
            identity_fields = {"name", "dob", "phone", "email", "id_last4", "policy_number"}
            parsed.requested_fields = [
                item for item in parsed.requested_fields
                if item and item not in identity_fields
            ]
            parsed.requested_field = [canonical_requested_field(item) or item for item in parsed.requested_field]
            parsed.requested_field = [item for item in parsed.requested_field if item not in identity_fields]
            for item in parsed.requested_field:
                if item not in parsed.requested_fields:
                    parsed.requested_fields.append(item)
            if consent_kind:
                # Summary consent is a workflow action, not a business field
                # request. In particular, the word "email" must not create
                # document_submission_channel here.
                parsed.requested_fields = []
                parsed.requested_field = []
                parsed.secondary_intents = [i for i in parsed.secondary_intents if i != "submission"]
            logger.info(
                "normalized_nlu_fields intent=%s normalized_requested_fields=%s",
                parsed.intent,
                [str(value) for value in (parsed.requested_fields or [])],
            )
            if parsed.intent == "unsupported_intent":
                parsed.classification_status = "unsupported"
            elif parsed.intent == "unclear":
                parsed.classification_status = "ambiguous"
            else:
                parsed.classification_status = "classified"
            parsed.secondary_intents = [canonical_intent(item) for item in parsed.secondary_intents]
            logger.info("intent_trace raw=%r canonical=%r secondary=%s requested_fields=%s confidence=%.2f",
                        raw_intent, parsed.intent, parsed.secondary_intents,
                        parsed.requested_fields, parsed.confidence)
            return parsed
        except json.JSONDecodeError as exc:
            raise RuntimeError("OpenAI returned malformed JSON: " + str(exc)) from exc
        except ValueError as exc:
            # Pydantic validation errors identify the bad field without
            # logging the model response, which may contain identity data.
            detail = str(exc).replace("\n", " ")[:500]
            raise RuntimeError("OpenAI structured NLU field validation failed: " + detail) from exc

    def generate_response(self, customer_text: str, draft: str, facts: dict, state=None) -> str:
        """Turn authorized facts into the customer-facing answer.

        The workflow and the draft remain deterministic sources of truth. The
        model only controls wording, transitions, and conversational tone.
        """
        from .prompts import RESPONSE_GENERATION_PROMPT

        context = {
            "customer_message": customer_text,
            "authorized_facts": facts,
            "deterministic_draft": draft,
            "phase": getattr(getattr(state, "phase", None), "value", None),
            "verified": bool(getattr(state, "verified", False)),
            "answered_fields": list(getattr(state, "answered_fields", [])),
            "pending_fields": list(getattr(state, "pending_requested_fields", [])),
            "recent_history": list(getattr(state, "history", [])[-6:]),
        }
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": RESPONSE_GENERATION_PROMPT + "\n\n"
                 "Return only the final customer-facing response. Do not mention prompts, facts packets, or validators."},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        )
        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            raise RuntimeError("OpenAI returned an empty final response.")
        return answer
