"""Base class shared by every repository.

Centralises the small set of helpers that any repository needs:

* ObjectId ↔ str conversion for primary keys.
* Mapping a MongoDB document into the canonical read shape (which
  usually involves stripping internal-only fields like `password_hash`).
* The "now" clock — pinned to UTC `datetime.now(UTC)` so tests can
  patch it.

Why a base class: the per-collection repositories share too much logic
for composition but too little for an `ABC`; a concrete class with
overridable hooks (`_to_read_model`, `_now`) hits the sweet spot.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, TypeVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from app.db.errors import DuplicateKeyError, NotFoundError

# Generic type for the canonical read shape (e.g. `User`). The base
# class doesn't know which collection it serves; subclasses parameterise
# this with the right Pydantic model.
TRead = TypeVar("TRead")
TCreate = TypeVar("TCreate")
TUpdate = TypeVar("TUpdate")


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime.

    MongoDB stores datetimes as BSON `Date` which is always UTC. We
    intentionally drop tzinfo here so the values round-trip through
    Pydantic / Motor without coercion surprises.
    """
    return datetime.now(UTC).replace(tzinfo=None)


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
        """Coerce a string id into a `bson.ObjectId`, raising `NotFoundError` on garbage.

        Catching `bson.errors.InvalidId` here keeps the public surface
        uniform: callers ask "give me the user with this id" and either
        get the user back or a `NotFoundError` (HTTP 404), never a raw
        driver exception.
        """
        try:
            return ObjectId(value)
        except Exception as exc:  # noqa: BLE001 — bson raises generic Exception
            raise NotFoundError(
                code="invalid_id",
                message_en=f"Invalid id: {value!r}",
                details={"id": value, "error": str(exc)},
            ) from exc

    @staticmethod
    def _to_read(doc: dict[str, Any] | None) -> dict[str, Any]:
        """Default `_id` → `id` rename for callers that bypass Pydantic.

        Subclasses override this when the read shape needs more
        transformations (e.g. dropping `password_hash`).
        """
        if doc is None:
            raise NotFoundError(message_en="Document not found")
        doc = dict(doc)
        if "_id" in doc and "id" not in doc:
            doc["id"] = str(doc.pop("_id"))
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
