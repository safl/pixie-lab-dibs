# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Things and the claims on them.

A **thing** is whatever people share and claim, mostly lab machines. The
things table is rebuilt from the sources on every sync (see
:mod:`dibs._sources`); dibs never edits it otherwise. **Claims** live in
a table of their own, keyed by thing name, so a claim outlives a sync in
which its source was unreachable or briefly forgot the thing.

A thing may have **parts**, named ``<thing>.<part>``, such as
``epyc-02.ssh`` and ``epyc-02.bmc``. Claiming the thing covers all of its
parts; claiming a part leaves the others free for someone else. So a
claim on a thing conflicts with another user's claim on any of its parts,
and a claim on a part conflicts with another user's claim on that part
or on the thing.

A claim is made by an **actor**: the user, and when an agent claims for
them, the agent (``assisted_by``) and its session. One actor cannot claim
over another's claim, even of the same user, so two agents of one person
do not collide unnoticed. Releasing is the same, except that the user
claiming by hand, without agent or session, may release any claim made
under their name: an agent answers to its human.

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
# label (lowercase a-z, 0-9 and -, at most 63 chars), optionally followed
# by one ".<part>" label. A hostname label has no dot, so the dot is free
# to mean "part of".
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
THING_NAME_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})?$")

PURPOSE_MAX = 128
ASSISTED_BY_MAX = 64

# Short, and plain, since it is typed into every command of a session.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

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
    expires_at   TEXT NOT NULL DEFAULT '',
    session      TEXT NOT NULL DEFAULT ''
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
            f"name {name!r} must be a hostname label (a-z / 0-9 / -, 1-63 chars, "
            "no leading or trailing -), optionally with one .part, e.g. epyc-02.bmc"
        )
    return name


def owner(name: str) -> str:
    """The thing a part belongs to; a thing is its own owner."""
    return name.split(".", 1)[0]


def overlaps(a: str, b: str) -> bool:
    """Whether claims on ``a`` and ``b`` cover any of the same thing: the
    same name, or a thing and one of its parts."""
    return a == b or a == owner(b) or b == owner(a)


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


def validate_session(raw: str) -> str:
    """Optional, so blank passes."""
    value = (raw or "").strip()
    if value and not SESSION_RE.match(value):
        raise ValueError(f"session {value!r} must be letters, digits, . _ or -, at most 32 chars")
    return value


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
    session: str = ""
    """Which of the holder's agent sessions made it; blank by hand."""

    @property
    def actor(self) -> tuple[str, str, str]:
        return (self.holder, self.assisted_by, self.session)

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
        if self.session:
            out["session"] = self.session
        if self.expires_at:
            out["expires_at"] = self.expires_at
        return out


class ClaimConflict(ValueError):
    """One or more things are claimed by another actor."""

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
    """One claim as it reads everywhere. The claim names the thing it is
    on, which for a part covered by its thing's claim is not ``name``."""
    if claim is None or not claim.is_live(now):
        return f"'{name}' is free"
    who = f"'{claim.holder}'"
    if claim.assisted_by:
        who += f" assisted by '{claim.assisted_by}'"
    if claim.session:
        who += f" (session {claim.session})"
    return (
        f"{who} has claimed '{claim.thing}' for '{claim.purpose}' "
        f"expires in: '{remaining(claim.expires_at, now)}'"
    )


def covering(name: str, claims: dict[str, Claim], now: datetime) -> builtins.list[Claim]:
    """The live claims that say who is on ``name``: its own claim, else
    its thing's claim for a part, else its parts' claims for a thing."""
    own = claims.get(name)
    if own is not None and own.is_live(now):
        return [own]
    if "." in name:
        whole = claims.get(owner(name))
        return [whole] if whole is not None and whole.is_live(now) else []
    return sorted(
        (c for c in claims.values() if owner(c.thing) == name and c.is_live(now)),
        key=lambda c: c.thing,
    )


def describe(name: str, claims: dict[str, Claim], now: datetime) -> str:
    """The sentence for ``name``, counting claims on its thing or parts."""
    found = covering(name, claims, now)
    if not found:
        return f"'{name}' is free"
    return "; ".join(sentence(c.thing, c, now) for c in found)


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
            if "session" not in cols:  # a database from before sessions
                conn.execute("ALTER TABLE claims ADD COLUMN session TEXT NOT NULL DEFAULT ''")

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
        session: str = "",
        expires_in: int | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> builtins.list[tuple[Claim | None, Claim]]:
        """Claim every thing in ``names`` for the actor ``holder`` /
        ``assisted_by`` / ``session``, or none of them: if any holds a live
        claim by another actor (and ``force`` is off) this raises
        :class:`ClaimConflict` and changes nothing. Claiming again as the
        same actor updates the purpose and expiry and keeps
        ``claimed_at``. ``expires_in`` is seconds from now; ``None`` never
        expires. Returns ``(before, after)`` pairs so the caller can log
        what a ``force`` took over; a forced claim on a thing also takes
        over other actors' claims on its parts, and those appear as pairs
        too. Callers check that the things exist."""
        purpose = validate_purpose(purpose)
        assisted_by = validate_assisted_by(assisted_by)
        session = validate_session(session)
        me = (holder, assisted_by, session)
        if expires_in is not None and expires_in <= 0:
            raise ValueError("expires_in must be a positive number of seconds")
        at = now or datetime.now(UTC)
        expires_at = ts(at + timedelta(seconds=expires_in)) if expires_in is not None else ""
        unique = list(dict.fromkeys(names))
        with _WRITE_LOCK, self._conn() as conn:
            before = {n: self._claim_row(conn, n) for n in unique}
            live = {n: c for n, c in before.items() if c is not None and c.is_live(at)}
            others = [c for c in self._live_rows(conn, at) if c.actor != me]
            blockers = [c for c in others if any(overlaps(c.thing, n) for n in unique)]
            if blockers and not force:
                raise ClaimConflict(blockers, at)
            out: builtins.list[tuple[Claim | None, Claim]] = []
            afters: dict[str, Claim] = {}
            for n in unique:
                prior = live.get(n)
                since = prior.claimed_at if prior and prior.actor == me else ts(at)
                after = afters[n] = Claim(
                    thing=n,
                    holder=holder,
                    purpose=purpose,
                    claimed_at=since,
                    assisted_by=assisted_by,
                    expires_at=expires_at,
                    session=session,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO claims "
                    "(thing, holder, assisted_by, purpose, claimed_at, expires_at, session) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (n, holder, assisted_by, purpose, since, expires_at, session),
                )
                out.append((prior, after))
            for b in blockers:
                if b.thing not in afters:
                    conn.execute("DELETE FROM claims WHERE thing = ?", (b.thing,))
                    covering_name = next(n for n in unique if overlaps(b.thing, n))
                    out.append((b, afters[covering_name]))
        return out

    def release(
        self,
        names: Sequence[str],
        *,
        holder: str,
        assisted_by: str = "",
        session: str = "",
        force: bool = False,
        now: datetime | None = None,
    ) -> builtins.list[Claim]:
        """Release the claims on ``names``, all or nothing like
        :meth:`claim`. Without ``force`` an actor may release its own
        claims, and the user by hand (no agent, no session) may also
        release its agents'. Expired claims are cleared whoever holds
        them. Returns the live claims released."""
        at = now or datetime.now(UTC)
        unique = list(dict.fromkeys(names))
        me = (holder, assisted_by.strip(), session.strip())
        by_hand = not me[1] and not me[2]

        def mine(c: Claim) -> bool:
            return c.actor == me or (by_hand and c.holder == holder)

        with _WRITE_LOCK, self._conn() as conn:
            before = [c for c in (self._claim_row(conn, n) for n in unique) if c is not None]
            blockers = [c for c in before if c.is_live(at) and not mine(c)]
            if blockers and not force:
                raise ClaimConflict(blockers, at)
            conn.executemany("DELETE FROM claims WHERE thing = ?", [(c.thing,) for c in before])
        return [c for c in before if c.is_live(at)]

    def claimed_by(
        self,
        holder: str,
        assisted_by: str = "",
        session: str = "",
        *,
        now: datetime | None = None,
    ) -> builtins.list[Claim]:
        """Live claims of exactly this actor, so an agent's ``release
        --mine`` leaves its human's and its other sessions' claims alone."""
        at = now or datetime.now(UTC)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE holder = ? AND assisted_by = ? AND session = ? "
                "ORDER BY thing",
                (holder, assisted_by.strip(), session.strip()),
            ).fetchall()
        return [c for c in (Claim(**dict(r)) for r in rows) if c.is_live(at)]

    @staticmethod
    def _live_rows(conn: sqlite3.Connection, at: datetime) -> builtins.list[Claim]:
        rows = conn.execute("SELECT * FROM claims").fetchall()
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
