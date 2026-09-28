"""Tests for the OIDC login state store (T08 / #46).

`OIDCStateStore` is the bridge between `GET /auth/sso/login` and
`POST /auth/sso/callback`. It must:

* Remember the (nonce, code_verifier) bundle for an issued `state`.
* Consume the entry on `take(...)` so a replayed callback fails
  (`OIDCStateMismatchError`).
* Reject expired entries (lazy on `take` — no sweeper thread).
* Allow `put(...)` to overwrite (the same `state` shouldn't exist
  twice in practice, but the store must not crash on the rare
  collision).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.auth.errors import OIDCStateMismatchError
from app.auth.login import OIDCLoginEntry, OIDCStateStore


def _entry(state: str = "st-1", *, age: float = 0.0) -> OIDCLoginEntry:
    """Build a state entry with a controllable `created_at` offset."""
    return OIDCLoginEntry(
        state=state,
        nonce="nonce-" + state,
        code_verifier="verifier-" + state,
        created_at=time.monotonic() - age,
    )


class TestStateStoreBasicOps:
    """`put` and `take` round-trip; concurrent takes fail the second caller."""

    async def test_put_then_take_round_trips(self) -> None:
        store = OIDCStateStore(ttl_seconds=60)
        entry = _entry()
        await store.put(entry)
        taken = await store.take(entry.state)
        assert taken is entry

    async def test_take_is_single_use(self) -> None:
        """A second `take` for the same state raises."""
        store = OIDCStateStore(ttl_seconds=60)
        entry = _entry()
        await store.put(entry)
        await store.take(entry.state)
        with pytest.raises(OIDCStateMismatchError) as exc:
            await store.take(entry.state)
        assert exc.value.code == "oidc_state_mismatch"

    async def test_take_unknown_state_raises(self) -> None:
        store = OIDCStateStore(ttl_seconds=60)
        with pytest.raises(OIDCStateMismatchError) as exc:
            await store.take("never-issued")
        assert exc.value.code == "oidc_state_mismatch"
        assert exc.value.details == {"state_prefix": "never-is"}

    async def test_put_overwrites_same_state(self) -> None:
        """A duplicate `state` replaces the prior entry."""
        store = OIDCStateStore(ttl_seconds=60)
        first = _entry("st-overlap")
        second = OIDCLoginEntry(
            state="st-overlap",
            nonce="nonce-new",
            code_verifier="verifier-new",
            created_at=time.monotonic(),
        )
        await store.put(first)
        await store.put(second)
        assert (await store.take("st-overlap")) is second

    async def test_len_reports_entry_count(self) -> None:
        store = OIDCStateStore(ttl_seconds=60)
        assert len(store) == 0
        await store.put(_entry("a"))
        await store.put(_entry("b"))
        assert len(store) == 2
        await store.take("a")
        assert len(store) == 1


class TestStateStoreTTL:
    """Lazy TTL on `take` — entries past their window raise."""

    async def test_take_past_ttl_raises(self) -> None:
        """An entry whose `created_at` is older than the TTL is rejected."""
        # TTL of 5 seconds, entry aged 10 seconds.
        store = OIDCStateStore(ttl_seconds=5)
        await store.put(_entry(age=10.0))
        with pytest.raises(OIDCStateMismatchError) as exc:
            await store.take("st-1")
        assert exc.value.code == "oidc_state_mismatch"
        assert exc.value.details == {"state_prefix": "st-1", "reason": "expired"}

    async def test_take_at_ttl_boundary_just_inside_window(self) -> None:
        """An entry `ttl - 0.1` old is still fresh enough to succeed."""
        store = OIDCStateStore(ttl_seconds=5)
        await store.put(_entry(age=4.9))
        taken = await store.take("st-1")
        assert taken.state == "st-1"

    async def test_purge_expired_drops_only_stale(self) -> None:
        """`purge_expired` cleans entries past their TTL, keeps fresh ones."""
        store = OIDCStateStore(ttl_seconds=5)
        await store.put(_entry("fresh", age=1.0))
        await store.put(_entry("stale", age=10.0))
        purged = await store.purge_expired()
        assert purged == 1
        assert len(store) == 1
        # The fresh entry is still there.
        fresh = await store.take("fresh")
        assert fresh.state == "fresh"

    async def test_zero_ttl_disables_caching(self) -> None:
        """`ttl_seconds=0` means every `take` raises as expired."""
        store = OIDCStateStore(ttl_seconds=0)
        await store.put(_entry())
        with pytest.raises(OIDCStateMismatchError):
            await store.take("st-1")

    async def test_zero_ttl_disables_purge(self) -> None:
        """`purge_expired` is a no-op when TTL is zero."""
        store = OIDCStateStore(ttl_seconds=0)
        await store.put(_entry(age=1_000.0))
        assert await store.purge_expired() == 0


class TestStateStoreConcurrency:
    """Concurrent `take`s for the same state — exactly one wins."""

    async def test_concurrent_takes_yield_one_success(self) -> None:
        store = OIDCStateStore(ttl_seconds=60)
        await store.put(_entry())
        results = await asyncio.gather(
            store.take("st-1"),
            store.take("st-1"),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, OIDCLoginEntry)]
        errors = [r for r in results if isinstance(r, OIDCStateMismatchError)]
        assert len(successes) == 1
        assert len(errors) == 1