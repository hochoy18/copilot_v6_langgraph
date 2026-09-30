"""Tests for the audit-retention settings fields (T42 / #37, ADR-0028).

Verifies:

* The defaults match ADR-0028 (1 year hot + 3 year cold = 4 year
  total; sweep cadence daily).
* The field-level bounds catch obviously-wrong operator values
  at boot, not at first sweep tick.
* Environment overrides apply (so a deployment can tighten the
  hot window without a code change).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import Settings


class TestAuditRetentionSettingsDefaults:
    """Defaults match ADR-0028's documented thresholds."""

    def test_default_hot_retention_is_one_year(self) -> None:
        s = Settings()
        assert s.audit_hot_retention_seconds == 31_536_000  # 365 days

    def test_default_cold_total_is_four_years(self) -> None:
        s = Settings()
        assert s.audit_cold_total_retention_seconds == 126_144_000  # 4 years

    def test_default_sweep_interval_is_daily(self) -> None:
        s = Settings()
        assert s.audit_cold_sweep_interval_seconds == 86_400.0

    def test_default_retention_enabled(self) -> None:
        s = Settings()
        assert s.audit_retention_enabled is True

    def test_default_cold_storage_dir(self) -> None:
        s = Settings()
        assert s.audit_cold_storage_dir == "./data/audit_cold"


class TestAuditRetentionSettingsBounds:
    """Misconfigured thresholds surface at boot, not at first sweep tick."""

    def test_hot_retention_must_be_at_least_one_day(self) -> None:
        # The floor (`ge=86_400`) catches operators who
        # misconfigure the hot window to "never" or "30 seconds".
        with pytest.raises(ValidationError):
            Settings(audit_hot_retention_seconds=60)

    def test_hot_retention_ceiling_is_five_years(self) -> None:
        with pytest.raises(ValidationError):
            Settings(audit_hot_retention_seconds=157_680_001)  # > 5 years

    def test_cold_total_must_be_at_least_one_day(self) -> None:
        with pytest.raises(ValidationError):
            Settings(audit_cold_total_retention_seconds=60)

    def test_cold_total_ceiling_is_twenty_years(self) -> None:
        with pytest.raises(ValidationError):
            Settings(audit_cold_total_retention_seconds=630_720_001)  # > 20 years

    def test_sweep_interval_must_be_at_least_60_seconds(self) -> None:
        with pytest.raises(ValidationError):
            Settings(audit_cold_sweep_interval_seconds=30.0)

    def test_cold_storage_dir_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            Settings(audit_cold_storage_dir="")


class TestAuditRetentionSettingsEnvOverrides:
    """Env-var overrides apply (`COPILOT_AUDIT_*` prefix)."""

    def test_env_override_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pick values inside the documented bounds — the test
        # exercises the env-var path, not the bounds checks
        # (which `TestAuditRetentionSettingsBounds` covers).
        monkeypatch.setenv("COPILOT_AUDIT_HOT_RETENTION_SECONDS", "604800")  # 7 days
        monkeypatch.setenv("COPILOT_AUDIT_RETENTION_ENABLED", "false")
        s = Settings()
        assert s.audit_hot_retention_seconds == 604_800
        assert s.audit_retention_enabled is False