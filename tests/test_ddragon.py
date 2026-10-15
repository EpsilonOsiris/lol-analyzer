"""Data Dragon cache: version choice, downloads with an injected fetcher, offline fallback."""
import json

import pytest

from lolan import ddragon
from lolan.ddragon import BASE_URL, DDragon, FetchError, pick_version

VERSIONS = ["16.20.1", "16.19.1", "16.18.1", "15.24.1", "15.23.1", "lolpatch_3.7", "16.7.1", "16.1.1"]
PNG = b"\x89PNG fake"


def champion_json(cid, name, spells):
    return json.dumps({"data": {cid: {"id": cid, "name": name,
                                      "spells": [{"id": s, "name": n} for s, n in spells],
                                      "passive": {"name": "Pasiva de " + name}}}}).encode("utf-8")


class FakeCdn:
    """Serves a tiny Data Dragon; records every URL asked for."""

    def __init__(self, fail=None, missing=()):
        self.calls = []
        self.fail = fail          # exception to raise for every request (offline)
        self.missing = set(missing)

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        if any(m in url for m in self.missing):
            raise FetchError("404", not_found=True)
        if url.endswith("/api/versions.json"):
            return json.dumps(VERSIONS).encode()
        if url.endswith("/champion.json"):
            return json.dumps({"data": {"MonkeyKing": {"id": "MonkeyKing", "name": "Wukong"},
                                        "Fiddlesticks": {"id": "Fiddlesticks", "name": "Fiddlesticks"},
                                        "Ezreal": {"id": "Ezreal", "name": "Ezreal"}}}).encode()
        if url.endswith("/champion/Ezreal.json"):
            return champion_json("Ezreal", "Ezreal", [("EzrealQ", "Disparo místico"), ("EzrealW", "Flujo de esencia"),
                                                       ("EzrealE", "Translocación arcana"), ("EzrealR", "Descarga")])
        if "/champion/" in url and url.endswith(".json"):
            cid = url.rsplit("/", 1)[1][:-5]
            return champion_json(cid, cid, [(cid + "Q", "Q de " + cid)] * 4)
        if url.endswith("/item.json"):
            return json.dumps({"data": {"3152": {"name": "Cinturón cohete hextech"}}}).encode()
        if url.endswith(".png"):
            return PNG
        raise AssertionError(f"unexpected url {url}")


# ------------------------------------------------------------ pure selection
@pytest.mark.parametrize("game_version, expected", [
    ("16.20.824.8524", "16.20.1"),          # same patch
    ("16.7.123.4", "16.7.1"),               # 7 is not confused with 17
    ("15.24.1", "15.24.1"),
    ("12.3.1", "16.20.1"),                  # patch no longer listed: latest
    (None, "16.20.1"), ("", "16.20.1"), ("garbage", "16.20.1"),
])
def test_pick_version_by_patch(game_version, expected):
    assert pick_version(VERSIONS, game_version) == expected


def test_pick_version_takes_the_newest_of_the_patch_and_ignores_junk():
    assert pick_version(["16.20.1", "16.20.3", "16.20.2", "lolpatch_3.7"], "16.20.9.9") == "16.20.3"
    assert pick_version([], "16.20.1") is None
    assert pick_version(["lolpatch_3.7"], None) is None


def test_display_fallbacks_without_data():
    assert ddragon.champion_display("MonkeyKing") == "Wukong"
    assert ddragon.champion_display("FiddleSticks") == "Fiddlesticks"
    assert ddragon.champion_display("Ahri") == "Ahri"


# ------------------------------------------------------------------ downloads
def make_dd(tmp_path, cdn=None, **kw):
    cdn = cdn or FakeCdn()
    return DDragon(tmp_path / "dd", fetcher=cdn, **kw), cdn


def test_prepare_downloads_once_and_names_come_from_the_cache(tmp_path):
    dd, cdn = make_dd(tmp_path)
    dd.prepare(("16.20.824.8524",), ["Ezreal", "MonkeyKing", "FiddleSticks"], wait=5)
    version = dd.version_for("16.20.824.8524")
    assert version == "16.20.1"
    for rel in ("versions.json", "16.20.1/data/es_MX/champion.json", "16.20.1/data/es_MX/champion/Ezreal.json",
                "16.20.1/data/es_MX/champion/MonkeyKing.json", "16.20.1/data/es_MX/item.json",
                "16.20.1/img/champion/Ezreal.png", "16.20.1/img/champion/MonkeyKing.png",
                "16.20.1/img/champion/Fiddlesticks.png", "16.20.1/img/map/map11.png"):
        assert (tmp_path / "dd" / rel).is_file(), rel
    assert dd.icon_rel(version, "MonkeyKing") == "16.20.1/img/champion/MonkeyKing.png"
    assert dd.icon_rel(version, "FiddleSticks") == "16.20.1/img/champion/Fiddlesticks.png"
    assert dd.icon_rel(version, "Zed") is None
    assert dd.minimap_rel() == "16.20.1/img/map/map11.png"
    assert dd.champion_name(version, "MonkeyKing") == "Wukong"
    book = dd.names(version)
    assert book.spell("Ezreal", "ezrealq", 0) == ("Q", "Disparo místico")
    assert book.spell("Ezreal", "somethingelse", 2) == ("E", "Translocación arcana")
    assert book.spell("Ezreal", "somethingelse", 64) is None
    assert book.passive("Ezreal") == "Pasiva de Ezreal"
    assert book.item(3152) == "Cinturón cohete hextech"

    n = len(cdn.calls)
    dd.prepare(("16.20.824.8524",), ["Ezreal", "MonkeyKing", "FiddleSticks"], wait=5)
    assert len(cdn.calls) == n or all("versions.json" in c for c in cdn.calls[n:])       # nothing new to fetch
    assert all(c.startswith(BASE_URL) for c in cdn.calls)


def test_prepare_uses_the_game_patch_version(tmp_path):
    dd, cdn = make_dd(tmp_path)
    dd.prepare(("15.24.9.9",), ["Ezreal"], wait=5)
    assert (tmp_path / "dd" / "15.24.1" / "img" / "champion" / "Ezreal.png").is_file()
    assert any("/cdn/15.24.1/" in c for c in cdn.calls)
    # The minimap is only ever taken from the latest version.
    assert (tmp_path / "dd" / "16.20.1" / "img" / "map" / "map11.png").is_file()


def test_prepare_waits_at_most_the_given_time(tmp_path):
    import threading
    import time

    gate = threading.Event()

    def slow(url):
        gate.wait(5)
        raise FetchError("nope")

    dd = DDragon(tmp_path / "dd", fetcher=slow)
    started = time.monotonic()
    dd.prepare((None,), ["Ezreal"], wait=0.2)
    assert time.monotonic() - started < 2
    assert dd.busy
    gate.set()
    assert dd.wait(5)


# -------------------------------------------------------------------- offline
def test_offline_degrades_and_backs_off(tmp_path):
    dd, cdn = make_dd(tmp_path, FakeCdn(fail=FetchError("sin red")), retry_after=3600)
    dd.prepare(("16.20.1",), ["Ezreal", "MonkeyKing"], wait=5)
    assert dd.offline
    assert dd.version_for("16.20.1") is None and dd.latest() is None
    assert dd.names(dd.version_for("16.20.1")) is None
    assert dd.icon_rel("16.20.1", "Ezreal") is None and dd.minimap_rel() is None
    assert dd.champion_name(None, "MonkeyKing") == "Wukong"
    tried = len(cdn.calls)
    dd.prepare(("16.20.1",), ["Ezreal"], wait=1)                # paused: no new network attempts
    assert len(cdn.calls) == tried
    assert not (tmp_path / "dd").exists() or not list((tmp_path / "dd").rglob("*.png"))


def test_any_fetcher_exception_counts_as_offline(tmp_path):
    dd, _cdn = make_dd(tmp_path, FakeCdn(fail=OSError("boom")))
    dd.prepare((None,), [], wait=5)
    assert dd.offline


def test_recovers_after_the_pause(tmp_path):
    now = [0.0]
    cdn = FakeCdn(fail=FetchError("sin red"))
    dd = DDragon(tmp_path / "dd", fetcher=cdn, retry_after=60, clock=lambda: now[0])
    dd.prepare((None,), ["Ezreal"], wait=5)
    assert dd.offline
    cdn.fail = None
    now[0] = 61
    assert not dd.offline
    dd.prepare((None,), ["Ezreal"], wait=5)
    assert dd.icon_rel(dd.latest(), "Ezreal") is not None


def test_missing_file_is_not_offline_and_is_not_retried(tmp_path):
    dd, cdn = make_dd(tmp_path, FakeCdn(missing=["img/champion/Ezreal.png"]))
    dd.prepare((None,), ["Ezreal", "MonkeyKing"], wait=5)
    assert not dd.offline
    assert dd.icon_rel("16.20.1", "Ezreal") is None
    assert dd.icon_rel("16.20.1", "MonkeyKing") is not None
    asked = sum(1 for c in cdn.calls if c.endswith("/Ezreal.png"))
    dd.prepare((None,), ["Ezreal", "MonkeyKing"], wait=5)
    assert sum(1 for c in cdn.calls if c.endswith("/Ezreal.png")) == asked


def test_stale_versions_list_is_used_when_refresh_fails(tmp_path):
    import os
    import time

    dd, _ = make_dd(tmp_path)
    (tmp_path / "dd").mkdir()
    path = tmp_path / "dd" / "versions.json"
    path.write_text(json.dumps(["16.19.1", "16.18.1"]), encoding="utf-8")
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))
    dd2 = DDragon(tmp_path / "dd", fetcher=FakeCdn(fail=FetchError("sin red")))
    dd2.prepare((None,), [], wait=5)
    assert dd2.latest() == "16.19.1"


# ------------------------------------------------------------------- the route
def test_resolve_path_only_serves_cached_pngs(tmp_path):
    dd, _ = make_dd(tmp_path)
    dd.prepare((None,), ["Ezreal"], wait=5)
    assert dd.resolve_path("16.20.1/img/champion/Ezreal.png") is not None
    assert dd.resolve_path("16.20.1/img/champion/Zed.png") is None
    assert dd.resolve_path("16.20.1/data/es_MX/item.json") is None
    assert dd.resolve_path("../versions.json") is None
    assert dd.resolve_path("16.20.1/../../x.png") is None
    assert dd.resolve_path("abc/img/champion/Ezreal.png") is None
