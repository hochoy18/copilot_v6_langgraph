# Copilot Backend

FastAPI service for the Copilot MVP. See [`docs/SPEC.md`](../docs/SPEC.md) and [`docs/adr/`](../docs/adr/) for the architectural picture.

## Quick start

```bash
# from repo root
cd copilot_backend
uv sync                                    # creates .venv + installs pinned deps
uv run uvicorn main:app --reload           # serves on http://127.0.0.1:8000

# smoke
curl -s http://127.0.0.1:8000/healthz      # {"status":"ok","dependencies":[...]}
```

### External dependencies

The three services this backend probes on `/healthz` (and uses at
runtime in later tickets) are **already deployed** in the development
environment — there is no `docker-compose.yml` in this repo, by design:

| Dependency | Where it lives | Probe in `/healthz` |
|---|---|---|
| MongoDB  | `localhost:27017` (local process) | TCP open |
| Milvus   | `localhost:19530` (local process) | TCP open |
| Langfuse | `https://langfuse.bananahochoy.online` (external) | HTTP `GET /api/public/health` |

Override the URLs via `COPILOT_MONGODB_URI`, `COPILOT_MILVUS_HOST/PORT`,
or `COPILOT_LANGFUSE_HOST` — see `.env.example` for the full list.

## Layout

```
app/
  main.py            # FastAPI app factory + lifespan + global error handler
  settings.py        # pydantic-settings (env-driven: CORS, MongoDB, Milvus, Langfuse, ...)
  exceptions.py      # AppError + unified error response shape
  health.py          # /healthz probes (Mongo TCP, Milvus TCP, Langfuse HTTP) + HealthChecker
  api/
    health.py        # /healthz route — returns 200/503 with per-dep breakdown
tests/
  test_health.py     # /healthz contract + probe unit tests (hermetic via httpx.MockTransport)
  test_errors.py     # unhandled exceptions return unified {code,message_zh,...}
  test_cors.py       # CORS middleware wiring
```

## Configuration

Settings are loaded from environment variables (or `.env`). All variables
start with `COPILOT_`; see [`.env.example`](./.env.example) for the full
template.

## Tests

```bash
uv run pytest            # full suite
uv run mypy              # strict typecheck
uv run ruff check .      # lint
```

## Entry point

`main:app` — the module exposes `app = create_app()` so `uvicorn main:app` works without extra ceremony.
