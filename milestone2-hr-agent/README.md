# Milestone 2 — HR Policy and Leave Assistant

A production-oriented educational prototype for answering HR policy questions with cited evidence, reading authenticated employees’ leave balances, preparing leave proposals, and creating `pending_manager_review` requests only after explicit confirmation.

All employees, tokens, balances, policies, requests, and traces in this repository are synthetic.

## Features

- FastAPI backend with documented OpenAPI contracts.
- Streamlit interface with Alya, Bima, and monitoring-admin demo personas.
- PostgreSQL and pgvector as the source of truth.
- Hybrid lexical/vector policy retrieval with reranking.
- Run-scoped citation validation and safe abstention.
- Four allowlisted tools:
  - `search_hr_policy`
  - `get_my_leave_balance`
  - `prepare_leave_request`
  - `create_hr_ticket`
- Bearer-token identity binding.
- Proposal-before-side-effect workflow.
- Transactional, idempotent confirmation.
- Redis cache with graceful degradation.
- Redacted traces, aggregate monitoring, and admin-only alerts.
- Versioned 15-case evaluation with release gates.
- Non-root Docker image shared by API, UI, seed, tests, and evaluation.

## System boundaries

The agent can:

- explain HR policies with citations;
- read the authenticated employee’s own balance and requests;
- prepare a leave proposal;
- create a leave request after explicit confirmation;
- create an HR ticket when explicitly requested.

The agent cannot:

- approve leave;
- create an `approved` request;
- change leave balances;
- read another employee’s private data;
- trust an employee ID supplied in the prompt;
- execute instructions embedded in retrieved documents;
- perform a proposal side effect without confirmation.

Every confirmed request is created as:

```text
pending_manager_review
```

## Architecture

See [architecture.md](architecture.md) for:

- trust boundaries;
- authentication and authorization;
- indexing and hybrid retrieval;
- agent/tool loop;
- confirmation transaction;
- telemetry and redaction;
- Redis degradation;
- evaluation and human-review flow;
- architecture decision records.

Security and governance risks are documented in [risk-register.md](risk-register.md).

## Project structure

```text
.
├── app.py
├── architecture.md
├── data/
│   ├── evaluation_cases.json
│   ├── policies/
│   └── synthetic fixtures
├── docker-compose.yml
├── Dockerfile
├── risk-register.md
├── src/
│   ├── agent.py
│   ├── api.py
│   ├── cache.py
│   ├── contracts.py
│   ├── domain.py
│   ├── providers.py
│   ├── repository.py
│   ├── retrieval.py
│   ├── run_evaluation.py
│   ├── security.py
│   ├── setup_lab.py
│   └── tools.py
├── tests/
└── ui_contract.py
```

## Prerequisites

For the supported submission workflow:

- Docker Desktop with Docker Compose;
- an OpenAI-compatible API key;
- internet access for model and embedding calls.

Host Python is not required for the Docker workflow.

## Configuration

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Fill the runtime values in `.env`:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
AGENT_MODEL=gpt-6-luna
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
POLICY_INDEX_VERSION=policies-2026-01
MODEL_MODE=live
```

Optional Windows host-port overrides:

```dotenv
POSTGRES_PORT=55432
REDIS_PORT=6379
API_PORT=8000
STREAMLIT_PORT=8501
```

Do not commit `.env`. It is excluded by `.gitignore` and `.dockerignore`.

## Run with Docker

Validate configuration:

```powershell
docker compose config --quiet
```

Build the application image:

```powershell
docker compose build
```

The image build does not require an API key. Secrets are supplied only at runtime.

Start PostgreSQL, Redis, seed, API, and Streamlit:

```powershell
docker compose up -d
```

Inspect services:

```powershell
docker compose ps
```

Expected endpoints:

- API documentation: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/healthz>
- Readiness: <http://127.0.0.1:8000/readyz>
- Streamlit: <http://127.0.0.1:8501>

Check readiness:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/readyz |
    ConvertTo-Json -Depth 10
```

Expected dependencies:

```json
{
  "status": "ready",
  "database": "ready",
  "cache": "ready",
  "model": "ready"
}
```

Verify the API is non-root:

```powershell
docker compose exec api id
```

Expected UID/GID:

```text
uid=10001(appuser) gid=10001(appgroup)
```

## Seed behavior

The `seed` service:

- creates the pgvector extension and application schema;
- seeds three synthetic identities;
- seeds leave balances and historical requests;
- chunks seven policies into 15 stable chunks;
- creates 1536-dimensional embeddings;
- preserves later requests;
- skips embeddings when the policy version and content are current.

Inspect seed output:

```powershell
docker compose logs seed --tail 100
```

The seed service exits successfully after setup; a stopped seed container is expected.

## Run deterministic tests in Docker

```powershell
docker compose build test
docker compose --profile test run --rm --no-deps test
```

Latest verified result:

```text
27 passed, 1 deselected
```

The remaining test is opt-in because it calls the live API.

## Run the 15-case evaluation in Docker

Ensure the main services are healthy, then run:

```powershell
docker compose --profile evaluation run --rm evaluation
```

Reports are written to:

```text
outputs/evaluation_report.json
outputs/evaluation_report.md
```

Latest verified result:

| Metric              | Result | Target |
| ------------------- | -----: | -----: |
| Overall pass rate   |   100% |   ≥80% |
| Hard-gate pass rate |   100% |   100% |
| Retrieval recall@3  |   100% |   ≥80% |
| Citation validity   |   100% |   100% |
| Release decision    |     GO |     GO |

Evaluation covers policy retrieval, citations, own balance, cross-user access, proposal safety, past dates, forbidden approval, prompt injection, medical PII, unknown policy, conflict escalation, and parental leave.

## API endpoints

| Method | Path                                             | Access                 |
| ------ | ------------------------------------------------ | ---------------------- |
| GET    | `/healthz`                                       | Public liveness        |
| GET    | `/readyz`                                        | Public readiness       |
| POST   | `/api/v1/chat`                                   | Authenticated persona  |
| POST   | `/api/v1/action-proposals/{proposal_id}/confirm` | Proposal owner         |
| GET    | `/api/v1/leave-requests/me`                      | Authenticated employee |
| GET    | `/api/v1/traces/{trace_id}`                      | Trace owner or admin   |
| GET    | `/api/v1/monitoring/summary`                     | Admin                  |
| GET    | `/api/v1/monitoring/alerts`                      | Admin                  |

## Safe action workflow

1. User asks to prepare leave.
2. Agent calls `prepare_leave_request`.
3. Backend validates dates, business days, overlap, and balance.
4. Only an `action_proposal` is stored.
5. UI displays the proposal and requests confirmation.
6. User confirms with an idempotency key.
7. Backend transaction creates one `pending_manager_review` request.
8. Repeating the same confirmation returns the same request.

No leave request is created during step 2–4.

## Monitoring

The admin persona can view:

- run count;
- error rate;
- p95 latency;
- tool-call count;
- actionable alerts.

Trace payloads exclude raw API keys, bearer tokens, chain-of-thought, and medical details. Monitoring data is aggregate and read-only.

## Development with host Python

Docker is the submission path, but local development is supported:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
docker compose up -d postgres redis
python -m src.setup_lab
python -m pytest -m "not live" -q
python -m uvicorn src.api:create_app --factory --reload
```

In another terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m streamlit run app.py
```

## Security controls

- Authentication tokens are stored only as hashes.
- Employee identity is backend-controlled.
- Cross-user IDs are removed from generated output.
- Tool arguments use strict schemas.
- Retrieved content cannot grant tool authority.
- Citation IDs are checked against evidence from the same run.
- Proposal confirmation is owner-scoped and idempotent.
- Trace output is redacted.
- Secrets are excluded from Git and image build context.
- Containers run as UID `10001`.

## Limitations

This is an educational prototype, not a production HR system. It lacks enterprise SSO, production key management, policy approval workflows, formal retention controls, high-availability database deployment, production rate limiting, full distributed tracing, and privacy/legal review for real employee data.

## Supporting documents

- [Architecture](architecture.md)
- [Risk register](risk-register.md)
- [Assessment](assessment.md)
- [Rubric](rubric.md)
- [Docker delivery](docker-delivery.md)
- [Submission checklist](submission-checklist.md)
- [Lab guide](lab.md)
