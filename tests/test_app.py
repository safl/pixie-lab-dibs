# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""The HTTP API: open reads, logged-in claims, pixie info, the login hook."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import bearer

AGENT = "Claude Code:claude-opus-5-5"


def _claim(
    client: TestClient, tokens: dict[str, str], *things: str, who: str = "alice", **extra: object
):
    body = {"things": list(things), "purpose": "xnvme", **extra}
    return client.post("/claims", json=body, headers=bearer(tokens, who))


def _release(client: TestClient, tokens: dict[str, str], who: str = "alice", **body: object):
    return client.post("/claims/release", json=body, headers=bearer(tokens, who))


def test_claim_needs_a_login_and_reads_stay_open(client: TestClient, tokens) -> None:
    r = client.post("/claims", json={"things": ["warp"], "purpose": "x"})
    assert r.status_code == 401 and "dibs login" in r.json()["detail"]
    bad = {"Authorization": "Bearer dibs_nope"}
    assert (
        client.post("/claims", json={"things": ["warp"], "purpose": "x"}, headers=bad).status_code
        == 401
    )
    assert client.get("/things").status_code == 200


def test_claims_are_listed_and_shown(client: TestClient, tokens) -> None:
    assert _claim(client, tokens, "warp").status_code == 200
    things = {t["name"]: t for t in client.get("/things").json()["things"]}
    assert sorted(things) == ["epyc-02", "ryzen-01", "warp"]
    assert (
        things["warp"]["sentence"] == "'alice' has claimed 'warp' for 'xnvme' expires in: 'never'"
    )
    assert things["warp"]["claim"]["holder"] == "alice"
    assert things["ryzen-01"]["sentence"] == "'ryzen-01' is free"
    text = client.get("/things/warp/claim")
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text == "'alice' has claimed 'warp' for 'xnvme' expires in: 'never'\n"


def test_conflict_unknown_and_invalid(client: TestClient, tokens) -> None:
    _claim(client, tokens, "warp", who="bob")
    r = _claim(client, tokens, "epyc-02", "warp")
    assert r.status_code == 409
    assert r.json()["detail"]["claimed"] == [
        "'bob' has claimed 'warp' for 'xnvme' expires in: 'never'"
    ]
    assert "claim" not in client.get("/things/epyc-02").json()
    assert _claim(client, tokens, "nope").status_code == 404
    assert _claim(client, tokens, "ryzen-01", purpose="").status_code == 422


def test_release_mine_is_scoped_to_assisted_by(client: TestClient, tokens) -> None:
    _claim(client, tokens, "epyc-02")
    r = _claim(client, tokens, "warp", assisted_by=AGENT)
    assert r.json()["things"][0]["sentence"].startswith(f"'alice' assisted by '{AGENT}'")
    released = _release(client, tokens, mine=True, assisted_by=AGENT).json()["things"]
    assert [t["name"] for t in released] == ["warp"]
    assert client.get("/things/epyc-02").json()["claim"]["holder"] == "alice"
    assert _release(client, tokens).status_code == 422
    assert _release(client, tokens, mine=True, things=["warp"]).status_code == 422


def test_a_claim_on_a_thing_that_went_away_still_shows(client: TestClient, tokens, pixie) -> None:
    _claim(client, tokens, "warp")
    pixie.machines = [m for m in pixie.machines if m.get("name") != "warp"]
    client.app.state.sync()  # type: ignore[attr-defined]
    things = {t["name"]: t for t in client.get("/things").json()["things"]}
    assert things["warp"]["source"] == "gone"
    assert _release(client, tokens, things=["warp"]).json()["things"][0]["name"] == "warp"


def test_info_adds_what_pixie_knows(client: TestClient) -> None:
    info = client.get("/things/warp").json()
    assert info["pixie"]["inventory"]["cpu"]["sockets"][0]["model"] == "AMD EPYC 7302P"
    assert "pixie" not in client.get("/things/ryzen-01").json()
    assert client.get("/things/nope").status_code == 404


def test_claim_lookup_by_mac_or_host(client: TestClient, tokens) -> None:
    _claim(client, tokens, "ryzen-01")
    r = client.get("/claim", params={"mac": "00:11:22:33:44:55,AA:BB:CC:DD:EE:60,", "host": "x"})
    assert r.text.startswith("'alice' has claimed 'ryzen-01'")
    assert client.get("/claim", params={"mac": "", "host": "warp"}).text == "'warp' is free\n"
    assert (
        client.get("/claim", params={"mac": "00:11:22:33:44:55", "host": "nope"}).status_code == 404
    )


def test_index_page_escapes(client: TestClient, tokens) -> None:
    _claim(client, tokens, "warp", purpose="<script>")
    page = client.get("/").text
    assert "&lt;script&gt;" in page and "<script>" not in page


# ---- the login hook --------------------------------------------------------


@pytest.fixture
def hook(client: TestClient, tmp_path: Path) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("needs sh")
    script = tmp_path / "dibs.sh"
    script.write_text(client.get("/login.sh").text)
    return script


def _source(script: Path, curl_body: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Source the hook in an interactive ``sh`` with a stub ``curl``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "curl"
    stub.write_text(f"#!/bin/sh\n{curl_body}\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "ENV": ""}
    return subprocess.run(
        ["sh", "-i", "-c", f". {script}"], capture_output=True, text=True, env=env, timeout=10
    )


def test_hook_points_at_the_fetching_url(hook: Path) -> None:
    assert '"http://testserver/claim"' in hook.read_text()
    assert subprocess.run(["sh", "-n", str(hook)]).returncode == 0


def test_hook_prints_the_claim(hook: Path, tmp_path: Path) -> None:
    r = _source(
        hook, "echo \"'alice' has claimed 'warp' for 'xnvme' expires in: 'never'\"", tmp_path
    )
    assert r.stdout == "*** 'alice' has claimed 'warp' for 'xnvme' expires in: 'never' ***\n"


def test_hook_sends_macs_and_hostname_once(hook: Path, tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    r = _source(hook, f'echo "$@" >> {calls}; exit 7', tmp_path)
    assert r.stdout == ""
    sent = calls.read_text().splitlines()
    assert len(sent) == 1 and "mac=" in sent[0] and "host=" in sent[0]


def test_parts_over_the_api(client: TestClient, tokens, tmp_path: Path) -> None:
    """A part and its thing, through claim, list, the text route and the
    login hook's lookup."""
    config = client.app.state.config  # type: ignore[attr-defined]
    from dibs._store import Thing

    config.things.append(Thing("ryzen-01.bmc", "machine", "config", {"host": "10.0.0.61"}))
    client.app.state.sync()  # type: ignore[attr-defined]
    assert _claim(client, tokens, "ryzen-01.bmc", who="bob").status_code == 200
    r = _claim(client, tokens, "ryzen-01")
    assert r.status_code == 409
    assert r.json()["detail"]["claimed"] == [
        "'bob' has claimed 'ryzen-01.bmc' for 'xnvme' expires in: 'never'"
    ]
    things = {t["name"]: t for t in client.get("/things").json()["things"]}
    assert things["ryzen-01"]["sentence"].startswith("'bob' has claimed 'ryzen-01.bmc'")
    assert "claim" not in things["ryzen-01"]
    assert things["ryzen-01"]["covered_by"][0]["thing"] == "ryzen-01.bmc"
    # The login hook on ryzen-01 (by MAC) sees the part's claim.
    hook = client.get("/claim", params={"mac": "aa:bb:cc:dd:ee:60"}).text
    assert hook.startswith("'bob' has claimed 'ryzen-01.bmc'")


def test_agents_page_is_set_up_for_this_server(client: TestClient, tokens) -> None:
    from dibs._skill import skill_body

    _claim(client, tokens, "warp")
    r = client.get("/agents")
    assert r.headers["content-type"].startswith("text/plain")
    page = r.text
    assert "export DIBS_URL=http://testserver\n" in page
    assert "- 'alice' has claimed 'warp' for 'xnvme' expires in: 'never'" in page
    assert page.endswith(skill_body())
    # The skill's Claude Code frontmatter and licence comment are not for this reader.
    assert "name: dibs" not in page and "SPDX" not in page
    assert '<a href="/agents">' in client.get("/").text
