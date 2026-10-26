# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Sources: the config file, pixie polling, and how a sync merges them."""

from __future__ import annotations

from pathlib import Path

import pytest

from dibs._sources import load_config, sync
from dibs._store import Store
from tests.conftest import CONFIG, PIXIE, FakePixie


def test_missing_config_is_empty(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.toml")
    assert (config.things, config.pixies) == ([], [])


@pytest.mark.parametrize(
    "text",
    [
        "sync_interval = 1",
        "[[pixie]]\nurl = 'pixie:8080'",
        "[[thing]]\nkind = 'machine'",
        "[[thing]]\nname = 'Bad_Name'",
        "[[thing]]\nname = 'a'\n[[thing]]\nname = 'a'",
        "not toml [",
    ],
)
def test_bad_config_names_the_problem(tmp_path: Path, text: str) -> None:
    path = tmp_path / "dibs.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match="dibs.toml"):
        load_config(path)


def _synced(tmp_path: Path, pixie: FakePixie) -> Store:
    path = tmp_path / "dibs.toml"
    path.write_text(CONFIG)
    store = Store(tmp_path / "dibs.db")
    sync(store, load_config(path), [pixie.source()])
    return store


def test_sync_merges_pixie_and_config(tmp_path: Path, pixie: FakePixie) -> None:
    store = _synced(tmp_path, pixie)
    things = {t.name: t for t in store.list_things()}
    # The unnamed pixie machine is skipped; the config adds ryzen-01.
    assert sorted(things) == ["epyc-02", "ryzen-01", "warp"]
    assert things["ryzen-01"].source == "config"
    assert things["warp"].source == f"pixie:{PIXIE}"
    assert things["warp"].attrs == {"mac": "aa:bb:cc:dd:ee:80", "pixie": PIXIE, "labels": ["gpu"]}
    # A config entry for a pixie machine adds to it instead of duplicating it.
    assert things["epyc-02"].source == f"pixie:{PIXIE}"
    assert things["epyc-02"].attrs["rack"] == "a1"
    assert things["epyc-02"].attrs["boot_mode"] == "nbdboot-overlay"


def test_unreachable_pixie_keeps_its_things(tmp_path: Path, pixie: FakePixie) -> None:
    store = _synced(tmp_path, pixie)
    pixie.up = False
    sync(store, load_config(tmp_path / "dibs.toml"), [pixie.source()])
    assert "warp" in {t.name for t in store.list_things()}


def test_a_machine_pixie_forgets_disappears(tmp_path: Path, pixie: FakePixie) -> None:
    store = _synced(tmp_path, pixie)
    pixie.machines = [m for m in pixie.machines if m.get("name") != "warp"]
    sync(store, load_config(tmp_path / "dibs.toml"), [pixie.source()])
    assert "warp" not in {t.name for t in store.list_things()}
