"""Tests for the conversation-lifecycle settings fields (T39 / #45, ADR-0011).

Verifies the defaults match ADR-0011 (15-minute idle, 30-day
archive) and that the field-level bounds catch obviously-wrong
operator values.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import Settings


class TestConversationLifecycleSettingsDefaults:
    """Defaults match ADR-0011's documented thresholds."""

    def test_default_idle_threshold_is_15_minutes(self) -> None:
        s = Settings()
        assert s.conversation_idle_after_seconds == 900

    def test_default_archive_threshold_is_30_days(self) -> None:
        s = Settings()
        assert s.conversation_archive_after_seconds == 2_592_000

    def test_default_scan_interval_is_60_seconds(self) -> None:
        s = Settings()
        assert s.conversation_lifecycle_scan_interval_seconds == 60.0

    def test_default_lifecycle_enabled(self) -> None:
        s = Settings()
        assert s.conversation_lifecycle_enabled is True


class TestConversationLifecycleSettingsBounds:
    """Misconfigured thresholds surface at boot, not at first sweep tick."""

    def test_idle_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Settings(conversation_idle_after_seconds=0)

    def test_archive_threshold_must_be_at_least_60_seconds(self) -> None:
        with pytest.raises(ValidationError):
            Settings(conversation_archive_after_seconds=30)

    def test_scan_interval_must_be_at_least_one_second(self) -> None:
        with pytest.raises(ValidationError):
            Settings(conversation_lifecycle_scan_interval_seconds=0.5)

    def test_env_overrides_apply(self) -> None:
        """Operators can dial thresholds via env vars without code changes."""
        s = Settings(
            conversation_idle_after_seconds=300,
            conversation_archive_after_seconds=86_400,
            conversation_lifecycle_scan_interval_seconds=10.0,
        )
        assert s.conversation_idle_after_seconds == 300
        assert s.conversation_archive_after_seconds == 86_400
        assert s.conversation_lifecycle_scan_interval_seconds == 10.0