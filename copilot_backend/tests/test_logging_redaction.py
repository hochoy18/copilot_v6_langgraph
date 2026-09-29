"""Tests for the credential redaction logging filter — T33 / #29.

Acceptance criterion: "日志无 key". The filter installs at the root
logger and scrubs `record.getMessage()` before the formatter writes
to stdout / file / log-shipper sidecar. The unit tests pin:

* a plain string carrying an `sk-…` token is scrubbed;
* `%`-formatted args that smuggle in a credential are also scrubbed;
* `extra={...}` payloads reach the filter as the formatted message
  (`record.getMessage()` is post-format), so we verify the message
  side — callers that want structured redaction must call
  `redact_log_payload` themselves;
* `install_credential_redaction_filter` is idempotent (multiple
  calls don't accumulate duplicate filters on the same logger);
* the filter never drops records (always returns True).
"""
from __future__ import annotations

import logging

import pytest

from app.security.logging_filter import (
    CredentialRedactionFilter,
    install_credential_redaction_filter,
    redact_log_payload,
)
from app.security.redactor import REDACTED_STRING


@pytest.fixture
def logger() -> logging.Logger:
    """A fresh logger per test — keeps filter state isolated."""
    return logging.getLogger(f"tests.logging_redaction.{id(object())}")


def _make_record(name: str, msg: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
    """Build a `LogRecord` for tests, bypassing `Logger.makeRecord`'s positional API."""
    return logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestCredentialRedactionFilter:
    """`CredentialRedactionFilter.filter` scrubs the formatted message."""

    def test_scrubs_known_credential_prefix(self, logger: logging.Logger) -> None:
        flt = CredentialRedactionFilter()
        record = _make_record(
            logger.name,
            "upstream returned sk-abcdef1234567890 in payload",
        )
        assert flt.filter(record) is True
        assert "sk-abcdef1234567890" not in record.getMessage()
        assert REDACTED_STRING in record.getMessage()

    def test_scrubs_bearer_token(self, logger: logging.Logger) -> None:
        flt = CredentialRedactionFilter()
        record = _make_record(logger.name, "auth: Bearer abcdefghijklmnop1234")
        flt.filter(record)
        assert "Bearer abcdefghijklmnop1234" not in record.getMessage()
        assert REDACTED_STRING in record.getMessage()

    def test_scrubs_after_percent_formatting(self, logger: logging.Logger) -> None:
        # The filter sees `record.getMessage()`, which is post-format.
        # A `%s` placeholder that resolves to a credential byte must
        # be scrubbed.
        flt = CredentialRedactionFilter()
        record = _make_record(
            logger.name,
            "upstream returned %s in payload",
            ("sk-abcdef1234567890",),
        )
        flt.filter(record)
        rendered = record.getMessage()
        assert "sk-abcdef1234567890" not in rendered
        assert REDACTED_STRING in rendered

    def test_no_credential_leaves_message_unchanged(self, logger: logging.Logger) -> None:
        flt = CredentialRedactionFilter()
        record = _make_record(logger.name, "all good")
        flt.filter(record)
        assert record.getMessage() == "all good"

    def test_extra_patterns_extend_set(self, logger: logging.Logger) -> None:
        flt = CredentialRedactionFilter(extra_patterns=(r"\btt-[A-Za-z0-9]{16,}\b",))
        record = _make_record(logger.name, "tenant=tt-abcdef1234567890abcd")
        flt.filter(record)
        assert "tt-abcdef1234567890abcd" not in record.getMessage()

    def test_always_returns_true(self, logger: logging.Logger) -> None:
        # The filter must never drop records — a malformed payload
        # is better logged (unredacted) than silenced.
        flt = CredentialRedactionFilter()
        record = _make_record(logger.name, "ordinary")
        assert flt.filter(record) is True


class TestInstallFilter:
    """`install_credential_redaction_filter` is idempotent."""

    def test_installs_filter_on_logger(self, logger: logging.Logger) -> None:
        assert not any(isinstance(f, CredentialRedactionFilter) for f in logger.filters)
        install_credential_redaction_filter(logger=logger)
        assert any(isinstance(f, CredentialRedactionFilter) for f in logger.filters)

    def test_install_is_idempotent(self, logger: logging.Logger) -> None:
        first = install_credential_redaction_filter(logger=logger)
        second = install_credential_redaction_filter(logger=logger)
        # Same instance returned; no duplicate filter installed.
        assert first is second
        assert (
            sum(1 for f in logger.filters if isinstance(f, CredentialRedactionFilter))
            == 1
        )

    def test_install_default_targets_root(self) -> None:
        root = logging.getLogger()
        before = [
            f for f in root.filters if isinstance(f, CredentialRedactionFilter)
        ]
        try:
            install_credential_redaction_filter()
            after = [
                f for f in root.filters if isinstance(f, CredentialRedactionFilter)
            ]
            assert len(after) == max(len(before), 1)
        finally:
            for f in list(root.filters):
                if isinstance(f, CredentialRedactionFilter) and f not in before:
                    root.removeFilter(f)


class TestRedactLogPayload:
    """`redact_log_payload` for `extra={...}` shape redaction."""

    def test_scrubs_credential_fields(self) -> None:
        scrubbed = redact_log_payload(
            {"request": {"headers": {"Authorization": "Bearer t"}}, "safe": "value"}
        )
        assert scrubbed == {
            "request": {"headers": {"Authorization": REDACTED_STRING}},
            "safe": "value",
        }

    def test_returns_copy(self) -> None:
        original = {"api_key": "sk-secret"}
        scrubbed = redact_log_payload(original)
        assert scrubbed is not original
        assert original == {"api_key": "sk-secret"}
