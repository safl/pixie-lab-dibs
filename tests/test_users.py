# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Users and SSH-key login: what the protocol must refuse."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dibs._users import LoginFailed, UsersStore
from tests.conftest import login_user, sign


def _pub(key: Path) -> str:
    return key.with_suffix(".pub").read_text()


@pytest.fixture
def other_key(tmp_path: Path) -> Path:
    key = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


def _challenge(client: TestClient, user: str) -> dict[str, str]:
    r = client.post("/auth/challenge", json={"user": user})
    assert r.status_code == 200, r.text
    result: dict[str, str] = r.json()
    return result


def _login(client: TestClient, user: str, nonce: str, signature: str) -> Any:
    return client.post("/auth/login", json={"user": user, "nonce": nonce, "signature": signature})


def test_register_validates_and_refuses_duplicates(client: TestClient, ssh_key: Path) -> None:
    r = client.post("/auth/register", json={"user": "Alice", "pubkey": _pub(ssh_key)})
    assert r.status_code == 202
    assert r.json()["user"] == "alice" and r.json()["status"] == "pending"
    assert r.json()["fingerprint"].startswith("SHA256:")
    again = client.post("/auth/register", json={"user": "alice", "pubkey": _pub(ssh_key)})
    assert again.status_code == 409
    for body in (
        {"user": "-bad", "pubkey": _pub(ssh_key)},
        {"user": "bob", "pubkey": "not a key"},
        {"user": "bob", "pubkey": "ssh-ed25519 AAAAnotbase64"},
        {"user": "bob", "pubkey": _pub(ssh_key) + _pub(ssh_key)},
    ):
        assert client.post("/auth/register", json=body).status_code == 422, body


def test_pending_or_unknown_user_cannot_log_in(client: TestClient, ssh_key: Path) -> None:
    client.post("/auth/register", json={"user": "alice", "pubkey": _pub(ssh_key)})
    r = client.post("/auth/challenge", json={"user": "alice"})
    assert r.status_code == 403
    assert "waiting for admin approval" in r.json()["detail"]
    assert client.post("/auth/challenge", json={"user": "nobody"}).status_code == 404


def test_login_issues_a_token_that_whoami_accepts(client: TestClient, ssh_key: Path) -> None:
    token = login_user(client, ssh_key, "alice")
    assert token.startswith("dibs_")
    r = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.json() == {"user": "alice"}
    assert client.get("/auth/whoami").status_code == 401


def test_a_challenge_is_single_use(client: TestClient, ssh_key: Path) -> None:
    login_user(client, ssh_key, "alice")
    ch = _challenge(client, "alice")
    signature = sign(ssh_key, ch["message"])
    assert _login(client, "alice", ch["nonce"], signature).status_code == 200
    # Replaying the captured signature fails: the challenge is spent.
    assert _login(client, "alice", ch["nonce"], signature).status_code == 401


def test_a_bad_attempt_also_spends_the_challenge(
    client: TestClient, ssh_key: Path, other_key: Path
) -> None:
    login_user(client, ssh_key, "alice")
    ch = _challenge(client, "alice")
    assert _login(client, "alice", ch["nonce"], sign(other_key, ch["message"])).status_code == 401
    assert _login(client, "alice", ch["nonce"], sign(ssh_key, ch["message"])).status_code == 401


def test_signature_must_be_over_this_challenge_in_the_dibs_namespace(
    client: TestClient, ssh_key: Path
) -> None:
    login_user(client, ssh_key, "bob")
    ch = _challenge(client, "bob")
    assert _login(client, "bob", ch["nonce"], sign(ssh_key, "some other text")).status_code == 401
    ch = _challenge(client, "bob")
    git = sign(ssh_key, ch["message"], namespace="git")
    assert _login(client, "bob", ch["nonce"], git).status_code == 401
    ch = _challenge(client, "bob")
    assert _login(client, "bob", ch["nonce"], "garbage").status_code == 401


def test_a_challenge_belongs_to_the_user_it_was_issued_to(
    client: TestClient, ssh_key: Path
) -> None:
    login_user(client, ssh_key, "alice")
    login_user(client, ssh_key, "bob")  # same key, so only the challenge tells them apart
    ch = _challenge(client, "alice")
    assert _login(client, "bob", ch["nonce"], sign(ssh_key, ch["message"])).status_code == 401


def test_challenge_expires(tmp_path: Path, ssh_key: Path) -> None:
    store = UsersStore(tmp_path / "dibs.db")
    store.register("alice", _pub(ssh_key))
    store.approve("alice")
    t0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    nonce, message = store.challenge("alice", now=t0)
    with pytest.raises(LoginFailed, match="expired"):
        store.login("alice", nonce, sign(ssh_key, message), now=t0 + timedelta(seconds=61))


def test_tokens_are_stored_hashed(client: TestClient, ssh_key: Path) -> None:
    token = login_user(client, ssh_key, "alice")
    db = client.app.state.users.db_path  # type: ignore[attr-defined]
    assert token.encode() not in Path(db).read_bytes()


def test_revoke_and_remove_end_logins(client: TestClient, ssh_key: Path) -> None:
    token = login_user(client, ssh_key, "alice")
    who = {"Authorization": f"Bearer {token}"}
    users = client.app.state.users  # type: ignore[attr-defined]
    users.revoke_tokens("alice")
    assert client.get("/auth/whoami", headers=who).status_code == 401
    token = login_user(client, ssh_key, "bob")
    users.remove("bob")
    assert (
        client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )


def test_logout_drops_only_that_token(client: TestClient, ssh_key: Path) -> None:
    first = login_user(client, ssh_key, "alice")
    ch = _challenge(client, "alice")
    second = _login(client, "alice", ch["nonce"], sign(ssh_key, ch["message"])).json()["token"]
    client.post("/auth/logout", headers={"Authorization": f"Bearer {first}"})
    for tok, want in ((first, 401), (second, 200)):
        assert (
            client.get("/auth/whoami", headers={"Authorization": f"Bearer {tok}"}).status_code
            == want
        )
