"""`build_chat_model` — the LangChain ChatModel factory (T16 / #14, ADR-0014).

ADR-0014 fixes the LLM provider abstraction: business code depends on
`BaseChatModel` only, and the default implementation is the OpenAI
compatible protocol (`ChatOpenAI` pointed at any base_url — OpenAI,
DeepSeek, 豆包, self-hosted vLLM). Swapping providers is a settings
change, never a code change.

The factory is the enforcement point for ADR-0016's data-usage opt-out:
enterprise deployments default `llm_data_usage_opt_out=true`, and the
operator declares endpoint capability with
`llm_provider_supports_no_train`. The two disagreeing is a hard
configuration error — we would rather refuse the call than quietly
train on enterprise API metadata.

Why a factory function instead of a module-level singleton: the model
object owns an httpx client, so construction belongs to the lifespan /
`app.db.dependencies` seam, not import time. Tests build fake
`BaseChatModel` instances and override the dependency instead of
calling through here.

Startup validation note: ADR-0016 phrases the capability check as a
boot-time gate, but `app.main`'s lifespan deliberately never raises (a
degraded boot is the contract for /healthz, and LLM-less deployments
are supported). The check therefore runs at model-construction time —
the first moment any LLM feature is actually used — and its failure is
rendered to admins as a degradation warning on the import preview.
"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.llm.errors import LLMConfigurationError
from app.settings import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Construct the configured OpenAI-compatible `BaseChatModel`.

    Raises:
        LLMConfigurationError: no endpoint configured, or the ADR-0016
            opt-out flag disagrees with the provider's declared
            no-train capability.
    """
    if not settings.llm_base_url or not settings.llm_api_key:
        raise LLMConfigurationError(
            message_en=(
                "LLM provider is not configured: set COPILOT_LLM_BASE_URL "
                "and COPILOT_LLM_API_KEY to enable LLM features"
            ),
            details={"reason": "llm_base_url or llm_api_key is empty"},
        )
    if settings.llm_data_usage_opt_out and not settings.llm_provider_supports_no_train:
        raise LLMConfigurationError(
            message_en=(
                "data_usage_opt_out is enabled but the configured provider does "
                "not support the no-train path (ADR-0016); point llm_base_url at "
                "a zero-retention endpoint or set llm_data_usage_opt_out=false"
            ),
            details={"reason": "no-train path unsupported by provider"},
        )
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=SecretStr(settings.llm_api_key),
        base_url=settings.llm_base_url,
        timeout=settings.llm_request_timeout_seconds,
        # One retry keeps the import preview responsive when a provider
        # hiccups; the generator degrades per-draft on the second miss.
        max_retries=1,
    )


__all__ = ["build_chat_model"]
