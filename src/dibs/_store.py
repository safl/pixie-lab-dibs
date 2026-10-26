# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Things and the claims on them.

A **thing** is whatever people share and claim, mostly lab machines. The
things table is rebuilt from the sources on every sync (see
:mod:`dibs._sources`); dibs never edits it otherwise. **Claims** live in
a table of their own, keyed by thing name, so a claim outlives a sync in
which its source was unreachable or briefly forgot the thing.

Every surface (``dibs``, the web page, the login hook on a machine)
prints a claim as the same sentence, from :func:`sentence`.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import re
import sqlite3
import threading
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()

# What people type and put in configs next to the hostname, so a host
# label: lowercase a-z, 0-9 and -, at most 63 chars.
THING_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

PURPOSE_MAX = 128
ASSISTED_BY_MAX = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS things (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    source     TEXT NOT NULL,
    attrs      TEXT NOT NULL DEFAULT '{}',
    synced_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    thing        TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    assisted_by  TEXT NOT NULL DEFAULT '',
    purpose      TEXT NOT NULL,
    claimed_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL DEFAULT ''
);
"""


def ts(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def validate_thing_name(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not THING_NAME_RE.match(name):
        raise ValueError(
            f"name {name!r} must be a hostname label: a-z / 0-9 / -, "
            "1-63 chars, no leading or trailing -"
        )
    return name


def _one_line(field_name: str, raw: str, limit: int) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field_name} must be at most {limit} chars")
    # Printed into other people's terminals by the login hook, so no
    # control characters (an ESC would let a claim restyle the terminal).
    if not value.isprintable():
        raise ValueError(f"{field_name} must be a single line of printable text")
    return value


def validate_purpose(raw: str) -> str:
    return _one_line("purpose", raw, PURPOSE_MAX)


def validate_assisted_by(raw: str) -> str:
    """Optional, so blank passes."""
    return _one_line("assisted_by", raw, ASSISTED_BY_MAX) if (raw or "").strip() else ""


@dataclass
class Thing:
    name: str
    kind: str
    source: str
    """``config``, or ``pixie:<url>`` for a thing a pixie server reported."""
    attrs: dict[str, Any] = field(default_factory=dict)
    synced_at: str = ""


@dataclass
class Claim:
    thing: str
    holder: str
    """The dibs user who holds the claim."""
    purpose: str
    claimed_at: str
    assisted_by: str = ""
    """The agent the holder claimed through, in ``Agent:model`` form like
    an ``Assisted-by:`` trailer; blank for a claim made by hand."""
    expires_at: str = ""
    """Blank for a claim that never expires."""

    def is_live(self, now: datetime) -> bool:
        """A claim past its expiry counts as no claim: the thing is free
        to take without ``force``, and nothing has to sweep it up."""
        return not self.expires_at or parse_ts(self.expires_at) > now

    def to_dict(self) -> dict[str, str]:
        out = {
            "holder": self.holder,
            "purpose": self.purpose,
            "claimed_at": self.claimed_at,
        }
        if self.assisted_by:
            out["assisted_by"] = self.assisted_by
        if self.expires_at:
            out["expires_at"] = self.expires_at
        return out


class ClaimConflict(ValueError):
    """One or more things are claimed by another user."""

    def __init__(self, blockers: Sequence[Claim], now: datetime) -> None:
        self.blockers = list(blockers)
        self.sentences = [sentence(c.thing, c, now) for c in blockers]
        super().__init__("; ".join(self.sentences))


def remaining(expires_at: str, now: datetime) -> str:
    """``never`` or the time left as ``2d 3h`` / ``1h 12m`` / ``4m``, to
    the two largest units. Rounds up to the minute, so a claim made for
    ``2h`` reads ``2h`` rather than ``1h 59m`` a second later."""
    if not expires_at:
        return "never"
    secs = max(0, int((parse_ts(expires_at) - now).total_seconds()))
    days, rest = divmod(-(-secs // 60), 1440)
    hours, minutes = divmod(rest, 60)
    parts = [f"{n}{u}" for n, u in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    return " ".join(parts[:2]) or "<1m"


def sentence(name: str, claim: Claim | None, now: datetime) -> str:
    if claim is None or not claim.is_live(now):
        return f"'{name}' is free"
    who = f"'{claim.holder}'"
    if claim.assisted_by:
        who += f" assisted by '{claim.assisted_by}'"
    return (
        f"{who} has claimed '{name}' for '{claim.purpose}' "
        f"expires in: '{remaining(claim.expires_at, now)}'"
    )


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- things -----------------------------------------------------

    def replace_things(self, things: Sequence[Thing]) -> None:
        """Make ``things`` the whole list, in one transaction."""
        with _WRITE_LOCK, self._conn() as conn:
            conn.execute("DELETE FROM things")
            conn.executemany(
                "INSERT INTO things (name, kind, source, attrs, synced_at) VALUES (?, ?, ?, ?, ?)",
                [(t.name, t.kind, t.source, json.dumps(t.attrs), t.synced_at) for t in things],
            )

    def list_things(self) -> builtins.list[Thing]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM things ORDER BY name").fetchall()
        return [_thing(r) for r in rows]

    def get_thing(self, name: str) -> Thing | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM things WHERE name = ?", ((name or "").strip().lower(),)
            ).fetchone()
        return _thing(row) if row else None

    def thing_by_mac(self, macs: Sequence[str]) -> Thing | None:
        wanted = {m.strip().lower() for m in macs if m.strip()}
        for thing in self.list_things():
            if str(thing.attrs.get("mac", "")).lower() in wanted:
                return thing
        return None

    # ---- claims -----------------------------------------------------

    def get_claim(self, name: str) -> Claim | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM claims WHERE thing = ?", (name,)).fetchone()
        return Claim(**dict(row)) if row else None

    def list_claims(self) -> dict[str, Claim]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM claims").fetchall()
        return {r["thing"]: Claim(**dict(r)) for r in rows}

    def claim(
        self,
        names: Sequence[str],
        *,
        holder: str,
        purpose: str,
        assisted_by: str = "",
        expires_in: int | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> builtins.list[tuple[Claim | None, Claim]]:
        """Claim every thing in ``names`` for ``holder``, or none of them:
        if any holds a live claim by another user (and ``force`` is off)
        this raises :class:`ClaimConflict` and changes nothing.
        Ownership is the user's: claiming again as the same holder, with
        or without an agent, updates the purpose, expiry and
        ``assisted_by`` and keeps ``claimed_at``. ``expires_in`` is
        seconds from now; ``None`` never expires. Returns ``(before,
        after)`` pairs so the caller can log what a ``force`` took over.
        Callers check that the things exist."""
        purpose = validate_purpose(purpose)
        assisted_by = validate_assisted_by(assisted_by)
        if expires_in is not None and expires_in <= 0:
            raise ValueError("expires_in must be a positive number of seconds")
        at = now or datetime.now(UTC)
        expires_at = ts(at + timedelta(seconds=expires_in)) if expires_in is not None else ""
        unique = list(dict.fromkeys(names))
        with _WRITE_LOCK, self._conn() as conn:
            before = {n: self._claim_row(conn, n) for n in unique}
            live = {n: c for n, c in before.items() if c is not None and c.is_live(at)}
            blockers = [c for c in live.values() if c.holder != holder]
            if blockers and not force:
                raise ClaimConflict(blockers, at)
            out: builtins.list[tuple[Claim | None, Claim]] = []
            for n in unique:
                prior = live.get(n)
                since = prior.claimed_at if prior and prior.holder == holder else ts(at)
                after = Claim(n, holder, purpose, since, assisted_by, expires_at)
                conn.execute(
                    "INSERT OR REPLACE INTO claims "
                    "(thing, holder, assisted_by, purpose, claimed_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (n, holder, assisted_by, purpose, since, expires_at),
                )
                out.append((prior, after))
        return out

    def release(
        self,
        names: Sequence[str],
        *,
        holder: str,
        force: bool = False,
        now: datetime | None = None,
    ) -> builtins.list[Claim]:
        """Release the claims on ``names``, all or nothing like
        :meth:`claim`: releasing another user's live claim needs
        ``force``. Expired claims are cleared whoever holds them. Returns
        the live claims released."""
        at = now or datetime.now(UTC)
        unique = list(dict.fromkeys(names))
        with _WRITE_LOCK, self._conn() as conn:
            before = [c for c in (self._claim_row(conn, n) for n in unique) if c is not None]
            blockers = [c for c in before if c.is_live(at) and c.holder != holder]
            if blockers and not force:
                raise ClaimConflict(blockers, at)
            conn.executemany("DELETE FROM claims WHERE thing = ?", [(c.thing,) for c in before])
        return [c for c in before if c.is_live(at)]

    def claimed_by(
        self, holder: str, assisted_by: str = "", *, now: datetime | None = None
    ) -> builtins.list[Claim]:
        """Live claims held by ``holder`` through exactly ``assisted_by``
        (blank: claimed by hand), so an agent's ``release --mine`` leaves
        its human's own claims alone."""
        at = now or datetime.now(UTC)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE holder = ? AND assisted_by = ? ORDER BY thing",
                (holder, assisted_by.strip()),
            ).fetchall()
        return [c for c in (Claim(**dict(r)) for r in rows) if c.is_live(at)]

    @staticmethod
    def _claim_row(conn: sqlite3.Connection, name: str) -> Claim | None:
        row = conn.execute("SELECT * FROM claims WHERE thing = ?", (name,)).fetchone()
        return Claim(**dict(row)) if row else None


def _thing(r: sqlite3.Row) -> Thing:
    try:
        attrs = json.loads(r["attrs"] or "{}")
    except ValueError:
        attrs = {}
    return Thing(r["name"], r["kind"], r["source"], attrs, r["synced_at"])
