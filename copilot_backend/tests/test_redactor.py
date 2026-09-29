"""Tests for `app.security.redactor` — T33 / #29.

T33's acceptance criteria are "no credential in trace / response / log".
This module is the unit-level seam that backs all three: the redactor
walks any Python shape and substitutes credential bytes with the
`[REDACTED]` marker. The acceptance contract falls out of the
following guarantees:

* A dict containing a sensitive-field key (header or body) has the
  value scrubbed — case-insensitive matching, all common credentials
  covered.
* Nested structures (list-in-dict, dict-in-list) are scrubbed to any
  depth.
* Bytes-shaped credentials are replaced with `b"[REDACTED]"` so a
  string-format at the caller can't accidentally surface them.
* The redactor returns a **copy**; the caller's original shape is
  untouched so the Worker's plaintext-on-stack doesn't leak by
  reference.
* Free-form text (`redact_text`) catches obvious API-key prefixes
  (`sk-…`, `Bearer …`, etc.) so a log message that accidentally
  string-formats a credential is still safe.
* Cyclic shapes terminate (defensive — a self-referencing dict
  shouldn't hang the worker).
"""
from __future__ import annotations

import pytest

from app.security.redactor import (
    REDACTED_BYTES,
    REDACTED_STRING,
    SENSITIVE_FIELDS,
    is_sensitive_field,
    redact,
    redact_envelope,
    redact_headers,
    redact_text,
)

# ---------------------------------------------------------------------------
# Field-name detection
# ---------------------------------------------------------------------------


class TestFieldDetection:
    """`is_sensitive_field` recognises every documented credential field."""

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "AUTHORIZATION",
            "X-Api-Key",
            "x-api-key",
            "X-Auth-Token",
            "Cookie",
            "Set-Cookie",
            "Proxy-Authorization",
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
            "credentials_ref",
            "credentials_payload",
            "plaintext_payload",
            "payload",
            "nonce",
        ],
    )
    def test_sensitive_field_names(self, name: str) -> None:
        assert is_sensitive_field(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Content-Type",
            "X-Request-ID",
            "name",
            "description",
            "amount",
            "customer",
            "result",
            "data",
        ],
    )
    def test_safe_field_names_pass_through(self, name: str) -> None:
        assert is_sensitive_field(name) is False

    def test_sensitive_fields_set_is_frozen(self) -> None:
        # The set is exposed for documentation / introspection; it
        # MUST be a frozenset so a caller can't mutate it at runtime
        # and silently weaken redaction.
        assert isinstance(SENSITIVE_FIELDS, frozenset)


# ---------------------------------------------------------------------------
# Dict / list scrubbing
# ---------------------------------------------------------------------------


class TestRedactDict:
    """`redact(...)` recursively walks dicts and scrubs sensitive values."""

    def test_top_level_credential_header_scrubbed(self) -> None:
        scrubbed = redact({"Authorization": "Bearer sk-secret-1234"})
        assert scrubbed == {"Authorization": REDACTED_STRING}

    def test_case_insensitive_header_match(self) -> None:
        # Header names are case-insensitive in HTTP semantics; the
        # redactor must match `x-api-key` the same way it matches
        # `X-Api-Key`.
        scrubbed = redact({"x-api-key": "sk-secret"})
        assert scrubbed["x-api-key"] == REDACTED_STRING

    def test_non_sensitive_fields_pass_through(self) -> None:
        scrubbed = redact({"Content-Type": "application/json", "name": "alice"})
        assert scrubbed == {"Content-Type": "application/json", "name": "alice"}

    def test_nested_dict_is_scrubbed(self) -> None:
        scrubbed = redact(
            {
                "request": {
                    "headers": {"Authorization": "Bearer sk-secret"},
                    "body": {"api_key": "sk-secret"},
                }
            }
        )
        assert scrubbed["request"]["headers"]["Authorization"] == REDACTED_STRING
        assert scrubbed["request"]["body"]["api_key"] == REDACTED_STRING

    def test_list_in_dict_is_walked(self) -> None:
        scrubbed = redact(
            {
                "attempts": [
                    {"headers": {"Authorization": "Bearer t1"}},
                    {"headers": {"Authorization": "Bearer t2"}},
                ]
            }
        )
        assert scrubbed["attempts"][0]["headers"]["Authorization"] == REDACTED_STRING
        assert scrubbed["attempts"][1]["headers"]["Authorization"] == REDACTED_STRING

    def test_dict_in_list_is_walked(self) -> None:
        scrubbed = redact([{"password": "hunter2"}, {"safe": "value"}])
        assert scrubbed[0]["password"] == REDACTED_STRING
        assert scrubbed[1] == {"safe": "value"}

    def test_bytes_value_scrubbed_to_bytes_marker(self) -> None:
        scrubbed = redact({"payload": b"\x00\x01\x02sk-secret"})
        assert scrubbed["payload"] == REDACTED_BYTES
        assert isinstance(scrubbed["payload"], bytes)

    def test_string_value_scrubbed_to_string_marker(self) -> None:
        scrubbed = redact({"api_key": "sk-secret-1234"})
        assert scrubbed["api_key"] == REDACTED_STRING
        assert isinstance(scrubbed["api_key"], str)

    def test_original_is_not_mutated(self) -> None:
        original = {"Authorization": "Bearer sk-secret", "safe": "value"}
        redact(original)
        # The caller's dict is untouched — the Worker's plaintext-on-
        # stack stays clear of the audit-grade copy.
        assert original == {"Authorization": "Bearer sk-secret", "safe": "value"}

    def test_nested_original_also_unmutated(self) -> None:
        inner = {"Authorization": "Bearer t"}
        outer = {"request": {"headers": inner}}
        redact(outer)
        assert inner["Authorization"] == "Bearer t"

    def test_passthrough_for_pure_scalars(self) -> None:
        # Scalars outside a dict container pass through. The
        # redactor's contract is "scrub fields"; free-form strings
        # belong to `redact_text`.
        assert redact("Bearer t1") == "Bearer t1"
        assert redact(42) == 42
        assert redact(None) is None

    def test_returns_copy_top_level(self) -> None:
        original = {"safe": "value"}
        scrubbed = redact(original)
        assert scrubbed is not original

    def test_returns_copy_nested(self) -> None:
        inner = {"safe": "value"}
        original = {"request": inner}
        scrubbed = redact(original)
        assert scrubbed["request"] is not inner

    def test_cycle_terminates(self) -> None:
        # A self-referencing shape must not hang the worker. The
        # cycle detection substitutes `[REDACTED]` at the back-edge.
        outer: dict[str, object] = {"name": "self"}
        outer["self"] = outer
        scrubbed = redact(outer)
        assert scrubbed["name"] == "self"
        assert scrubbed["self"] == REDACTED_STRING  # back-edge break


# ---------------------------------------------------------------------------
# Free-form text
# ---------------------------------------------------------------------------


class TestRedactText:
    """`redact_text(...)` catches credential-shaped tokens in free-form text."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("logged sk-abcdef1234567890 from upstream", "logged [REDACTED] from upstream"),
            (
                "header: Bearer abcdefghijklmnopqrstuvwxyz",
                "header: [REDACTED]",
            ),
            (
                "X-Api-Key: abcdefghij12345",
                "[REDACTED]",
            ),
        ],
    )
    def test_known_prefixes_redacted(self, message: str, expected: str) -> None:
        assert redact_text(message) == expected

    def test_short_token_passes_through(self) -> None:
        # The pattern is conservative — a 4-character token won't
        # match. Over-eager replacement would mangle UUIDs / hashes.
        assert redact_text("id=1234") == "id=1234"

    def test_extra_patterns_extend_default_set(self) -> None:
        # Tenant-specific token format added on top of the defaults.
        text = "tenant=tt-abcdef1234567890abcd"
        scrubbed = redact_text(text, extra_patterns=(r"\btt-[A-Za-z0-9]{16,}\b",))
        assert scrubbed == "tenant=[REDACTED]"

    def test_no_credential_returns_unchanged(self) -> None:
        assert redact_text("all good") == "all good"


# ---------------------------------------------------------------------------
# Envelope + header convenience
# ---------------------------------------------------------------------------


class TestEnvelopeScrub:
    """`redact_envelope` is the Worker call-site shape."""

    def test_scrubs_headers_and_body(self) -> None:
        envelope = {
            "method": "POST",
            "url": "https://upstream.test/echo",
            "headers": {"Authorization": "Bearer t", "Content-Type": "application/json"},
            "body": {"api_key": "sk-secret"},
        }
        scrubbed = redact_envelope(envelope)
        assert scrubbed["headers"]["Authorization"] == REDACTED_STRING
        assert scrubbed["headers"]["Content-Type"] == "application/json"
        assert scrubbed["body"]["api_key"] == REDACTED_STRING

    def test_scrubs_query_string_in_url(self) -> None:
        # `?api_key=…` lives inside the URL string, not as a field
        # key. `redact_envelope` reaches it via `redact_text` so a
        # secret tucked into the URL doesn't survive.
        envelope = {
            "url": "https://upstream.test/echo?api_key=sk-secret-1234567890",
            "headers": {},
            "body": {},
        }
        scrubbed = redact_envelope(envelope)
        assert "sk-secret-1234567890" not in scrubbed["url"]
        assert REDACTED_STRING in scrubbed["url"]

    def test_scrubs_authorization_header_in_url(self) -> None:
        envelope = {
            "url": "https://upstream.test/echo",
            "headers": {"Authorization": "Bearer pk-abcdef1234567890"},
            "body": None,
        }
        scrubbed = redact_envelope(envelope)
        assert scrubbed["headers"]["Authorization"] == REDACTED_STRING

    def test_passthrough_envelope(self) -> None:
        envelope = {
            "method": "GET",
            "url": "https://upstream.test/x",
            "headers": {"Accept": "application/json"},
            "body": None,
        }
        scrubbed = redact_envelope(envelope)
        assert scrubbed["method"] == "GET"
        assert scrubbed["headers"]["Accept"] == "application/json"


class TestRedactHeaders:
    """`redact_headers` is the legacy header-only helper."""

    def test_authorization_header_redacted(self) -> None:
        scrubbed = redact_headers({"Authorization": "Bearer t", "Content-Type": "application/json"})
        assert scrubbed == {
            "Authorization": REDACTED_STRING,
            "Content-Type": "application/json",
        }

    def test_does_not_mutate_input(self) -> None:
        headers = {"Authorization": "Bearer t"}
        redact_headers(headers)
        assert headers == {"Authorization": "Bearer t"}
