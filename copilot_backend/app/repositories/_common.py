"""Internal helpers shared by the T06 (#7) repositories.

T04 / T05 repositories live alongside these but predate the helper
extraction; centralising the boilerplate here keeps the new
`conversations` / `turns` / `plans` / `plan_executions` / `audit_logs`
repositories DRY without rewriting the established `users` /
`tools` / `credentials` surfaces. The T06 code review flagged the
helpers as duplicated code — collecting them here is the minimal
blast radius.

Everything in this module is *internal* — no caller outside
`app/repositories/*.py` should import from here. The `_common`
module name signals "package-private" without forcing a private
import path that Pyright complains about.
"""
from __future__ import annotations

from typing import Any, TypeVar, cast

from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel

from app.db.errors import NotFoundError
from app.repositories.base import BaseRepository

TRead = TypeVar("TRead", bound=BaseModel)
TInDB = TypeVar("TInDB", bound=BaseModel)


def coerce_doc_id(doc: dict[str, Any]) -> dict[str, Any]:
    """Pass-through wrapper for `BaseRepository._coerce_id`.

    Re-exports so the new repos don't need to reach into the base
    class internals. The base class deliberately keeps this static
    so existing `users`/`tools`/`credentials` call-sites keep
    compiling.
    """
    return BaseRepository._coerce_id(doc)


def doc_to_read(doc: dict[str, Any], model: type[TRead]) -> TRead:
    """Parse a Mongo document into the canonical read shape.

    Centralises the `_doc_to_read` boilerplate each T04/T05 repo
    hand-rolled. For most collections `Read == InDB` and the parse
    hits the canonical class directly — `User` and `Credential`
    still need their explicit `from_db` redactors.
    """
    return model.model_validate(coerce_doc_id(doc))


def doc_to_in_db(doc: dict[str, Any], model: type[TInDB]) -> TInDB:
    """Parse a Mongo document into the persisted-shape model."""
    return model.model_validate(coerce_doc_id(doc))


async def refetch_after_insert(
    collection: AsyncIOMotorCollection[Any],
    inserted_doc: dict[str, Any],
    read_model: type[TRead],
) -> TRead:
    """Re-fetch the just-inserted document and return the canonical shape.

    `insert_one` returns an `_id` but not the full row — the safest
    persistence-symmetric path is to read back. The helper absorbs a
    pattern that several T04/T05 repos hand-roll.
    """
    stored = await collection.find_one({"_id": inserted_doc["_id"]})
    if stored is None:
        raise NotFoundError(
            message_en=f"{read_model.__name__} disappeared after insert",
        )
    return doc_to_read(stored, read_model)


async def upsert_array_element(
    collection: AsyncIOMotorCollection[Any],
    *,
    filter_doc: dict[str, Any],
    array_field: str,
    element_id_field: str,
    element_id_value: Any,
    new_element: dict[str, Any],
    not_found_details: dict[str, Any],
) -> dict[str, Any]:
    """Atomically upsert one element of a Mongo array field.

    Two atomic ops:

    1. Try `$set` with a positional (`$`) operator. The filter
       carries `<array_field>.<element_id_field>: element_id_value`
       so Mongo automatically narrows to the matching element —
       the positional then refers unambiguously to that one. If the
       row has the element, `find_one_and_update` returns the
       post-update document in one round trip.
    2. If no element matched, fall back to `$push` filtered by
       `<array_field>.<element_id_field>: {$ne: element_id_value}`,
       so a concurrent insert for the same element can't double-
       push. This is the standard race-free "find-or-insert" Mongo
       idiom.

    Returns the post-update document. Raises `NotFoundError` if the
    parent row was deleted between the two ops (or never existed).
    """
    set_filter = {
        **filter_doc,
        f"{array_field}.{element_id_field}": element_id_value,
    }
    set_result = await collection.find_one_and_update(
        set_filter,
        {"$set": {f"{array_field}.$": new_element}},
        return_document=True,
    )
    if set_result is not None:
        return cast(dict[str, Any], set_result)

    push_filter = {
        **filter_doc,
        f"{array_field}.{element_id_field}": {"$ne": element_id_value},
    }
    push_result = await collection.find_one_and_update(
        push_filter,
        {"$push": {array_field: new_element}},
        return_document=True,
    )
    if push_result is None:
        raise NotFoundError(
            message_en=f"{not_found_details.get('entity', 'Document')} not found",
            details=not_found_details,
        )
    return cast(dict[str, Any], push_result)


async def partial_update_array_element(
    collection: AsyncIOMotorCollection[Any],
    *,
    filter_doc: dict[str, Any],
    array_field: str,
    element_id_field: str,
    element_id_value: Any,
    partial_set: dict[str, Any],
    default_element: dict[str, Any],
    not_found_details: dict[str, Any],
) -> dict[str, Any]:
    """Atomic partial-update of one Mongo array element by id.

    Two atomic ops, mirroring `upsert_array_element`:

    1. `find_one_and_update` with `arrayFilters` targeting the
       specific element by `element_id_field`. The partial `$set`
       paths are rooted at `array_field.$[<id>]` so only the matched
       element is touched — sibling elements are unaffected. If the
       element exists, the post-update doc comes back in one round
       trip.
    2. If the filter didn't match, fall back to `$push` filtered by
       `array_field.element_id_field: {$ne: value}` to avoid a
       double-insert.

    `partial_set` keys are written into the matched element. Any
    field the caller omits is preserved at its prior value (Mongo
    doesn't touch it). Use this seam for the "set one field, leave
    others" pattern.

    NOTE: requires MongoDB ≥ 3.6 (`arrayFilters` syntax). Test
    suites that use `mongomock` should exercise
    `upsert_array_element` instead — mongomock 4.x supports the
    positional `$` but the `$[<id>]` form trips a parsing quirk.
    """
    array_filter = {f"elem.{element_id_field}": element_id_value}
    set_paths: dict[str, Any] = {
        f"{array_field}.$[elem].{k}": v for k, v in partial_set.items()
    }
    existing = await collection.find_one_and_update(
        {**filter_doc, f"{array_field}.{element_id_field}": element_id_value},
        {"$set": set_paths},
        array_filters=[array_filter],
        return_document=True,
    )
    if existing is not None:
        return cast(dict[str, Any], existing)

    pushed = await collection.find_one_and_update(
        {
            **filter_doc,
            f"{array_field}.{element_id_field}": {"$ne": element_id_value},
        },
        {"$push": {array_field: default_element}},
        return_document=True,
    )
    if pushed is None:
        raise NotFoundError(
            message_en=f"{not_found_details.get('entity', 'Document')} not found",
            details=not_found_details,
        )
    return cast(dict[str, Any], pushed)


__all__ = [
    "coerce_doc_id",
    "doc_to_read",
    "doc_to_in_db",
    "refetch_after_insert",
    "upsert_array_element",
    "partial_update_array_element",
]
