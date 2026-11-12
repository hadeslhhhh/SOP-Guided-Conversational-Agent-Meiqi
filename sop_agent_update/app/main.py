"""API layer for the independent SOP architecture."""
from __future__ import annotations

import hmac
import os
import uuid
import logging
import time
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel, Field
from .workflow import ConversationService
from .store import ConcurrentTurnError
from .models import Session

app = FastAPI(title="SOP-Guided-Conversational-Agent-Meiqi")
service = ConversationService()
logger = logging.getLogger("uvicorn.error")


class Message(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    thread_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=4000)
    message: str | None = Field(default=None, max_length=4000)

    def content(self) -> str:
        return (self.text or self.message or "").strip()


def api_config():
    enabled = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("NLU_MODE", "openai") == "openai"
    return {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "api_mode": enabled, "api_provider": "OpenAI" if enabled else "Unavailable"}


def state_payload(state):
    phase = state.phase.value
    return {"phase": phase, "emotion": state.emotion or "Neutral / Calm",
            "emotion_level": state.emotion_level,
            "stage_progress": {
                "VERIFY_ID": float(phase != "VERIFY_ID"),
                "RESOLVE_INTENT": float(phase in {"PROCESS_CASE", "POST_PROCESS", "COMPLETE"}),
                "PROCESS_CASE": float(phase in {"POST_PROCESS", "COMPLETE"}),
                "POST_PROCESS": float(phase == "COMPLETE")},
            "post_process_step": state.post_process_step,
            "matched_identity_field_count": state.matched_identity_field_count,
            "disconnect_scheduled": state.conversation_complete,
            "disconnected": phase == "COMPLETE", "handoff": state.handoff,
            "case_id": state.case_id, "active_claim_id": state.active_claim_id,
            "case_hints": state.hint.model_dump(exclude_none=True),
            "claim_candidates": state.claim_candidates_after_filter,
            "verified": state.verified, "verification_status": state.verification_status,
            "identity": state.identity, "current_intent": state.current_intent,
            "requested_fields": state.requested_fields,
            "answered_fields": state.answered_fields,
            "pending_requested_fields": state.pending_requested_fields}


@app.post("/api/chat")
def chat(message: Message):
    text = message.content()
    if not text:
        raise HTTPException(422, "Message is required")
    session_id = message.session_id or message.thread_id or str(uuid.uuid4())
    # Keep a caller-supplied identifier stable even when its checkpoint has
    # expired or the backend has restarted. Rotating it here makes the client
    # silently lose the conversation and re-enter identity verification.
    logger.info("chat_checkpoint thread_id=%s checkpoint_restored=%s supplied_session_id=%s",
                session_id, service.store.exists(session_id), bool(message.session_id or message.thread_id))
    try:
        reply, state = service.respond(session_id, text)
    except ConcurrentTurnError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        # OpenAI-only runtime: report the provider failure directly to the UI.
        # Never silently switch to a local interpreter.
        logger.error("OpenAI request failed: %s: %s", type(exc).__name__, str(exc)[:300])
        state = service.store.get(session_id) or Session()
        now = time.monotonic()
        state.connection_failure_since = state.connection_failure_since or now
        state.connection_failure_started_at = state.connection_failure_started_at or now
        if now - state.connection_failure_since >= 300:
            state.handoff = "offered"
            state.handoff_reason = "OpenAI connection remained unavailable for five minutes."
            state.awaiting_handoff_confirmation = True
        failure = "OpenAI API request failed. Please check the API key, model, or network connection."
        with service.store.turn():
            service.store.save(session_id, state, expected_revision=state.revision)
        return {"session_id": session_id, "thread_id": session_id, "reply": failure, "answer": failure,
                "phase": state.phase.value, "verified": state.verified,
                "case_id": state.case_id, "captured_fields": list(state.identity),
                "handoff": state.handoff, "response_mode": "model",
                "state": state_payload(state), "error": "openai_api_failed",
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "api_mode": False, "api_provider": "OpenAI"}
    config = api_config()
    return {"session_id": session_id, "thread_id": session_id, "reply": reply, "answer": reply,
            "phase": state.phase.value,
            "verified": state.verified, "case_id": state.case_id,
            "captured_fields": list(state.identity), "handoff": state.handoff,
            "response_mode": state.response_mode, "state": state_payload(state), **config}


@app.post("/api/chat-v2")
def chat_v2(message: Message):
    return chat(message)


@app.get("/api/health")
def health():
    return {"ok": True, "architecture": "ConversationService -> LangGraph -> independent nodes"}


@app.get("/api/config")
def config():
    return api_config()


@app.post("/api/reset")
def reset(payload: dict):
    session_id = payload.get("session_id")
    if session_id:
        service.store.delete(session_id)
    return {"ok": True}


@app.get("/api/outbox")
def outbox(authorization: str | None = Header(default=None)):
    token = os.getenv("ADMIN_API_TOKEN")
    if not token:
        raise HTTPException(404, "Not found")
    if not authorization or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Authentication required")
    return {"count": len(service.store.messages()), "messages": service.store.messages()}


@app.get("/")
def ui():
    return HTMLResponse((Path(__file__).parent / "index.html").read_text())
