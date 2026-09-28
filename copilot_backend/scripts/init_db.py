"""CLI: create the four core collections and their indexes.

Run from `copilot_backend/`:

    uv run python -m scripts.init_db

The script is idempotent — re-running it on an already-initialised
database is a no-op that still reports success. That's the contract
deploy pipelines rely on; the init container can call this on every
boot without provisioning logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from app.db.init_db import init_database
from app.db.mongo import MongoClient
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def _run() -> int:
    settings = get_settings()
    client = MongoClient(settings)
    try:
        result = await init_database(client.database)
    finally:
        await client.close()
    # Machine-readable summary on stdout so CI logs are easy to grep.
    sys.stdout.write(json.dumps({"initialized": result}, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
