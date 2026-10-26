# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Shared fixtures: a dibs app over a fake pixie, an SSH key, and a
helper that registers, approves and logs a user in for real."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dibs._sources import PixieSource
from dibs.app import create_app

PIXIE = "http://pixie:8080"

CONFIG = """
[[pixie]]
url = "http://pixie:8080"

[[thing]]
name = "ryzen-01"
mac = "aa:bb:cc:dd:ee:60"

[[thing]]
name = "epyc-02"
rack = "a1"
"""


class FakePixie:
    """What a pixie server answers, editable by the test."""

    def __init__(self) -> None:
        self.up = True
        self.machines: list[dict[str, Any]] = [
            {"name": "epyc-02", "mac": "aa:bb:cc:dd:ee:02", "boot_mode": "nbdboot-overlay"},
            {"name": "warp", "mac": "aa:bb:cc:dd:ee:80", "labels": ["gpu"]},
            {"mac": "aa:bb:cc:dd:ee:99"},  # from a pixie before machine names
        ]
        self.info: dict[str, dict[str, Any]] = {
            "warp": {
                "name": "warp",
                "binding": {"boot_mode": "nbdboot-ephemeral", "boot_mode_label": "Netboot"},
                "last_seen": {"at": "2026-10-26T12:00:00Z", "pxe_ip": "10.0.0.80"},
                "inventory": {
                    "taken_at": "2026-10-26T11:00:00Z",
                    "cpu": {"sockets": [{"model": "AMD EPYC 7302P", "cores": 16, "threads": 32}]},
                    "disks": [{"path": "/dev/nvme0n1", "size": "1.8T", "model": "990 PRO"}],
                },
            }
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("pixie is down", request=request)
        path = request.url.path
        if path == "/machines":
            return httpx.Response(200, json={"machines": self.machines})
        name = path.removeprefix("/machines/").removesuffix("/info")
        if name in self.info:
            return httpx.Response(200, json=self.info[name])
        return httpx.Response(404, json={"detail": "no machine"})

    def source(self) -> PixieSource:
        return PixieSource(PIXIE, httpx.Client(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def pixie() -> FakePixie:
    return FakePixie()


@pytest.fixture
def client(tmp_path: Path, pixie: FakePixie) -> Iterator[TestClient]:
    config = tmp_path / "dibs.toml"
    config.write_text(CONFIG)
    app = create_app(
        config, tmp_path / "data", pixie_sources=[pixie.source()], background_sync=False
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway ed25519 key; returns the private key path (``.pub`` beside it)."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required for login")
    key = tmp_path_factory.mktemp("sshkey") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dibs-test", "-f", str(key)],
        check=True,
    )
    return key


def sign(key: Path, message: str, namespace: str = "dibs") -> str:
    return subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-n", namespace, "-f", str(key)],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def login_user(client: TestClient, key: Path, name: str) -> str:
    """Register ``name`` with ``key``, approve it, log in through the real
    challenge, and return the bearer token."""
    pubkey = key.with_suffix(".pub").read_text()
    client.post("/auth/register", json={"user": name, "pubkey": pubkey})
    client.app.state.users.approve(name)  # type: ignore[attr-defined]
    challenge = client.post("/auth/challenge", json={"user": name}).json()
    body = {"user": name, "nonce": challenge["nonce"], "signature": sign(key, challenge["message"])}
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200, r.text
    return str(r.json()["token"])


@pytest.fixture
def tokens(client: TestClient, ssh_key: Path) -> dict[str, str]:
    """Bearer tokens for two logged-in users, ``alice`` and ``bob``."""
    return {name: login_user(client, ssh_key, name) for name in ("alice", "bob")}


def bearer(tokens: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[who]}"}
