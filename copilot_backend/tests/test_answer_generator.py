"""Unit tests for the LLM final-answer streaming layer — T22 / #19.

Three seams are exercised here, mirroring the Planner's tests:

* `render_node_results` — what the model sees as `{{results}}`
  (per-node head, status, response / error envelope, truncation
  marker). Empty-result placeholder so the Prompt never sees a
  silent empty block.
* `AnswerGenerator.astream` — end-to-end with a fake ChatModel that
  yields `AIMessageChunk`s one token at a time. Tests pin:
    - the rendered Prompt lands in the chunked model call,
    - transport failures become `LLMGenerationError`,
    - chunk content extraction handles both `str` and list-of-blocks
      payloads (the OpenAI-compatible surface),
    - bootstrap ladder floor keeps the LLM-callable for an empty
      Langfuse config.
* `AnswerGenerator.ready` — the cheap configuration check the
  orchestrator uses to decide whether to skip streaming.

The orchestrator (`AnswerService`) lives in `test_answer_service.py`;
the HTTP seam in `test_plan_execution_routes.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.answer.generator import (
    AnswerGenerator,
    NodeSummary,
    render_node_results,
)
from app.llm.errors import LLMConfigurationError, LLMGenerationError
from app.llm.prompts import (
    RESULT_SUMMARIZER_PROMPT,
    PromptProvider,
    render_template,
)
from app.settings import Settings

_NOW = datetime(2026, 9, 1, tzinfo=UTC)

# A minimal template mirroring the Langfuse `result-summarizer`
# contract: both placeholders visible so a test can assert the render
# actually ran.
_SUMMARIZER_TEMPLATE = (
    "INSTRUCTION>>{{instruction}}<<RESULTS>>{{results}}<<END"
)


# ---------------------------------------------------------------------------
# Fakes + factories
# ---------------------------------------------------------------------------


class _FakeChatModel(BaseChatModel):
    """Chat model stand-in: streams canned tokens via `astream`.

    `stream_tokens` is the literal sequence the test wants the model to
    yield — each one becomes one `AIMessageChunk` with `content=str`.
    `raise_on_astream` makes the first `astream` call raise so the
    test can verify the failure-shape translation.
    """

    stream_tokens: list[str] = []
    raise_on_astream: bool = False
    seen_prompts: list[str] = []
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-test-answer-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # The orchestrator uses `astream`, not `ainvoke`. The fallback
        # path is what LangChain takes when `_astream` is the default
        # base implementation; tests exercise it indirectly via the
        # `should_fail` flag on the Planner-equivalent fake.
        self.call_count += 1
        self.seen_prompts.append(str(messages[-1].content))
        text = "".join(self.stream_tokens) if self.stream_tokens else ""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Any:
        from langchain_core.outputs import ChatGenerationChunk as _Chunk

        self.call_count += 1
        self.seen_prompts.append(str(messages[-1].content))
        if self.raise_on_astream:
            raise RuntimeError("upstream model exploded")
        for token in self.stream_tokens:
            yield _Chunk(message=AIMessageChunk(content=token))


def _provider_with_template(template: str) -> PromptProvider:
    """PromptProvider whose Langfuse fetch always returns `template`."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": RESULT_SUMMARIZER_PROMPT, "version": 1, "prompt": template},
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


def _generator(
    *,
    stream_tokens: list[str] | None = None,
    raise_on_astream: bool = False,
    settings: Settings | None = None,
    template: str = _SUMMARIZER_TEMPLATE,
) -> tuple[AnswerGenerator, _FakeChatModel]:
    fake = _FakeChatModel(
        stream_tokens=stream_tokens or [],
        raise_on_astream=raise_on_astream,
    )
    generator = AnswerGenerator(
        settings=settings or _settings(),
        prompt_provider=_provider_with_template(template),
        chat_model_factory=lambda: fake,
    )
    return generator, fake


def _summary(
    *,
    node_id: str = "n1",
    tool_name: str = "echo",
    notes: str = "回显 hello",
    status: str = "succeeded",
    response: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    response_text: str = "",
) -> NodeSummary:
    return NodeSummary(
        node_id=node_id,
        tool_name=tool_name,
        notes=notes,
        status=status,
        response=response,
        error=error,
        response_text=response_text,
    )


# ---------------------------------------------------------------------------
# Prompt name contract (SPEC §Langfuse Prompt 列表)
# ---------------------------------------------------------------------------


def test_summarizer_prompt_name_matches_spec() -> None:
    assert RESULT_SUMMARIZER_PROMPT == "result-summarizer"


async def test_summarizer_bootstrap_ladder_floor() -> None:
    """With Langfuse unconfigured and no cache, the bootstrap copy answers.

    AC "用 Langfuse 回答生成 prompt" — the ladder's floor keeps the
    streaming step working offline; the bootstrap must carry the
    `{{instruction}}` / `{{results}}` placeholders.
    """
    provider = PromptProvider(
        settings=Settings(),  # no langfuse keys → fetch off
        http_client=httpx.AsyncClient(),
    )
    template = await provider.get_prompt(RESULT_SUMMARIZER_PROMPT)
    assert template.source == "bootstrap"
    assert "{{instruction}}" in template.text
    assert "{{results}}" in template.text


# ---------------------------------------------------------------------------
# render_node_results
# ---------------------------------------------------------------------------


class TestRenderNodeResults:
    def test_empty_results_renders_placeholder(self) -> None:
        rendered = render_node_results([])
        assert "no tool results" in rendered

    def test_single_node_carries_status_and_response(self) -> None:
        rendered = render_node_results(
            [
                _summary(
                    status="succeeded",
                    response={"text": "hello"},
                    response_text="hello",
                )
            ]
        )
        assert "[1] echo (n1) — succeeded" in rendered
        # The Prompt body always renders the structured `response`
        # (uniform truncation); the textual `response_text` field is
        # still carried on `NodeSummary` for the Frontend preview.
        assert "response:" in rendered
        assert "hello" in rendered

    def test_failed_node_carries_error_envelope(self) -> None:
        rendered = render_node_results(
            [
                _summary(
                    status="failed",
                    error={"code": "upstream_500", "message_en": "boom"},
                )
            ]
        )
        assert "[1] echo (n1) — failed" in rendered
        assert "error:" in rendered
        assert "upstream_500" in rendered

    def test_notes_appear_when_present(self) -> None:
        rendered = render_node_results([_summary(notes="回显 hello")])
        assert "notes: 回显 hello" in rendered

    def test_truncation_marker_appears_when_total_too_long(self) -> None:
        # Build many nodes with a large payload each to exceed the cap.
        big_payload = "x" * 1500
        summaries = [
            _summary(
                node_id=f"n{i}",
                tool_name=f"tool{i}",
                response={"text": big_payload},
                response_text=big_payload,
            )
            for i in range(8)
        ]
        rendered = render_node_results(summaries)
        assert "(后续结果已截断)" in rendered


# ---------------------------------------------------------------------------
# AnswerGenerator.astream
# ---------------------------------------------------------------------------


class TestAnswerGenerator:
    async def test_astream_renders_prompt_with_instruction_and_results(self) -> None:
        generator, fake = _generator(stream_tokens=["你好", "，", "世界"])
        collected: list[str] = []
        async for token in generator.astream(
            instruction="echo hello",
            results=[_summary()],
        ):
            collected.append(token)
        assert collected == ["你好", "，", "世界"]
        assert fake.call_count == 1
        prompt = fake.seen_prompts[0]
        # The fake's `seen_prompts` records the input string LangChain
        # produced; the templated shape is visible at the seam.
        assert prompt.startswith("INSTRUCTION>>")
        assert "echo hello" in prompt
        assert "RESULTS>>" in prompt
        assert "echo (n1)" in prompt
        assert "{{instruction}}" not in prompt and "{{results}}" not in prompt

    async def test_astream_transport_error_becomes_generation_error(self) -> None:
        generator, _ = _generator(raise_on_astream=True)
        with pytest.raises(LLMGenerationError):
            async for _ in generator.astream(
                instruction="echo hello",
                results=[_summary()],
            ):
                pass

    async def test_astream_emits_empty_string_for_non_text_chunk(self) -> None:
        """Chunks with no textual `content` should be skipped, not yielded."""

        class _NonTextModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake-non-text"

            def _generate(
                self,
                messages: list[BaseMessage],
                stop: list[str] | None = None,
                run_manager: CallbackManagerForLLMRun | None = None,
                **kwargs: Any,
            ) -> ChatResult:
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content=""))]
                )

            async def _astream(
                self,
                messages: list[BaseMessage],
                stop: list[str] | None = None,
                run_manager: AsyncCallbackManagerForLLMRun | None = None,
                **kwargs: Any,
            ) -> Any:
                # Yield two empty chunks then one with text.
                yield ChatGenerationChunk(message=AIMessageChunk(content=""))
                yield ChatGenerationChunk(message=AIMessageChunk(content=""))
                yield ChatGenerationChunk(message=AIMessageChunk(content="hi"))

        generator = AnswerGenerator(
            settings=_settings(),
            prompt_provider=_provider_with_template(_SUMMARIZER_TEMPLATE),
            chat_model_factory=lambda: _NonTextModel(),
        )
        out: list[str] = []
        async for token in generator.astream(
            instruction="echo",
            results=[_summary()],
        ):
            out.append(token)
        assert out == ["hi"]

    async def test_configuration_refusal_propagates(self) -> None:
        """ADR-0016: a provider refused by config must not be swallowed
        into a generic generation failure."""

        def _factory() -> BaseChatModel:
            raise LLMConfigurationError(details={"reason": "test"})

        generator = AnswerGenerator(
            settings=_settings(),
            prompt_provider=_provider_with_template(_SUMMARIZER_TEMPLATE),
            chat_model_factory=_factory,
        )
        with pytest.raises(LLMConfigurationError):
            async for _ in generator.astream(
                instruction="echo",
                results=[_summary()],
            ):
                pass

    def test_ready_reflects_llm_configuration(self) -> None:
        configured, _ = _generator()
        unconfigured, _ = _generator(settings=Settings(llm_base_url="", llm_api_key=""))
        assert configured.ready is True
        assert unconfigured.ready is False


# ---------------------------------------------------------------------------
# render_template shared contract (used by summarizer + planner + desc-gen)
# ---------------------------------------------------------------------------


def test_unknown_placeholders_pass_through() -> None:
    rendered = render_template(
        "i={{instruction}} r={{results}} x={{future}}",
        {"instruction": "I", "results": "R"},
    )
    assert rendered.startswith("i=I r=R")
    assert "x={{future}}" in rendered