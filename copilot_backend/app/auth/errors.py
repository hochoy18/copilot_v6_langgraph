"""Auth-domain exceptions (T07 / #8).

The repository layer raises generic database-shaped errors
(`NotFoundError`, `DuplicateKeyError`, …). The auth layer needs
finer-grained errors so callers can render the right HTTP envelope:

* `RefreshTokenNotFoundError` — token absent (404 envelope).
* `RefreshTokenRevokedError` — token was revoked (rotation,
  logout, admin force-logout).
* `RefreshTokenExpiredError` — `expires_at` is in the past. The TTL
  index will purge the row eventually; this gives a sensible
  message in the meantime.
* `RefreshTokenReuseError` — a previously-rotated token was presented
  again. The service has already revoked the entire family; this
  signal lets the caller route the user to "re-authenticate" rather
  than "session expired".

All classes derive from `app.exceptions.AppError` so the global
exception handler renders them uniformly.
"""
from __future__ import annotations

from fastapi import status

from app.exceptions import AppError


class RefreshTokenNotFoundError(AppError):
    """Raised when a presented refresh token has no row at all.

    Distinct from `RefreshTokenRevokedError`: this means the token was
    never issued (typo, never issued, or manually inserted for testing
    without `issue(...)`). 404 keeps the auth envelope consistent with
    `NotFoundError`.
    """

    code = "refresh_token_not_found"
    message_zh = "刷新令牌不存在"
    message_en = "Refresh token not found"
    http_status = status.HTTP_404_NOT_FOUND


class RefreshTokenRevokedError(AppError):
    """Raised when a presented refresh token has `revoked_at` set.

    Either the user logged out, an admin force-logged them out, or an
    earlier refresh rotated it. The reuse path raises a sibling error
    (`RefreshTokenReuseError`) that the service has already-revoked
    the family for; plain revocations land here.
    """

    code = "refresh_token_revoked"
    message_zh = "刷新令牌已失效"
    message_en = "Refresh token revoked"
    http_status = status.HTTP_401_UNAUTHORIZED


class RefreshTokenExpiredError(AppError):
    """Raised when `expires_at` is in the past.

    The TTL index on `expires_at` will purge the row eventually; this
    error gives a human-readable 401 in the gap.
    """

    code = "refresh_token_expired"
    message_zh = "刷新令牌已过期"
    message_en = "Refresh token expired"
    http_status = status.HTTP_401_UNAUTHORIZED


class RefreshTokenReuseError(AppError):
    """Raised when a previously-rotated token is presented again.

    Per the OAuth 2.0 Security BCP, this is a compromise signal: an
    attacker who exfiltrated a refresh token and replayed it after the
    legitimate user rotated has just exposed themselves. The service
    revokes the entire `family_id` before raising this; the caller
    should log the event and walk the user through re-authentication.
    """

    code = "refresh_token_reuse_detected"
    message_zh = "检测到刷新令牌被重放,会话已强制登出"
    message_en = "Refresh token reuse detected; family revoked"
    http_status = status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# OIDC SSO (T08 / #46)
# ---------------------------------------------------------------------------
#
# These mirror the OIDC callback's "couldn't complete safely" family
# per ADR-0009 / ADR-0006. Status codes are tuned per cause rather
# than collapsed to a single 401:
#
# * `OIDCStateMismatchError` — 401: client-side mismatch (no state, OR
#   TTL elapsed). Same envelope as the refresh-token family so the
#   front-end's i18n layer renders one shape.
# * `OIDCIDTokenInvalidError` — 401: signature / shape problem with
#   the IdP-issued `id_token`.
# * `OIDCClaimsMismatchError` — 401: `id_token` decoded cleanly but
#   one of `iss` / `aud` / `nonce` / `exp` / identity claims didn't
#   match — config drift or a forged token.
# * `OIDCTokenExchangeError` — 502: the IdP rejected the code, or the
#   upstream call failed. Upstream's fault, not the client's.
# * `OIDCDiscoveryError` — 502: IdP metadata fetch failed.
# * `UserInactiveError` — 403: IdP can vouch for identity, but our
#   local admin offboarded the user. Distinct from the auth-failure
#   401s so the front-end renders "account disabled" not "wrong
#   password".


class OIDCStateMismatchError(AppError):
    """The `state` posted to the callback doesn't match one we issued.

    Two causes — both treated as 401:

    * the callback is a CSRF / replay (state never seen).
    * the entry was swept by TTL (user took longer than
      `oidc_state_ttl_seconds` to round-trip through the IdP).

    Either way, the safe response is "start the login over".
    """

    code = "oidc_state_mismatch"
    message_zh = "OIDC 登录会话已过期"
    message_en = "OIDC login session expired or invalid"
    http_status = status.HTTP_401_UNAUTHORIZED


class OIDCIDTokenInvalidError(AppError):
    """The IdP-issued `id_token` failed verification.

    Covers signature mismatch, malformed JWT shape, malformed JSON
    payload, and unsupported `alg`. The detail envelope names the
    specific failure so audit forensics can log it without a
    stringly-typed message.
    """

    code = "oidc_id_token_invalid"
    message_zh = "IdP 返回的身份凭据无效"
    message_en = "IdP-issued id_token is invalid"
    http_status = status.HTTP_401_UNAUTHORIZED


class OIDCClaimsMismatchError(AppError):
    """An `id_token` claim didn't match what we expected.

    Per ADR-0009 we pin `iss`, `aud`, `nonce`, and `exp`. Any mismatch
    is rejected with a distinct `code` so the front-end can show
    "configuration drifted" rather than the generic "invalid token".
    """

    code = "oidc_claims_mismatch"
    message_zh = "IdP 身份凭据与本系统配置不匹配"
    message_en = "OIDC claims do not match local configuration"
    http_status = status.HTTP_401_UNAUTHORIZED


class OIDCTokenExchangeError(AppError):
    """The IdP's token endpoint returned an error or non-JSON response.

    Distinct from `OIDCIDTokenInvalidError` — this is the upstream
    HTTP call failing, not the resulting token being wrong. The
    detail envelope carries the IdP-side `error` and optional
    `error_description` so ops can correlate against the IdP logs.
    """

    code = "oidc_token_exchange_failed"
    message_zh = "IdP 换票失败"
    message_en = "OIDC token exchange with IdP failed"
    http_status = status.HTTP_502_BAD_GATEWAY


class OIDCDiscoveryError(AppError):
    """Discovery document fetch or parse failed.

    Surfaced as a 502 because the upstream IdP is the failure point.
    The detail envelope carries the IdP URL + error message so the
    front-end can render "SSO temporarily unavailable" with a
    distinguishing error class.
    """

    code = "oidc_discovery_failed"
    message_zh = "无法获取 IdP 元数据"
    message_en = "Failed to fetch OIDC discovery document"
    http_status = status.HTTP_502_BAD_GATEWAY


class UserInactiveError(AppError):
    """A previously-known user is deactivated and cannot log in.

    Distinct from a missing/incorrect credential: the IdP can vouch
    for identity, but local policy (admin offboarding, compliance
    hold) supersedes that. Surfaced as 403 to distinguish from the
    401/404 family that signals "credentials don't match".
    """

    code = "user_inactive"
    message_zh = "用户已停用"
    message_en = "User is deactivated"
    http_status = status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Local admin login (T09 / #10)
# ---------------------------------------------------------------------------


class InvalidLocalCredentialsError(AppError):
    """Raised when an admin local-login attempt fails the credential check.

    Two causes share this envelope so the front-end cannot enumerate
    usernames: the user does not exist, or the password does not match.
    Either way the wire response is `401 invalid_local_credentials` —
    matching the OAuth 2.0 Security BCP recommendation to be
    indistinguishable from a missing user.
    """

    code = "invalid_local_credentials"
    message_zh = "用户名或密码错误"
    message_en = "Invalid username or password"
    http_status = status.HTTP_401_UNAUTHORIZED


class AdminEndpointRequiresLocalUserError(AppError):
    """A non-local user reached an admin-only endpoint (T09 / #10).

    The `/admin` shell and the `/admin/me` route are reserved for the
    local admin path per ADR-0006; an SSO caller authenticates
    correctly but the endpoint isn't theirs. Distinct from
    `UserInactiveError`: that signals a credentials problem, this one
    signals "your account type doesn't fit this endpoint".
    """

    code = "admin_endpoint_requires_local_user"
    message_zh = "该接口仅供本地管理员使用"
    message_en = "This endpoint is reserved for local admin users"
    http_status = status.HTTP_403_FORBIDDEN


__all__ = [
    "RefreshTokenNotFoundError",
    "RefreshTokenRevokedError",
    "RefreshTokenExpiredError",
    "RefreshTokenReuseError",
    "OIDCStateMismatchError",
    "OIDCIDTokenInvalidError",
    "OIDCClaimsMismatchError",
    "OIDCTokenExchangeError",
    "OIDCDiscoveryError",
    "UserInactiveError",
    "InvalidLocalCredentialsError",
    "AdminEndpointRequiresLocalUserError",
]
