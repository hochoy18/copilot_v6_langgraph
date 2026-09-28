"""MongoDB wiring for the Copilot backend.

T04 (#5) introduces Motor (the async MongoDB driver) and the `copilot`
logical database that hosts the four core collections defined in
`docs/SPEC.md` (users / roles / refresh_tokens / tool_groups). The
package owns:

* `mongo.py` — `MongoClient` lifecycle (open per process, close on
  shutdown). One client per process is the documented Motor pattern; it
  shares a connection pool across requests.
* `schemas.py` — Pydantic models that mirror the wire shape of each
  collection. Repositories exchange these, never raw `dict`s.
* `indexes.py` — Single source of truth for the index spec. `init_db.py`
  reads from here so the schema (collections + indexes) is created in
  one pass.
* `init_db.py` — CLI + library entrypoint: create collections + indexes
  idempotently. Run with `python -m scripts.init_db` (see the
  `scripts/init_db.py` shim in the repo root).

The seam is the `AsyncIOMotorDatabase` handle: repositories and the
init script accept it as an argument. The FastAPI lifespan in
`app/main.py` opens one client and stashes both the client and the
database handle on `app.state`, then closes the client on shutdown.

Why a package (`db/`) rather than a single module: the schema /
index / init pieces have non-trivial size and want their own test
files; a flat module would push the repo over 200 lines.
"""
