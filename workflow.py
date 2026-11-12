"""LangGraph orchestration for the existing insurance SOP state machine.

The business logic remains in ``app._process_turn``. This graph only selects
the existing phase entry point, so the SOP ordering, gates, authorization, NLU,
and response behavior are unchanged.
Each public node owns one SOP stage. Business behavior remains in ``app`` so
the existing state schema, gates, authorization, NLU, and responses are not
changed by this orchestration layer.
"""

from typing import Any, TypedDict
from state import SessionState


class WorkflowState(TypedDict, total=False):
    state: SessionState
    text: str
    answer: str
    extracted: dict[str, Any]


def _run_stage(data: WorkflowState, expected_phase: str) -> dict[str, Any]:
    """Run one turn through the existing controller at one explicit stage."""
    actual_phase = data["state"].get("phase", "VERIFY_ID")
    if actual_phase != expected_phase:
        raise ValueError(f"SOP stage mismatch: expected {expected_phase}, got {actual_phase}")
    # Import lazily to avoid app.py <-> workflow.py import cycles.
    import app

    stage_handlers = {
        "VERIFY_ID": app.verify_id_stage,
        "RESOLVE_INTENT": app.resolve_intent_stage,
        "PROCESS_CASE": app.process_case_stage,
        "POST_PROCESS": app.post_process_stage,
    }
    answer, extracted = stage_handlers[expected_phase](data["state"], data["text"])
    return {"answer": answer, "extracted": extracted, "state": data["state"], "text": data["text"]}


def verify_id_node(data: WorkflowState) -> dict[str, Any]:
    return _run_stage(data, "VERIFY_ID")


def resolve_intent_node(data: WorkflowState) -> dict[str, Any]:
    return _run_stage(data, "RESOLVE_INTENT")


def process_case_node(data: WorkflowState) -> dict[str, Any]:
    return _run_stage(data, "PROCESS_CASE")


def post_process_node(data: WorkflowState) -> dict[str, Any]:
    return _run_stage(data, "POST_PROCESS")


def route_by_phase(data: WorkflowState) -> str:
    phase = data["state"].get("phase", "VERIFY_ID")
    return {
        "VERIFY_ID": "VERIFY_ID",
        "RESOLVE_INTENT": "RESOLVE_INTENT",
        "PROCESS_CASE": "PROCESS_CASE",
        "POST_PROCESS": "POST_PROCESS",
    }.get(phase, "VERIFY_ID")


def _build_graph():
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    graph = StateGraph(WorkflowState)
    graph.add_node("VERIFY_ID", verify_id_node)
    graph.add_node("RESOLVE_INTENT", resolve_intent_node)
    graph.add_node("PROCESS_CASE", process_case_node)
    graph.add_node("POST_PROCESS", post_process_node)
    graph.add_conditional_edges(
        START,
        route_by_phase,
        {
            "VERIFY_ID": "VERIFY_ID",
            "RESOLVE_INTENT": "RESOLVE_INTENT",
            "PROCESS_CASE": "PROCESS_CASE",
            "POST_PROCESS": "POST_PROCESS",
        },
    )
    for node in ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"):
        graph.add_edge(node, END)
    return graph.compile()


SOP_GRAPH = _build_graph()


def run_turn(state: dict[str, Any], text: str):
    """Run one customer turn through the phase-routed workflow graph."""
    payload: WorkflowState = {"state": state, "text": text}
    if SOP_GRAPH is None:
        # The project can still be imported before dependencies are installed;
        # production requirements include LangGraph and use the compiled path.
        result = _run_stage(payload, route_by_phase(payload))
        return result["answer"], result["extracted"]
    result = SOP_GRAPH.invoke(payload)
    return result.get("answer", ""), result.get("extracted", {})
