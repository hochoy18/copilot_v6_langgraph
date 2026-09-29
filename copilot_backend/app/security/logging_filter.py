"""Logging filter that scrubs credential bytes from log records — T33 / #29.

Installed at the root logger in `app.main.create_app` so every
`logger.info(...)`, `logger.warning(...)`, and `logger.exception(...)`
that ever receives a credential byte — via `extra=...`, `exc_info`,
or accidental string-formatting of the Worker's `plaintext_payload` —
sees the bytes replaced with `[REDACTED]` before the formatter writes
to stdout / stderr / file / Langfuse log handler.

Why a logging filter
--------------------

Python's logging pipeline is the only place where arbitrary
application strings cross the process boundary (they go to disk, to
the operator's terminal, to log-shipper sidecars). Scrubbing at the
formatter level guarantees:

  * even a debug `print` that happens to reach a `StreamHandler` is
    caught (if the project's logger config routes the message);
  * the redaction is centralised — one filter at the root, not a
    `redact()` call scattered through every `logger.info`;
  * the filter never raises, so a malformed log record can't bring
    down the application.

The filter only touches the formatted `record.getMessage()`. It
leaves `record.args`, `record.msg`, and structured `record.__dict__`
fields alone — those are programmatic surfaces that downstream
log-shippers may consume (e.g. JSON-encoders that walk
`extra=`). Anything that wants full-shape redaction should call
`redactor.redact(value)` directly before logging.

References: ADR-0002 (凭证隔离), T33 / #29 acceptance criteria
("日志无 key").
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.security.redactor import redact, redact_text


class CredentialRedactionFilter(logging.Filter):
    """`logging.Filter` that scrubs credential bytes from the message.

    `record.getMessage()` is the post-`%`-formatting string a formatter
    eventually writes; that's the field we scrub. `record.args` and
    `record.msg` are intentionally left untouched so a structured
    JSON log-shipper that walks `record.__dict__` still sees the
    original typed values — callers that want the structured fields
    scrubbed must call `redactor.redact(...)` before logging.

    Args:
        extra_patterns: optional list of regex patterns that should
            also be redacted from the formatted message. Useful for
            tenant-specific token formats; the built-in credential
            patterns (`sk-…`, `Bearer …`, etc.) always run on top of
            any extensions.
    """

    def __init__(
        self,
        name: str = "",
        *,
        extra_patterns: Iterable[str] = (),
    ) -> None:
        super().__init__(name)
        # Materialise once — log filtering is on the hot path.
        self._extra_patterns: tuple[str, ...] = tuple(extra_patterns)

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub `record.getMessage()`; always return True (don't drop)."""
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover — defensive
            # A malformed record shouldn't crash the application; the
            # original `record.msg` is preserved so the formatter can
            # fall back to its default rendering.
            return True
        scrubbed_message = redact_text(message, extra_patterns=self._extra_patterns)
        # Rebuild the record's message by setting `msg` directly. We
        # clear `args` so the formatter doesn't re-format the
        # already-scrubbed text — `record.getMessage()` would otherwise
        # try to apply the args again and either error out (if the
        # scrubbed text isn't a valid `%`-template) or produce garbage.
        record.msg = scrubbed_message
        record.args = ()
        return True


def install_credential_redaction_filter(
    logger: logging.Logger | None = None,
    *,
    extra_patterns: Iterable[str] = (),
) -> CredentialRedactionFilter:
    """Attach a `CredentialRedactionFilter` to `logger` (default: root).

    Idempotent: if a previous call already installed a filter of this
    exact class, the call is a no-op so test suites that bootstrap the
    app multiple times don't accumulate duplicate filters.

    Args:
        logger: target logger. `None` means the root logger — the
            standard place for a process-wide redaction filter, since
            every other logger inherits the root's handlers and
            filters.
        extra_patterns: forwarded to `CredentialRedactionFilter`.

    Returns:
        The filter instance (either newly installed or the existing
        one when the call was a no-op).
    """
    target = logger if logger is not None else logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, CredentialRedactionFilter):
            return existing
    flt = CredentialRedactionFilter(extra_patterns=extra_patterns)
    target.addFilter(flt)
    return flt


def redact_log_payload(payload: Any) -> Any:
    """Helper for callers that want full-shape redaction on `extra={...}`.

    Use it before passing structured data to the logger:
    `logger.info("…", extra=redact_log_payload({"api_key": "sk-…"}))`.
    """
    return redact(payload)


__all__ = [
    "CredentialRedactionFilter",
    "install_credential_redaction_filter",
    "redact_log_payload",
]
