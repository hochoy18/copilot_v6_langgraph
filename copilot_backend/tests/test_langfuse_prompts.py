"""Unit tests for `PromptProvider` — T16 / #14 (ADR-0013).

`PromptProvider.get_prompt(name)` is the single seam business code uses
to pull a Prompt template from Langfuse. ADR-0013 pins two behaviours:

* the canonical Prompt lives on Langfuse, never in the repo — a fresh
  fetch wins over anything cached;
* when Langfuse is unreachable the backend degrades to the last-good
  cached copy; with no cache at all, first-party prompts fall back to
  the code-embedded bootstrap template so the feature stays usable.

Tests drive the provider against an injected `httpx.MockTransport` so
no network is involved; the clock is injected too, so TTL behaviour is
exercised deterministically.
"""
from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.llm.errors import PromptUnavailableError
from app.llm.prompts import PromptProvider
from app.settings import Settings

_PROMPT_NAME = "tool-description-generator"
_PROMPT_PATH = f"/api/public/v2/prompts/{_PROMPT_NAME}"


class _Clock:
    """Manually advanced monotonic clock stand-in."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(**overrides: object) -> Settings:
    base = {
        "langfuse_host": "https://langfuse.example.com",
        "langfuse_public_key": "pk-lf-test",
        "langfuse_secret_key": "sk-lf-test",
        "langfuse_prompt_cache_ttl_seconds": 300,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: Settings | None = None,
    clock: _Clock | None = None,
) -> tuple[PromptProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _tracking(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider = PromptProvider(
        settings=settings or _settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_tracking)),
        time_func=clock or _Clock(),
    )
    return provider, seen


PromptHandler = Callable[[httpx.Request], httpx.Response]


def _ok(text: str = "hello {{name}}", version: int = 3) -> PromptHandler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": _PROMPT_NAME, "version": version, "prompt": text},
        )

    return handler


def _unavailable(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"message": "langfuse down"})


async def test_fresh_fetch_returns_langfuse_prompt_with_basic_auth() -> None:
    provider, seen = _provider(_ok())

    template = await provider.get_prompt(_PROMPT_NAME)

    assert template.text == "hello {{name}}"
    assert template.source == "langfuse"
    assert template.version == 3
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == _PROMPT_PATH
    # Langfuse public API authenticates with HTTP Basic (public:secret).
    assert request.headers["Authorization"].startswith("Basic ")


async def test_prompt_within_ttl_is_served_from_cache_without_refetching() -> None:
    provider, seen = _provider(_ok(), clock=_Clock())

    first = await provider.get_prompt(_PROMPT_NAME)
    second = await provider.get_prompt(_PROMPT_NAME)

    assert len(seen) == 1  # second call never left the process
    assert second.text == first.text
    assert second.source == "cache"


async def test_stale_cache_is_served_when_langfuse_fails_after_expiry() -> None:
    clock = _Clock()
    ok_handler = _ok("cached prompt")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return ok_handler(request)
        return _unavailable(request)

    provider, _seen = _provider(handler, clock=clock)

    await provider.get_prompt(_PROMPT_NAME)
    clock.now += 301  # past the 300s TTL
    stale = await provider.get_prompt(_PROMPT_NAME)

    assert stale.text == "cached prompt"
    assert stale.source == "cache"


async def test_bootstrap_template_is_used_when_langfuse_never_reachable() -> None:
    provider, seen = _provider(_unavailable)

    template = await provider.get_prompt(_PROMPT_NAME)

    assert len(seen) == 1
    assert template.source == "bootstrap"
    # The bootstrap copy must carry the placeholders the generator renders.
    assert "{{name}}" in template.text
    assert "{{description}}" in template.text


async def test_unconfigured_keys_skip_network_entirely() -> None:
    provider, seen = _provider(
        _unavailable,
        settings=_settings(langfuse_public_key="", langfuse_secret_key=""),
    )

    template = await provider.get_prompt(_PROMPT_NAME)

    assert seen == []  # no Langfuse credentials → no requests attempted
    assert template.source == "bootstrap"


async def test_unknown_prompt_name_without_bootstrap_fallback_raises() -> None:
    provider, _seen = _provider(_unavailable)

    with pytest.raises(PromptUnavailableError):
        await provider.get_prompt("some-future-prompt")


async def test_chat_format_prompt_from_langfuse_degrades_to_bootstrap() -> None:
    """Langfuse chat prompts return a message list; MVP wants text only.

    A list-shaped prompt must not crash the provider — the ladder falls
    through to the bootstrap copy so the generator still runs.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": _PROMPT_NAME,
                "version": 1,
                "prompt": [{"role": "user", "content": "hi {{name}}"}],
            },
        )

    provider, _seen = _provider(handler)
    template = await provider.get_prompt(_PROMPT_NAME)
    assert template.source == "bootstrap"
