"""Conversation-domain exceptions — T10 / #40.

`ConversationService` raises these from the seam between "the request
is well-formed" and "we can serve it". The router never has to know
about `bson.ObjectId` coercion or which MongoDB row was returned —
every failure path lands here, every success path returns the
canonical shape.

Renders the same `not_found` envelope as `app.db.errors.NotFoundError`
on cross-user access: a stranger reaching for someone else's
conversation must not be able to tell "this exists but isn't yours"
apart from "this doesn't exist". The two failures share an envelope
on purpose (ADR-0002 layers the privacy story on top of the standard
`AppError` contract).
"""
from __future__ import annotations

from fastapi import status

from app.db.errors import NotFoundError
from app.exceptions import AppError


class ConversationAccessDeniedError(NotFoundError):
    """Raised when the requested conversation belongs to another user.

    Inherits `NotFoundError` (same `code` / `http_status` envelope) so
    cross-user lookups look identical to absent-resource lookups on
    the wire. The detail envelope carries the caller's `user_id` for
    audit forensics without leaking the conversation id (an attacker
    could probe their own `user_id` vs the response shape; the
    conversation id is never echoed).
    """

    code = "not_found"
    message_zh = "会话不存在"
    message_en = "Conversation not found"
    http_status = status.HTTP_404_NOT_FOUND


class ConversationArchivedError(AppError):
    """Raised when a new Turn is submitted into an `archived` conversation.

    ADR-0011 makes archived conversations read-only storage: the user
    must reactivate (which creates a fresh conversation referencing
    the old data) before chatting again. 409 rather than 404 because
    the caller legitimately owns the row — this is a state conflict,
    not an existence question. `POST /conversations/{id}/turns`
    (T18 / #16) is the first raiser; the reactivation endpoint
    lands with T39.
    """

    code = "conversation_archived"
    message_zh = "会话已归档, 请重新激活后再发起对话"
    message_en = "Conversation is archived; reactivate it before sending a new turn"
    http_status = status.HTTP_409_CONFLICT


__all__ = ["ConversationAccessDeniedError", "ConversationArchivedError"]