"""`ToolDescriptionGenerator` — LLM-friendly Tool descriptions (T16 / #14, ADR-0018).

ADR-0018 pins the flow: on OpenAPI import the backend calls the
Langfuse-hosted `tool-description-generator` Prompt to rewrite the
developer-facing operation text into an LLM-friendly description plus
typical use cases, and the result lands on the draft for admin review.
This module owns the rewrite; `app.api.admin_tools.import_openapi`
invokes it over each parse of `POST /api/v1/admin/tools/import/openapi`.

Behavioural rules worth knowing before editing:

* **Never blocks the import.** ADR-0003's graceful-degradation rule
  applies: an unconfigured or failing LLM must not turn a good parse
  into a failed request. Every failure path becomes a warning string
  (import-level for global problems, per-draft for single-operation
  ones) while the draft keeps its raw OpenAPI description — the admin
  can still review, edit, and activate it.
* **The rewrite rides in the same `description` field.** CONTEXT.md's
  lifecycle keeps `draft` as the admin-review-pending state; the
  generated text is written to `draft.description` and the replaced
  raw text to `draft.original_description` (preview-only) so the UI
  can show 原文 vs LLM-friendly side by side (ticket AC #1 and #3).
* **Use cases are appended, not left implicit.** AC #2 requires the
  typical-use-case hints to be part of the stored text because the
  Planner only ever sees `description`; the structured list also
  travels on `GeneratedDescription` for UI display.
* **Bounded fan-out.** An import preview generates descriptions for at
  most `MAX_DESCRIPTIONS_PER_IMPORT` operations, running at most
  `_GENERATION_CONCURRENCY` LLM calls in flight, so a 200-operation
  spec can't monopolise the provider or the request budget. What is
  skipped is announced in an import-level warning — never silently
  capped.

The ADR-0016 data-usage opt-out applies to these calls too: the chat
model is built through `app.llm.provider.build_chat_model`, which
refuses endpoints that don't support the no-train path.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.llm.errors import LLMConfigurationError, LLMGenerationError
from app.llm.prompts import TOOL_DESCRIPTION_GENERATOR_PROMPT, PromptProvider
from app.settings import Settings
from app.tools.openapi_parser import ToolDraft

# Upper bound on LLM rewrites per import preview. Beyond it the extra
# drafts keep their raw text and the response carries an import-level
# warning. Sized for typical enterprise specs; the admin can import a
# slice, review, and re-run for the long tail.
MAX_DESCRIPTIONS_PER_IMPORT = 32

# In-flight LLM calls per import. OpenAI-compatible providers throttle
# aggressively past a handful of concurrent requests from one key; five
# keeps a 32-operation preview under ~30s without tripping rate limits.
_GENERATION_CONCURRENCY = 5

# `ToolBase.description` caps at 4096 chars (schemas.py); composed text
# is clamped to this before it reaches a draft.
_MAX_DESCRIPTION_LENGTH = 4096

# Character budget for the rendered parameter summary. Long OpenAPI
# descriptions would swamp the prompt; the LLM mainly needs names,
# types, and locations.
_MAX_PARAMETERS_SUMMARY = 1200

# Langfuse-style `{{variable}}` placeholder. `\w+` matches the variable
# names we render; anything else (e.g. JSON braces in the template)
# passes through untouched.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Fenced code block extractor: ```json … ``` or plain ``` … ```.
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_USE_CASE_HEADER = "\n\n典型用例:\n"


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """One LLM rewrite, ready to be swapped onto a `ToolDraft`.

    `description` is the final composed text (rewrite + 典型用例 lines,
    already clamped to the schema cap) — this is what lands on
    `draft.description`. `typical_use_cases` mirrors the structured
    model output so callers (the preview UI) can render the hints
    without re-parsing. `prompt_source` records which rung of the
    ADR-0013 ladder supplied the template.
    """

    description: str
    typical_use_cases: list[str]
    prompt_source: str


class ToolDescriptionGenerator:
    """Rewrites developer-facing operation text for the LLM Planner.

    Dependencies arrive through the constructor so tests can hand in a
    fake `BaseChatModel` (via `chat_model_factory`) and a stubbed
    `PromptProvider`; production wires all three in
    `app.db.dependencies.get_description_generator`. The factory is
    lazy: `ChatOpenAI` construction needs a configured key, and an
    unconfigured backend must stay constructible (its imports just
    skip generation).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        prompt_provider: PromptProvider,
        chat_model_factory: Callable[[], BaseChatModel],
    ) -> None:
        self._settings = settings
        self._prompts = prompt_provider
        self._model_factory = chat_model_factory
        self._model: BaseChatModel | None = None

    @property
    def ready(self) -> bool:
        """Cheap "is the LLM configured" check — never touches the network."""
        return bool(self._settings.llm_base_url and self._settings.llm_api_key)

    # ------------------------------------------------------------------
    # Single-draft rewrite
    # ------------------------------------------------------------------

    async def generate(self, draft: ToolDraft) -> GeneratedDescription:
        """Produce the LLM-friendly description for one draft.

        Raises:
            LLMConfigurationError: provider refused by config (ADR-0016).
            PromptUnavailableError: no template on any rung (only for
                prompt names without a bootstrap copy).
            LLMGenerationError: transport failure or output the JSON
                contract can't be honoured.
        """
        template = await self._prompts.get_prompt(TOOL_DESCRIPTION_GENERATOR_PROMPT)
        prompt_text = render_template(template.text, _draft_variables(draft))

        model = self._chat_model()
        try:
            response = await model.ainvoke(prompt_text)
        except LLMConfigurationError:
            raise
        except Exception as exc:  # langchain wraps provider errors variably
            raise LLMGenerationError(
                message_en=f"LLM call failed for {draft.name!r}: {exc}",
                details={"tool_name": draft.name, "error_type": type(exc).__name__},
            ) from exc

        content = _content_to_text(response.content)
        description, use_cases = _parse_model_output(content, draft.name)
        composed = _compose_description(description, use_cases)
        return GeneratedDescription(
            description=composed,
            typical_use_cases=use_cases,
            prompt_source=template.source,
        )

    # ------------------------------------------------------------------
    # Batch orchestration — called by the import route
    # ------------------------------------------------------------------

    async def enrich_drafts(self, drafts: list[ToolDraft]) -> list[str]:
        """Rewrite `drafts` in place; return import-level warnings.

        Mutations on success: `description` ← generated text,
        `original_description` ← the replaced raw text,
        `description_generated` ← True. Per-draft failures append a
        warning to that draft's `warnings` and leave its text untouched
        — the raw OpenAPI description is still reviewable, which keeps
        ADR-0018's admin-review step intact even with a broken model.
        """
        if not self.ready:
            return [
                "LLM description generation is not configured "
                "(COPILOT_LLM_BASE_URL / COPILOT_LLM_API_KEY); drafts keep "
                "their raw OpenAPI descriptions for manual review."
            ]
        # Surface provider-level config refusals (ADR-0016) once, before
        # fanning out — a misconfigured no-train posture is an import
        # problem, not 32 identical per-draft failures.
        try:
            self._chat_model()
        except LLMConfigurationError as exc:
            assert exc.details is not None  # raised with details
            return [
                f"LLM description generation disabled: {exc.details.get('reason', exc)}; "
                "drafts keep their raw OpenAPI descriptions."
            ]

        within = drafts[:MAX_DESCRIPTIONS_PER_IMPORT]
        skipped = len(drafts) - len(within)
        import_warnings: list[str] = []
        if skipped:
            import_warnings.append(
                f"LLM descriptions were generated for the first "
                f"{MAX_DESCRIPTIONS_PER_IMPORT} operations; {skipped} further "
                "draft(s) kept their raw descriptions and can be edited during review."
            )

        semaphore = asyncio.Semaphore(_GENERATION_CONCURRENCY)

        async def _one(draft: ToolDraft) -> None:
            async with semaphore:
                try:
                    generated = await self.generate(draft)
                except Exception as exc:  # degradation must not abort the batch
                    reason = exc if isinstance(exc, LLMGenerationError) else str(exc)
                    draft.warnings.append(
                        f"LLM description generation failed ({reason}); kept the "
                        "original OpenAPI description."
                    )
                    return
                draft.original_description = draft.description
                draft.description = generated.description
                draft.description_generated = True

        await asyncio.gather(*(_one(draft) for draft in within))
        return import_warnings

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _chat_model(self) -> BaseChatModel:
        """Lazily build (and cache) the configured chat model."""
        if self._model is None:
            self._model = self._model_factory()
        return self._model


# ---------------------------------------------------------------------------
# Module-level pure helpers — importable for targeted tests
# ---------------------------------------------------------------------------


def render_template(text: str, variables: dict[str, str]) -> str:
    """Substitute Langfuse `{{variable}}` placeholders.

    Unknown placeholders stay literal — an admin can extend the template
    with new variables before the code learns to supply them, and the
    LLM sees the marker rather than a silent empty string.
    """

    def _replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, text)


def _draft_variables(draft: ToolDraft) -> dict[str, str]:
    """Flatten one draft into the template variable set."""
    _, _, path = draft.operation_ref.partition(" ")
    return {
        "name": draft.name,
        "method": draft.http_method,
        "path": path or draft.http_url_template,
        "description": draft.description,
        "parameters": _summarize_parameters(draft.parameters_schema),
        "risk_level": draft.risk_level,
    }


def _summarize_parameters(schema: dict[str, Any]) -> str:
    """Render `parameters_schema` as a compact one-line-per-parameter digest.

    The LLM doesn't need the full JSON Schema (type unions, nested
    bodies); it needs "what does the caller supply and what shape is
    it". `__location__` markers from the parser (path / query / body)
    travel along because they change how a natural-language instruction
    maps to arguments.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "(无参数 / no parameters)"
    required = schema.get("required")
    required_set = set(required) if isinstance(required, list) else set()
    lines: list[str] = []
    for name, spec in properties.items():
        spec_dict = spec if isinstance(spec, dict) else {}
        type_name = spec_dict.get("type", "any")
        location = spec_dict.get("__location__")
        parts = [f"- {name}: {type_name}"]
        if isinstance(location, str):
            parts.append(f"({location})")
        if name in required_set:
            parts.append("required")
        description = spec_dict.get("description")
        if isinstance(description, str) and description.strip():
            clipped = description.strip()[:80]
            parts.append(f"— {clipped}")
        lines.append(" ".join(str(p) for p in parts))
    summary = "\n".join(lines)
    return summary[:_MAX_PARAMETERS_SUMMARY]


def _content_to_text(content: Any) -> str:
    """Normalise a LangChain message content payload to plain text.

    Chat models return either `str` or a list of content blocks
    (`{"type": "text", "text": …}` for OpenAI-compatible providers).
    Anything else is stringified defensively — the JSON extractor
    still gets a chance to find braces inside it.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "".join(chunks)
    return str(content)


def _parse_model_output(content: str, tool_name: str) -> tuple[str, list[str]]:
    """Extract the `{description, typical_use_cases}` contract.

    Tries, in order: the whole payload as JSON, a fenced ```json block,
    the outermost brace pair. Any of those producing a non-empty
    `description` string passes; use cases filter down to non-empty
    strings. Everything else raises `LLMGenerationError` so the batch
    degrades this draft rather than storing junk.
    """
    candidate = content.strip()
    parsed: dict[str, Any] | None = None

    try:
        maybe = json.loads(candidate)
        parsed = maybe if isinstance(maybe, dict) else None
    except ValueError:
        pass

    if parsed is None:
        fenced = _FENCED_JSON_RE.search(candidate)
        if fenced is not None:
            try:
                maybe = json.loads(fenced.group(1))
                parsed = maybe if isinstance(maybe, dict) else None
            except ValueError:
                parsed = None

    if parsed is None:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                maybe = json.loads(candidate[start : end + 1])
                parsed = maybe if isinstance(maybe, dict) else None
            except ValueError:
                parsed = None

    if parsed is None:
        raise LLMGenerationError(
            message_en=f"LLM output for {tool_name!r} is not the JSON contract",
            details={"tool_name": tool_name},
        )

    description = parsed.get("description")
    if not isinstance(description, str) or not description.strip():
        raise LLMGenerationError(
            message_en=f"LLM output for {tool_name!r} has no usable description",
            details={"tool_name": tool_name},
        )

    raw_cases = parsed.get("typical_use_cases")
    cases: list[str] = []
    if isinstance(raw_cases, list):
        cases = [c.strip() for c in raw_cases if isinstance(c, str) and c.strip()]
    return description.strip(), cases


def _compose_description(description: str, use_cases: list[str]) -> str:
    """Join rewrite + 典型用例 bullets, clamped to the schema's 4096 cap.

    Use cases are appended whole or dropped whole (from the end), so
    the stored text never contains a half-finished bullet. The list is
    tiny (the prompt asks for 2–4), so re-measuring per bullet is fine.
    """
    description = description[:_MAX_DESCRIPTION_LENGTH]
    if not use_cases:
        return description
    bullets: list[str] = []
    for case in use_cases:
        candidate = (
            description
            + _USE_CASE_HEADER
            + "\n".join([*(f"- {c}" for c in bullets), f"- {case}"])
        )
        if len(candidate) > _MAX_DESCRIPTION_LENGTH:
            break
        bullets.append(case)
    if not bullets:
        return description
    return description + _USE_CASE_HEADER + "\n".join(f"- {c}" for c in bullets)


__all__ = [
    "GeneratedDescription",
    "ToolDescriptionGenerator",
    "MAX_DESCRIPTIONS_PER_IMPORT",
    "render_template",
]
