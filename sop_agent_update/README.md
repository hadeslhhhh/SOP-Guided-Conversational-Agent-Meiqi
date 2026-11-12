# SOP-Guided-Conversational-Agent-Meiqi

This directory contains the canonical implementation of the API-only conversational agent.

See the repository-level [architecture guide](../ARCHITECTURE.md) for the complete system design and feature list.

See the repository-level [requirements coverage](../REQUIREMENTS_COVERAGE.md) for an implementation and delivery checklist.

```text
API → ConversationService → LangGraph Workflow → Stage Nodes
                                         ├─ verify_identity
                                         ├─ resolve_intent
                                         ├─ process_case
                                         └─ post_process
                                                ↓
                         Authorized Facts / OpenAI / Guards / SQLite Store
```

## Run

Run from the repository root:

```bash
export OPENAI_API_KEY="your-api-key"
export NLU_MODE="openai"
export OPENAI_MODEL="gpt-4o-mini"
uvicorn app.main:app --app-dir sop_agent_update --reload
```

The runtime uses OpenAI Structured NLU followed by OpenAI final response generation. Local NLU and automatic local fallback are not part of the production path.

Runtime versions are pinned in `requirements.txt`. The API regression progress bar is optional and is isolated in `requirements-api-regression.txt`.

For local code-logic tests, use `requirements-dev.txt`. The 20 tests in `tests/test_logic_20.py` do not call OpenAI.

## Persistence and email

Session state is persisted in SQLite through `SESSION_DB`. Email summaries use a demo outbox queue and must not be represented as externally delivered mail.

## Testing

### Local pytest logic tests

Install development dependencies and run the 20 API-independent logic tests:

```bash
pip install -r sop_agent_update/requirements-dev.txt
pytest -q sop_agent_update/tests/test_logic_20.py
```

These tests do not call OpenAI. They check deterministic response composition, field tracking, consent handling, redaction, and session behavior.

### API-only end-to-end regression

Run the 15 cases from the repository root. These cases call OpenAI for Structured NLU and final response generation:

```bash
export OPENAI_API_KEY="your API key"
export NLU_MODE=openai
pip install -r sop_agent_update/requirements-api-regression.txt
python sop_agent_update/tools/run_api_regression_15.py
```

The command displays a progress bar and writes its report to `sop_agent_update/tests/api_regression_15_results.txt`.
