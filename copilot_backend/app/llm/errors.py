"""LLM-layer exceptions — T16 / #14.

All classes derive from `app.exceptions.AppError` so the global handler
renders them in the unified envelope (ADR-0031). None of them are
intended to reach the wire on the import-preview path — the T16
generator catches them and degrades to a per-draft warning — but they
stay typed so a future caller can choose to surface them instead.

`LLMUnavailableError` is the explicit-envelope variant the
single-draft `POST /admin/tools/descriptions/generate` endpoint
(T16-followup / #51) raises when generation is impossible: the
import-preview route degrades silently, but the per-row endpoint has
no fall-back state to keep, so the admin gets a clean error instead of
a 500. The 503 status matches the "service is configured but cannot
serve the request right now" reading of the contract — `Retry-After`
is left to the operator because the recovery model is deployment-
specific (re-configure / re-provision / upstage).
"""
from __future__ import annotations

from fastapi import status

from app.exceptions import AppError


class LLMConfigurationError(AppError):
    """Raised when an LLM call is attempted without a usable configuration.

    Covers the two refusal paths:

    * provider not configured (`llm_base_url` / `llm_api_key` empty)
      — the deployment opted out of LLM features entirely;
    * ADR-0016 mismatch — `data_usage_opt_out` is on but the endpoint
      is declared as not supporting the no-train path. Training on
      enterprise API metadata is the failure mode this guards against,
      so the refusal is hard.
    """

    code = "llm_configuration_error"
    message_zh = "LLM 未正确配置"
    message_en = "LLM provider is not configured correctly"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR


class PromptUnavailableError(AppError):
    """Raised when no Prompt template can be resolved for a name.

    The Langfuse fetch, the stale cache, and the bootstrap table all
    missed. Only bootstrap-less prompt names can hit this — every
    first-party prompt ships a bootstrap fallback precisely so the
    degradation ladder has a floor (ADR-0013).
    """

    code = "prompt_unavailable"
    message_zh = "无法获取 Prompt 模板"
    message_en = "Prompt template could not be resolved"
    http_status = status.HTTP_502_BAD_GATEWAY


class LLMGenerationError(AppError):
    """Raised when an LLM call fails to produce usable structured output.

    Covers transport errors surfaced by the ChatModel and the
    "model answered, but not with parseable JSON" case. The T16
    generator raises this per draft; the import route turns it into a
    warning while keeping the raw OpenAPI description.
    """

    code = "llm_generation_error"
    message_zh = "LLM 生成失败"
    message_en = "LLM generation failed"
    http_status = status.HTTP_502_BAD_GATEWAY


class LLMUnavailableError(AppError):
    """Raised when an explicit LLM call cannot be served at all.

    Used by the per-draft `descriptions/generate` endpoint (T16-followup
    / #51). The import-preview path catches the lower-level
    `LLMConfigurationError` / `LLMGenerationError` / `PromptUnavailableError`
    and degrades to a per-draft warning because the preview has the
    raw OpenAPI text to fall back on. The single-draft endpoint has no
    fall-back state, so the admin gets an explicit 503 envelope rather
    than a 500 — the request was understood, the upstream is not in a
    position to serve it, and the operator can act.
    """

    code = "llm_unavailable"
    message_zh = "LLM 暂不可用，请稍后重试或联系运维"
    message_en = "LLM is unavailable; retry later or contact ops"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


__all__ = [
    "LLMConfigurationError",
    "LLMGenerationError",
    "LLMUnavailableError",
    "PromptUnavailableError",
]
