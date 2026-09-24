# Incident Sentinel

Multi-agent AI system that triages infrastructure incidents. Four coordinated
LLM agents read the logs, check the metrics, argue their way to a root cause,
and propose a fix — and the UI shows you that reasoning arriving one agent at a
time, with every claim cited back to the log line or metric point behind it.

**FastAPI · PostgreSQL · Redis · LangGraph · Groq (Ollama fallback) · React + TypeScript + Tailwind**

---

## What it does

Pick one of four synthetic failure scenarios. The backend generates ~5,700 log
lines and ~3,000 metric points across five interdependent services, persists
them, and runs a four-agent pipeline over the result:

```
1 · Log Analysis      clusters errors by service and type, records onset times
2 · Metrics Correlation  finds anomalies — and which metrics stayed normal
3 · Root Cause        ranks candidate causes against the service topology
4 · Fix Suggestion    ordered remediation, mitigations before permanent fixes
```

A real run against `bad_deploy`:

```
confidence : 0.85   payments-service
root cause : payments-service v2.4.1 adds a Decimal to a None value, raising
             an unhandled TypeError in POST /payments/charge
citations  : L1, M1, L2
fix        : 1. roll back to v2.4.0
             2. patch with a None guard + unit tests → v2.4.2
trace      : log_analysis 6.6s → metrics_correlation 3.4s
             → root_cause 3.2s → fix_suggestion 2.9s
```

It got there by inference, not lookup — the agents are given observations and
the service topology, never a catalogue of known failure modes. In that run it
used the *flat* api-gateway resource metrics as a rule-out: "the exception
aborts work without increasing load."

### Design notes worth knowing

- **Every agent is deterministic extraction + LLM narration.** Python computes
  the clusters, ratios and onset offsets; the LLM explains them. Numbers in the
  UI are measured, never generated.
- **Citations cannot be hallucinated.** Agents cite short tokens (`L1`, `M1`);
  anything that does not resolve to a stored row is silently dropped.
- **Ground truth is stored but never served.** It scores the agents in tests;
  it never reaches them at runtime.

Full design rationale: [`docs/architecture.md`](docs/architecture.md).

---

## Quick start

### Prerequisites

- Python 3.13
- Node 18+
- PostgreSQL 16 and Redis 7 — via Docker, or installed locally (see
  [without Docker](#running-without-docker))

### 1 · Configure

```bash
cp .env.example .env
```

For local development the defaults work as-is and **no API key is required** —
`LLM_PROVIDER=ollama` runs against a local model. To use Groq instead, add a
free key from [console.groq.com](https://console.groq.com) and set
`LLM_PROVIDER=groq`.

### 2 · Start PostgreSQL and Redis

```bash
docker compose up -d
```

### 3 · Backend

```bash
cd backend
python -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Windows Git Bash
source .venv/Scripts/activate

pip install -r requirements.txt
python -m app.db.session --init      # create the schema
uvicorn app.main:app --reload --port 8000
```

API docs at http://127.0.0.1:8000/docs, health at `/health`.

### 4 · Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. Vite proxies `/api` to the backend, so the
browser sees one origin and SSE needs no CORS configuration.

### 5 · Try it

Click a scenario in the sidebar, then **Run triage**. The timeline fills in as
each agent finishes.

Or from the terminal:

```bash
# simulate
curl -X POST http://127.0.0.1:8000/simulate \
  -H 'Content-Type: application/json' \
  -d '{"scenario_type":"bad_deploy","seed":1337}'

# triage (use the incident_id from above)
curl -X POST http://127.0.0.1:8000/incidents/<id>/triage \
  -H 'Content-Type: application/json'
```

---

## LLM providers

| Provider | Model | Setup | Notes |
| --- | --- | --- | --- |
| **Ollama** | `llama3.2:3b` | `ollama pull llama3.2:3b` | Default. No key, fully offline. Slow on CPU-only machines — expect minutes per triage without a supported GPU. |
| **Groq** | `openai/gpt-oss-120b` | Free key from console.groq.com | ~20s per full triage. |
| **Stub** | — | `LLM_PROVIDER=stub` | Canned responses. Used by the test suite. |

**On the Groq free tier:** the limit is 8,000 tokens per minute and a full
triage costs roughly 7,000, so back-to-back runs will hit 429s. The client
handles them — it parses the server's own usage accounting out of the error
body and backs off accordingly — but a live demo wants about a minute between
runs.

---

## Running without Docker

Docker is only used for PostgreSQL and Redis; nothing about the app requires
it. If Docker Desktop will not run (it needs several GB free on the system
drive), install the services directly.

**Windows — portable PostgreSQL.** `scripts/pg.ps1` manages a standalone
PostgreSQL 16 install. Extract the binaries somewhere (the script defaults to
`D:\pgsql`), then:

```powershell
.\scripts\pg.ps1 init      # one-time: cluster, role, both databases
.\scripts\pg.ps1 start
.\scripts\pg.ps1 status
.\scripts\pg.ps1 psql
```

**Redis.** There is no official Windows build. Either use a free hosted
instance ([Upstash](https://upstash.com) — set `REDIS_URL` to the `rediss://`
URL they give you), or skip it: the cache degrades gracefully to in-memory, and
`/health` will simply report `cache: down`.

---

## Configuration

All settings come from the repo-root `.env` (see `.env.example`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://sentinel:sentinel@localhost:5432/incident_sentinel` | Main database |
| `TEST_DATABASE_URL` | …`/incident_sentinel_test` | Test database — keep it separate |
| `REDIS_URL` | `redis://localhost:6379/0` | Triage result cache |
| `LLM_PROVIDER` | `ollama` | `groq` \| `ollama` \| `stub` |
| `GROQ_API_KEY` | _empty_ | Required only when `LLM_PROVIDER=groq` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local model endpoint |
| `ENVIRONMENT` | `development` | |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Comma-separated. Must name your frontend origin in production. |

`.env` is gitignored. Never commit a key.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Database, cache and LLM reachability |
| `GET` | `/scenarios` | The four injectable failure scenarios |
| `POST` | `/simulate` | Generate and persist an incident |
| `GET` | `/incidents` | History |
| `GET` | `/incidents/{id}` | One incident with its verdict |
| `POST` | `/incidents/{id}/triage` | Run the pipeline (cached) |
| `GET` | `/incidents/{id}/triage/stream` | Same, streamed over SSE |
| `GET` | `/incidents/{id}/trace` | Stored reasoning trace |
| `GET` | `/incidents/{id}/logs` | Generated logs |
| `GET` | `/incidents/{id}/metrics` | Generated metric points |

To re-run a cached triage, pass `refresh` **in the POST body**
(`-d '{"refresh": true}'`). Only the SSE `GET` takes it as a query parameter,
because `EventSource` cannot send a body.

---

## Tests

```bash
cd backend
pytest
```

**113 tests.** They use the stub LLM provider, so they need no API key and no
network. Database-backed tests require PostgreSQL to be reachable at
`TEST_DATABASE_URL` and will skip cleanly if it is not — if you see a wall of
`s`, that is why.

The suite covers scenario signature distinctness, generator determinism, each
agent in isolation, the orchestrator end to end, every endpoint, cache
behaviour with Redis both up and down, SSE stream framing, and the settings
normalisation the deploy path depends on.

---

## Deployment

Config lives in `render.yaml` (backend + database) and `frontend/vercel.json`.

**Backend → Render.** Point a new Blueprint at the repo; `render.yaml` defines
the Docker web service and a free PostgreSQL instance. Set `GROQ_API_KEY` and
`REDIS_URL` in the dashboard — both are marked `sync: false` so they are never
committed.

**Frontend → Vercel.** Import the repo, set the root directory to `frontend`,
and set `VITE_API_URL` to the Render backend URL.

**Then set `CORS_ORIGINS` on the backend to the Vercel domain.** Development
proxies `/api` through Vite so there is one origin; production has two. This is
the most common thing to get wrong.

Render's free tier idles after inactivity, so the first request following a
quiet period pays roughly a 50-second cold start. Wake the backend before a
live demo.

---

## Project layout

```
backend/
  app/
    agents/       four agents + LangGraph orchestrator
    api/          health, simulate, incidents (+ SSE)
    cache/        Redis with in-memory fallback
    db/           models, repository, session
    llm/          provider clients, token budget, prompts
    simulator/    scenario definitions + incident generator
  tests/
  Dockerfile
frontend/
  src/
    api/          typed client + SSE wrapper
    components/   timeline, agent step, evidence, verdict, history
docs/architecture.md
scripts/pg.ps1    portable PostgreSQL helper (Windows, no Docker)
docker-compose.yml
render.yaml
```

---

## License

MIT
