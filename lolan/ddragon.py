"""Riot's public Data Dragon CDN: minimap, champion icons and Spanish ability names.

Everything is cached under data/ddragon/<version>/ and the panel only ever reads that cache while
rendering a page. Downloads run in one background thread; a page may wait a few seconds for them
(`prepare(..., wait=...)`) but never longer. With no connection (or nothing cached) every lookup
returns None and the pages fall back to a schematic map and champion initials.

Layout of the cache:
    versions.json
    <version>/data/es_MX/champion.json        list of champions (display names)
    <version>/data/es_MX/champion/<Id>.json   spells + passive of one champion
    <version>/data/es_MX/item.json            item names
    <version>/img/champion/<Id>.png           square icon
    <version>/img/item/<id>.png               item icon (only the items a live game shows)
    <version>/img/map/map11.png               minimap (Summoner's Rift)
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE_URL = "https://ddragon.leagueoflegends.com"
LANG = "es_MX"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSIONS_MAX_AGE_S = 6 * 3600
MINIMAP = "img/map/map11.png"
SPELL_LETTERS = "QWER"

# championName in the match JSON vs. the id Data Dragon uses: the lookup is done on the lowercase
# alphanumeric form, so "FiddleSticks" finds "Fiddlesticks". These names are only used when the
# champion list is not cached (offline); Data Dragon's own list wins otherwise.
DISPLAY_FALLBACKS = {
    "monkeyking": "Wukong", "fiddlesticks": "Fiddlesticks", "kaisa": "Kai'Sa", "ksante": "K'Sante",
    "chogath": "Cho'Gath", "khazix": "Kha'Zix", "velkoz": "Vel'Koz", "kogmaw": "Kog'Maw",
    "reksai": "Rek'Sai", "belveth": "Bel'Veth", "leblanc": "LeBlanc", "drmundo": "Dr. Mundo",
    "jarvaniv": "Jarvan IV", "masteryi": "Master Yi", "missfortune": "Miss Fortune",
    "twistedfate": "Twisted Fate", "tahmkench": "Tahm Kench", "xinzhao": "Xin Zhao",
    "aurelionsol": "Aurelion Sol", "leesin": "Lee Sin", "nunu": "Nunu y Willump",
    "renata": "Renata Glasc",
}


class FetchError(Exception):
    """A download failed. `not_found` means the server answered 404 (retrying is pointless)."""

    def __init__(self, message="", not_found=False):
        super().__init__(message)
        self.not_found = not_found


_local = threading.local()


def default_fetch(url: str) -> bytes:
    """GET `url` with short timeouts (the panel never waits long for the CDN)."""
    import requests

    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()      # keep-alive: one TLS handshake per thread
    try:
        resp = session.get(url, timeout=(3.05, 8))
    except requests.RequestException as exc:
        raise FetchError(str(exc)) from exc
    if resp.status_code == 404:
        raise FetchError("404", not_found=True)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    return resp.content


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def champion_display(name) -> str:
    """Readable champion name without any download ("MonkeyKing" -> "Wukong")."""
    return DISPLAY_FALLBACKS.get(norm(name)) or str(name or "?")


def version_key(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def pick_version(versions, game_version) -> str | None:
    """Newest Data Dragon version of the game's patch (16.20.824.8524 -> 16.20.x), else the latest."""
    valid = sorted((v for v in versions or [] if isinstance(v, str) and VERSION_RE.match(v)),
                   key=version_key, reverse=True)
    if not valid:
        return None
    parts = str(game_version or "").split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        prefix = f"{int(parts[0])}.{int(parts[1])}."
        for v in valid:
            if v.startswith(prefix):
                return v
    return valid[0]


class NameBook:
    """Spanish ability / item names of one Data Dragon version, read from the cache."""

    def __init__(self, dd: "DDragon", version: str):
        self._dd = dd
        self.version = version
        self._champs: dict = {}
        self._items = None

    def _champion(self, champion):
        cid = self._dd.champion_id(self.version, champion)
        if cid not in self._champs:
            self._champs[cid] = self._dd.read_json(f"{self.version}/data/{LANG}/champion/{cid}.json")
        data = (self._champs[cid] or {}).get("data") or {}
        return data.get(cid)

    def spell(self, champion, spell_name, slot):
        """(letter, name) of the ability, matching the internal spell name first, then the slot."""
        info = self._champion(champion)
        spells = (info or {}).get("spells") or []
        wanted = str(spell_name or "").lower()
        for i, sp in enumerate(spells[:4]):
            if wanted and str(sp.get("id", "")).lower() == wanted:
                return SPELL_LETTERS[i], sp.get("name") or ""
        if isinstance(slot, int) and 0 <= slot <= 3:
            name = spells[slot].get("name") if slot < len(spells) else None
            return SPELL_LETTERS[slot], name or ""
        return None

    def passive(self, champion):
        info = self._champion(champion)
        return ((info or {}).get("passive") or {}).get("name") or None

    def item(self, item_id):
        if not self._items:         # also retried while empty: the file may be downloaded later
            self._items = (self._dd.read_json(f"{self.version}/data/{LANG}/item.json") or {}).get("data") or {}
        entry = self._items.get(str(item_id))
        return entry.get("name") if isinstance(entry, dict) else None

    def item_cost(self, item_id):
        """Total gold cost of the item (components included), or None when the item is not in the cache."""
        self.item(item_id)
        entry = (self._items or {}).get(str(item_id))
        total = ((entry or {}).get("gold") or {}).get("total") if isinstance(entry, dict) else None
        return total if isinstance(total, (int, float)) and not isinstance(total, bool) else None

    def has_items(self) -> bool:
        self.item(0)
        return bool(self._items)


class DDragon:
    def __init__(self, cache_dir, fetcher=None, retry_after: float = 60.0, clock=time.monotonic):
        self.cache_dir = Path(cache_dir)
        self._fetcher = fetcher
        self._retry_after = retry_after
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._down_until = 0.0
        self._missing: set = set()          # urls the CDN answered 404 for
        self._pending: set = set()          # job keys queued or running
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._json_cache: dict = {}
        self._books: dict = {}

    # ----------------------------------------------------------- cache reads
    def _path(self, rel: str) -> Path:
        return self.cache_dir / rel

    def has(self, rel: str) -> bool:
        return self._path(rel).is_file()

    def read_json(self, rel: str):
        path = self._path(rel)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._json_cache.get(rel)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            return None
        self._json_cache[rel] = (stamp, data)
        return data

    def versions(self) -> list[str]:
        data = self.read_json("versions.json")
        return [v for v in data if isinstance(v, str) and VERSION_RE.match(v)] if isinstance(data, list) else []

    def version_for(self, game_version) -> str | None:
        return pick_version(self.versions(), game_version)

    def latest(self) -> str | None:
        return pick_version(self.versions(), None)

    def _champion_map(self, version) -> dict:
        """normalized id -> (id, display name) from the cached champion list of that version."""
        data = (self.read_json(f"{version}/data/{LANG}/champion.json") or {}).get("data") or {}
        return {norm(cid): (cid, (c or {}).get("name") or cid) for cid, c in data.items()}

    def champion_id(self, version, name) -> str:
        found = self._champion_map(version).get(norm(name))
        if found:
            return found[0]
        # Not in the list (offline or a brand new champion): Riot's ids are the plain names
        # except a couple of capitalisation quirks.
        return "Fiddlesticks" if norm(name) == "fiddlesticks" else str(name or "")

    def champion_name(self, version, name) -> str:
        found = self._champion_map(version).get(norm(name)) if version else None
        return found[1] if found else champion_display(name)

    def icon_rel(self, version, name) -> str | None:
        """Cache-relative path of the champion icon, or None while it is not downloaded."""
        if not version:
            return None
        rel = f"{version}/img/champion/{self.champion_id(version, name)}.png"
        return rel if self.has(rel) else None

    def item_icon_rel(self, version, item_id) -> str | None:
        """Cache-relative path of the item icon, or None while it is not downloaded."""
        if not version or not str(item_id).isdigit():
            return None
        rel = f"{version}/img/item/{int(item_id)}.png"
        return rel if self.has(rel) else None

    def minimap_rel(self) -> str | None:
        for v in sorted(self.versions(), key=version_key, reverse=True):
            rel = f"{v}/{MINIMAP}"
            if self.has(rel):
                return rel
        return None

    def names(self, version) -> NameBook | None:
        if not version:
            return None
        if version not in self._books:
            self._books[version] = NameBook(self, version)
        return self._books[version]

    def resolve_path(self, rel: str) -> Path | None:
        """Absolute path of a cached .png for the file route; None for anything else."""
        parts = rel.split("/")
        if (len(parts) < 3 or not VERSION_RE.match(parts[0]) or not rel.endswith(".png")
                or any(p in ("", ".", "..") for p in parts)):
            return None
        path = self._path(rel)
        return path if path.is_file() else None

    # ------------------------------------------------------------- download
    def _is_down(self) -> bool:
        with self._lock:
            return self._clock() < self._down_until

    @property
    def offline(self) -> bool:
        """True while downloads are paused after a failed attempt."""
        return self._is_down()

    def _mark_down(self) -> None:
        with self._lock:
            self._down_until = self._clock() + self._retry_after

    def _fetch(self, url: str) -> bytes:
        return (self._fetcher or default_fetch)(url)

    def _download(self, rel: str, url: str) -> bool:
        """Fetch `url` into the cache (once). Raises _Offline when the network seems gone."""
        if self.has(rel):
            return True
        if url in self._missing:
            return False
        if self._is_down():
            raise _Offline()
        try:
            content = self._fetch(url)
        except FetchError as exc:
            if exc.not_found:
                self._missing.add(url)
                return False
            self._mark_down()
            raise _Offline() from exc
        except Exception as exc:  # any other failure of the fetcher counts as "no connection"
            self._mark_down()
            raise _Offline() from exc
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        self._json_cache.pop(rel, None)
        return True

    def _ensure_versions(self) -> list[str]:
        path = self._path("versions.json")
        try:
            fresh = time.time() - path.stat().st_mtime < VERSIONS_MAX_AGE_S
        except OSError:
            fresh = False
        if not fresh and not self._is_down():
            try:
                content = self._fetch(f"{BASE_URL}/api/versions.json")
                json.loads(content.decode("utf-8"))      # refuse to cache garbage
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
                tmp.write_bytes(content)
                os.replace(tmp, path)
                self._json_cache.pop("versions.json", None)
            except Exception:
                self._mark_down()
        return self.versions()

    def _run_game_job(self, game_version, champions, minimap, items=()) -> None:
        versions = self._ensure_versions()
        version = pick_version(versions, game_version)
        if version is None:
            return
        data_rel = f"{version}/data/{LANG}"
        self._download(f"{data_rel}/champion.json", f"{BASE_URL}/cdn/{version}/data/{LANG}/champion.json")
        tasks = []
        for name in champions:
            cid = self.champion_id(version, name)
            tasks.append((f"{data_rel}/champion/{cid}.json",
                          f"{BASE_URL}/cdn/{version}/data/{LANG}/champion/{cid}.json"))
            tasks.append((f"{version}/img/champion/{cid}.png", f"{BASE_URL}/cdn/{version}/img/champion/{cid}.png"))
        if minimap and self.minimap_rel() is None:
            latest = pick_version(versions, None)
            tasks.append((f"{latest}/{MINIMAP}", f"{BASE_URL}/cdn/{latest}/{MINIMAP}"))
        tasks.append((f"{data_rel}/item.json", f"{BASE_URL}/cdn/{version}/data/{LANG}/item.json"))
        for item_id in items:
            tasks.append((f"{version}/img/item/{item_id}.png", f"{BASE_URL}/cdn/{version}/img/item/{item_id}.png"))
        offline = False
        with ThreadPoolExecutor(max_workers=6) as pool:
            for future in [pool.submit(self._download, rel, url) for rel, url in tasks]:
                try:
                    future.result()
                except _Offline:
                    offline = True
        if offline:
            raise _Offline()

    def _loop(self) -> None:
        while True:
            key, func = self._jobs.get()
            try:
                func()
            except _Offline:
                pass
            except Exception:
                pass            # a broken download must never take the worker down
            finally:
                with self._cond:
                    self._pending.discard(key)
                    self._cond.notify_all()

    def _submit(self, key, func) -> None:
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="lolan-ddragon")
                self._worker.start()
        self._jobs.put((key, func))

    def prepare(self, game_versions=(None,), champions=(), wait: float = 0.0, minimap: bool = True,
                items=()) -> None:
        """Queue the downloads a page needs and wait for them at most `wait` seconds.

        `game_versions` are the game patches involved (None = the latest Data Dragon version).
        Nothing is queued while downloads are paused after a failure.
        """
        champs = tuple(sorted({c for c in champions if c}))
        item_ids = tuple(sorted({int(i) for i in items if str(i).isdigit()}))
        for gv in dict.fromkeys(game_versions):
            if self._is_down():
                break
            key = ("game", gv, champs, minimap, item_ids)
            self._submit(key, lambda gv=gv: self._run_game_job(gv, champs, minimap, item_ids))
        if wait > 0:
            self.wait(wait)

    def wait(self, timeout: float) -> bool:
        """Block until every queued download is done or `timeout` seconds pass; True if idle."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._pending)


class _Offline(Exception):
    """Internal: stop the current download job, the CDN cannot be reached right now."""
