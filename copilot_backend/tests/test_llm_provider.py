"""Unit tests for the LangChain ChatModel factory (T16 / #14, ADR-0014 / ADR-0016).

`build_chat_model` is the single seam every LLM call in the backend is
built through: it translates `Settings` into an OpenAI-compatible
`BaseChatModel` and enforces the two configuration rules that the ADRs
make hard requirements:

* ADR-0014 — the provider is OpenAI-compatible (base_url + api_key +
  model_name); swapping providers never touches business code.
* ADR-0016 — when `data_usage_opt_out` is on but the endpoint is
  declared as not supporting the no-train path, the call must be
  refused rather than silently training on enterprise metadata.
"""
from __future__ import annotations

import pytest
from langchain_openai import ChatOpenAI

from app.llm.errors import LLMConfigurationError
from app.llm.provider import build_chat_model
from app.settings import Settings


def _configured(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": "sk-test-key",
        "llm_model": "deepseek-chat",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_unconfigured_llm_raises_configuration_error() -> None:
    """Defaults (no base_url / api_key) mean "no LLM": refuse loudly."""
    settings = Settings()
    with pytest.raises(LLMConfigurationError, match="not configured"):
        build_chat_model(settings)


def test_configured_llm_builds_openai_compatible_chat_model() -> None:
    """The factory hands back a ChatOpenAI carrying the configured endpoint."""
    # `build_chat_model` returns the abstract `BaseChatModel` (ADR-0014);
    # the default provider is OpenAI-compatible, so the concrete cast
    # is how a test inspects the wire config without business code
    # depending on it.
    model = build_chat_model(_configured())
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-chat"
    assert model.openai_api_base == "https://llm.example.com/v1"


def test_opt_out_with_unsupported_no_train_provider_raises() -> None:
    """ADR-0016: opt-out + provider without no-train support = hard refusal."""
    settings = _configured(
        llm_data_usage_opt_out=True,
        llm_provider_supports_no_train=False,
    )
    with pytest.raises(LLMConfigurationError, match="no-train"):
        build_chat_model(settings)


def test_opt_in_allows_training_enabled_provider() -> None:
    """Explicitly opting out of the opt-out lets a training endpoint build."""
    settings = _configured(
        llm_data_usage_opt_out=False,
        llm_provider_supports_no_train=False,
    )
    model = build_chat_model(settings)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-chat"
