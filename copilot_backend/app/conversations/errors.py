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


__all__ = ["ConversationAccessDeniedError"]