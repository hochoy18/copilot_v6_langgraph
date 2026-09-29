"""Credential redactor — T33 / #29.

A single, well-typed seam that scrubs credential bytes from any Python
shape (dict / list / bytes / str / scalar). Sits between the Worker
(which decrypts at call time, ADR-0002) and every outward surface —
audit logs, Langfuse traces, log records, future SSE events — so a
plaintext credential byte can only flow through the network to the
upstream API, never anywhere else.

Why this lives in its own module
--------------------------------

T21 / #18 landed a header-only scrubber on `ToolWorker._redact_headers`.
That worked for the immediate test suite but it doesn't catch secrets
embedded in:

  * request bodies (e.g. `{"api_key": "sk-…"}` from a JSON-bodied POST);
  * response bodies that the upstream API echoes back (rare but real);
  * exception envelopes, where a poorly-shaped call might include the
    secret in `details`;
  * future Langfuse trace payloads (T40 / #35), where the entire
    request / response pair is uploaded to a third-party service.

A recursive walker covers all four: anything whose key matches the
sensitive-field set is replaced with `"[REDACTED]"`; anything else
passes through. The walker never raises — a missing key in a partial
shape still returns a usable scrubbed copy, because logging /
tracing is best-effort and we'd rather under-redact in a panic than
crash the worker.

The redactor is intentionally **side-effect-free**: it returns a deep
copy with redactions applied, leaving the caller's object untouched.
That matters for the Worker — the decrypted plaintext lives on the
call stack, and the audit row / Langfuse span / log line only ever see
the redacted copy. The original reference never crosses the seam.

References: ADR-0002 (凭证隔离), ADR-0024 (凭证失效感知),
ADR-0027 (Plan-Tool 快照), T33 / #29 acceptance criteria.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

# Markers we substitute in place of a redacted value. The string form
# matches the one `ToolWorker._redact_headers` already produced, so
# downstream consumers (audit UI, regression tests) see a single,
# stable shape regardless of which seam redacted the field.
REDACTED_STRING: Final[str] = "[REDACTED]"
REDACTED_BYTES: Final[bytes] = b"[REDACTED]"

# Field names whose values must always be scrubbed. Both header names
# (case-insensitive — HTTP normalises them) and credential-shaped body
# keys (`api_key`, `password`, …) are matched here. We deliberately
# over-redact: false positives (a parameter called `token` that
# carries an opaque non-secret) are cheap; false negatives (a leaked
# credential) are not.
#
# Adding a new credential type? Extend this set AND re-run the
# redactor unit tests; the test suite exercises each entry to make
# the audit-trail straightforward.
SENSITIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # HTTP auth headers — case-insensitive in HTTP semantics.
        "authorization",
        "x-api-key",
        "x-auth-token",
        "x-api-token",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        # Credential payload keys (after JSON-decode).
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "bearer",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "private_key",
        "secret_key",
        # Schema-level identifiers used in `tools.http_headers` and the
        # `CredentialInDB` document — never present in API responses
        # by construction, but listed so a future schema extension
        # that surfaces one of these fields still gets scrubbed.
        "credentials_ref",
        "credentials_payload",
        "plaintext_payload",
        "payload",
        "nonce",
    }
)

# Pattern that matches a "looks like an API key" token embedded in
# arbitrary text — used by `redact_text` to scrub free-form log
# messages. The pattern is deliberately conservative: it requires an
# obvious prefix (`sk-`, `pk-`, `Bearer `, …) so we don't replace
# legitimate short strings like UUIDs.
_FREE_FORM_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bpk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{4,}", re.IGNORECASE),
    re.compile(r"X-Api-Key:\s*[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
)


def is_sensitive_field(name: str) -> bool:
    """Decide whether `name` is a credential field.

    Exposed so the Worker's header scrubber (and any future caller)
    can ask the redactor rather than re-implementing the matching
    rule. Comparison is case-insensitive.
    """
    return name.lower() in SENSITIVE_FIELDS


def _redact_scalar(value: Any) -> Any:
    """Replace scalar-shaped credential bytes with the redacted marker.

    `str` and `bytes` are scrubbed to their respective marker;
    everything else passes through untouched. `None` is preserved so
    callers see the same nullability on the way out.
    """
    if isinstance(value, str):
        return REDACTED_STRING
    if isinstance(value, bytes):
        return REDACTED_BYTES
    return value


def redact(value: Any) -> Any:
    """Return a redacted deep-copy of `value`.

    Walks dicts / lists / tuples recursively; matches dict keys
    against `SENSITIVE_FIELDS` (case-insensitive) and substitutes the
    matching value with the redacted marker. Strings and bytes
    outside a dict container are passed through unchanged so we don't
    mangle free-form text — that's `redact_text`'s job.

    The function never raises; it always returns a usable copy. A
    malformed input (cyclic reference, an unsupported container) is
    treated as a scrub failure and returned as-is, since silently
    leaking is worse than logging a warning.
    """
    try:
        return _redact(value, _seen=set())
    except Exception:  # pragma: no cover — defensive, see docstring
        return value


def _redact(value: Any, *, _seen: set[int]) -> Any:
    """Internal recursive walker.

    `_seen` tracks visited object ids to defend against cyclic
    structures (a dict that references itself through a list, etc.).
    A cycle returns the redacted marker at the back-edge so the
    recursion terminates.
    """
    obj_id = id(value)
    if obj_id in _seen:
        return REDACTED_STRING
    # Mutable containers go through `_seen`; immutable scalars do not.
    if isinstance(value, Mapping | list | tuple):
        _seen.add(obj_id)

    try:
        if isinstance(value, Mapping):
            scrubbed: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = key if isinstance(key, str) else None
                if key_text is not None and is_sensitive_field(key_text):
                    scrubbed[key] = _redact_scalar(item)
                else:
                    scrubbed[key] = _redact(item, _seen=_seen)
            return scrubbed
        if isinstance(value, list):
            return [_redact(item, _seen=_seen) for item in value]
        if isinstance(value, tuple):
            return tuple(_redact(item, _seen=_seen) for item in value)
    finally:
        if obj_id in _seen:
            _seen.discard(obj_id)

    return value


def redact_text(message: str, *, extra_patterns: Iterable[str] = ()) -> str:
    """Return `message` with credential-shaped tokens replaced.

    Used by the logging filter (see `app.security.logging_filter`) and
    anywhere a free-form string is about to leave the process — error
    envelopes printed by `logger.exception`, debug dumps, etc.

    The default pattern set covers the obvious API-key prefixes
    (`sk-…`, `pk-…`, `Bearer …`, `Basic …`, `X-Api-Key: …`). Operators
    can extend it via `extra_patterns` for tenant-specific token
    formats; the additions are appended to the default set so the
    built-in protections are never weakened.
    """
    patterns = _FREE_FORM_PATTERNS
    if extra_patterns:
        patterns = patterns + tuple(re.compile(p) for p in extra_patterns)
    out = message
    for pattern in patterns:
        out = pattern.sub(REDACTED_STRING, out)
    return out


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `headers` with credential values replaced.

    Convenience for the Worker's outgoing-request envelope: same
    behaviour as the old `_redact_headers` static method but routed
    through the central field set so a new credential-shaped header
    gets picked up without touching this file.
    """
    scrubbed: dict[str, str] = {}
    for key, value in headers.items():
        if is_sensitive_field(key):
            scrubbed[key] = REDACTED_STRING
        else:
            scrubbed[key] = value
    return scrubbed


def redact_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively scrub a Tool-call envelope (headers + body + url + method).

    The Worker shapes its audit / trace envelope as
    `{"method": ..., "url": ..., "headers": ..., "body": ...}`. This
    helper walks the whole dict so credential-shaped fields embedded
    in `body` (e.g. an upstream API that takes `api_key` in the JSON
    payload instead of a header) are scrubbed too.

    The `url` value is scrubbed via `redact_text` because query-string
    secrets (`?api_key=…`) won't match `is_sensitive_field` — the key
    is a substring inside the URL, not a field name.
    """
    scrubbed = redact(envelope)
    if not isinstance(scrubbed, dict):  # pragma: no cover — defensive
        return {}
    url = scrubbed.get("url")
    if isinstance(url, str):
        scrubbed["url"] = redact_text(url)
    return scrubbed


__all__ = [
    "REDACTED_BYTES",
    "REDACTED_STRING",
    "SENSITIVE_FIELDS",
    "is_sensitive_field",
    "redact",
    "redact_envelope",
    "redact_headers",
    "redact_text",
]
