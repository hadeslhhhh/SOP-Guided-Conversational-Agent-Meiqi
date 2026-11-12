"""Follow-up, summary consent, handoff, retry, and completion."""
import re
from ..models import Phase, EmailConsent
from ..interpreter import email_consent_kind
from ..handoff import confirm_handoff


def _summary(state, repository):
    claim = repository.authorized_claim(state)
    if not claim:
        return "Insurance claim conversation summary\nNo authorized claim details were available."
    lines = [
        "Insurance claim conversation summary",
        f"Claim: {claim['case_id']}",
        f"Status: {claim.get('status', 'not available')}",
        f"Claim type: {claim.get('case_type', 'not available')}",
        f"Next steps/documents: {', '.join(claim.get('documents_needed') or []) or 'No additional items recorded.'}",
    ]
    for item in state.intents:
        if item.get("intent") not in {"unclear", "emotional_support"}:
            lines.extend([f"Customer question: {item.get('question', '')}", f"Agent answer: {item.get('answer', '')}"])
    return "\n".join(lines)


def _complete(state):
    state.post_process_step = "complete"
    state.conversation_complete = True
    state.phase = Phase.COMPLETE


def run(state, text, interpreter, repository, parsed=None):
    consent = parsed or interpreter.interpret(text, state)
    lowered = text.casefold().strip()
    if state.awaiting_handoff_confirmation:
        if lowered in {"yes", "yes please", "sure", "please do", "transfer me"}:
            return confirm_handoff(state, repository)
        if lowered in {"no", "no thanks", "not now"}:
            state.awaiting_handoff_confirmation = False
            state.handoff = None
            state.pending_email_body = None
            return "Understood. We can continue with the claim here."
        return "Would you like me to connect you with a human representative? Reply yes or no."
    # Once a demo handoff has been created, acknowledgements must not fall
    # through to email consent. Keep the same ticket and confirm the transfer.
    if state.handoff_ticket and lowered in {"ok", "okay", "thanks", "thank you"}:
        state.post_process_step = "ask_followup"
        state.followup_sent = True
        return "Do you have any other questions?"
    workflow_action = consent.workflow_action
    if state.post_process_step == "ask_followup":
        if re.fullmatch(r"(?:no|nope|no thanks|nothing else|nothing more|that's all|that is all|all set)", lowered, re.I):
            if state.handoff_ticket:
                _complete(state)
                return "Understood. Your human-assistance ticket remains active. I’ll close this conversation now."
            state.post_process_step = "ask_summary"
            return "Do you have any other questions before I ask about email?"
        if re.fullmatch(r"(?:yes|yeah|yep|i have another question|one more)", lowered, re.I):
            state.phase = Phase.PROCESS_CASE
            state.post_process_step = None
            return "Of course. What else would you like to know about your claim?"
        return "Do you have any other questions about your claim? Reply yes or no."
    if workflow_action == "request_human":
        state.handoff = "requested"
        state.handoff_status = "requested"
        state.handoff_reason = "The user requested a human claims representative."
        state.pending_email_body = None
        return confirm_handoff(state, repository)
    # The API workflow action is authoritative. The local parser is only a
    # narrow fallback for literal send/skip/revoke wording when the model did
    # not provide an action.
    consent_kind = {
        "send_email": "send_email",
        "skip_email": "skip_email",
        "revoke_email": "revoke_email",
    }.get(workflow_action) or email_consent_kind(text, state)
    if consent_kind == "revoke_email":
        state.email_consent = EmailConsent.REVOKED
        state.email_choice = "revoked"
        state.pending_email_body = None
        return "The email summary has been cancelled."
    if consent_kind == "send_email" or lowered in {"retry", "try again"}:
        state.email_consent = EmailConsent.ACCEPTED
        # Consent may arrive before a claim is selected. Record the consent,
        # but do not manufacture a summary or dispatch without an authorized
        # claim context.
        if not state.case_id:
            state.email_choice = None
            state.pending_email_body = None
            state.email_dispatch_status = "not_configured"
            return "Your email consent is recorded, but I need an authorized claim selected before preparing a summary. Email not sent."
        person = repository.person(state.party_id) if state.party_id else None
        if not person or not person.get('email'):
            state.post_process_step = 'email_retry'
            return "I could not find a registered email address. Email not sent. Would you like a human representative?"
        state.summary_email = person['email']
        body = _summary(state, repository)
        if not repository.email_tool:
            # Demo mode records a successful synthetic dispatch without
            # claiming that an external mail provider was contacted.
            state.pending_email_body = None
            state.email_choice = "sent"
            state.email_dispatch_status = "demo_sent"
            _complete(state)
            return "Your summary email was sent successfully (demo)."
        try:
            sent = bool(repository.email_tool.send(person['email'], "Insurance claim conversation summary", body))
        except Exception:
            sent = False
        if sent:
            state.email_choice = "sent"
            state.email_dispatch_status = "confirmed"
            _complete(state)
            return "Your summary email was sent successfully."
        state.email_choice = "send_failed"
        state.email_dispatch_status = "failed"
        return "I could not confirm email delivery. No email was reported as sent. You can retry or skip."
    if consent_kind == "skip_email":
        state.email_consent = EmailConsent.DECLINED
        state.email_choice = "skipped"
        # Declining a summary email is not itself a conversation-closing
        # signal. Keep the workflow open when the business request has not
        # been answered yet; only an explicit closing turn may complete it.
        if (state.pending_requested_fields
                or state.answer_status in {"unknown", "unavailable", "needs_clarification"}):
            state.phase = Phase.PROCESS_CASE if state.verified and state.case_id else Phase.RESOLVE_INTENT
            state.conversation_complete = False
            return "Understood. I will not send an email summary. We can continue with your claim question."
        _complete(state)
        return "Understood. I will not send an email summary and will close this conversation."
    if state.post_process_step == 'email_retry':
        return "I could not send the summary. Reply retry to try again, or skip to close the conversation."
    if state.post_process_step != 'ask_summary':
        state.post_process_step = 'ask_summary'
    return "Would you like a summary of this claim conversation sent to your registered email address? Reply send or skip."
