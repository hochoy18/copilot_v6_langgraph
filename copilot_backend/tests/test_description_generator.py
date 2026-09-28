"""Unit tests for `ToolDescriptionGenerator` — T16 / #14 (ADR-0018).

The generator is the capability the ticket names: take an OpenAPI-derived
`ToolDraft` whose description is developer-facing text and produce an
LLM-friendly rewrite that includes typical-use-case hints. Acceptance
criteria mapped here:

* 描述从原文变 LLM-friendly 版本 — `generate` returns the rewritten text
  and `enrich_drafts` swaps it onto the draft, preserving the original
  on `original_description` for admin review;
* 含典型用例提示 — the composed text carries a 典型用例 section built
  from the model's structured `typical_use_cases` output;
* 管理员可 review — every failure path degrades to "raw description +
  warning" instead of blocking, so the preview stays reviewable/editable.

Tests drive the generator through a fake `BaseChatModel` (ADR-0014's
abstraction) and a `PromptProvider` backed by an injected Langfuse-style
template, so rendering and parsing are asserted without any network.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.llm.errors import LLMGenerationError
from app.llm.prompts import PromptProvider
from app.settings import Settings
from app.tools.description_generator import (
    MAX_DESCRIPTIONS_PER_IMPORT,
    GeneratedDescription,
    ToolDescriptionGenerator,
)
from app.tools.openapi_parser import ToolDraft


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
    """PromptProvider whose Langfuse fetch always returns `template`.

    The keys are configured and the transport answers 200, so
    `get_prompt` resolves to the injected text — generator tests own
    the exact placeholder set the render step must honour.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "tool-description-generator",
                "version": 1,
                "prompt": template,
            },
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


_TEMPLATE = (
    "name={{name}} method={{method}} path={{path}} "
    "desc={{description}} params={{parameters}}"
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": "sk-test",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _generator(
    response_text: str = '{"description": "查宠物信息", "typical_use_cases": ["今天有哪些新宠物"]}',
    *,
    should_fail: bool = False,
    settings: Settings | None = None,
    template: str = _TEMPLATE,
) -> tuple[ToolDescriptionGenerator, _FakeChatModel]:
    model = _FakeChatModel(response_text=response_text, should_fail=should_fail)
    generator = ToolDescriptionGenerator(
        settings=settings or _settings(),
        prompt_provider=_provider_with_template(template),
        chat_model_factory=lambda: model,
    )
    return generator, model


def _draft(**overrides: object) -> ToolDraft:
    base: dict[str, Any] = {
        "operation_ref": "GET /pets/{id}",
        "name": "getPetById",
        "description": "Returns a user by ID. See Swagger section 4.",
        "risk_level": "read",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Pet identifier", "__location__": "path"},
            },
            "required": ["id"],
        },
        "http_method": "GET",
        "http_url_template": "https://api.example.com/pets/{id}",
        "http_headers": {},
        "http_body_template": None,
        "status": "draft",
        "source": "openapi",
        "source_ref": "get /pets/{id}",
        "credentials_ref": None,
    }
    base.update(overrides)
    return ToolDraft(**base)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


async def test_generate_renders_prompt_with_draft_fields() -> None:
    """The rendered template carries every operation fact the LLM needs."""
    generator, model = _generator()

    await generator.generate(_draft())

    prompt = model.seen_prompts[-1]
    assert "name=getPetById" in prompt
    assert "method=GET" in prompt
    assert "path=/pets/{id}" in prompt
    assert "desc=Returns a user by ID" in prompt
    # Parameter summary keeps name + type + location so the LLM can
    # describe what callers must supply.
    assert "id" in prompt and "integer" in prompt and "path" in prompt


async def test_generate_composes_description_with_typical_use_cases() -> None:
    """典型用例 hints ride along in the stored text (AC #2)."""
    generator, _model = _generator()

    result = await generator.generate(_draft())

    assert isinstance(result, GeneratedDescription)
    assert result.description == "查宠物信息\n\n典型用例:\n- 今天有哪些新宠物"
    assert result.typical_use_cases == ["今天有哪些新宠物"]


async def test_generate_accepts_fenced_json_output() -> None:
    """Models wrap JSON in code fences; the parser strips them."""
    generator, _model = _generator(
        response_text='```json\n{"description": "ok", "typical_use_cases": ["a"]}\n```'
    )

    result = await generator.generate(_draft())

    assert result.description.startswith("ok")


async def test_generate_without_use_cases_returns_bare_description() -> None:
    generator, _model = _generator(
        response_text='{"description": "只有描述", "typical_use_cases": []}'
    )

    result = await generator.generate(_draft())

    assert result.description == "只有描述"
    assert result.typical_use_cases == []


async def test_generate_raises_on_unparseable_model_output() -> None:
    generator, _model = _generator(response_text="Sure! The tool returns a pet.")

    with pytest.raises(LLMGenerationError):
        await generator.generate(_draft())


async def test_generate_wraps_model_failures_in_generation_error() -> None:
    generator, _model = _generator(should_fail=True)

    with pytest.raises(LLMGenerationError):
        await generator.generate(_draft())


# ---------------------------------------------------------------------------
# enrich_drafts() — orchestration over a parse result
# ---------------------------------------------------------------------------


async def test_enrich_skips_generation_and_warns_once_when_llm_unconfigured() -> None:
    """No LLM configured → raw text survives, one import-level warning."""
    generator, model = _generator(settings=_settings(llm_base_url="", llm_api_key=""))
    drafts = [_draft(), _draft(operation_ref="POST /pets", name="addPet")]

    warnings = await generator.enrich_drafts(drafts)

    assert model.call_count == 0
    assert len(warnings) == 1
    assert "not configured" in warnings[0]
    for d in drafts:
        assert d.description_generated is False
        assert d.original_description is None


async def test_enrich_swaps_description_and_keeps_original_for_review() -> None:
    """AC #1 + #3: drafts carry the rewrite, originals stay for admin comparison."""
    generator, model = _generator()
    drafts = [_draft(), _draft()]

    warnings = await generator.enrich_drafts(drafts)

    assert warnings == []
    assert model.call_count == 2
    for d in drafts:
        assert d.description.startswith("查宠物信息")
        assert "典型用例" in d.description
        assert d.original_description == "Returns a user by ID. See Swagger section 4."
        assert d.description_generated is True
        assert d.warnings == []


async def test_enrich_degrades_per_draft_when_generation_fails() -> None:
    """A failing model keeps each draft usable: raw text + per-draft warning."""
    generator, _model = _generator(should_fail=True)
    drafts = [_draft(), _draft()]

    warnings = await generator.enrich_drafts(drafts)

    assert warnings == []  # failures are per-draft, not import-level
    for d in drafts:
        assert d.description == "Returns a user by ID. See Swagger section 4."
        assert d.description_generated is False
        assert len(d.warnings) == 1
        assert "LLM" in d.warnings[0]


async def test_enrich_caps_generation_and_warns_about_skipped_drafts() -> None:
    """Beyond the cap the extra drafts stay raw — and say so, loudly."""
    generator, model = _generator()
    extra = MAX_DESCRIPTIONS_PER_IMPORT + 2
    drafts = [_draft(name=f"tool{i}", operation_ref=f"GET /t{i}") for i in range(extra)]

    warnings = await generator.enrich_drafts(drafts)

    assert model.call_count == MAX_DESCRIPTIONS_PER_IMPORT
    generated = [d for d in drafts if d.description_generated]
    skipped = [d for d in drafts if not d.description_generated]
    assert len(generated) == MAX_DESCRIPTIONS_PER_IMPORT
    assert len(skipped) == 2
    assert len(warnings) == 1
    assert "32" in warnings[0]


async def test_enrich_clamps_generated_description_to_schema_max_length() -> None:
    """ToolBase.description caps at 4096; generated text must fit."""
    big_case = "用" * 3000
    payload = json.dumps(
        {
            "description": "描" * 3000,
            "typical_use_cases": [big_case, big_case, big_case],
        },
        ensure_ascii=False,
    )
    generator, _model = _generator(response_text=payload)
    drafts = [_draft()]

    await generator.enrich_drafts(drafts)

    assert len(drafts[0].description) <= 4096
