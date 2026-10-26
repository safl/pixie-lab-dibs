# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Users, their login tokens, and the one-time login challenges.

A user registers a name and an SSH public key, an admin approves them
(``dibs admin approve``), and the user then logs in by signing a
one-time challenge with that key to get a token. Tokens are
stored as SHA-256 hashes, so a copy of the database does not hand out
working tokens. A token does not expire; it lasts until the user logs
out, an admin revokes it, or the user is removed.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dibs._sshsig import parse_pubkey, verify

_WRITE_LOCK = threading.Lock()

# Lowercase, like machine names, and safe to write as an
# ``allowed_signers`` principal (no spaces, quotes or commas).
USER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

CHALLENGE_TTL = timedelta(seconds=60)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name           TEXT PRIMARY KEY,
    pubkey         TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    status         TEXT NOT NULL,
    registered_at  TEXT NOT NULL,
    approved_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS user_tokens (
    token_sha256   TEXT PRIMARY KEY,
    user           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_challenges (
    nonce          TEXT PRIMARY KEY,
    user           TEXT NOT NULL,
    expires_at     TEXT NOT NULL
);
"""

PENDING = "pending"
APPROVED = "approved"


class DuplicateUser(ValueError):
    pass


class NotApproved(PermissionError):
    pass


class LoginFailed(PermissionError):
    pass


@dataclass
class User:
    name: str
    pubkey: str
    fingerprint: str
    status: str
    registered_at: str
    approved_at: str
    tokens: int = 0


def validate_user_name(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not USER_NAME_RE.match(name):
        raise ValueError(
            f"user name {name!r} must be a-z / 0-9 / . _ -, start with a letter or digit, "
            "and be at most 32 chars"
        )
    return name


def login_message(name: str, nonce: str) -> str:
    """What the client signs: the purpose and the user as well as the
    nonce, so the signed text says what it authorises. Moving a login
    between users is already ruled out by each challenge belonging to
    one user (see :meth:`UsersStore.login`)."""
    return f"dibs-login {name} {nonce}"


def _ts(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UsersStore:
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

    def _user(self, conn: sqlite3.Connection, name: str) -> User | None:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        tokens = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE user = ?", (name,)
        ).fetchone()[0]
        return User(**dict(row), tokens=tokens)

    def get(self, name: str) -> User | None:
        with self._conn() as conn:
            return self._user(conn, (name or "").strip().lower())

    def list_users(self) -> list[User]:
        with self._conn() as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM users ORDER BY name")]
            return [u for u in (self._user(conn, n) for n in names) if u is not None]

    def register(self, name: str, pubkey: str, *, now: datetime | None = None) -> User:
        """Add a pending user. Raises :class:`DuplicateUser` when the
        name is taken, pending or not."""
        name = validate_user_name(name)
        key, fingerprint = parse_pubkey(pubkey)
        at = _ts(now or datetime.now(UTC))
        with _WRITE_LOCK, self._conn() as conn:
            if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise DuplicateUser(f"user {name!r} is already registered")
            conn.execute(
                "INSERT INTO users (name, pubkey, fingerprint, status, registered_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, key, fingerprint, PENDING, at),
            )
            user = self._user(conn, name)
        assert user is not None
        return user

    def approve(self, name: str, *, now: datetime | None = None) -> User:
        at = _ts(now or datetime.now(UTC))
        with _WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET status = ?, approved_at = ? WHERE name = ? AND status = ?",
                (APPROVED, at, name, PENDING),
            )
            if cur.rowcount == 0:
                raise LookupError(f"no pending user {name!r}")
            user = self._user(conn, name)
        assert user is not None
        return user

    def remove(self, name: str) -> bool:
        """Delete a user, pending or approved, with all their tokens."""
        with _WRITE_LOCK, self._conn() as conn:
            conn.execute("DELETE FROM user_tokens WHERE user = ?", (name,))
            conn.execute("DELETE FROM login_challenges WHERE user = ?", (name,))
            return conn.execute("DELETE FROM users WHERE name = ?", (name,)).rowcount > 0

    def revoke_tokens(self, name: str) -> int:
        with _WRITE_LOCK, self._conn() as conn:
            return conn.execute("DELETE FROM user_tokens WHERE user = ?", (name,)).rowcount

    def challenge(self, name: str, *, now: datetime | None = None) -> tuple[str, str]:
        """``(nonce, message)`` for an approved user to sign. Raises
        :class:`LookupError` for an unknown user, :class:`NotApproved`
        for a pending one."""
        name = validate_user_name(name)
        at = now or datetime.now(UTC)
        nonce = secrets.token_urlsafe(32)
        with _WRITE_LOCK, self._conn() as conn:
            user = self._user(conn, name)
            if user is None:
                raise LookupError(f"no user {name!r}; register first")
            if user.status != APPROVED:
                raise NotApproved(f"user {name!r} is waiting for admin approval")
            conn.execute("DELETE FROM login_challenges WHERE expires_at <= ?", (_ts(at),))
            conn.execute(
                "INSERT INTO login_challenges (nonce, user, expires_at) VALUES (?, ?, ?)",
                (nonce, name, _ts(at + CHALLENGE_TTL)),
            )
        return nonce, login_message(name, nonce)

    def login(self, name: str, nonce: str, signature: str, *, now: datetime | None = None) -> str:
        """Check ``signature`` over the challenge and return a new token.
        The challenge is used up by the attempt, good or bad, so a
        captured signature cannot be replayed. Raises
        :class:`LoginFailed` with a reason the caller may show."""
        name = validate_user_name(name)
        at = _ts(now or datetime.now(UTC))
        with _WRITE_LOCK, self._conn() as conn:
            row = conn.execute(
                "DELETE FROM login_challenges WHERE nonce = ? AND user = ? RETURNING expires_at",
                (nonce, name),
            ).fetchone()
            user = self._user(conn, name)
        if row is None or row["expires_at"] <= at:
            raise LoginFailed("challenge unknown, used or expired; log in again")
        if user is None or user.status != APPROVED:
            raise LoginFailed(f"user {name!r} is not approved")
        message = login_message(name, nonce)
        if not verify(principal=name, key=user.pubkey, message=message, signature=signature):
            raise LoginFailed(f"signature does not match the key registered for {name!r}")
        token = "dibs_" + secrets.token_urlsafe(32)
        with _WRITE_LOCK, self._conn() as conn:
            conn.execute(
                "INSERT INTO user_tokens (token_sha256, user, created_at) VALUES (?, ?, ?)",
                (_hash(token), name, at),
            )
        return token

    def user_for_token(self, token: str) -> str | None:
        if not token:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT t.user FROM user_tokens t JOIN users u ON u.name = t.user "
                "WHERE t.token_sha256 = ? AND u.status = ?",
                (_hash(token), APPROVED),
            ).fetchone()
        return row[0] if row else None

    def logout(self, token: str) -> bool:
        with _WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM user_tokens WHERE token_sha256 = ?", (_hash(token),))
            return cur.rowcount > 0
