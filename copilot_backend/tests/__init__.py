"""Test suite for the Copilot backend scaffold.

Tests target the HTTP API seam (per SPEC § Testing Decisions). They boot the
FastAPI app in-process via httpx.AsyncClient + ASGITransport so they exercise
the real router/middleware/handler stack without a separate server.
"""