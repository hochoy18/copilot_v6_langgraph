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
* **Parameter notes ride into `parameters_schema` (T16-followup /
  #50).** The LLM contract grows to
  `{description, typical_use_cases, parameter_notes: {param: note}}`;
  the notes rewrite `parameters_schema.properties[*].description` so
  the preview UI sees the business-facing wording for every argument.
  Internal markers (`__location__`) and structural fields (`type`,
  `required`) survive untouched — they are routing/validation signals,
  not copy that the LLM is allowed to touch. Parameters the LLM
  omits from `parameter_notes` keep their original OpenAPI
  descriptions; the original schema is parked on
  `draft.original_parameters_schema` for review.
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.llm.errors import LLMConfigurationError, LLMGenerationError
from app.llm.output import content_to_text, extract_json_object
from app.llm.prompts import (
    TOOL_DESCRIPTION_GENERATOR_PROMPT,
    PromptProvider,
    render_template,
)
from app.settings import Settings
from app.tools.openapi_parser import ToolDraft

# Upper bound on LLM rewrites per import preview (parameters recorded
# in ADR-0033). Beyond it the extra drafts keep their raw text and the
# response carries an import-level warning — skipped drafts are
# editable during review; a per-draft regeneration endpoint is tracked
# as a follow-up of #14 for the long tail.
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

# Per-parameter note cap (T16-followup / #50). Long enough for a real
# business-facing sentence, short enough that one chatty model can't
# bloat the persisted schema. `ToolBase.parameters_schema` itself has
# no per-string cap, but anything close to the description limit would
# indicate the LLM is regenerating the whole operation text.
_MAX_PARAMETER_NOTE_LENGTH = 256

_USE_CASE_HEADER = "\n\n典型用例:\n"


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """One LLM rewrite, ready to be swapped onto a `ToolDraft`.

    `description` is the final composed text (rewrite + 典型用例 lines,
    already clamped to the schema cap) — this is what lands on
    `draft.description`. `typical_use_cases` mirrors the structured
    model output so callers can tell whether the hints actually made
    it into the text (an empty list triggers a review warning) without
    re-parsing.

    T16-followup / #50 adds `parameter_notes` (the LLM's
    `{param: note}` map) and `parameters_schema` (the rewritten schema
    with notes applied to `properties[*].description`, keeping the
    `__location__` marker and structural fields intact). When the LLM
    omits `parameter_notes` both fields mirror the original draft —
    `parameter_notes` is `{}` and `parameters_schema` is the unchanged
    parser output. Tests assert on `parameters_schema` to keep the
    preview-side contract (raw → rewritten) explicit.
    """

    description: str
    typical_use_cases: list[str]
    parameter_notes: dict[str, str]
    parameters_schema: dict[str, Any]


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

        content = content_to_text(response.content)
        description, use_cases, raw_notes = _parse_model_output(content, draft.name)
        composed = _compose_description(description, use_cases)
        rewritten_schema, applied_notes = _apply_parameter_notes(
            draft.parameters_schema, raw_notes
        )
        return GeneratedDescription(
            description=composed,
            typical_use_cases=use_cases,
            parameter_notes=applied_notes,
            parameters_schema=rewritten_schema,
        )

    # ------------------------------------------------------------------
    # Batch orchestration — called by the import route
    # ------------------------------------------------------------------

    async def enrich_drafts(self, drafts: list[ToolDraft]) -> list[str]:
        """Rewrite `drafts` in place; return import-level warnings.

        Mutations on success: `description` ← generated text,
        `original_description` ← the replaced raw text,
        `description_generated` ← True. For T16-followup / #50 the
        same pattern extends to `parameters_schema`: when the LLM
        returned `parameter_notes`, `parameters_schema` carries the
        rewritten per-property descriptions, the raw schema lands on
        `original_parameters_schema`, and `parameters_schema_generated`
        flips to True. Per-draft failures append a warning to that
        draft's `warnings` and leave its text untouched — the raw
        OpenAPI description is still reviewable, which keeps
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
                # T16-followup / #50 — apply parameter notes onto the
                # schema only when the LLM actually returned any; the
                # empty-map case keeps the original schema verbatim so
                # graceful degradation matches the description path.
                if generated.parameter_notes:
                    draft.original_parameters_schema = draft.parameters_schema
                    draft.parameters_schema = generated.parameters_schema
                    draft.parameters_schema_generated = True
                else:
                    # The description rewrite succeeded but the model
                    # didn't supply parameter notes — surface that so
                    # the admin can add them by hand during review.
                    draft.warnings.append(
                        "LLM 生成结果未包含参数说明，请 review 时补充。"
                    )
                if not generated.typical_use_cases:
                    # AC #2 guard: a rewrite without 典型用例 is still
                    # reviewable, but the admin must be told to add one.
                    draft.warnings.append(
                        "LLM 生成结果未包含典型用例，请 review 时补充。"
                    )

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


def _draft_variables(draft: ToolDraft) -> dict[str, str]:
    """Flatten one draft into the template variable set."""
    _, _, path = draft.operation_ref.partition(" ")
    return {
        "name": draft.name,
        "method": draft.http_method,
        "path": path or draft.http_url_template,
        "description": draft.description,
        "parameters": summarize_parameters(draft.parameters_schema),
        "risk_level": draft.risk_level,
    }


def summarize_parameters(schema: dict[str, Any]) -> str:
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


def _parse_model_output(
    content: str, tool_name: str
) -> tuple[str, list[str], dict[str, str]]:
    """Extract the `{description, typical_use_cases, parameter_notes}` contract.

    `app.llm.output.extract_json_object` handles the "fenced block /
    prose-wrapped JSON" mess; here we only police the contract: a
    non-empty `description` string must survive, use cases filter down
    to non-empty strings, and `parameter_notes` (T16-followup / #50)
    survives as a `{name: note}` map of non-empty trimmed strings.
    Anything else raises `LLMGenerationError` so the batch degrades
    this draft rather than storing junk.

    `parameter_notes` is optional — older Langfuse copies or models
    that decide not to produce notes yield an empty dict, which keeps
    AC #3's graceful-degradation parity with the description rewrite.
    """
    parsed = extract_json_object(content)

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
    return description.strip(), cases, _coerce_parameter_notes(parsed.get("parameter_notes"))


def _coerce_parameter_notes(value: Any) -> dict[str, str]:
    """Normalise the LLM's `parameter_notes` payload into a clean dict.

    Anything that isn't a string→string map is dropped wholesale —
    partial / typed-wrong values would otherwise corrupt the
    schema rewrite with `TypeError` surprises downstream.
    """
    if not isinstance(value, dict):
        return {}
    notes: dict[str, str] = {}
    for key, note in value.items():
        if not isinstance(key, str) or not isinstance(note, str):
            continue
        cleaned = note.strip()
        if not cleaned:
            # An empty/whitespace note would clobber the original
            # description with no signal — skip and let the original
            # OpenAPI text stand.
            continue
        notes[key] = cleaned
    return notes


def _apply_parameter_notes(
    schema: dict[str, Any], notes: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Rewrite `parameters_schema` with the LLM's notes; return applied map.

    Returns `(rewritten_schema, applied_notes)`:

    * `rewritten_schema` is a shallow-cloned schema; the input is
      never mutated so `draft.original_parameters_schema` can keep the
      raw text intact. Internal markers (`__location__`) and
      structural fields (`type`, `required`) survive untouched —
      they're routing/validation signals, not copy the LLM is
      allowed to touch.
    * `applied_notes` is the subset of `notes` that landed on the
      schema (keys present in `properties`, non-empty after
      trimming, clamped to `_MAX_PARAMETER_NOTE_LENGTH`). Notes
      that name a parameter absent from the schema are silently
      dropped: validation is the schema's job (ADR-0020), and a
      stray note must not manufacture a parameter that doesn't
      exist. Surfacing only the applied map lets callers tell the
      admin which notes actually made it onto the preview.
    """
    if not notes:
        return schema, {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema, {}
    rewritten_properties: dict[str, Any] = {}
    applied: dict[str, str] = {}
    for name, spec in properties.items():
        spec_dict = spec if isinstance(spec, dict) else {}
        note = notes.get(name)
        if note is None:
            rewritten_properties[name] = spec_dict
            continue
        # Clamp the note so a chatty model can't bloat the persisted
        # schema — see `_MAX_PARAMETER_NOTE_LENGTH` for the rationale.
        clipped = note[:_MAX_PARAMETER_NOTE_LENGTH]
        # Copy the spec dict so we never mutate the original; only
        # `description` is replaced, every other key (including
        # `__location__` and `type`) is preserved verbatim.
        new_spec = dict(spec_dict)
        new_spec["description"] = clipped
        rewritten_properties[name] = new_spec
        applied[name] = clipped
    rewritten = dict(schema)
    rewritten["properties"] = rewritten_properties
    return rewritten, applied


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
    # Re-exported from `app.llm.prompts` / defined here — both belong
    # to the Prompt-render contract the planner (T18) shares.
    "render_template",
    "summarize_parameters",
]
