"""Tests for `ToolService` schema enforcement — T34 / #30.

ADR-0020 § "校验依据" says: "Tool 注册时存的标准 JSON Schema…对没
声明 schema 的 Tool,后端拒绝注册 (强制 schema 完整性)."

These tests pin the service-level enforcement so future refactors of
`ToolRepository` / `ToolService.create` don't quietly drop the check.
The repository / wire shape accept whatever the Pydantic model lets
through; the service is where the "no schema → reject" rule lives
because it owns the "rules that aren't pure Mongo" (per the service
docstring).
"""
from __future__ import annotations

from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.db.init_db import init_database
from app.db.schemas import (
    ToolCreate,
    ToolUpdate,
)
from app.repositories.tools import ToolRepository
from app.tools.errors import ToolSchemaInvalidError
from app.tools.service import ToolService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def service() -> ToolService:
    """A fresh `ToolService` against an isolated in-memory Mongo."""
    db = AsyncMongoMockClient()["copilot_tool_service_test"]
    await init_database(db)
    repo = ToolRepository(db)
    return ToolService(tool_repository=repo)


def _valid_create(**overrides: Any) -> ToolCreate:
    """Build a valid `ToolCreate`; per-test overrides land on top."""
    base: dict[str, Any] = {
        "name": "list_customers",
        "description": "List customers by region.",
        "status": "draft",
        "risk_level": "read",
        "parameters_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/customers?region={region}",
        "http_headers": {"Accept": "application/json"},
        "http_body_template": None,
        "source": "manual",
        "source_ref": None,
        "credentials_ref": None,
    }
    base.update(overrides)
    return ToolCreate(**base)


# ---------------------------------------------------------------------------
# Registration-time rejection (ADR-0020)
# ---------------------------------------------------------------------------


class TestCreateSchemaEnforcement:
    """`ToolService.create` rejects Tools without a usable schema."""

    async def test_create_with_empty_schema_raises_tool_schema_invalid(
        self, service: ToolService
    ) -> None:
        """An admin trying to land `parameters_schema={}` is rejected.

        Per ADR-0020 "强制 schema 完整性" the service must reject
        before the row reaches Mongo — once a Tool is persisted with
        `{}`, the Worker's snapshot-binding makes the missing-schema
        condition sticky for the lifetime of any Plan that captures
        it.
        """
        with pytest.raises(ToolSchemaInvalidError) as exc_info:
            await service.create(data=_valid_create(parameters_schema={}))
        assert exc_info.value.code == "tool_schema_invalid"
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "empty_schema"

    async def test_create_with_invalid_json_schema_raises_tool_schema_invalid(
        self, service: ToolService
    ) -> None:
        """A schema the validator rejects is rejected at registration.

        `Draft202012Validator.check_schema` raises on a schema whose
        own keywords contradict themselves — e.g. `type` set to a
        non-string value. Surfacing this at registration keeps the
        Worker's `_validate_parameters` path narrow: by the time a
        snapshot reaches execution its `parameters_schema` is
        guaranteed well-formed.
        """
        bad_schema: dict[str, Any] = {"type": 1234}  # `type` must be string or array of strings
        with pytest.raises(ToolSchemaInvalidError) as exc_info:
            await service.create(data=_valid_create(parameters_schema=bad_schema))
        assert exc_info.value.code == "tool_schema_invalid"
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "invalid_schema"

    async def test_create_with_minimal_object_schema_succeeds(
        self, service: ToolService
    ) -> None:
        """A no-parameter Tool may declare `{"type": "object"}`.

        The "no params" shape is non-empty and valid JSON Schema —
        the rejection targets `{}` specifically, not legitimate
        object schemas that happen to take no parameters.
        """
        tool = await service.create(
            data=_valid_create(parameters_schema={"type": "object"}),
        )
        assert tool.parameters_schema == {"type": "object"}
        assert tool.status == "draft"


# ---------------------------------------------------------------------------
# Update-time rejection
# ---------------------------------------------------------------------------


class TestUpdateSchemaEnforcement:
    """`ToolService.update` rejects clearing / invalidating a schema."""

    async def test_update_clearing_schema_to_empty_raises_tool_schema_invalid(
        self, service: ToolService
    ) -> None:
        """A PATCH that empties `parameters_schema` is rejected.

        The `update` path mirrors the create rule: any PATCH that
        carries an explicit `parameters_schema` must leave it in a
        usable state. A `None` patch value (field not touched) is the
        "no change" path and stays valid.
        """
        created = await service.create(data=_valid_create())
        with pytest.raises(ToolSchemaInvalidError) as exc_info:
            await service.update(
                tool_id=created.id,
                patch=ToolUpdate(parameters_schema={}),
            )
        assert exc_info.value.code == "tool_schema_invalid"
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "empty_schema"

    async def test_update_with_invalid_json_schema_raises_tool_schema_invalid(
        self, service: ToolService
    ) -> None:
        """Invalid schema on PATCH is rejected (same as create)."""
        created = await service.create(data=_valid_create())
        with pytest.raises(ToolSchemaInvalidError):
            await service.update(
                tool_id=created.id,
                patch=ToolUpdate(parameters_schema={"type": "not-a-real-type"}),
            )

    async def test_update_without_schema_field_keeps_existing_schema(
        self, service: ToolService
    ) -> None:
        """A PATCH that doesn't touch `parameters_schema` succeeds."""
        created = await service.create(data=_valid_create())
        updated = await service.update(
            tool_id=created.id,
            patch=ToolUpdate(description="Refined description."),
        )
        # The original schema is preserved when the patch omits the field.
        assert updated.parameters_schema == {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        }


__all__ = [
    "TestCreateSchemaEnforcement",
    "TestUpdateSchemaEnforcement",
]