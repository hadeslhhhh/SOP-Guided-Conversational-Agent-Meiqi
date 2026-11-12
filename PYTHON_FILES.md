# Python File Guide

This document summarizes the Python files in the canonical SOP-guided insurance claims agent.

## Core application

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI entry point. Defines chat, health, configuration, reset, outbox, and browser UI endpoints. |
| `app/workflow.py` | Main orchestration layer. Runs OpenAI Structured NLU, prepares session state, routes LangGraph phases, invokes stage handlers, saves checkpoints, and returns responses. |
| `app/models.py` | Pydantic models for workflow phases, email consent, parsed NLU output, claim hints, and conversation sessions. |
| `app/interpreter.py` | NLU layer. Contains OpenAI Structured NLU, intent normalization, requested-field normalization, identity extraction, email-consent detection, scope detection, and human-transfer detection. |
| `app/prompts.py` | Prompts and structured-output instructions used by the OpenAI model. |
| `app/repository.py` | Loads fixture data and retrieves policyholders, claims, document guidance, and authorized claim records. |
| `app/store.py` | SQLite session persistence and demo email outbox. Handles session revisions and concurrent-turn protection. |
| `app/response_service.py` | Builds claim answers from authorized facts, tracks answered and pending fields, and formats customer-facing responses. |
| `app/customer_state.py` | Writes and removes customer-state JSON snapshots for conversion and debugging visibility. |

## Safety and validation

| File | Responsibility |
|---|---|
| `app/verification.py` | Normalizes and compares identity fields including name, date of birth, phone, email, and ID last four digits. |
| `app/identity_guard.py` | Enforces identity verification and claim-access authorization rules. |
| `app/factual_guard.py` | Checks whether generated responses remain grounded in authorized claim facts. |
| `app/guards_runtime.py` | Runtime guards for factual grounding, privacy leakage, and requested-field coverage. |
| `app/escalation.py` | Determines when to escalate unresolved intent, unsupported requests, emotional distress, or repeated failures. |
| `app/handoff.py` | Creates and confirms demo human-support handoff tickets. |

## Workflow stages

| File | Workflow state | Responsibility |
|---|---|---|
| `app/stages/verify_identity.py` | `VERIFY_ID` | Collects identity data, requires at least three matching fields, blocks premature claim disclosure, and handles refusals and emotional language. |
| `app/stages/resolve_intent.py` | `RESOLVE_INTENT` | Selects the correct claim, resolves multiple claims, handles claim switching, out-of-scope requests, and human-transfer requests. |
| `app/stages/process_case.py` | `PROCESS_CASE` | Answers grounded claim questions about status, denial reasons, documents, payments, deadlines, appeals, and next steps. |
| `app/stages/post_process.py` | `POST_PROCESS` | Handles additional questions, conversation-summary email consent, send/skip/revoke actions, and conversation closure. |

## Tests

| File | Responsibility |
|---|---|
| `tests/conftest.py` | Pytest configuration and test collection behavior. |
| `tests/test_logic_20.py` | Deterministic API-independent tests for response grounding, field aliases, consent, invalid values such as `none`, out-of-scope detection, and explicit claim-type extraction. |

The local logic suite does not call OpenAI:

```bash
pytest -q tests/test_logic_20.py
```

## API and test tools

| File | Responsibility |
|---|---|
| `tools/run_complex_30_api.py` | Main 30-case API regression runner. Calls OpenAI Structured NLU and final response generation, prints progress, and writes conversation and diagnostic reports. |
| `tools/run_api_regression_15.py` | Smaller 15-case API regression runner. |
| `tools/run_sop_harness_30_api.py` | API runner for the generated SOP harness input format. |
| `tools/run_complex_25_update.py` | Runs and formats the older 25-case update test set. |
| `tools/debug_50_cases.py` | Diagnoses intent and response behavior for a 50-case dataset. |
| `tools/generate_complex_25_tests.py` | Generates 25 complex test cases. |
| `tools/generate_complex_50_tests.py` | Generates 50 complex test cases. |
| `tools/regenerate_conversation_reports.py` | Regenerates readable conversation reports from test outputs. |

## End-to-end request flow

```text
app/main.py
    ↓
app/workflow.py
    ↓
app/interpreter.py
    ↓
VERIFY_ID
    ↓
RESOLVE_INTENT
    ↓
PROCESS_CASE
    ↓
POST_PROCESS
    ↓
app/response_service.py
    ↓
app/guards_runtime.py
    ↓
Customer response
```

`app/workflow.py` is the central integration point. It connects the OpenAI model, LangGraph states, authorization, response generation, persistence, and safety guards.
