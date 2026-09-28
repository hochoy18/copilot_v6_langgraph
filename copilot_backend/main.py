"""Uvicorn entrypoint.

Exists so the ticket's stated command (`uvicorn main:app`) works verbatim.
Canonical module is `app.main` — this is a thin re-export.
"""

from app.main import app  # noqa: F401  (re-export for uvicorn import string)