"""`PromptProvider` — runtime Prompt fetching from Langfuse (T16 / #14, ADR-0013).

ADR-0013 keeps every Prompt template on Langfuse so edits land without a
deploy and every LLM call is replayable against the version it actually
used. The ADR also pins the degradation ladder for Langfuse outages:

    fresh fetch (within TTL) → cached last-good copy → bootstrap default

ADR-0033 records the two deviations from ADR-0013's wording that this
module embodies, with their rationale — do not re-litigate them in
review without reopening ADR-0033:

* Prompt reads go to the Langfuse public REST API over httpx, not the
  v3 SDK (the SDK's OTel bundle belongs to T40; its sync API would
  block the event loop). When T40 lands, `get_prompt` can delegate to
  the SDK without touching callers.
* The ladder's floor is a code-embedded *bootstrap* copy, because
  ADR-0013's "locally cached Prompt" fallback presupposes at least one
  successful fetch — impossible on a first boot with Langfuse down.
  The bootstrap text is a usability floor, not the canonical asset:
  creating the same-named Prompt on Langfuse takes over on the next
  fetch, and `PromptTemplate.source` records which rung answered.

Placeholders use the Langfuse `{{variable}}` convention; see
`render_template` below for the render step.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import quote

import httpx

from app.llm.errors import PromptUnavailableError
from app.settings import Settings

PromptSource = Literal["langfuse", "cache", "bootstrap"]

# The canonical Langfuse Prompt names. Referenced by name only —
# version pinning is deliberately absent (ADR-0013: always the active
# version).
TOOL_DESCRIPTION_GENERATOR_PROMPT = "tool-description-generator"

# SPEC §Langfuse Prompt 列表: `planner` is the Planner LLM's Prompt
# (T18 / #16, T25 multi-node upgrade edits the Langfuse copy only).
PLANNER_PROMPT = "planner"

# SPEC §Langfuse Prompt 列表: `result-summarizer` is the final-answer
# Prompt (T22 / #19). After a Plan finishes, the user's instruction
# plus the per-node Tool results render into `{{instruction}}` /
# `{{results}}`, and the model streams a concise Chinese business-
# language reply back to the chat panel.
RESULT_SUMMARIZER_PROMPT = "result-summarizer"

# Langfuse-style `{{variable}}` placeholder. `\w+` matches the variable
# names we render; anything else (e.g. JSON braces in the template)
# passes through untouched.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One resolved Prompt template plus provenance for replay / debugging.

    `source` records which rung of the ADR-0013 ladder produced the copy
    (`langfuse` fresh fetch, `cache` last-good, `bootstrap` embedded
    default) so a trace or warning can tell admins where the text came
    from without exposing the secret-bearing fetch path.
    """

    name: str
    text: str
    source: PromptSource
    version: int | None = None


# Bootstrap fallback for `tool-description-generator`. Mirrors the
# acceptance criteria of T16: LLM-friendly rewrite plus typical use
# cases, strict-JSON output. Written for OpenAI-compatible chat models.
_BOOTSTRAP_TOOL_DESCRIPTION_GENERATOR = """\
你是企业 API Copilot 的 Tool 描述撰写助手。请把下面这个面向开发者的 \
OpenAPI operation 改写成 LLM Planner 能语义匹配的业务化 Tool 描述。

Tool 名称: {{name}}
HTTP 方法与路径: {{method}} {{path}}
原始描述(面向开发者): {{description}}
参数摘要: {{parameters}}

要求:
1. 用一两句业务语言说明这个 Tool 做什么, 覆盖"业务人员可能怎么称呼它", \
不要复述 HTTP 细节, 也不要照抄原文。
2. 给出 2-4 条典型用例: 业务人员可能用什么样的自然语言指令会需要调用它, \
每条一句话。
3. 原始描述缺失或含糊时, 基于路径与参数推断, 并在描述中注明这是推断。
4. 只输出一个 JSON 对象, 不要输出其它任何内容(包括代码围栏之外的文字):
{"description": "<业务化描述>", "typical_use_cases": ["<用例1>", "<用例2>"]}
其中 typical_use_cases 为 2-4 条中文短句。
"""

# Bootstrap fallback for `planner` (T18 / #16). Same contract the
# Langfuse copy must keep: strict-JSON output, `nodes` array, Tool
# names drawn from the rendered catalog. T18's single-node scope is
# enforced by rule 2 — T25 lifts it in Langfuse (plus edges) without
# a code change; the parser already accepts N nodes.
_BOOTSTRAP_PLANNER = """\
你是企业 API Copilot 的 Planner。请把业务人员的自然语言指令翻译成 Tool 调用计划。

可用 Tool 目录(每行一个 Tool):
{{tools}}

用户指令: {{input}}

规则:
1. 只能选择目录中出现的 Tool, `tool` 必须与目录中的 name 完全一致; \
目录中没有合适的 Tool 时输出空节点列表, 不要臆造 Tool。
2. 当前版本一次最多规划 1 个 Tool 调用(单节点计划)。
3. `parameters` 是 JSON 对象, 键必须来自所选 Tool 的参数说明; \
指令未给出的可选参数直接省略, 必填参数无法确定时也输出空节点列表。
4. `notes` 用一句中文向业务人员解释这个计划要做什么。
5. 只输出一个 JSON 对象, 不要输出其它任何内容(包括代码围栏之外的文字):
{"nodes": [{"tool": "<目录中的 name>", "parameters": {...}, "notes": "<一句话说明>"}]}
不需要调用任何 Tool(闲聊 / 无法匹配)时输出 {"nodes": []}。
"""

# Bootstrap fallback for `result-summarizer` (T22 / #19). The Prompt
# takes the user's instruction plus the per-node execution results
# (ordered, with status / response / error) and asks for a concise
# Chinese reply — no preamble, no "好的" / "根据结果" prefix, no JSON
# wrapper. The streamed output IS the answer, byte-for-byte.
_BOOTSTRAP_RESULT_SUMMARIZER = """\
你是企业 API Copilot 的回答生成助手。请把 Tool 执行结果整理成给业务人员的中文回答。

用户原始问题:
{{instruction}}

Tool 执行结果(按 Plan 节点顺序, status / response / error 一起给出):
{{results}}

要求:
1. 用业务人员易懂的语言回答, 直接给出结论或要点, 不要重复用户问题。
2. 多节点结果时, 综合各节点信息形成整体答案, 不要逐条复述调用细节。
3. 部分节点失败时, 简洁说明哪些步骤失败以及失败原因; 成功的部分照常呈现。
4. 只输出回答正文, 不要任何前缀(如"好的"、"根据结果")或元说明, 也不要 JSON 围栏。
"""

_BOOTSTRAP_PROMPTS: dict[str, str] = {
    TOOL_DESCRIPTION_GENERATOR_PROMPT: _BOOTSTRAP_TOOL_DESCRIPTION_GENERATOR,
    PLANNER_PROMPT: _BOOTSTRAP_PLANNER,
    RESULT_SUMMARIZER_PROMPT: _BOOTSTRAP_RESULT_SUMMARIZER,
}


def render_template(text: str, variables: dict[str, str]) -> str:
    """Substitute Langfuse `{{variable}}` placeholders.

    Unknown placeholders stay literal — an admin can extend the template
    with new variables before the code learns to supply them, and the
    LLM sees the marker rather than a silent empty string.
    """

    def _replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, text)


class PromptProvider:
    """Resolves Prompt templates with the ADR-0013 degradation ladder.

    One instance lives per process (built in the lifespan alongside the
    OIDC adapter); `get_prompt` is the only public method. The cache is
    an in-process map of name → (fetched_at, template): Langfuse is the
    source of truth, the cache only exists so a transient outage doesn't
    take the feature down — there is no on-disk persistence (a restart
    falls back to bootstrap, which is acceptable for MVP).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = http_client
        self._now = time_func
        # name → (monotonic fetch instant, raw template from Langfuse)
        self._cache: dict[str, tuple[float, PromptTemplate]] = {}

    async def get_prompt(self, name: str) -> PromptTemplate:
        """Resolve `name` down the ladder: fetch → cache → bootstrap.

        A fresh cache entry (age < TTL) short-circuits the network.
        Within-TTL refetch failures fall through to the stale cache, then
        to the bootstrap copy, and only prompts without a bootstrap copy
        raise `PromptUnavailableError`.
        """
        cached = self._cache.get(name)
        if cached is not None and self._is_fresh(cached[0]):
            return replace(cached[1], source="cache")

        if self._langfuse_configured():
            fetched = await self._fetch(name)
            if fetched is not None:
                self._cache[name] = (self._now(), fetched)
                return fetched

        if cached is not None:
            return replace(cached[1], source="cache")

        bootstrap = _BOOTSTRAP_PROMPTS.get(name)
        if bootstrap is not None:
            return PromptTemplate(name=name, text=bootstrap, source="bootstrap")

        raise PromptUnavailableError(
            details={"prompt_name": name},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_fresh(self, fetched_at: float) -> bool:
        return (self._now() - fetched_at) < self._settings.langfuse_prompt_cache_ttl_seconds

    def _langfuse_configured(self) -> bool:
        keys = self._settings
        return bool(keys.langfuse_host and keys.langfuse_public_key and keys.langfuse_secret_key)

    async def _fetch(self, name: str) -> PromptTemplate | None:
        """One attempt against the Langfuse public API.

        Any transport error, non-200 status, or unparseable payload
        returns `None` so the caller continues down the ladder. A
        chat-format prompt (`prompt` as a message list) is treated as
        unparseable for the MVP — text prompts are the supported shape
        and the ladder keeps the feature alive meanwhile.
        """
        url = (
            f"{self._settings.langfuse_host.rstrip('/')}"
            f"/api/public/v2/prompts/{quote(name, safe='')}"
        )
        try:
            response = await self._client.get(
                url,
                auth=(self._settings.langfuse_public_key, self._settings.langfuse_secret_key),
                timeout=self._settings.langfuse_prompt_timeout_seconds,
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        text = payload.get("prompt")
        if not isinstance(text, str):
            return None
        version = payload.get("version")
        return PromptTemplate(
            name=name,
            text=text,
            source="langfuse",
            version=version if isinstance(version, int) else None,
        )


__all__ = [
    "PLANNER_PROMPT",
    "PromptProvider",
    "PromptTemplate",
    "RESULT_SUMMARIZER_PROMPT",
    "TOOL_DESCRIPTION_GENERATOR_PROMPT",
    "render_template",
]
