"""Admin-role guard — T12 / #11.

The `/api/v1/admin/*` router family is reserved for callers whose
`users.role_ids` resolves to a Role with `name='admin'` (ADR-0006 /
ADR-0031). `require_admin_user` extends `get_current_user` with that
check so route handlers can declare a single dependency and trust the
caller is an admin.

Why a separate module from `app.security.auth`
-----------------------------------------------

`get_current_user` answers "who is this caller?", which is a question
every authenticated route asks. "Is this caller an admin?" is a
narrower question only the `/admin/*` routes ask. Putting both in the
same module would either force every conversation / chat route to
import admin-only helpers or pull role-lookup code into the auth
seam. Splitting keeps each module focused: `auth.py` handles the
JWT-decode contract, `admin.py` handles the role-resolution contract.

Role lookup semantics
---------------------

We deliberately resolve role names server-side instead of trusting
`role_ids` from the JWT alone. Two reasons:

1. **Freshness.** A role rename / role grant should take effect on the
   next request, not the next access-token mint (15-minute TTL). The
   user's `role_ids` on the `users` row can change without rotating
   the JWT.
2. **Composability.** Future tickets (T45, role management UI) will
   let admins rename the `admin` slug — e.g. to `tenant_admin` per
   tenant. A JWT that hard-codes the slug would block that. The
   `RoleRepository.get_by_id` lookup keeps the slug-resolution at the
   admin boundary where renames live.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.dependencies import get_database
from app.db.schemas import User
from app.exceptions import AppError
from app.repositories.roles import RoleRepository
from app.security.auth import get_current_user

# Slug for the admin role. ADR-0006 fixes the seed name; future
# tenant-scoped admin tickets (T45) can extend the resolver without
# touching every route that depends on it.
ADMIN_ROLE_NAME: str = "admin"


class AdminEndpointRequiresAdminRoleError(AppError):
    """Raised when a non-admin caller reaches an admin-only endpoint.

    Distinct from `AdminEndpointRequiresLocalUserError` (T09 / #10):
    that one is "wrong account type" (SSO caller hitting `/admin/me`),
    this one is "right account type but missing the `admin` role".
    The two share the 403 status so the admin shell renders one
    "forbidden" page; the `code` differs so the audit log / ops
    dashboards can tell them apart.
    """

    code = "admin_endpoint_requires_admin_role"
    message_zh = "该接口仅供管理员使用"
    message_en = "This endpoint is reserved for users with the admin role"
    http_status = status.HTTP_403_FORBIDDEN


async def _user_has_admin_role(
    user: User,
    *,
    database: AsyncIOMotorDatabase[Any],
) -> bool:
    """Return `True` iff `user.role_ids` contains a Role with `name='admin'`.

    Resolved against the live `roles` collection — never the JWT
    claim — so a role rename or grant change takes effect on the next
    request. Returns `False` for an empty `role_ids` list (which is
    the seed shape for non-admin users).
    """
    if not user.role_ids:
        return False
    roles = RoleRepository(database)
    resolved = await roles.list_by_ids(list(user.role_ids))
    return any(role.name == ADMIN_ROLE_NAME for role in resolved)


async def require_admin_user(
    user: User = Depends(get_current_user),  # noqa: B008
    database: AsyncIOMotorDatabase[Any] = Depends(get_database),  # noqa: B008
) -> User:
    """FastAPI dependency: return the authenticated `User` iff they hold the admin role.

    Wraps `get_current_user` (which decodes the access JWT and looks
    up the canonical `User` row). On success the canonical `User` is
    returned unchanged — admin routes can use it like any other
    authenticated route. On failure raises
    `AdminEndpointRequiresAdminRoleError` (403), rendered by the
    global `AppError` handler.

    Raises:
        AdminEndpointRequiresAdminRoleError: caller authenticates
            successfully but does not hold the `admin` role. 403.
    """
    is_admin = await _user_has_admin_role(user, database=database)
    if not is_admin:
        raise AdminEndpointRequiresAdminRoleError(
            details={"user_id": user.id},
        )
    return user


__all__ = [
    "ADMIN_ROLE_NAME",
    "AdminEndpointRequiresAdminRoleError",
    "require_admin_user",
]
