"""Session persistence, summary, and disconnect service."""

import json
import os
import smtplib
from email.message import EmailMessage


def save_session(session_id, session):
    import app
    target = app.conversion_path(session_id)
    temporary = target.with_suffix(".json.tmp")
    with app.SESSION_LOCK:
        safe_session = app.redact_session_for_storage(session)
        temporary.write_text(json.dumps(safe_session, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


def load_sessions():
    import app
    for path in app.CONVERSIONS.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(session, dict) and "state" in session and "messages" in session:
                app.SESSIONS[path.stem] = session
        except (OSError, json.JSONDecodeError):
            continue


def build_conversion_summary(session):
    state = session.get("state", {})
    lines = ["Insurance claim conversation summary", ""]
    claim = state.get("case") or {}
    if claim:
        lines.extend([
            f"Claim: {claim.get('case_id', 'not available')}",
            f"Status/outcome: {claim.get('status', 'not available')}",
            f"Claim type: {claim.get('case_type', 'not available')}",
            f"Follow-up items/next steps: {', '.join(claim.get('documents_needed') or []) or 'No additional items recorded.'}", "",
        ])
    for item in session.get("intents", []):
        if item.get("intent") in {"unclear", "emotional_support"}:
            continue
        lines.extend([f"Customer question: {item.get('question', item.get('text', ''))}", f"Agent answer: {item.get('answer', 'No answer was recorded.')}", ""])
    return "\n".join(lines).strip()


def disconnect_session(session_id):
    import app
    session = app.SESSIONS.get(session_id)
    if not session:
        return
    session["state"]["disconnected"] = True
    session["state"]["conversation_complete"] = True
    save_session(session_id, session)


def send_summary_email(session, address):
    import app
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SUMMARY_FROM_EMAIL"]
    if not all(os.getenv(name) for name in required):
        return False
    message = EmailMessage()
    message["Subject"] = "Your insurance claim conversation summary"
    message["From"] = os.environ["SUMMARY_FROM_EMAIL"]
    message["To"] = address
    message.set_content(build_conversion_summary(session))
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=300) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        return False


__all__ = ["save_session", "load_sessions", "build_conversion_summary", "disconnect_session", "send_summary_email"]
