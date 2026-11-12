# Insurance Claims SOP Agent

The implementation uses Python/LangGraph for identity verification, intent routing, claim authorization, email consent, and human handoff. OpenAI handles semantic understanding and final customer-facing response generation within these safety boundaries.

## Current workflow

```text
VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS → COMPLETE
```

The production entry point is `sop_agent_update/`. Root-level Python files are legacy compatibility code and are not used by the Docker image. The reference project `insurance-claims-sop-agent-main 2` supports rule mode and multiple LLM providers; this version adds stricter phase gates, SQLite session checkpoints, claim grounding, and an API-only runtime.

## Requirements

- Docker Desktop, or Python 3.12+
- `OPENAI_API_KEY` for the API-only runtime

The production image installs only the direct runtime dependencies listed in `sop_agent_update/requirements.txt`. The optional progress-bar dependency for API regression is isolated in `sop_agent_update/requirements-api-regression.txt`; pytest and local NLU packages are not installed for production.

For code-logic tests only, install the development dependencies:

```bash
pip install -r sop_agent_update/requirements-dev.txt
pytest -q sop_agent_update/tests/test_logic_20.py
```

These 20 tests do not call OpenAI and do not represent the API-only end-to-end regression suite.

Pinned runtime versions:

| Package | Version | Purpose |
|---|---:|---|
| `fastapi` | `0.143.0` | HTTP API and UI serving |
| `uvicorn` | `0.54.0` | ASGI server |
| `openai` | `2.54.0` | Structured NLU and final response generation |
| `pydantic` | `2.14.0` | Request and session models |
| `langgraph` | `1.2.20` | SOP workflow routing |

The optional `tqdm==4.70.1` package is used only by the 15-case API regression progress bar.

## Docker deployment

From the repository root:

```bash
export OPENAI_API_KEY="your API key"
docker compose up --build -d
```

Open <http://localhost:8000>. Check the service:

```bash
curl http://localhost:8000/api/health
docker compose logs -f sop-guided-conversational-agent-meiqi
```

Stop the service:

```bash
docker compose down
```

Session data is stored in the Docker volume `sop_guided_conversational_agent_meiqi_sessions`. `docker compose down` stops the service while preserving the volume.

### Docker configuration

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | empty | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `NLU_MODE` | `openai` | Required production mode |
| `SESSION_DB` | `/data/sessions.sqlite3` | SQLite checkpoint path |

Never commit an API key to the README, Dockerfile, or Git. Use an untracked local `.env` file when appropriate.

## Local server startup

The server can be started locally for development, but it still uses OpenAI APIs and does not use local NLU:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r sop_agent_update/requirements.txt
export OPENAI_API_KEY="your API key"
export OPENAI_MODEL="gpt-4o-mini"
export NLU_MODE="openai"
export SESSION_DB="$(pwd)/sessions/sessions.sqlite3"
uvicorn app.main:app --app-dir sop_agent_update --host 127.0.0.1 --port 8000
```

## API-only response pipeline

Every production turn follows this flow:

```text
customer input
  → OpenAI Structured NLU
  → Python workflow, authorization, and authorized facts
  → OpenAI final response generation
  → Python factual and privacy guards
  → customer response
```

Python remains authoritative for identity, claim ownership, facts, answered fields, and workflow transitions. OpenAI controls interpretation and natural-language phrasing. No local `RuleInterpreter` is used in the production path.

## Testing

The project has two separate test layers.

### 1. Local pytest logic tests

These 20 tests validate deterministic code logic only. They do not call OpenAI, do not require an API key, and do not run the production NLU path.

Install development dependencies:

```bash
pip install -r sop_agent_update/requirements-dev.txt
```

Run the 20 tests:

```bash
pytest -q sop_agent_update/tests/test_logic_20.py
```

They cover response composition, requested-field extraction, field tracking, consent classification, claim ID redaction, and unverified-state behavior.

### 2. API-only end-to-end regression

These 15 cases call OpenAI for both Structured NLU and final response generation. They require a valid API key and test the complete customer-to-response workflow.

```bash
export OPENAI_API_KEY="your API key"
export NLU_MODE=openai
pip install -r sop_agent_update/requirements-api-regression.txt
python sop_agent_update/tools/run_api_regression_15.py
```

The script shows a live progress bar and writes the full conversation report to `sop_agent_update/tests/api_regression_15_results.txt`.

The API regression flow is:

```text
Customer message
  → OpenAI Structured NLU
  → Python / LangGraph workflow
  → OpenAI final response generation
  → Python factual and privacy guards
  → Customer response
```

## API example

```bash
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"text":"I am Margaret Chen. I want to ask about my denied healthcare claim."}'
```

Endpoints:

- `GET /api/health` — health check
- `GET /api/config` — model configuration status without exposing the API key
- `POST /api/chat` — send a message; accepts `session_id` or `thread_id`
- `POST /api/chat-v2` — compatibility endpoint equivalent to `/api/chat`

## Safety boundaries

- Claim details are not disclosed before identity verification.
- A claim must belong to the verified policyholder.
- Claim IDs, amounts, dates, and document requirements must come from authorized facts or fixtures.
- “Yes, email the instructions” is treated as explicit email consent, not as a missing-document question.
- Email is currently a demo outbox operation; the agent must not claim that an external email was delivered.
- OpenAI failures are reported explicitly; the service does not silently switch to local NLU.

## Directory structure

See [ARCHITECTURE.md](ARCHITECTURE.md) for the system architecture, end-to-end request flow, workflow phases, and feature list.

See [REQUIREMENTS_COVERAGE.md](REQUIREMENTS_COVERAGE.md) for a requirement-by-requirement implementation and delivery assessment.

```text
sop_agent_update/
  app/                 FastAPI, LangGraph workflow, guards, and store
  fixtures/            Claims, identity, document, and consent data
  tests/               API regression inputs and reports
  Dockerfile           Subdirectory Docker image definition
  docker-compose.yml   Subdirectory Compose configuration
Dockerfile             Root production image entry point
docker-compose.yml      Root production Compose entry point
```

## Cleanup policy

The Docker image copies only the canonical application, fixtures, and required dependencies. It excludes `.venv`, `.env`, `__pycache__`, generated reports, sessions, and legacy root-level code. Legacy source files remain only for historical reference.
