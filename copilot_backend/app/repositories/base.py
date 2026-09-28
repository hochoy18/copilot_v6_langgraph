"""Base class shared by every repository.

Centralises the small set of helpers that any repository needs:

* `to_object_id` — string → `bson.ObjectId`, with `InvalidIdError` on
  malformed input (the seam operators use).
* `_coerce_id` — Mongo doc with `_id: ObjectId` → dict copy with
  `_id: str`. Subclass parse helpers call this and then `model_validate`
  on the right Pydantic model.
* `_translate_duplicate` — `pymongo.errors.DuplicateKeyError` →
  `app.db.errors.DuplicateKeyError` with the offending index name
  surfaced under `details.index`.
* `_now` — UTC clock, overridable by subclasses for deterministic
  tests.

Why a base class: the per-collection repositories share too much logic
for composition but too little for an `ABC`; a concrete class with
overridable static helpers hits it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, TypeVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import DuplicateKeyError, InvalidIdError

# Generic type for the canonical read shape (e.g. `User`). The base
# class doesn't know which collection it serves; subclasses parameterise
# this with the right Pydantic model.
TRead = TypeVar("TRead")
TCreate = TypeVar("TCreate")
TUpdate = TypeVar("TUpdate")


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime, millisecond-precision.

    MongoDB stores datetimes as BSON `Date` which is always UTC and
    milliseconds — anything finer is truncated on the round-trip. We
    intentionally drop tzinfo AND sub-millisecond precision so an
    in-memory timestamp equals what comes out of a follow-up
    `find_one_and_update`. The two together let tests assert
    `created_at == updated_at` (same insert) and `updated.created_at ==
    created.created_at` (unchanged across an update) without flake.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


class BaseRepository(Generic[TRead, TCreate, TUpdate]):
    """Common scaffolding for all repositories."""

    # Subclasses set this to the Mongo collection name (see app.db.indexes).
    collection_name: ClassVar[str]

    def __init__(self, database: AsyncIOMotorDatabase[Any]) -> None:
        self._db: AsyncIOMotorDatabase[Any] = database
        self._collection: AsyncIOMotorCollection[Any] = database[self.collection_name]

    # --- Helpers --------------------------------------------------------

    @staticmethod
    def to_object_id(value: str) -> ObjectId:
        """Coerce a string id into a `bson.ObjectId`, raising `InvalidIdError` on garbage.

        Catching `bson.errors.InvalidId` here keeps the public surface
        uniform: callers ask "give me the user with this id" and either
        get the user back or a 404 envelope — never a raw driver
        exception.
        """
        try:
            return ObjectId(value)
        except Exception as exc:  # noqa: BLE001 — bson raises generic Exception
            raise InvalidIdError(
                message_en=f"Invalid id: {value!r}",
                details={"id": value, "error": str(exc)},
            ) from exc

    @staticmethod
    def _coerce_id(doc: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `doc` with `_id` coerced to str (idempotent).

        Mongo stores `_id` as `bson.ObjectId`; the canonical read shape
        carries `id` as a string. Centralising the coercion here means
        every read path produces the same parsed shape and a future
        migration (e.g. uuid ids) only needs to change one place.
        """
        if "_id" in doc and not isinstance(doc["_id"], str):
            doc = {**doc, "_id": str(doc["_id"])}
        return doc

    @staticmethod
    def _translate_duplicate(exc: PyMongoDuplicateKeyError) -> DuplicateKeyError:
        """Translate the driver's `DuplicateKeyError` into the unified envelope.

        Pymongo embeds the offending index name in the exception args;
        we surface it under `details.index` so admin tooling can show
        "email already exists" vs "sso_subject already exists" without
        parsing strings.
        """
        msg = str(exc)
        # `E11000 duplicate key error collection: ... index: uniq_...`
        index_name = None
        marker = "index: "
        if marker in msg:
            index_name = msg.split(marker, 1)[1].split(" ", 1)[0].strip()
        return DuplicateKeyError(
            message_en=f"Duplicate key on index {index_name or '?'}",
            details={"index": index_name, "driver_message": msg},
        )

    @staticmethod
    def _now() -> datetime:
        """Inject the wall clock — overridable for tests."""
        return utcnow()
