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
    [(_, c)] = store.claim(
        ["node-02"], holder="alice", purpose="x", assisted_by=AGENT, session="s1", now=T0
    )
    assert sentence("node-02", c, T0).startswith(
        f"'alice' assisted by '{AGENT}' (session s1) has claimed 'node-02'"
    )


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


def test_two_agent_sessions_of_one_user_conflict(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", assisted_by=AGENT, session="s1", now=T0)
    with pytest.raises(ClaimConflict) as exc:
        store.claim(["a"], holder="alice", purpose="y", assisted_by=AGENT, session="s2", now=T0)
    assert "(session s1)" in exc.value.sentences[0]
    with pytest.raises(ClaimConflict):
        store.release(["a"], holder="alice", assisted_by=AGENT, session="s2", now=T0)
    # The same session claiming again just updates its claim.
    store.claim(["a"], holder="alice", purpose="z", assisted_by=AGENT, session="s1", now=T0)
    assert store.get_claim("a").purpose == "z"  # type: ignore[union-attr]


def test_an_agent_cannot_take_or_release_its_humans_claim(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", now=T0)
    with pytest.raises(ClaimConflict):
        store.claim(["a"], holder="alice", purpose="y", assisted_by=AGENT, session="s1", now=T0)
    with pytest.raises(ClaimConflict):
        store.release(["a"], holder="alice", assisted_by=AGENT, session="s1", now=T0)


def test_the_human_may_release_their_agents_claims(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", assisted_by=AGENT, session="s1", now=T0)
    # Claiming over it still needs force, so nothing is overwritten unnoticed...
    with pytest.raises(ClaimConflict):
        store.claim(["a"], holder="alice", purpose="y", now=T0)
    # ...but cleaning up after one's own agent does not.
    assert [c.thing for c in store.release(["a"], holder="alice", now=T0)] == ["a"]


def test_another_users_human_may_not_release_my_agents_claim(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", assisted_by=AGENT, session="s1", now=T0)
    with pytest.raises(ClaimConflict):
        store.release(["a"], holder="bob", now=T0)


def test_old_database_gains_sessions(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE claims (thing TEXT PRIMARY KEY, holder TEXT NOT NULL, "
        "assisted_by TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL, claimed_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL DEFAULT '');"
        "INSERT INTO claims (thing, holder, purpose, claimed_at) VALUES ('a', 'alice', 'x', 'y');"
    )
    conn.commit()
    conn.close()
    assert Store(db).get_claim("a").session == ""  # type: ignore[union-attr]


@pytest.mark.parametrize("raw", ["-x", "a b", "x" * 33, "a/b"])
def test_bad_sessions_are_refused(store: Store, raw: str) -> None:
    with pytest.raises(ValueError):
        store.claim(["a"], holder="alice", purpose="x", session=raw, now=T0)


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


def test_claimed_by_is_scoped_to_the_actor(store: Store) -> None:
    store.claim(["a"], holder="alice", purpose="x", now=T0)
    store.claim(["b"], holder="alice", purpose="y", assisted_by=AGENT, session="s1", now=T0)
    store.claim(["d"], holder="alice", purpose="y", assisted_by=AGENT, session="s2", now=T0)
    store.claim(["c"], holder="alice", purpose="z", expires_in=60, now=T0 - timedelta(hours=1))
    assert [c.thing for c in store.claimed_by("alice", now=T0)] == ["a"]
    assert [c.thing for c in store.claimed_by("alice", AGENT, "s1", now=T0)] == ["b"]


@pytest.mark.parametrize(
    ("purpose", "assisted_by"), [("", ""), ("two\nlines", ""), ("x", "\x1b[31m"), ("x" * 129, "")]
)
def test_claim_rejects_bad_text(store: Store, purpose: str, assisted_by: str) -> None:
    with pytest.raises(ValueError):
        store.claim(["a"], holder="alice", purpose=purpose, assisted_by=assisted_by, now=T0)


# ---- parts ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["epyc-02", "epyc-02.bmc", "a.b"])
def test_part_names_are_accepted(name: str) -> None:
    from dibs._store import validate_thing_name

    assert validate_thing_name(name) == name


@pytest.mark.parametrize("name", ["epyc-02.", ".bmc", "a.b.c", "a..b", "a.-b"])
def test_bad_part_names_are_refused(name: str) -> None:
    from dibs._store import validate_thing_name

    with pytest.raises(ValueError):
        validate_thing_name(name)


def test_two_users_may_hold_different_parts(store: Store) -> None:
    store.claim(["box.ssh"], holder="alice", purpose="x", now=T0)
    store.claim(["box.bmc"], holder="bob", purpose="y", now=T0)
    assert {n: c.holder for n, c in store.list_claims().items()} == {
        "box.ssh": "alice",
        "box.bmc": "bob",
    }


def test_the_whole_thing_conflicts_with_another_users_part(store: Store) -> None:
    store.claim(["box.bmc"], holder="bob", purpose="y", now=T0)
    with pytest.raises(ClaimConflict) as exc:
        store.claim(["box"], holder="alice", purpose="x", now=T0)
    assert exc.value.sentences == ["'bob' has claimed 'box.bmc' for 'y' expires in: 'never'"]


def test_a_part_conflicts_with_another_users_whole_thing(store: Store) -> None:
    store.claim(["box"], holder="bob", purpose="y", now=T0)
    with pytest.raises(ClaimConflict):
        store.claim(["box.ssh"], holder="alice", purpose="x", now=T0)
    # The same user holding the whole thing may also claim a part of it.
    store.claim(["box.ssh"], holder="bob", purpose="z", now=T0)


def test_parts_of_other_things_do_not_conflict(store: Store) -> None:
    store.claim(["box.bmc"], holder="bob", purpose="y", now=T0)
    store.claim(["boxer", "boxer.bmc"], holder="alice", purpose="x", now=T0)


def test_forcing_the_whole_thing_takes_over_its_parts(store: Store) -> None:
    store.claim(["box.bmc"], holder="bob", purpose="y", now=T0)
    pairs = store.claim(["box"], holder="alice", purpose="x", force=True, now=T0)
    taken = [(b.thing, b.holder, a.thing) for b, a in pairs if b is not None]
    assert taken == [("box.bmc", "bob", "box")]
    assert set(store.list_claims()) == {"box"}


def test_describe_reads_through_parts_and_wholes(store: Store) -> None:
    from dibs._store import describe

    store.claim(["box.bmc"], holder="bob", purpose="power", now=T0)
    claims = store.list_claims()
    assert (
        describe("box", claims, T0) == "'bob' has claimed 'box.bmc' for 'power' expires in: 'never'"
    )
    assert describe("box.ssh", claims, T0) == "'box.ssh' is free"
    store.release(["box.bmc"], holder="bob", now=T0)
    store.claim(["box"], holder="alice", purpose="all", now=T0)
    claims = store.list_claims()
    assert (
        describe("box.ssh", claims, T0) == "'alice' has claimed 'box' for 'all' expires in: 'never'"
    )
