"""Repository layer for the Copilot backend.

Each repository owns one MongoDB collection and exposes a small,
typed surface to the rest of the backend:

* `create(input)` — insert a new document, returning the canonical read shape.
* `get(id)` — look up by primary key, raising `NotFoundError` on miss.
* `get_by_*` — alternate lookups (email, token_hash, etc.) backed by
  the indexes defined in `app.db.indexes`.
* `list(...)` — paginated reads (cursor-based per SPEC).
* `update(id, patch)` — atomic partial update with `updated_at` bump.
* `delete(id)` — hard delete; soft-delete semantics land with each
  collection's domain logic, not here.

Repositories accept an `AsyncIOMotorDatabase` and a `collection_name`
so they can be instantiated against any database handle — production
uses `app.state.database`; tests use `mongomock_motor`.

Why a dedicated package: SPEC § "模块边界" names a `repositories`
seam; grouping all CRUD here keeps the routers thin and the test seam
broad. New collections (T05 tools, T06 credentials, …) extend this
package one file at a time.
"""
