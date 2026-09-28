"""Motor (async MongoDB) client lifecycle.

Exports `MongoClient` — a thin wrapper that owns one `AsyncIOMotorClient`
per process and hands out `AsyncIOMotorDatabase` handles. The wrapper
exists for two reasons:

1. **Deterministic shutdown.** Motor's client owns a connection pool
   and a background monitoring task. Letting it leak until GC means a
   graceful-shutdown signal leaves them dangling. `MongoClient.close`
   awaits the underlying `AsyncIOMotorClient.close()` so FastAPI's
   lifespan can clean up.
2. **Seam for tests.** Tests can pass a `mongomock_motor.AsyncMongoMockClient`
   through the same constructor; the public surface is identical to
   production.

The client is configured via the `COPILOT_MONGODB_URI` /
`COPILOT_MONGODB_DATABASE` / `COPILOT_MONGODB_SERVER_SELECTION_TIMEOUT_MS`
env vars (see `app.settings.Settings`).
"""
from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ReadPreference

from app.settings import Settings

logger = logging.getLogger(__name__)


# A factory signature — anything with the same constructor shape as
# `AsyncIOMotorClient` (uri, **kwargs) → `AsyncIOMotorClient`. Tests pass
# `mongomock_motor.AsyncMongoMockClient` here so production and test
# code share the construction path.
ClientFactory = type[AsyncIOMotorClient[Any]]


class MongoClient:
    """One process, one Motor client, one logical database.

    The wrapper is deliberately small. The interesting behavior —
    settings parsing, server-selection timeout — lives in Motor
    itself; this class exists so `app.main` and the init script can
    share one construction path.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        """Build a client (and the bound database handle) from settings.

        Args:
            settings: env-driven backend configuration. The wrapper reads
                `mongodb_uri`, `mongodb_database`, and
                `mongodb_server_selection_timeout_ms` and nothing else.
            client_factory: optional override used by tests that want a
                `mongomock_motor.AsyncMongoMockClient` instead of a real
                Motor client. Defaults to `AsyncIOMotorClient`.
        """
        factory: ClientFactory = client_factory or AsyncIOMotorClient
        # `serverSelectionTimeoutMS` is the SDK-side knob that mirrors
        # the /healthz TCP probe budget. Keeping them in lockstep means
        # a slow Mongo surfaces the same way in /healthz and in a live
        # `find_one` call.
        self._client: AsyncIOMotorClient[Any] = factory(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
            # Default to primary reads so any future secondary-routing
            # decision is explicit; today every node in the MVP cluster
            # is primary-eligible.
            read_preference=ReadPreference.PRIMARY,
        )
        self._database: AsyncIOMotorDatabase[Any] = self._client[settings.mongodb_database]

    @property
    def database(self) -> AsyncIOMotorDatabase[Any]:
        """Return the configured database handle.

        Repositories receive this — never the raw client — so they
        cannot accidentally reach into other databases on the same
        cluster without an explicit decision.
        """
        return self._database

    async def close(self) -> None:
        """Close the underlying Motor client.

        Safe to call multiple times: Motor's `close()` is idempotent.
        """
        self._client.close()

    async def ping(self) -> None:
        """Round-trip the admin `ping` command.

        Used by tests as a smoke check that the URI + DB name actually
        resolve to a live server. Production code does not need this —
        the /healthz probe already covers liveness.
        """
        await self._client.admin.command("ping")
