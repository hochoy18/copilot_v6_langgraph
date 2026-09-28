# Copilot Backend

FastAPI service for the Copilot MVP. See [`docs/SPEC.md`](../docs/SPEC.md) and [`docs/adr/`](../docs/adr/) for the architectural picture.

## Quick start

```bash
# from repo root
cd copilot_backend
uv sync                                    # creates .venv + installs pinned deps
uv run uvicorn main:app --reload           # serves on http://127.0.0.1:8000

# smoke
curl -s http://127.0.0.1:8000/healthz      # {"status":"ok"}
```

## Layout

```
app/
  main.py            # FastAPI app factory + global error handler wiring
  settings.py        # pydantic-settings (env-driven CORS origins, etc.)
  exceptions.py      # AppError + unified error response shape
  api/
    health.py        # /healthz route
tests/
  test_health.py     # /healthz returns 200 with {status: ok}
  test_errors.py     # unhandled exceptions return unified {code,message_zh,...}
```

## Configuration

Settings are loaded from environment variables (or `.env`). For MVP scaffold only:

| Var | Default | Purpose |
|---|---|---|
| `COPILOT_CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Comma-separated allowed origins. CORS policy is deferred to V1.1 per SPEC; this default keeps the dev SPA working. |

## Tests

```bash
uv run pytest            # full suite
uv run mypy              # strict typecheck
uv run ruff check .      # lint
```

## Entry point

`main:app` — the module exposes `app = create_app()` so `uvicorn main:app` works without extra ceremony.