# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""``dibs``: the one command, for the server and for everyone using it.

On the server host: ``dibs serve`` runs the server, ``dibs server
install`` makes it a systemd service (``start``, ``stop``, ``status``,
``logs`` and ``uninstall`` then wrap ``systemctl``), and ``dibs admin``
approves users by working on the server's database directly.

Everywhere else: ``dibs register`` and ``dibs login`` get you a token,
and ``dibs list``, ``info``, ``claim`` and ``release`` use it. The client
finds the server with ``--url`` or ``DIBS_URL``, keeps its token per URL
in ``$XDG_CONFIG_HOME/dibs/client.json`` (``DIBS_TOKEN`` overrides it, for
runners), and an agent sets ``DIBS_ASSISTED_BY`` (``Agent:model``, as in
an ``Assisted-by:`` trailer) so its claims say so.

Exit status: 0 done, 1 refused because a thing is claimed by someone
else, 2 anything else (usage, unknown thing, server unreachable).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import tomlkit
import yaml

EXIT_REFUSED = 1
EXIT_ERROR = 2

DEFAULT_PORT = 8090
UNIT_NAME = "dibs.service"
SERVICE_USER = "dibs"

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_DEFAULT_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


class CliError(Exception):
    pass


class Refused(Exception):
    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences


def parse_duration(raw: str) -> int:
    """``2h``, ``90m``, ``1d12h``, ``30s`` -> seconds."""
    m = _DURATION_RE.match(raw.strip())
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"{raw!r} is not a duration like 2h, 90m or 1d12h")
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if total <= 0:
        raise argparse.ArgumentTypeError("duration must be longer than zero")
    return total


# ---- server-side paths ---------------------------------------------------


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def default_config(user: bool) -> Path:
    """Where the server reads its config. Under systemd that is the
    unit's ``ConfigurationDirectory``, which systemd passes in."""
    if os.environ.get("CONFIGURATION_DIRECTORY"):
        return Path(os.environ["CONFIGURATION_DIRECTORY"]) / "dibs.toml"
    return (
        _xdg("XDG_CONFIG_HOME", ".config") / "dibs" if user else Path("/etc/dibs")
    ) / "dibs.toml"


def default_data(user: bool) -> Path:
    """Where the server keeps its database: the unit's ``StateDirectory``."""
    if os.environ.get("STATE_DIRECTORY"):
        return Path(os.environ["STATE_DIRECTORY"])
    return _xdg("XDG_STATE_HOME", ".local/state") / "dibs" if user else Path("/var/lib/dibs")


# ---- client config -------------------------------------------------------


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "dibs" / "client.json"


def _load_client() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry(url: str) -> dict[str, Any]:
    entry = _load_client().get(url.rstrip("/"), {})
    return entry if isinstance(entry, dict) else {}


def _save(url: str, **fields: str | None) -> None:
    """Merge ``fields`` into this URL's entry; ``None`` drops one. The
    file holds a token, so it is created readable by its owner only."""
    data = _load_client()
    entry = data.setdefault(url.rstrip("/"), {})
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def token(url: str) -> str:
    return os.environ.get("DIBS_TOKEN", "").strip() or str(_entry(url).get("token", ""))


def assisted_by() -> str:
    return os.environ.get("DIBS_ASSISTED_BY", "").strip()


def default_key() -> Path:
    for name in _DEFAULT_KEYS:
        path = Path.home() / ".ssh" / name
        if path.exists():
            return path
    raise CliError("no SSH public key found in ~/.ssh; pass -i PATH.pub")


def sign(key: Path, message: str) -> str:
    """Sign ``message`` in the ``dibs`` namespace. Uses the private key
    beside ``key`` when it exists, else asks the ssh-agent for the key
    whose public half ``key`` is."""
    private = key.with_suffix("") if key.suffix == ".pub" else key
    out = subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-Y",
            "sign",
            "-n",
            "dibs",
            "-f",
            str(private if private.exists() else key),
        ],
        input=message,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise CliError(f"ssh-keygen could not sign with {key}: {out.stderr.strip()}")
    return out.stdout


def _request(url: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    headers = {}
    bearer = token(url)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        r = httpx.request(method, url.rstrip("/") + path, json=body, headers=headers, timeout=10)
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach dibs at {url}: {exc}") from exc
    if r.status_code == 409:
        raise Refused(r.json()["detail"]["claimed"])
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise CliError(str(detail))
    return r.json() if r.content else {}


def dump(data: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2) + "\n"
    if fmt == "yaml":
        return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return tomlkit.dumps(data)


# ---- client commands -----------------------------------------------------


def _url(args: argparse.Namespace) -> str:
    if not args.url:
        raise CliError("set DIBS_URL (or pass --url), e.g. http://dibs:8090")
    return str(args.url)


def cmd_list(args: argparse.Namespace) -> None:
    url = _url(args)
    things = _request(url, "GET", "/things")["things"]
    if args.names:
        print("\n".join(t["name"] for t in things))
        return
    me = ""
    if args.mine:
        me = _request(url, "GET", "/auth/whoami")["user"]
        things = [t for t in things if t.get("claim", {}).get("holder") == me]
    if args.format:
        sys.stdout.write(dump({"things": things}, args.format))
        return
    if args.mine and not things:
        print(f"'{me}' has no claims")
    for t in things:
        print(t["sentence"])


def humanize_bytes(n: float) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TiB"


def _join(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p)


def _pick(item: dict[str, Any], *keys: str) -> str:
    return _join(*(item.get(k) for k in keys))


def render_info(info: dict[str, Any]) -> str:
    """One thing as aligned ``label  value`` lines; for a pixie machine,
    what pixie knows about it too."""
    rows: list[tuple[str, str]] = [("claim", info["sentence"])]
    rows.append(("source", info["source"]))
    for key, value in info.get("attrs", {}).items():
        rows.append((key, ", ".join(map(str, value)) if isinstance(value, list) else str(value)))
    pixie = info.get("pixie")
    if pixie:
        b = pixie.get("binding", {})
        boot = b.get("boot_mode_label", "")
        image = b.get("image")
        if image:
            boot += f": {image.get('name') or image['sha256'][:12]}"
        if b.get("overlay"):
            boot += f", overlay '{b['overlay']}'"
        rows.append(("boot", boot))
        seen = pixie.get("last_seen", {})
        rows.append(
            ("last seen", _join(seen.get("at"), seen.get("pxe_ip") and f"from {seen['pxe_ip']}"))
        )
        inv = pixie.get("inventory", {})
        system = inv.get("system", {})
        if system:
            firmware = system.get("firmware") and f"firmware {system['firmware']}"
            rows.append(("system", _join(system.get("vendor"), system.get("model"), firmware)))
        for sock in inv.get("cpu", {}).get("sockets", []):
            counts = (
                sock.get("cores")
                and f"({sock['cores']} cores / {sock.get('threads', '?')} threads)"
            )
            rows.append(("cpu", _join(sock.get("model"), counts)))
        mem = inv.get("memory", {})
        if mem.get("total_bytes"):
            slots = (
                mem.get("slots_total") and f"{mem['slots_populated']}/{mem['slots_total']} slots"
            )
            rows.append(
                (
                    "memory",
                    _join(humanize_bytes(mem["total_bytes"]), slots, mem.get("dominant_type")),
                )
            )
        rows.extend(
            ("nic", _pick(n, "name", "mac", "speed", "driver")) for n in inv.get("nics", [])
        )
        rows.extend(
            ("disk", _pick(d, "path", "size", "vendor", "model", "serial", "tran"))
            for d in inv.get("disks", [])
        )
        if inv:
            rows.append(("inventory", f"taken {inv.get('taken_at', '?')}"))
    width = max(len(label) for label, _ in rows)
    lines = [f"{info['name']}  ({info['kind']})"]
    lines += [f"  {label.ljust(width)}  {value}" for label, value in rows]
    return "\n".join(lines) + "\n"


def cmd_info(args: argparse.Namespace) -> None:
    info = _request(_url(args), "GET", f"/things/{args.thing}")
    sys.stdout.write(dump(info, args.format) if args.format else render_info(info))


def cmd_claim(args: argparse.Namespace) -> None:
    body = {
        "things": args.things,
        "purpose": args.purpose,
        "assisted_by": assisted_by(),
        "expires_in": args.expires,
        "force": args.force,
    }
    for t in _request(_url(args), "POST", "/claims", body)["things"]:
        print(t["sentence"])


def cmd_release(args: argparse.Namespace) -> None:
    if bool(args.things) == args.mine:
        raise CliError("name the things to release, or pass --mine")
    body = {
        "things": args.things,
        "assisted_by": assisted_by(),
        "mine": args.mine,
        "force": args.force,
    }
    released = _request(_url(args), "POST", "/claims/release", body)["things"]
    if not released:
        print("nothing to release")
    for t in released:
        print(t["sentence"])


def cmd_register(args: argparse.Namespace) -> None:
    url = _url(args)
    key = Path(args.key).expanduser() if args.key else default_key()
    try:
        pubkey = key.read_text()
    except OSError as exc:
        raise CliError(f"cannot read {key}: {exc}") from exc
    answer = _request(url, "POST", "/auth/register", {"user": args.user, "pubkey": pubkey})
    _save(url, user=answer["user"], key=str(key), token=None)
    print(f"Registered '{answer['user']}' with key {answer['fingerprint']}.")
    print(f"Ask the dibs admin to run: dibs admin approve {answer['user']}")
    print(f"Then: dibs login {answer['user']}")


def cmd_login(args: argparse.Namespace) -> None:
    url = _url(args)
    entry = _entry(url)
    user = args.user or entry.get("user")
    if not user:
        raise CliError("which user? run: dibs login <user>")
    key = Path(args.key).expanduser() if args.key else Path(entry.get("key") or default_key())
    challenge = _request(url, "POST", "/auth/challenge", {"user": user})
    body = {"user": user, "nonce": challenge["nonce"], "signature": sign(key, challenge["message"])}
    answer = _request(url, "POST", "/auth/login", body)
    _save(url, user=answer["user"], key=str(key), token=answer["token"])
    print(f"Logged in as '{answer['user']}'.")


def cmd_logout(args: argparse.Namespace) -> None:
    url = _url(args)
    if token(url):
        _request(url, "POST", "/auth/logout")
    _save(url, token=None)
    print("Logged out.")


# ---- server commands -----------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> None:
    import logging

    import uvicorn

    from dibs.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = Path(args.config) if args.config else default_config(args.user)
    data = Path(args.data) if args.data else default_data(args.user)
    try:
        app = create_app(config, data)
    except ValueError as exc:  # a broken config file
        raise CliError(str(exc)) from exc
    uvicorn.run(app, host=args.host, port=args.port)


def _exe() -> Path:
    """The ``dibs`` being run, so the unit starts this same install."""
    found = shutil.which(sys.argv[0]) or sys.argv[0]
    return Path(found).resolve()


def unit_text(exe: Path, *, user: bool, host: str, port: int) -> str:
    serve = f"{exe} serve --host {host} --port {port}"
    if user:
        service = f"""ExecStart={serve} --user
StateDirectory=dibs
ConfigurationDirectory=dibs
Restart=on-failure"""
        target = "default.target"
    else:
        # A static user rather than DynamicUser=: ssh-keygen looks its own
        # user up, and a dynamic user resolves only where nss-systemd is set
        # up, which plain Debian does not do.
        service = f"""ExecStart={serve}
User={SERVICE_USER}
Group={SERVICE_USER}
StateDirectory=dibs
ConfigurationDirectory=dibs
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes"""
        target = "multi-user.target"
    return f"""# Written by: dibs server install{" --user" if user else ""}
[Unit]
Description=dibs: who is using which lab machine
Documentation=https://github.com/safl/pixie-lab-dibs
After=network-online.target
Wants=network-online.target

[Service]
{service}

[Install]
WantedBy={target}
"""


def unit_path(user: bool) -> Path:
    if user:
        return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user" / UNIT_NAME
    return Path("/etc/systemd/system") / UNIT_NAME


def _systemctl(user: bool, *argv: str) -> None:
    cmd = ["systemctl", *(["--user"] if user else []), *argv]
    if subprocess.run(cmd).returncode != 0:
        raise CliError(f"{' '.join(cmd)} failed")


def ensure_service_user() -> None:
    """Create the ``dibs`` system user the system service runs as, once."""
    if subprocess.run(["getent", "passwd", SERVICE_USER], capture_output=True).returncode == 0:
        return
    cmd = [
        "useradd",
        "--system",
        "--user-group",
        "--home-dir",
        "/var/lib/dibs",
        "--no-create-home",
        "--shell",
        "/usr/sbin/nologin",
        SERVICE_USER,
    ]
    if subprocess.run(cmd).returncode != 0:
        raise CliError(f"{' '.join(cmd)} failed; run dibs server install as root")


def cmd_server_install(args: argparse.Namespace) -> None:
    exe = _exe()
    if not args.user and str(exe).startswith(str(Path.home())):
        raise CliError(
            f"{exe} is in your home directory, which the system service cannot use. "
            "Install dibs system-wide, or run: dibs server install --user"
        )
    text = unit_text(exe, user=args.user, host=args.host, port=args.port)
    if args.print:
        sys.stdout.write(text)
        return
    if not args.user:
        ensure_service_user()
    path = unit_path(args.user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _systemctl(args.user, "daemon-reload")
    _systemctl(args.user, "enable", UNIT_NAME)
    # Restart rather than start, so installing over a running service, as
    # an upgrade does, replaces the process with the new code and unit.
    _systemctl(args.user, "restart", UNIT_NAME)
    print(f"Installed {path} and started it; dibs listens on port {args.port}.")
    if args.user:
        print("To keep it running while you are logged out: loginctl enable-linger")


def cmd_server_uninstall(args: argparse.Namespace) -> None:
    path = unit_path(args.user)
    _systemctl(args.user, "disable", "--now", UNIT_NAME)
    path.unlink(missing_ok=True)
    _systemctl(args.user, "daemon-reload")
    print(f"Stopped and removed {path}. The database is kept in {default_data(args.user)}.")


def cmd_server_ctl(args: argparse.Namespace) -> None:
    if args.verb == "logs":
        cmd = ["journalctl", *(["--user"] if args.user else []), "-u", UNIT_NAME, "-f"]
        subprocess.run(cmd)
        return
    _systemctl(args.user, args.verb, UNIT_NAME)


# ---- admin commands ------------------------------------------------------


def _users(args: argparse.Namespace) -> Any:
    from dibs._users import UsersStore

    data = Path(args.data) if args.data else default_data(args.user)
    db = data / "dibs.db"
    if not db.exists():
        raise CliError(f"no dibs database at {db}; is the server installed here? (--data PATH)")
    return UsersStore(db)


def cmd_admin_users(args: argparse.Namespace) -> None:
    users = _users(args).list_users()
    if not users:
        print("no users")
    for u in users:
        print(f"{u.name:<20} {u.status:<9} {u.fingerprint}  tokens: {u.tokens}")


def cmd_admin_approve(args: argparse.Namespace) -> None:
    try:
        _users(args).approve(args.name)
    except LookupError as exc:
        raise CliError(str(exc)) from exc
    print(f"Approved '{args.name}'; they can run: dibs login {args.name}")


def cmd_admin_remove(args: argparse.Namespace) -> None:
    if not _users(args).remove(args.name):
        raise CliError(f"no user {args.name!r}")
    print(f"Removed '{args.name}' and their tokens.")


def cmd_admin_revoke(args: argparse.Namespace) -> None:
    count = _users(args).revoke_tokens(args.name)
    print(f"Revoked {count} token(s) of '{args.name}'; they must log in again.")


# ---- agent skill -----------------------------------------------------------

SKILL = Path(__file__).parent / "skill" / "SKILL.md"


def cmd_skill(args: argparse.Namespace) -> None:
    """Install the Claude Code skill that teaches an agent to claim
    machines before using them, or print it for another agent's setup."""
    text = SKILL.read_text()
    if args.action == "print":
        sys.stdout.write(text)
        return
    target = Path(args.dir).expanduser() if args.dir else Path.home() / ".claude" / "skills"
    path = target / "dibs" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"Installed {path}. Claude Code picks it up in new sessions.")


# ---- shell completion ------------------------------------------------------

# Commands whose positional arguments are thing names, which the
# completion fetches from the server.
_THING_COMMANDS = ("info", "claim", "release")


def _walk(parser: argparse.ArgumentParser, path: str, out: dict[str, Any]) -> None:
    children: list[str] = []
    opts: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                children.append(name)
                _walk(child, f"{path} {name}", out)
        elif action.option_strings and action.help != argparse.SUPPRESS:
            opts.extend(o for o in action.option_strings if o.startswith("--"))
    out[path] = (children, opts)


def completion_bash(parser: argparse.ArgumentParser) -> str:
    """A bash completion script, generated from the parser so it cannot
    drift from the commands it completes."""
    # Rooted at "dibs" because bash refuses an empty associative-array key.
    tree: dict[str, Any] = {}
    _walk(parser, "dibs", tree)
    children = "\n".join(f'    ["{p}"]="{" ".join(c)}"' for p, (c, _) in tree.items())
    opts = "\n".join(f'    ["{p}"]="{" ".join(o)}"' for p, (_, o) in tree.items())
    things = "|".join(f"dibs {c}" for c in _THING_COMMANDS)
    return f"""# bash completion for dibs, from: dibs completion bash
declare -gA _dibs_children=(
{children}
)
declare -gA _dibs_opts=(
{opts}
)
_dibs() {{
    local cur=${{COMP_WORDS[COMP_CWORD]}} path=dibs word i
    for ((i = 1; i < COMP_CWORD; i++)); do
        word=${{COMP_WORDS[i]}}
        [[ " ${{_dibs_children[$path]}} " == *" $word "* ]] && path="$path $word"
    done
    if [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "${{_dibs_opts[$path]}}" -- "$cur"))
    elif [[ -n ${{_dibs_children[$path]}} ]]; then
        COMPREPLY=($(compgen -W "${{_dibs_children[$path]}}" -- "$cur"))
    elif [[ "|{things}|" == *"|$path|"* ]]; then
        COMPREPLY=($(compgen -W "$(timeout 1 dibs list --names 2>/dev/null)" -- "$cur"))
    fi
}}
complete -o default -F _dibs dibs
"""


def cmd_completion(args: argparse.Namespace) -> None:
    sys.stdout.write(completion_bash(build_parser()))


# ---- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dibs",
        description=(
            "Call dibs on shared lab machines. See who is using what, claim it, release "
            "it. A claim is a note for everyone else, not a lock, and it never expires "
            "unless you say so."
        ),
        epilog=(
            "The client finds the server via --url or DIBS_URL. Claiming needs a login: "
            "register, have the admin approve you, then login. Agents set "
            "DIBS_ASSISTED_BY; runners may pass a token in DIBS_TOKEN. Exit status: 0 done, "
            "1 claimed by someone else, 2 other errors."
        ),
    )
    parser.add_argument("--url", default=os.environ.get("DIBS_URL", ""), help="dibs server URL")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    data = argparse.ArgumentParser(add_help=False)
    group = data.add_mutually_exclusive_group()
    for fmt in ("json", "yaml", "toml"):
        group.add_argument(
            f"--{fmt}",
            dest="format",
            action="store_const",
            const=fmt,
            help=f"print the answer as {fmt.upper()}, for scripts and agents",
        )

    p = sub.add_parser("list", parents=[data], help="show every thing, free or claimed")
    p.add_argument("--mine", action="store_true", help="only the things you have claimed")
    # For the shell completion: thing names, one per line.
    p.add_argument("--names", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", parents=[data], help="show one thing, and what pixie knows of it")
    p.add_argument("thing")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("claim", help="claim things, all or none")
    p.add_argument("things", nargs="+", metavar="thing")
    p.add_argument("--for", dest="purpose", required=True, help="what the claim is for")
    p.add_argument(
        "--expires",
        type=parse_duration,
        default=None,
        metavar="DURATION",
        help="e.g. 2h, 90m, 1d12h (default: never)",
    )
    p.add_argument("--force", action="store_true", help="take things claimed by someone else")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="release things, or all of yours with --mine")
    p.add_argument("things", nargs="*", metavar="thing")
    p.add_argument("--mine", action="store_true", help="release everything you have claimed")
    p.add_argument("--force", action="store_true", help="release someone else's claim")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("register", help="ask the admin for a user, with an SSH public key")
    p.add_argument("user")
    p.add_argument("-i", "--key", help="SSH public key file (default: ~/.ssh/id_ed25519.pub, ...)")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("login", help="sign in with your SSH key and keep the token")
    p.add_argument("user", nargs="?", help="default: the user you registered or last logged in as")
    p.add_argument("-i", "--key", help="SSH public key file (default: the one you registered with)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="drop your token for this server")
    p.set_defaults(func=cmd_logout)

    def user_mode(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--user", action="store_true", help="the per-user service rather than the system one"
        )

    p = sub.add_parser("serve", help="run the server in the foreground")
    p.add_argument("--host", default=os.environ.get("DIBS_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DIBS_PORT", DEFAULT_PORT)))
    p.add_argument("--config", help="default: /etc/dibs/dibs.toml (with --user: ~/.config/dibs/)")
    p.add_argument("--data", help="default: /var/lib/dibs (with --user: ~/.local/state/dibs)")
    user_mode(p)
    p.set_defaults(func=cmd_serve)

    server = sub.add_parser("server", help="run dibs as a systemd service")
    ssub = server.add_subparsers(dest="verb", required=True, metavar="action")
    p = ssub.add_parser("install", help="write the unit for this dibs, enable and start it")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--print", action="store_true", help="only print the unit")
    user_mode(p)
    p.set_defaults(func=cmd_server_install)
    p = ssub.add_parser("uninstall", help="stop the service and remove its unit")
    user_mode(p)
    p.set_defaults(func=cmd_server_uninstall)
    for verb, text in (
        ("start", "start the service"),
        ("stop", "stop the service"),
        ("restart", "restart it, e.g. after editing dibs.toml"),
        ("status", "show whether it is running"),
        ("logs", "follow its log"),
    ):
        p = ssub.add_parser(verb, help=text)
        user_mode(p)
        p.set_defaults(func=cmd_server_ctl)

    admin = sub.add_parser("admin", help="approve and manage users, on the server host")
    asub = admin.add_subparsers(dest="action", required=True, metavar="action")
    for action, text, func, takes_name in (
        ("users", "list users and whether they are approved", cmd_admin_users, False),
        ("approve", "let a registered user log in", cmd_admin_approve, True),
        ("remove", "reject or remove a user, with their tokens", cmd_admin_remove, True),
        ("revoke", "end every login a user has", cmd_admin_revoke, True),
    ):
        p = asub.add_parser(action, help=text)
        if takes_name:
            p.add_argument("name")
        p.add_argument("--data", help="the server's data directory (default: /var/lib/dibs)")
        user_mode(p)
        p.set_defaults(func=func)

    p = sub.add_parser("skill", help="install the agent skill that says how to use dibs")
    p.add_argument("action", choices=["install", "print"])
    p.add_argument("--dir", help="skills directory (default: ~/.claude/skills)")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("completion", help="print a shell completion script")
    p.add_argument("shell", choices=["bash"])
    p.set_defaults(func=cmd_completion)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except Refused as exc:
        for sentence in exc.sentences:
            print(f"dibs: refused, {sentence}", file=sys.stderr)
        return EXIT_REFUSED
    except CliError as exc:
        print(f"dibs: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
