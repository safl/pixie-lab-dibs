# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Claims: the sentence, all-or-nothing claiming, ownership by user,
expiry, and release."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dibs._store import ClaimConflict, Store, remaining, sentence, ts

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
AGENT = "Claude Code:claude-opus-5-5"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "dibs.db")


def test_sentence_free_claimed_and_assisted(store: Store) -> None:
    assert sentence("node-01", None, T0) == "'node-01' is free"
    [(_, c)] = store.claim(["node-01"], holder="alice", purpose="xnvme", now=T0)
    assert sentence("node-01", c, T0) == (
        "'alice' has claimed 'node-01' for 'xnvme' expires in: 'never'"
    )
    [(_, c)] = store.claim(["node-01"], holder="alice", purpose="x", assisted_by=AGENT, now=T0)
    assert sentence("node-01", c, T0).startswith(f"'alice' assisted by '{AGENT}' has claimed")


@pytest.mark.parametrize(
    ("delta", "want"),
    [
        (timedelta(0), "<1m"),
        (timedelta(seconds=30), "1m"),
        (timedelta(hours=1, minutes=59, seconds=59), "2h"),
        (timedelta(hours=1, minutes=12, seconds=5), "1h 13m"),
        (timedelta(days=2, hours=3, minutes=9), "2d 3h"),
    ],
)
def test_remaining_rounds_up_to_two_units(delta: timedelta, want: str) -> None:
    assert remaining(ts(T0 + delta), T0) == want


def test_reclaim_by_same_user_keeps_since(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="xnvme", now=T0)
    later = T0 + timedelta(hours=1)
    [(_, c)] = store.claim(["a"], holder="alice", purpose="upcie", expires_in=600, now=later)
    assert (c.purpose, c.claimed_at, c.expires_at) == (
        "upcie",
        "2026-10-26T12:00:00Z",
        "2026-10-26T13:10:00Z",
    )


def test_claim_is_all_or_nothing(store: Store) -> None:
    store.claim(["b"], holder="bob", purpose="ggml", now=T0)
    with pytest.raises(ClaimConflict) as exc:
        store.claim(["a", "b"], holder="alice", purpose="xnvme", now=T0)
    assert exc.value.sentences == ["'bob' has claimed 'b' for 'ggml' expires in: 'never'"]
    assert store.get_claim("a") is None


def test_an_agent_and_its_human_do_not_block_each_other(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", now=T0)
    store.claim(["a"], holder="alice", purpose="y", assisted_by=AGENT, now=T0)
    store.release(["a"], holder="alice", now=T0)
    assert store.get_claim("a") is None


def test_force_takeover_reports_previous_holder(store: Store) -> None:
    store.claim(["b"], holder="bob", purpose="ggml", now=T0)
    [(before, after)] = store.claim(["b"], holder="alice", purpose="x", force=True, now=T0)
    assert before is not None and (before.holder, after.holder) == ("bob", "alice")


def test_expired_claim_is_free(store: Store) -> None:
    store.claim(["a"], holder="bob", purpose="ggml", expires_in=60, now=T0)
    later = T0 + timedelta(minutes=2)
    assert sentence("a", store.get_claim("a"), later) == "'a' is free"
    store.claim(["a"], holder="alice", purpose="x", now=later)
    assert store.get_claim("a").holder == "alice"  # type: ignore[union-attr]


def test_release_rules(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", now=T0)
    store.claim(["b"], holder="bob", purpose="y", now=T0)
    with pytest.raises(ClaimConflict):
        store.release(["a", "b"], holder="alice", now=T0)
    assert store.get_claim("a") is not None
    assert [c.thing for c in store.release(["a"], holder="alice", now=T0)] == ["a"]
    assert store.release(["a"], holder="alice", now=T0) == []
    store.release(["b"], holder="alice", force=True, now=T0)
    assert store.get_claim("b") is None


def test_claimed_by_is_scoped_to_assisted_by(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", now=T0)
    store.claim(["b"], holder="alice", purpose="y", assisted_by=AGENT, now=T0)
    store.claim(["c"], holder="alice", purpose="z", expires_in=60, now=T0 - timedelta(hours=1))
    assert [c.thing for c in store.claimed_by("alice", now=T0)] == ["a"]
    assert [c.thing for c in store.claimed_by("alice", AGENT, now=T0)] == ["b"]


@pytest.mark.parametrize(
    ("purpose", "assisted_by"), [("", ""), ("two\nlines", ""), ("x", "\x1b[31m"), ("x" * 129, "")]
)
def test_claim_rejects_bad_text(store: Store, purpose: str, assisted_by: str) -> None:
    with pytest.raises(ValueError):
        store.claim(["a"], holder="alice", purpose=purpose, assisted_by=assisted_by, now=T0)
