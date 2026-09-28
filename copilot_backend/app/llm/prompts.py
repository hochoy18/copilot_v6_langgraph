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
`app.tools.description_generator` for the render step.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import quote

import httpx

from app.llm.errors import PromptUnavailableError
from app.settings import Settings

PromptSource = Literal["langfuse", "cache", "bootstrap"]

# The canonical Langfuse Prompt name for T16. Referenced by name only —
# version pinning is deliberately absent (ADR-0013: always the active
# version).
TOOL_DESCRIPTION_GENERATOR_PROMPT = "tool-description-generator"


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

_BOOTSTRAP_PROMPTS: dict[str, str] = {
    TOOL_DESCRIPTION_GENERATOR_PROMPT: _BOOTSTRAP_TOOL_DESCRIPTION_GENERATOR,
}


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
    "PromptProvider",
    "PromptTemplate",
    "TOOL_DESCRIPTION_GENERATOR_PROMPT",
]
