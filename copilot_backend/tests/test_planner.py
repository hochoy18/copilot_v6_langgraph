"""Unit tests for the Planner LLM layer — T18 / #16.

Three seams are exercised here, bottom-up:

* `render_tool_catalog` — what the model sees as its choice set
  (empty-registry placeholder, truncation warning).
* `parse_planner_output` — the strict-JSON contract: fenced / prose-
  wrapped answers parse, unknown Tool names are dropped with a
  warning (a hallucinated Tool must never bind), malformed answers
  raise `LLMGenerationError`.
* `ToolPlanner.plan` — end-to-end with a fake ChatModel: the
  Langfuse `planner` Prompt is fetched and rendered with catalog +
  instruction, transport failures become `LLMGenerationError`, and
  the ADR-0016 config refusal propagates untouched.

The Plan-persistence side (snapshot freezing, Turn linking) lives in
`test_planner_service.py`; the HTTP seam in `test_turn_routes.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.db.schemas import Tool
from app.llm.errors import LLMConfigurationError, LLMGenerationError
from app.llm.prompts import (
    PLANNER_PROMPT,
    PromptProvider,
    render_template,
)
from app.planner.planner import (
    MAX_TOOLS_IN_CATALOG,
    PlanIntent,
    ToolPlanner,
    parse_planner_output,
    render_tool_catalog,
)
from app.settings import Settings

_NOW = datetime(2026, 9, 1, tzinfo=UTC)

# A minimal template mirroring the Langfuse `planner` contract: both
# placeholders visible so a test can assert the render actually ran.
_PLANNER_TEMPLATE = "CATALOG>>{{tools}}<<INSTRUCTION>>{{input}}<<JSON ONLY"


# ---------------------------------------------------------------------------
# Fakes + factories
# ---------------------------------------------------------------------------


class _FakeChatModel(BaseChatModel):
    """Chat model stand-in: returns canned text and records the prompts it saw."""

    response_text: str = ""
    should_fail: bool = False
    seen_prompts: list[str] = []
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-test-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.call_count += 1
        self.seen_prompts.append(str(messages[-1].content))
        if self.should_fail:
            raise RuntimeError("upstream model exploded")
        generation = ChatGeneration(message=AIMessage(content=self.response_text))
        return ChatResult(generations=[generation])


def _provider_with_template(template: str) -> PromptProvider:
    """PromptProvider whose Langfuse fetch always returns `template`."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": PLANNER_PROMPT, "version": 1, "prompt": template},
        )

    settings = Settings(
        langfuse_host="https://langfuse.example.com",
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
    )
    return PromptProvider(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": "sk-test",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _planner(
    response_text: str = '{"nodes": []}',
    *,
    should_fail: bool = False,
    settings: Settings | None = None,
    template: str = _PLANNER_TEMPLATE,
) -> tuple[ToolPlanner, _FakeChatModel]:
    fake = _FakeChatModel(response_text=response_text, should_fail=should_fail)
    planner = ToolPlanner(
        settings=settings or _settings(),
        prompt_provider=_provider_with_template(template),
        chat_model_factory=lambda: fake,
    )
    return planner, fake


def _tool(name: str = "echo", **overrides: object) -> Tool:
    base: dict[str, Any] = {
        "name": name,
        "description": "把传入的文本原样返回",
        "risk_level": "read",
        "status": "active",
        "parameters_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
        "http_method": "POST",
        "http_url_template": "https://api.example.test/echo",
        "http_headers": {},
        "http_body_template": None,
        "source": "manual",
        "source_ref": None,
        "credentials_ref": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return Tool(**base)


# ---------------------------------------------------------------------------
# Prompt name contract (SPEC §Langfuse Prompt 列表)
# ---------------------------------------------------------------------------


def test_planner_prompt_name_matches_spec() -> None:
    assert PLANNER_PROMPT == "planner"


async def test_planner_bootstrap_ladder_floor() -> None:
    """With Langfuse unconfigured and no cache, the bootstrap copy answers.

    AC "用 Langfuse planner prompt" — the ladder's floor keeps the
    Planner working offline; the bootstrap must carry the same
    `{{tools}}` / `{{input}}` placeholders and the `nodes` contract.
    """
    provider = PromptProvider(
        settings=Settings(),  # no langfuse keys → fetch off
        http_client=httpx.AsyncClient(),
    )
    template = await provider.get_prompt(PLANNER_PROMPT)
    assert template.source == "bootstrap"
    assert "{{tools}}" in template.text
    assert "{{input}}" in template.text
    assert '"nodes"' in template.text


# ---------------------------------------------------------------------------
# render_tool_catalog
# ---------------------------------------------------------------------------


class TestRenderToolCatalog:
    def test_empty_registry_renders_placeholder(self) -> None:
        assert "no active tools" in render_tool_catalog([])

    def test_line_carries_name_description_and_parameters(self) -> None:
        catalog = render_tool_catalog([_tool()])
        assert "echo" in catalog
        assert "把传入的文本原样返回" in catalog
        # parameter digest uses the shared T16 summariser
        assert "text" in catalog
        assert "required" in catalog

    def test_long_description_is_clipped(self) -> None:
        tool = _tool(description="很长的描述" * 200)
        catalog = render_tool_catalog([tool])
        assert len(catalog) < len(tool.description)

    def test_truncation_is_announced_not_silent(self) -> None:
        tools = [_tool(name=f"t{i}") for i in range(MAX_TOOLS_IN_CATALOG + 5)]
        catalog = render_tool_catalog(tools)
        assert "目录已截断" in catalog
        assert f"{len(tools)}" in catalog


# ---------------------------------------------------------------------------
# parse_planner_output
# ---------------------------------------------------------------------------


class TestParsePlannerOutput:
    def test_plain_json_single_node_binds_tool(self) -> None:
        tools = [_tool()]
        intent = parse_planner_output(
            '{"nodes": [{"tool": "echo", "parameters": {"text": "hello"}, "notes": "回显 hello"}]}',
            tools,
        )
        assert isinstance(intent, PlanIntent)
        assert len(intent.nodes) == 1
        node = intent.nodes[0]
        assert node.tool is tools[0]
        assert node.parameters == {"text": "hello"}
        assert node.notes == "回显 hello"
        assert intent.warnings == []

    def test_fenced_json_is_accepted(self) -> None:
        intent = parse_planner_output(
            '好的:\n```json\n{"nodes": [{"tool": "echo", "parameters": {"text": "hi"}}]}\n```',
            [_tool()],
        )
        assert len(intent.nodes) == 1
        assert intent.nodes[0].notes == ""

    def test_prose_wrapped_json_is_accepted(self) -> None:
        intent = parse_planner_output(
            'The plan is {"nodes": [{"tool": "echo"}]} per your request.',
            [_tool()],
        )
        assert len(intent.nodes) == 1

    def test_empty_nodes_is_a_valid_no_plan(self) -> None:
        intent = parse_planner_output('{"nodes": []}', [_tool()])
        assert intent.nodes == []
        assert intent.warnings == []

    def test_missing_nodes_key_treated_as_no_plan(self) -> None:
        intent = parse_planner_output("{}", [_tool()])
        assert intent.nodes == []

    def test_non_json_raises_generation_error(self) -> None:
        with pytest.raises(LLMGenerationError):
            parse_planner_output("我不知道该用什么工具", [_tool()])

    def test_nodes_not_a_list_raises(self) -> None:
        with pytest.raises(LLMGenerationError):
            parse_planner_output('{"nodes": "echo"}', [_tool()])

    def test_node_without_string_tool_raises(self) -> None:
        with pytest.raises(LLMGenerationError):
            parse_planner_output('{"nodes": [{"parameters": {}}]}', [_tool()])

    def test_unknown_tool_dropped_with_warning(self) -> None:
        intent = parse_planner_output(
            '{"nodes": [{"tool": "send_invoice", "parameters": {}}]}',
            [_tool()],
        )
        assert intent.nodes == []
        assert any("send_invoice" in w for w in intent.warnings)

    def test_unknown_dropped_but_known_kept(self) -> None:
        intent = parse_planner_output(
            '{"nodes": [{"tool": "nope"}, {"tool": "echo"}]}',
            [_tool()],
        )
        assert [n.tool.name for n in intent.nodes] == ["echo"]
        assert any("nope" in w for w in intent.warnings)

    def test_non_dict_parameters_degrade_to_empty(self) -> None:
        intent = parse_planner_output(
            '{"nodes": [{"tool": "echo", "parameters": "hello"}]}',
            [_tool()],
        )
        assert intent.nodes[0].parameters == {}
        assert any("参数" in w for w in intent.warnings)

    def test_non_string_notes_degrade_to_empty(self) -> None:
        intent = parse_planner_output(
            '{"nodes": [{"tool": "echo", "notes": 42}]}',
            [_tool()],
        )
        assert intent.nodes[0].notes == ""

    def test_multiple_nodes_preserved_in_order(self) -> None:
        """T18 prompts single-node output, but the parser must not cap —
        T25 upgrades the Langfuse copy without a code change."""
        intent = parse_planner_output(
            '{"nodes": [{"tool": "echo"}, {"tool": "echo", "parameters": {"text": "b"}}]}',
            [_tool()],
        )
        assert len(intent.nodes) == 2


# ---------------------------------------------------------------------------
# ToolPlanner.plan
# ---------------------------------------------------------------------------


class TestToolPlanner:
    async def test_plan_renders_prompt_with_catalog_and_instruction(self) -> None:
        planner, fake = _planner('{"nodes": [{"tool": "echo", "parameters": {"text": "hello"}}]}')
        intent = await planner.plan("echo hello", [_tool()])
        assert len(intent.nodes) == 1
        assert fake.call_count == 1
        prompt = fake.seen_prompts[0]
        assert prompt.startswith("CATALOG>>")  # template was fetched + rendered
        assert "echo" in prompt  # tool catalog landed in {{tools}}
        assert "echo hello" in prompt  # instruction landed in {{input}}
        assert "{{tools}}" not in prompt and "{{input}}" not in prompt

    async def test_plan_transport_error_becomes_generation_error(self) -> None:
        planner, _ = _planner(should_fail=True)
        with pytest.raises(LLMGenerationError):
            await planner.plan("echo hello", [_tool()])

    async def test_plan_unparseable_output_becomes_generation_error(self) -> None:
        planner, _ = _planner(response_text="抱歉, 我无法规划")
        with pytest.raises(LLMGenerationError):
            await planner.plan("echo hello", [_tool()])

    async def test_configuration_refusal_propagates(self) -> None:
        """ADR-0016: a provider refused by config must not be swallowed
        into a generic generation failure."""

        def _factory() -> BaseChatModel:
            raise LLMConfigurationError(details={"reason": "test"})

        planner = ToolPlanner(
            settings=_settings(),
            prompt_provider=_provider_with_template(_PLANNER_TEMPLATE),
            chat_model_factory=_factory,
        )
        with pytest.raises(LLMConfigurationError):
            await planner.plan("echo hello", [_tool()])

    def test_ready_reflects_llm_configuration(self) -> None:
        configured, _ = _planner()
        unconfigured, _ = _planner(
            settings=Settings(llm_base_url="", llm_api_key=""),
        )
        assert configured.ready is True
        assert unconfigured.ready is False


# ---------------------------------------------------------------------------
# render_template shared contract (used by planner + description generator)
# ---------------------------------------------------------------------------


def test_unknown_placeholders_pass_through() -> None:
    rendered = render_template("a={{tools}} b={{future_var}}", {"tools": "T"})
    assert "b={{future_var}}" in rendered
