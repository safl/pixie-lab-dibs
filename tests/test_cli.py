# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""The ``dibs`` command: client flows against the real app, the systemd
unit it writes, the admin commands, and the bash completion."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from dibs import cli

Run = Callable[..., int]


@pytest.fixture
def run(client: TestClient, tokens, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Run:
    """``dibs`` with its HTTP served by the app, as a ``tokens`` user
    (``who=``, through DIBS_TOKEN) or with no token (``who=None``)."""

    def request(method: str, url: str, json: Any = None, headers: Any = None, timeout: float = 0):
        return client.request(method, url.removeprefix("http://dibs"), json=json, headers=headers)

    monkeypatch.setattr(cli.httpx, "request", request)
    monkeypatch.setenv("DIBS_URL", "http://dibs")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DIBS_ASSISTED_BY", raising=False)

    def go(*argv: str, who: str | None = "alice") -> int:
        if who:
            monkeypatch.setenv("DIBS_TOKEN", tokens[who])
        else:
            monkeypatch.delenv("DIBS_TOKEN", raising=False)
        return cli.main(list(argv))

    return go


def test_claim_refuse_list_release(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("claim", "epyc-02", "warp", "--for", "xnvme", "--expires", "2h") == 0
    assert "'alice' has claimed 'epyc-02' for 'xnvme' expires in: '2h'" in capsys.readouterr().out
    assert run("claim", "warp", "--for", "ggml", who="bob") == cli.EXIT_REFUSED
    assert "refused, 'alice' has claimed 'warp'" in capsys.readouterr().err
    assert run("list", "--mine", who="bob") == 0
    assert capsys.readouterr().out == "'bob' has no claims\n"
    assert run("list", "--names") == 0
    assert capsys.readouterr().out == "epyc-02\nryzen-01\nwarp\n"
    assert run("release", "--mine") == 0
    assert capsys.readouterr().out == "'epyc-02' is free\n'warp' is free\n"
    assert run("release", "--mine") == 0
    assert capsys.readouterr().out == "nothing to release\n"


def test_errors_exit_2(run: Run, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    assert run("release") == cli.EXIT_ERROR
    assert run("claim", "nope", "--for", "x") == cli.EXIT_ERROR
    assert "no such thing: nope" in capsys.readouterr().err
    assert run("claim", "warp", "--for", "x", who=None) == cli.EXIT_ERROR
    assert "dibs login" in capsys.readouterr().err
    monkeypatch.delenv("DIBS_URL")
    assert cli.main(["list"]) == cli.EXIT_ERROR
    assert "DIBS_URL" in capsys.readouterr().err


def test_register_login_logout(run: Run, client: TestClient, ssh_key: Path, capsys) -> None:
    pub = str(ssh_key.with_suffix(".pub"))
    assert run("register", "Carol", "-i", pub, who=None) == 0
    out = capsys.readouterr().out
    assert "Registered 'carol' with key SHA256:" in out and "dibs admin approve carol" in out
    assert run("login", who=None) == cli.EXIT_ERROR
    assert "waiting for admin approval" in capsys.readouterr().err
    client.app.state.users.approve("carol")  # type: ignore[attr-defined]
    assert run("login", who=None) == 0
    assert capsys.readouterr().out == "Logged in as 'carol'.\n"
    assert oct(cli.config_path().stat().st_mode & 0o777) == "0o600"
    assert run("claim", "warp", "--for", "xnvme", who=None) == 0
    assert "'carol' has claimed 'warp'" in capsys.readouterr().out
    assert run("logout", who=None) == 0
    assert run("release", "warp", who=None) == cli.EXIT_ERROR


def test_info_text_and_data_formats(run: Run, client: TestClient, capsys) -> None:
    assert run("info", "warp") == 0
    out = capsys.readouterr().out
    assert out.startswith("warp  (machine)\n")
    assert "  claim      'warp' is free\n" in out
    assert "AMD EPYC 7302P (16 cores / 32 threads)" in out
    assert "/dev/nvme0n1 1.8T 990 PRO" in out
    want = client.get("/things/warp").json()
    for flag, load in (
        ("--json", json.loads),
        ("--yaml", yaml.safe_load),
        ("--toml", tomllib.loads),
    ):
        assert run("info", "warp", flag) == 0
        assert load(capsys.readouterr().out) == want, flag
    with pytest.raises(SystemExit):
        run("info", "warp", "--json", "--yaml")


@pytest.mark.parametrize(("raw", "secs"), [("2h", 7200), ("90m", 5400), ("1d12h", 129600)])
def test_parse_duration(raw: str, secs: int) -> None:
    assert cli.parse_duration(raw) == secs


@pytest.mark.parametrize("raw", ["", "2", "h", "0m", "2 h", "1h2d"])
def test_parse_duration_rejects(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_duration(raw)


# ---- server --------------------------------------------------------------


def test_system_unit_runs_this_dibs_sandboxed() -> None:
    text = cli.unit_text(Path("/usr/local/bin/dibs"), user=False, host="0.0.0.0", port=8090)
    assert "ExecStart=/usr/local/bin/dibs serve --host 0.0.0.0 --port 8090\n" in text
    for line in (
        "User=dibs",
        "StateDirectory=dibs",
        "ProtectHome=yes",
        "WantedBy=multi-user.target",
    ):
        assert line in text
    user = cli.unit_text(Path("/home/a/.local/bin/dibs"), user=True, host="::", port=9000)
    assert "serve --host :: --port 9000 --user\n" in user and "DynamicUser" not in user
    assert "WantedBy=default.target" in user


def test_server_install_writes_enables_and_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["server", "install", "--user", "--port", "9000"]) == 0
    unit = tmp_path / "systemd" / "user" / "dibs.service"
    assert "--port 9000 --user" in unit.read_text()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "dibs.service"],
        ["systemctl", "--user", "restart", "dibs.service"],
    ]
    calls.clear()
    cli.main(["server", "stop", "--user"])
    cli.main(["server", "start"])
    assert calls == [
        ["systemctl", "--user", "stop", "dibs.service"],
        ["systemctl", "start", "dibs.service"],
    ]
    calls.clear()
    cli.main(["server", "uninstall", "--user"])
    assert not unit.exists() and calls[0] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "dibs.service",
    ]


def test_system_unit_avoids_dynamic_user() -> None:
    """ssh-keygen needs its user to resolve, and a DynamicUser= user does
    not resolve without nss-systemd (plain Debian has none)."""
    text = cli.unit_text(Path("/usr/local/bin/dibs"), user=False, host="0.0.0.0", port=8090)
    assert "DynamicUser" not in text and "User=dibs\nGroup=dibs\n" in text


@pytest.mark.parametrize(("exists", "want_useradd"), [(True, False), (False, True)])
def test_service_user_is_created_once(
    monkeypatch: pytest.MonkeyPatch, exists: bool, want_useradd: bool
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0 if exists or cmd[0] != "getent" else 2)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.ensure_service_user()
    assert calls[0] == ["getent", "passwd", "dibs"]
    assert any(c[0] == "useradd" and c[-1] == "dibs" for c in calls) == want_useradd


def test_system_install_refuses_a_dibs_in_home(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli, "_exe", lambda: Path.home() / ".local" / "bin" / "dibs")
    assert cli.main(["server", "install"]) == cli.EXIT_ERROR
    assert "dibs server install --user" in capsys.readouterr().err


def test_admin_commands(client: TestClient, ssh_key: Path, tmp_path: Path, capsys) -> None:
    data = str(tmp_path / "data")
    client.post(
        "/auth/register", json={"user": "dave", "pubkey": ssh_key.with_suffix(".pub").read_text()}
    )
    assert cli.main(["admin", "users", "--data", data]) == 0
    assert "dave" in capsys.readouterr().out and "pending"
    assert cli.main(["admin", "approve", "dave", "--data", data]) == 0
    assert client.post("/auth/challenge", json={"user": "dave"}).status_code == 200
    assert cli.main(["admin", "revoke", "dave", "--data", data]) == 0
    assert cli.main(["admin", "remove", "dave", "--data", data]) == 0
    assert cli.main(["admin", "remove", "dave", "--data", data]) == cli.EXIT_ERROR
    assert cli.main(["admin", "users", "--data", str(tmp_path / "nowhere")]) == cli.EXIT_ERROR


# ---- completion ----------------------------------------------------------


def _complete(script: Path, line: str, tmp_path: Path) -> list[str]:
    """Run the completion for ``line`` in a real bash, with a stub dibs."""
    stub = tmp_path / "bin" / "dibs"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("#!/bin/sh\nprintf 'epyc-02\\nwarp\\n'\n")
    stub.chmod(0o755)
    words = line.split(" ")
    prog = f"""
export PATH={stub.parent}:$PATH
source {script}
COMP_WORDS=({" ".join(repr(w) for w in words)})
COMP_CWORD={len(words) - 1}
_dibs
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    out = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_bash_completion(tmp_path: Path, capsys) -> None:
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    assert cli.main(["completion", "bash"]) == 0
    script = tmp_path / "dibs.bash"
    script.write_text(capsys.readouterr().out)
    assert set(_complete(script, "dibs ", tmp_path)) >= {
        "list",
        "claim",
        "serve",
        "server",
        "admin",
    }
    assert sorted(_complete(script, "dibs server st", tmp_path)) == ["start", "status", "stop"]
    assert _complete(script, "dibs claim --e", tmp_path) == ["--expires"]
    assert _complete(script, "dibs claim w", tmp_path) == ["warp"]
    assert "--names" not in _complete(script, "dibs list --", tmp_path)


def test_skill_install_and_print(tmp_path: Path, capsys) -> None:
    assert cli.main(["skill", "install", "--dir", str(tmp_path)]) == 0
    installed = (tmp_path / "dibs" / "SKILL.md").read_text()
    assert installed.startswith("---\nname: dibs\n")
    assert "dibs release --mine" in installed and "--force" in installed
    capsys.readouterr()
    assert cli.main(["skill", "print"]) == 0
    assert capsys.readouterr().out == installed


def test_skill_ships_in_the_wheel(tmp_path: Path) -> None:
    """The skill is data, not code, so check the built wheel carries it."""
    import zipfile

    if shutil.which("uv") is None:
        pytest.skip("needs uv")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=Path(__file__).parent.parent,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    assert "dibs/skill/SKILL.md" in zipfile.ZipFile(wheel).namelist()


def test_skill_body_drops_only_the_preamble() -> None:
    from dibs._skill import skill_body, skill_text

    body = skill_body()
    assert body.startswith("# Claim lab machines with dibs\n")
    assert skill_text().endswith(body)


def test_whoami_shows_the_actor(run: Run, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    assert run("whoami") == 0
    assert capsys.readouterr().out == "'alice'\n"
    monkeypatch.setenv("DIBS_ASSISTED_BY", "Claude Code:claude-opus-5-5")
    monkeypatch.setenv("DIBS_SESSION", "s1")
    assert run("whoami") == 0
    assert (
        capsys.readouterr().out
        == "'alice' assisted by 'Claude Code:claude-opus-5-5' (session s1)\n"
    )
    assert run("whoami", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "user": "alice",
        "assisted_by": "Claude Code:claude-opus-5-5",
        "session": "s1",
    }
    # With nobody logged in it fails, which is how an agent finds out.
    assert run("whoami", who=None) == cli.EXIT_ERROR
    assert "agents use their person's login" in capsys.readouterr().err


def test_claims_carry_the_session(run: Run, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("DIBS_ASSISTED_BY", "Claude Code:claude-opus-5-5")
    monkeypatch.setenv("DIBS_SESSION", "s1")
    assert run("claim", "warp", "--for", "x") == 0
    assert "(session s1) has claimed 'warp'" in capsys.readouterr().out
    monkeypatch.setenv("DIBS_SESSION", "s2")
    assert run("claim", "warp", "--for", "y") == cli.EXIT_REFUSED
    assert run("release", "--mine") == 0
    assert capsys.readouterr().out.endswith("nothing to release\n")


def test_session_tags_are_fresh_and_valid(capsys) -> None:
    from dibs._store import validate_session

    tags = set()
    for _ in range(20):
        assert cli.main(["session"]) == 0
        tag = capsys.readouterr().out.strip()
        assert validate_session(tag) == tag
        tags.add(tag)
    assert len(tags) == 20


def test_registering_a_taken_name_is_an_error_not_a_crash(run: Run, ssh_key: Path, capsys) -> None:
    pub = str(ssh_key.with_suffix(".pub"))
    assert run("register", "erin", "-i", pub, who=None) == 0
    capsys.readouterr()
    assert run("register", "erin", "-i", pub, who=None) == cli.EXIT_ERROR
    assert "already registered" in capsys.readouterr().err
