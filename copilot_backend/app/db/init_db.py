"""`init_db` — create the four core collections and their indexes.

The function `init_database(database)` is the library entrypoint and
is the same code path the CLI script invokes. Both are idempotent:

* `create_collection` is skipped if the collection already exists
  (Mongo raises `CollectionInvalid` — caught and swallowed).
* `create_indexes(...)` is a no-op when an identical index exists.

Calling the function twice therefore returns success without altering
state. That's the contract deploy pipelines rely on.

Usage (library):

    from motor.motor_asyncio import AsyncIOMotorClient
    from app.db.mongo import MongoClient
    from app.db.init_db import init_database
    from app.settings import get_settings

    async def main() -> None:
        client = MongoClient(get_settings())
        try:
            await init_database(client.database)
        finally:
            await client.close()

Usage (CLI), from the `copilot_backend/` directory:

    uv run python -m scripts.init_db
"""
from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import CollectionInvalid

from app.db.indexes import (
    CORE_COLLECTIONS,
    INDEX_SPECS,
    all_indexes,
)

logger = logging.getLogger(__name__)


async def init_database(database: AsyncIOMotorDatabase[Any]) -> dict[str, list[str]]:
    """Create the four core collections and their indexes.

    Args:
        database: Motor handle to the target database.

    Returns:
        A mapping `collection_name -> [index_names created]`. Indexes
        that already existed are listed as well — the function is
        idempotent and the caller may want to log "already present" vs
        "newly created".
    """
    created: dict[str, list[str]] = {}
    existing = set(await database.list_collection_names())

    for name in CORE_COLLECTIONS:
        if name in existing:
            logger.info("init_db: collection %r already exists", name)
        else:
            try:
                await database.create_collection(name)
                logger.info("init_db: created collection %r", name)
            except CollectionInvalid:
                # Race: another worker created it concurrently. Treat as
                # success — the index step below is the real gate.
                logger.info("init_db: collection %r appeared concurrently", name)

        index_names = await database[name].create_indexes(INDEX_SPECS[name])
        created[name] = list(index_names)
        logger.info(
            "init_db: ensured %d index(es) on %r: %s",
            len(index_names),
            name,
            index_names,
        )

    return created


__all__ = ["init_database", "all_indexes"]
