# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""The dibs server: things, claims, users, and the login hook.

Reads are open to anyone who can reach the server. Claiming and
releasing take a user's bearer token, which ``/auth/login`` hands out
for a signature by the user's registered SSH key. Approving users is
not an HTTP route at all: it is ``dibs admin approve`` on the server
host, so dibs has no admin password to share or leak.

Claims, takeovers and logins are logged to the ``dibs`` logger, which
the systemd unit sends to the journal.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

import dibs
from dibs._skill import skill_body
from dibs._sources import Config, PixieSource, load_config, sync
from dibs._store import Claim, ClaimConflict, Store, Thing, covering, describe, sentence
from dibs._users import DuplicateUser, LoginFailed, NotApproved, UsersStore

log = logging.getLogger("dibs")


class ClaimBody(BaseModel):
    things: list[str] = Field(..., min_length=1)
    purpose: str
    assisted_by: str = Field(default="", description="Agent:model, as in an Assisted-by trailer")
    session: str = Field(default="", description="Which agent session of the user is claiming")
    expires_in: int | None = Field(default=None, description="Seconds; null never expires")
    force: bool = False


class ReleaseBody(BaseModel):
    things: list[str] = Field(default_factory=list)
    assisted_by: str = ""
    session: str = ""
    mine: bool = Field(
        default=False, description="Every live claim of this user, assisted_by and session"
    )
    force: bool = False


class RegisterBody(BaseModel):
    user: str
    pubkey: str


class ChallengeBody(BaseModel):
    user: str


class LoginBody(BaseModel):
    user: str
    nonce: str
    signature: str


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def current_user(request: Request) -> str:
    """Dependency: the user a bearer token belongs to, else 401."""
    user = request.app.state.users.user_for_token(_bearer(request))
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="not logged in; a person sets up with dibs register and dibs login "
            "(agents use their person's login)",
        )
    return str(user)


def _view(
    name: str, thing: Thing | None, claims: dict[str, Claim], now: datetime
) -> dict[str, Any]:
    """One thing as the API shows it. ``claim`` is a claim on this very
    name; ``covered_by`` lists every live claim that says who is on it,
    which for a part includes its thing's claim and for a thing its
    parts'. A claim on a thing no source lists any more still shows,
    with ``source`` ``gone``, so it can be released."""
    out: dict[str, Any] = {
        "name": name,
        "kind": thing.kind if thing else "unknown",
        "source": thing.source if thing else "gone",
        "sentence": describe(name, claims, now),
    }
    if thing and thing.attrs:
        out["attrs"] = thing.attrs
    own = claims.get(name)
    if own is not None and own.is_live(now):
        out["claim"] = own.to_dict()
    found = covering(name, claims, now)
    if found:
        out["covered_by"] = [{"thing": c.thing, **c.to_dict()} for c in found]
    return out


def _conflict(exc: ClaimConflict) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"message": "claimed by someone else", "claimed": exc.sentences}
    )


def create_app(
    config_path: Path,
    data_dir: Path,
    *,
    pixie_sources: list[PixieSource] | None = None,
    background_sync: bool = True,
) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "dibs.db")
    users = UsersStore(data_dir / "dibs.db")
    config = load_config(config_path)
    pixies = pixie_sources if pixie_sources is not None else [PixieSource(u) for u in config.pixies]

    def sync_now() -> None:
        sync(store, config, pixies)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        sync_now()
        task = asyncio.create_task(_sync_loop(config, sync_now)) if background_sync else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="dibs", version=dibs.__version__, lifespan=lifespan)
    app.state.store = store
    app.state.users = users
    app.state.config = config
    app.state.pixies = {p.id: p for p in pixies}
    app.state.sync = sync_now

    def resolve(names: list[str]) -> list[str]:
        known = {t.name for t in store.list_things()}
        wanted = [n.strip().lower() for n in names]
        unknown = [n for n in wanted if n not in known]
        if unknown:
            raise HTTPException(status_code=404, detail=f"no such thing: {', '.join(unknown)}")
        return wanted

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": dibs.__version__}

    @app.get("/things")
    def list_things() -> dict[str, list[dict[str, Any]]]:
        now = datetime.now(UTC)
        things = {t.name: t for t in store.list_things()}
        claims = store.list_claims()
        names = sorted(set(things) | {n for n, c in claims.items() if c.is_live(now)})
        return {"things": [_view(n, things.get(n), claims, now) for n in names]}

    @app.get("/things/{name}")
    def get_thing(name: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        name = name.strip().lower()
        thing = store.get_thing(name)
        claims = store.list_claims()
        if thing is None and name not in claims:
            raise HTTPException(status_code=404, detail=f"no such thing: {name}")
        out = _view(name, thing, claims, now)
        if thing is not None and thing.source in app.state.pixies:
            info = app.state.pixies[thing.source].info(thing.name)
            if info is not None:
                out["pixie"] = info
        return out

    @app.get("/things/{name}/claim", response_class=PlainTextResponse)
    def thing_claim_text(name: str) -> PlainTextResponse:
        name = name.strip().lower()
        if store.get_thing(name) is None:
            raise HTTPException(status_code=404, detail=f"no such thing: {name}")
        return PlainTextResponse(describe(name, store.list_claims(), datetime.now(UTC)) + "\n")

    @app.get("/claim", response_class=PlainTextResponse)
    def claim_for_host(mac: str = "", host: str = "") -> PlainTextResponse:
        """The claim sentence for the machine asking, found by any of its
        MACs (comma-separated) or else its hostname. For the login hook."""
        thing = store.thing_by_mac(mac.split(",")) or (store.get_thing(host) if host else None)
        if thing is None:
            raise HTTPException(status_code=404, detail="not a thing dibs knows")
        text = describe(thing.name, store.list_claims(), datetime.now(UTC))
        return PlainTextResponse(text + "\n")

    @app.post("/claims")
    def claim(body: ClaimBody, user: str = Depends(current_user)) -> dict[str, Any]:
        names = resolve(body.things)
        now = datetime.now(UTC)
        try:
            pairs = store.claim(
                names,
                holder=user,
                purpose=body.purpose,
                assisted_by=body.assisted_by,
                session=body.session,
                expires_in=body.expires_in,
                force=body.force,
                now=now,
            )
        except ClaimConflict as exc:
            raise _conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for before, after in pairs:
            if before is not None and before.holder != after.holder:
                log.warning("%s took %s from %s", user, after.thing, before.holder)
            log.info("%s", sentence(after.thing, after, now))
        things = {t.name: t for t in store.list_things()}
        claims = store.list_claims()
        claimed = list(dict.fromkeys(a.thing for _, a in pairs))
        return {"things": [_view(n, things.get(n), claims, now) for n in claimed]}

    @app.post("/claims/release")
    def release(body: ReleaseBody, user: str = Depends(current_user)) -> dict[str, Any]:
        now = datetime.now(UTC)
        if body.mine == bool(body.things):
            raise HTTPException(status_code=422, detail="name things to release, or set mine")
        if body.mine:
            names = [
                c.thing for c in store.claimed_by(user, body.assisted_by, body.session, now=now)
            ]
        else:
            names = [n.strip().lower() for n in body.things]
        try:
            released = store.release(
                names,
                holder=user,
                assisted_by=body.assisted_by,
                session=body.session,
                force=body.force,
                now=now,
            )
        except ClaimConflict as exc:
            raise _conflict(exc) from exc
        things = {t.name: t for t in store.list_things()}
        claims = store.list_claims()
        for c in released:
            log.info("%s released %s (held by %s)", user, c.thing, c.holder)
        return {"things": [_view(c.thing, things.get(c.thing), claims, now) for c in released]}

    @app.post("/auth/register", status_code=202)
    def register(body: RegisterBody) -> dict[str, str]:
        try:
            u = users.register(body.user, body.pubkey)
        except DuplicateUser as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        log.info(
            "%s registered with key %s; approve with: dibs admin approve %s",
            u.name,
            u.fingerprint,
            u.name,
        )
        return {"user": u.name, "status": u.status, "fingerprint": u.fingerprint}

    @app.post("/auth/challenge")
    def challenge(body: ChallengeBody) -> dict[str, str]:
        try:
            nonce, message = users.challenge(body.user)
        except NotApproved as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"nonce": nonce, "message": message}

    @app.post("/auth/login")
    def login(body: LoginBody) -> dict[str, str]:
        try:
            token = users.login(body.user, body.nonce, body.signature)
        except LoginFailed as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        name = body.user.strip().lower()
        log.info("%s logged in", name)
        return {"user": name, "token": token}

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        users.logout(_bearer(request))
        return Response(status_code=204)

    @app.get("/auth/whoami")
    def whoami(user: str = Depends(current_user)) -> dict[str, str]:
        return {"user": user}

    @app.get("/login.sh", response_class=PlainTextResponse)
    def login_hook(request: Request) -> PlainTextResponse:
        """A ``profile.d`` snippet that prints this machine's claim at
        login. It points back at whichever dibs URL fetched it."""
        return PlainTextResponse(LOGIN_HOOK.format(base=str(request.base_url)))

    @app.get("/agents", response_class=PlainTextResponse)
    def agents(request: Request) -> PlainTextResponse:
        """How to use dibs on this lab, for a person or an agent pointed at
        it: the setup with this server's URL filled in, what is claimed
        right now, then the protocol from the agent skill."""
        base = str(request.base_url).rstrip("/")
        now_lines = "\n".join(f"- {t['sentence']}" for t in list_things()["things"])
        page = AGENTS_PAGE.format(base=base, now=now_lines or "- nothing to claim yet")
        return PlainTextResponse(page + "\n" + skill_body(), media_type="text/plain; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        rows = list_things()["things"]
        body = "\n".join(
            f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(r['kind'])}</td>"
            f"<td>{html.escape(r['sentence'])}</td></tr>"
            for r in rows
        )
        return HTMLResponse(PAGE.format(version=dibs.__version__, rows=body))

    return app


async def _sync_loop(config: Config, sync_now: Callable[[], None]) -> None:
    while True:
        await asyncio.sleep(config.sync_interval)
        try:
            await asyncio.to_thread(sync_now)
        except Exception:  # keep syncing; one bad round must not stop the loop
            log.exception("sync failed")


# Sourced from /etc/profile.d by login shells on the machine. It must
# never slow down or break a login: interactive shells only, one request
# with a short timeout, and silence when dibs is unreachable.
LOGIN_HOOK = """\
# dibs claim notice, from {base}login.sh
case $- in *i*)
    if command -v curl >/dev/null 2>&1; then
        _dibs_macs=$(cat /sys/class/net/*/address 2>/dev/null \\
            | grep -v '^00:00:00:00:00:00$' | tr '\\n' ',')
        _dibs_line=$(curl -fsS --connect-timeout 1 --max-time 2 -G \\
            --data-urlencode "mac=$_dibs_macs" --data-urlencode "host=$(hostname -s)" \\
            "{base}claim" 2>/dev/null) && printf '*** %s ***\\n' "$_dibs_line"
        unset _dibs_macs _dibs_line
    fi
    ;;
esac
"""

AGENTS_PAGE = """\
# dibs on this lab

The machines on this lab are shared by people, ai-agents and
automation-runners. Each is claimed with dibs, at {base}, before anyone
touches it. Read the part for you.

## If you are a person: set up once per workstation

```
uv tool install pixie-lab-dibs
echo 'export DIBS_URL={base}' >> ~/.bashrc
dibs register <name>     # then ask the lab's admin to approve you
dibs login
dibs skill install       # Claude Code agents then follow the rules below by themselves
```

Every agent you run on this workstation uses this login. Agents never
register or log in themselves.

## If you are an agent: before anything else

1. Check that your human has set up this workstation:

   ```
   DIBS_URL={base} dibs whoami
   ```

   It prints your human's name. If it fails, stop, and give your human the
   setup above. Do not register or log in yourself.

1. Get a session tag for this conversation, once, with `dibs session`, and
   pass it, with who you are, on every dibs command:

   ```
   DIBS_URL={base} DIBS_ASSISTED_BY="Claude Code:<model>" DIBS_SESSION=<tag> dibs ...
   ```

   Your claims then say which agent made them, and no other agent of the
   same person can take or release them without `--force`.

## Right now

{now}

"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>dibs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
</style></head><body>
<h1>dibs</h1>
<p>Who is using which thing. Claim one with
<code>dibs claim &lt;name&gt; --for &lt;purpose&gt;</code>.
New here, or an agent? Read <a href="/agents">/agents</a>.</p>
<table><thead><tr><th>Name</th><th>Kind</th><th>Claim</th></tr></thead>
<tbody>
{rows}
</tbody></table>
<p><small>dibs {version}</small></p>
</body></html>
"""
