"""Shared human-assistance ticket semantics for all workflow stages."""
import secrets
from .models import Phase


def request_handoff(state, reason):
    state.handoff = "offered"
    state.handoff_reason = reason
    state.handoff_status = "offered"
    state.awaiting_handoff_confirmation = True


def confirm_handoff(state, repository=None):
    # Handoff is idempotent per conversation. Never create a second ticket
    # when the customer repeats the request after a confirmed transfer.
    if state.handoff_ticket and state.handoff_execution_status in {"confirmed", "demo_recorded"}:
        state.handoff_status = "requested"
        state.awaiting_handoff_confirmation = False
        state.conversation_complete = True
        state.phase = Phase.COMPLETE
        return f"Your request has already been transferred to a human representative under ticket {state.handoff_ticket}."
    tool = getattr(repository, "handoff_tool", None)
    if not tool:
        # Demo mode still creates a stable, clearly synthetic ticket so the
        # conversation has a complete handoff state without claiming that an
        # external transfer system was contacted.
        state.handoff_ticket = f"DEMO-HI-{secrets.token_hex(5).upper()}"
        state.handoff_execution_status = "demo_recorded"
        state.handoff_status = "requested"
        state.awaiting_handoff_confirmation = False
        state.conversation_complete = True
        state.phase = Phase.COMPLETE
        return f"Your human-assistance ticket {state.handoff_ticket} has been created (demo)."
    try:
        ticket = tool.create(reason=state.handoff_reason, party_id=state.party_id, case_id=state.case_id)
    except Exception:
        ticket = None
    if not ticket:
        state.handoff_execution_status = "failed"
        state.handoff_status = "offered"
        state.awaiting_handoff_confirmation = True
        state.conversation_complete = False
        return "I could not confirm the human transfer. Your request remains recorded, but no transfer was completed."
    state.handoff_ticket = str(ticket)
    state.handoff_execution_status = "confirmed"
    state.awaiting_handoff_confirmation = False
    state.handoff = "requested"
    state.handoff_status = "requested"
    state.conversation_complete = True
    state.phase = Phase.COMPLETE
    return f"Your human-assistance ticket {state.handoff_ticket} has been created."
