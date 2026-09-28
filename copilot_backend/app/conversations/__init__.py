"""Conversation-domain service layer — T10 / #40.

The router (`app.api.conversations`) translates HTTP envelopes into
service calls; the repositories (`app.repositories.conversations`,
`app.repositories.turns`, `app.repositories.plans`) translate Mongo;
this package owns the rules that bridge the two — chiefly the
ownership check that ties a conversation to the authenticated user,
the `archive` lifecycle transition described in ADR-0011, and the
Plan approve / reject HITL endpoints (T20 / #43 / ADR-0004).

Submodules
----------

* `service` — `ConversationService` (create / list / detail / archive /
  approve_plan / reject_plan).
* `errors` — domain exceptions rendered by the global error handler.
"""

from app.conversations.errors import (
    ConversationAccessDeniedError,
    PlanNotPendingError,
)
from app.conversations.service import (
    ConversationDetail,
    ConversationService,
)

__all__ = [
    "ConversationService",
    "ConversationDetail",
    "ConversationAccessDeniedError",
    "PlanNotPendingError",
]
