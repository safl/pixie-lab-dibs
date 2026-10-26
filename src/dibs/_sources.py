# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Where things come from: the config file, and pixie servers.

The config file (``/etc/dibs/dibs.toml`` by default) lists things by
hand and names the pixie servers to poll::

    sync_interval = 60          # seconds between syncs

    [[pixie]]
    url = "http://pixie:8080"   # every machine pixie knows becomes a thing

    [[thing]]
    name = "ryzen-01"           # a machine pixie does not boot
    kind = "machine"            # default
    mac = "aa:bb:cc:dd:ee:ff"   # any other key is kept as an attribute

A ``[[thing]]`` with the name of a pixie machine adds its attributes to
that machine rather than making a second thing.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from dibs._store import Store, Thing, ts, validate_thing_name

log = logging.getLogger("dibs")

CONFIG_SOURCE = "config"


@dataclass
class Config:
    things: list[Thing] = field(default_factory=list)
    pixies: list[str] = field(default_factory=list)
    sync_interval: int = 60


def load_config(path: Path) -> Config:
    """Read the config file; a missing file is an empty config, so a
    fresh install starts with no things rather than failing."""
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    interval = raw.get("sync_interval", 60)
    if not isinstance(interval, int) or interval < 5:
        raise ValueError(f"{path}: sync_interval must be a whole number of seconds, at least 5")
    pixies = []
    for i, entry in enumerate(raw.get("pixie", [])):
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f'{path}: pixie #{i + 1} needs a url = "http://..."')
        pixies.append(url.rstrip("/"))
    things = []
    seen: set[str] = set()
    for i, entry in enumerate(raw.get("thing", [])):
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError(f"{path}: thing #{i + 1} needs a name")
        attrs = dict(entry)
        try:
            name = validate_thing_name(str(attrs.pop("name")))
        except ValueError as exc:
            raise ValueError(f"{path}: thing #{i + 1}: {exc}") from exc
        if name in seen:
            raise ValueError(f"{path}: thing {name!r} is listed twice")
        seen.add(name)
        kind = str(attrs.pop("kind", "machine"))
        things.append(Thing(name, kind, CONFIG_SOURCE, attrs))
    return Config(things, pixies, interval)


class PixieSource:
    """A pixie server's machines, as things of kind ``machine``."""

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.id = f"pixie:{self.url}"
        self._client = client or httpx.Client(timeout=10)

    def fetch(self) -> list[Thing] | None:
        """The machines pixie lists now, or ``None`` when pixie cannot
        be reached, so the caller can keep what it had."""
        try:
            r = self._client.get(f"{self.url}/machines")
            r.raise_for_status()
            machines = r.json()["machines"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("pixie %s: %s; keeping its things from the last sync", self.url, exc)
            return None
        things = []
        for m in machines:
            try:
                name = validate_thing_name(str(m.get("name", "")))
            except ValueError:
                continue  # a pixie from before machine names; nothing to call it
            attrs: dict[str, Any] = {"mac": m["mac"], "pixie": self.url}
            for key in ("boot_mode", "labels", "last_seen_ip"):
                if m.get(key):
                    attrs[key] = m[key]
            things.append(Thing(name, "machine", self.id, attrs))
        return things

    def info(self, name: str) -> dict[str, Any] | None:
        """pixie's description of one machine, or ``None`` if it has none."""
        try:
            r = self._client.get(f"{self.url}/machines/{name}/info")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            info: dict[str, Any] = r.json()
            return info
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("pixie %s: info for %s: %s", self.url, name, exc)
            return None


def sync(
    store: Store, config: Config, pixies: list[PixieSource], *, now: datetime | None = None
) -> None:
    """Rebuild the things table from every source."""
    stamp = ts(now or datetime.now(UTC))
    previous = store.list_things()
    merged: dict[str, Thing] = {}
    for source in pixies:
        fetched = source.fetch()
        if fetched is None:
            fetched = [t for t in previous if t.source == source.id]
        else:
            for t in fetched:
                t.synced_at = stamp
        for t in fetched:
            merged.setdefault(t.name, t)
    for t in config.things:
        base = merged.get(t.name)
        if base is not None:
            base.attrs = {**base.attrs, **t.attrs}
            base.kind = t.kind
        else:
            merged[t.name] = Thing(t.name, t.kind, t.source, dict(t.attrs), stamp)
    store.replace_things(sorted(merged.values(), key=lambda t: t.name))
