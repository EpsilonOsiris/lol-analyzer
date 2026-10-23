"""Diamond+ sample downloader: fills data/lineas with lane rows of strangers (source "diamante").

What it samples, and why
------------------------
* Champions: the pool of the clan (every clan member's champions per role with a few games, read
  from the stored matches). Their matchups against every opponent are what the draft needs.
* Players, in this order per region:
  1. OP.GG's champion leaderboards (`lol_list_champion_leaderboard`, ~10 master+ specialists per
     champion). Taken round-robin across the pool champions so all of them get coverage. OP.GG's
     puuids are encrypted for OP.GG's key and never used: each Riot ID is resolved with account-v1.
  2. Riot's league-v4 for breadth: challenger / grandmaster / master leagues and random pages of
     Diamond I-IV. Entries carry the puuid.
* Matches: solo queue only, the last few of each player, only the newest KEPT_PATCHES patches. Rows
  go through `samples.add_matches` (no raw match or timeline JSON is kept, no puuids or names of
  strangers in the rows; the pending-player queue in descarga.json holds identities only until each
  player is processed).

Rate limits and etiquette
-------------------------
* Riot counts requests per routing value, so the default regions (KR -> asia, EUW -> europe) never
  take quota from the panel's LA1 / americas. One RateLimiter per host (`riot.Limiters`); the
  scheduler works on whichever region is ready, so two regions are downloaded at the same time.
* RiotClient already sleeps the Retry-After of a 429. An invalid or expired key (dev keys last
  24 h) stops the run cleanly with everything kept.
* Before every step the League client phase is read; in champion select or in a game the
  downloader pauses (polling every PHASE_POLL_S) and resumes by itself. No client: it goes on.

State (descarga.json, next to the rows) makes a run resumable after Ctrl+C or a key expiry: the
leaderboard queues, the player being processed, the league buffer, counts and the last error.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from collections import Counter
from pathlib import Path

from . import samples
from .lanephase import LANE_ROLES, MIN_GAME_S, patch_of
from .metrics import SR_QUEUES, game_duration_seconds, is_remake
from .opgg import McpClient, OpggError, OpggRejected, champion_arg, parse_tool_text
from .riot import (INVALID_KEY_MESSAGE, InvalidApiKeyError, Limiters, NotFoundError, RiotClient,
                   RiotConnectionError, RiotError)

QUEUE_SOLO = 420
MATCHES_PER_PLAYER = 20
KEPT_PATCHES = samples.RECENT_PATCHES
WINDOW_DAYS = 14 * KEPT_PATCHES + 7        # startTime of the match lists: about three patches back
POOL_MIN_GAMES = 3                         # games of a person with a champion in a role to be in the pool
SKIPS_FLUSH_EVERY = 200
FLUSH_EVERY = 25                           # analyzed matches held before writing rows
REBUILD_EVERY = 200                        # new matches between rebuilds of the aggregates
PHASE_POLL_S = 15.0
PHASE_CHECK_S = 3.0                        # the client is asked at most this often while not paused
PAUSE_PHASES = frozenset({"ChampSelect", "GameStart", "InProgress", "Reconnect"})
RESAMPLE_S = 6 * 3600                      # a player sampled less than this ago is skipped
SEEN_KEEP_S = 7 * 24 * 3600
LB_REFRESH_S = 12 * 3600                   # leaderboards are fetched again after this long
LB_RETRY_S = 10 * 60                       # ... or after this long when OP.GG failed
LEAGUE_BUFFER = 300                        # league entries kept per refill
LEAGUE_PAGE_START = 10                     # Diamond pages tried at first (grows while pages are full)
MAX_ERRORS = 8                             # consecutive failed steps before giving up
STATE_SAVE_S = 5.0                         # descarga.json is rewritten at most this often
ITEM_WAIT_S = 30.0                         # wait for the Data Dragon item list of a new patch
DIAMOND_PLUS = frozenset({"DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"})
LEAGUE_ROTATION = (("DIAMOND", "I"), ("master", None), ("DIAMOND", "II"), ("grandmaster", None),
                   ("DIAMOND", "III"), ("challenger", None), ("DIAMOND", "IV"))
SOURCE = samples.SOURCE_DIAMOND

# name -> (platform, routing value, OP.GG region code)
REGIONS = {
    "kr": ("kr", "asia", "KR"), "jp": ("jp1", "asia", "JP"),
    "euw": ("euw1", "europe", "EUW"), "eune": ("eun1", "europe", "EUNE"),
    "tr": ("tr1", "europe", "TR"), "ru": ("ru", "europe", "RU"),
    "na": ("na1", "americas", "NA"), "br": ("br1", "americas", "BR"),
    "lan": ("la1", "americas", "LAN"), "las": ("la2", "americas", "LAS"),
    "oce": ("oc1", "sea", "OCE"),
}
REGION_ALIASES = {"euw1": "euw", "eun1": "eune", "na1": "na", "br1": "br", "la1": "lan", "la2": "las",
                  "jp1": "jp", "tr1": "tr", "oc1": "oce"}
DEFAULT_REGIONS = ("kr", "euw")

STOP_TEXT = {
    "horas": "se cumplió el tiempo indicado",
    "max": "se alcanzó el máximo de partidas",
    "agotado": "no quedan jugadores por muestrear",
    "clave": INVALID_KEY_MESSAGE,
    "conexion": "no hay conexión con la API de Riot",
    "errores": "demasiados errores seguidos de la API de Riot",
    "interrumpido": "interrumpido por el usuario",
}


class Region:
    def __init__(self, name):
        self.name = name
        self.platform, self.route, self.opgg = REGIONS[name]


def parse_regions(text) -> list[str]:
    """"kr,euw" -> ["kr", "euw"]; ValueError (Spanish) for an unknown region."""
    out = []
    for part in str(text or "").replace(";", ",").split(","):
        key = part.strip().casefold()
        if not key:
            continue
        key = REGION_ALIASES.get(key, key)
        if key not in REGIONS:
            raise ValueError(f"Región desconocida: «{part.strip()}». Usa: {', '.join(sorted(REGIONS))}.")
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("Indica al menos una región (por ejemplo: --regiones kr,euw).")
    return out


# ------------------------------------------------------------------ the pool
def clan_pool(lol_store, root=None, min_games=POOL_MIN_GAMES) -> dict[int, int]:
    """championId -> games, for the champions a clan member plays in a role at least `min_games` times.

    Clan = registered players and game names starting with the clan prefix (like Composiciones).
    Reads only the stored match JSON (not the timelines).
    """
    labeler = samples.PersonLabeler.from_store(lol_store, root)
    played, latest = [], {}
    for (text,) in lol_store.conn.execute("SELECT json FROM matches ORDER BY game_creation, match_id"):
        info = json.loads(text).get("info") or {}
        if info.get("queueId") not in SR_QUEUES or is_remake(info):
            continue
        for p in info.get("participants") or []:
            puuid, role, champ = p.get("puuid"), p.get("teamPosition"), p.get("championId")
            name = (p.get("riotIdGameName") or "").strip()
            if name:
                latest[puuid] = name
            if puuid and role in LANE_ROLES and champ:
                played.append((puuid, role, int(champ)))
    games: Counter = Counter()
    for puuid, role, champ in played:
        name = latest.get(puuid) or labeler.tracked.get(puuid, "").split("#")[0]
        if puuid in labeler.tracked or labeler.clan.matches_prefix(latest.get(puuid)):
            games[(labeler.clan.person(name) if name else puuid, role, champ)] += 1
    pool: Counter = Counter()
    for (_person, _role, champ), n in games.items():
        if n >= min_games:
            pool[champ] += n
    return dict(pool)


# ------------------------------------------------------------------ the APIs
class RiotRegionApi:
    """The Riot calls of one region: league-v4 on the platform host, account / match-v5 on the route."""

    def __init__(self, region: Region, api_key: str, limiters: Limiters, factory=RiotClient):
        self.region = region
        self._platform = factory(api_key, region=region.platform, limiter=limiters.for_host(region.platform))
        self._route = factory(api_key, region=region.route, limiter=limiters.for_host(region.route))

    def wait_estimate(self) -> float:
        return self._route.limiter.wait_estimate()

    def top_league(self, tier):
        return self._platform.get_top_league(tier)

    def league_page(self, tier, division, page):
        return self._platform.get_league_page(tier, division, page)

    def puuid(self, name, tag):
        return self._route.get_puuid(name, tag)

    def match_ids(self, puuid, count, start_time):
        return self._route.get_match_ids(puuid, count=count, queue=QUEUE_SOLO, start_time=start_time)

    def match(self, match_id):
        return self._route.get_match(match_id)

    def timeline(self, match_id):
        return self._route.get_timeline(match_id)


class OpggLeaderboards:
    """Top players of a champion per region from OP.GG's MCP: [(game name, tagline, tier)], Diamond+ only."""

    FIELDS = ["leaderboard[].rank", "leaderboard[].summoner.game_name", "leaderboard[].summoner.tagline",
              "leaderboard[].summoner.league_stats[].game_type",
              "leaderboard[].summoner.league_stats[].tier_info.tier"]

    def __init__(self, client=None):
        self.client = client or McpClient()
        self._names: dict | None = None

    def names(self) -> dict:
        """championId -> English display name (lol_list_champions)."""
        if self._names is None:
            text = self.client.call("lol_list_champions", {"desired_output_fields": [
                "data.champions[].champion_id", "data.champions[].key", "data.champions[].name"]})
            rows = ((parse_tool_text(text) or {}).get("data") or {}).get("champions") or []
            self._names = {row["champion_id"]: row["name"] for row in rows
                           if isinstance(row, dict) and isinstance(row.get("champion_id"), int)
                           and 0 < row["champion_id"] < 60000 and row.get("name")}
        return self._names

    def leaderboard(self, opgg_region: str, champion_id: int) -> list[tuple]:
        name = self.names().get(int(champion_id))
        if not name:
            return []
        try:
            text = self.client.call("lol_list_champion_leaderboard", {
                "region": opgg_region, "champion": champion_arg(name), "desired_output_fields": self.FIELDS})
        except OpggRejected:
            return []
        out = []
        for row in (parse_tool_text(text) or {}).get("leaderboard") or []:
            summoner = row.get("summoner") if isinstance(row, dict) else None
            if not isinstance(summoner, dict) or not summoner.get("game_name") or not summoner.get("tagline"):
                continue
            tier = next((str((s.get("tier_info") or {}).get("tier") or "").upper()
                         for s in summoner.get("league_stats") or []
                         if isinstance(s, dict) and s.get("game_type") == "SOLORANKED"), "")
            if tier in DIAMOND_PLUS:
                out.append((str(summoner["game_name"]), str(summoner["tagline"]), tier))
        return out


# ------------------------------------------------------------------ state
def _hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p).casefold() for p in parts).encode("utf-8")).hexdigest()[:12]


def _empty_region() -> dict:
    return {"lb_fetched": {}, "lb_taken": {}, "lb_queue": {}, "current": None,
            "league": {"turn": 0, "buffer": [], "max_page": {}}, "fetched": 0}


class DownloadState:
    """descarga.json: everything needed to resume (see the module docstring)."""

    def __init__(self, path):
        self.path = Path(path)
        data = samples._read_json(self.path, {})
        self.data = {
            "version": 1, "regions": data.get("regions") if isinstance(data.get("regions"), dict) else {},
            "totals": {"matches": 0, "rows": 0, "skipped": {}, "by_champion": {}, "runs": 0},
            "seen": data.get("seen") if isinstance(data.get("seen"), dict) else {},
            "pool": data.get("pool") if isinstance(data.get("pool"), dict) else {},
            "last_error": data.get("last_error"), "last_run": data.get("last_run"),
        }
        if isinstance(data.get("totals"), dict):
            self.data["totals"].update(data["totals"])

    @property
    def totals(self) -> dict:
        return self.data["totals"]

    def region(self, name) -> dict:
        reg = self.data["regions"].setdefault(name, _empty_region())
        for key, value in _empty_region().items():
            reg.setdefault(key, value)
        return reg

    def prune_seen(self, now) -> None:
        self.data["seen"] = {k: t for k, t in self.data["seen"].items()
                             if isinstance(t, (int, float)) and now - t < SEEN_KEEP_S}

    def save(self) -> None:
        samples._write_json(self.path, self.data)


# ------------------------------------------------------------------ the downloader
class RunResult:
    def __init__(self, stopped, fetched, rows, elapsed_s, paused_s, message=""):
        self.stopped, self.fetched, self.rows = stopped, fetched, rows
        self.elapsed_s, self.paused_s, self.message = elapsed_s, paused_s, message

    @property
    def ok(self) -> bool:
        return self.stopped in ("horas", "max", "agotado")


class Downloader:
    """Samples Diamond+ lane rows into a `samples.LineStore`.

    apis: {region name: object with wait_estimate / top_league / league_page / puuid / match_ids /
    match / timeline} (`RiotRegionApi`; tests pass fakes). opgg: object with `leaderboard(code, champ)`.
    pool: {championId: weight}. phase_fn() -> the League client phase or None. dd: a DDragon (or None).
    """

    def __init__(self, store: samples.LineStore, apis: dict, pool=None, opgg=None, dd=None,
                 phase_fn=None, solo_pool=False, hours=None, max_matches=None, say=print,
                 clock=time.time, sleep=time.sleep, rng=None, state=None):
        self.store, self.apis, self.opgg, self.dd = store, apis, opgg, dd
        self.regions = {name: Region(name) for name in apis}
        self.pool = {int(c): int(w) for c, w in (pool or {}).items()}
        self.pool_order = sorted(self.pool, key=lambda c: (-self.pool[c], c))
        self.phase_fn, self.solo_pool = phase_fn, solo_pool
        self.hours, self.max_matches = hours, max_matches
        self.say, self.clock, self.sleep = say, clock, sleep
        self.rng = rng or random.Random()
        self.state = state or DownloadState(store.base / "descarga.json")
        self.state.data["pool"] = {str(c): w for c, w in self.pool.items()}
        self.known = set(store.processed())
        self.pending: list = []             # (match_id, match, timeline, tier) waiting to be written
        self.skips: dict = {}               # match_id -> [source, reason], marked on the next flush
        self.fetched = 0
        self.rows = 0
        self.errors = 0
        self.paused_s = 0.0
        self.by_region: Counter = Counter()
        self._exhausted: set = set()
        self._since_rebuild = 0
        self._last_save = -1e9
        self._started = self.clock()
        self._deadline = self._started + hours * 3600 if hours else None
        self._items_ready: set = set()
        self._cutoff: tuple | None = None
        self._warned_items = False
        self._last_phase = -1e9

    # ---- setup
    def _prepare(self) -> None:
        """Data Dragon versions (to know the newest patches) and the item lists, when it is available."""
        if self.dd is not None:
            try:
                self.dd.prepare((None,), (), wait=ITEM_WAIT_S, minimap=False)
                patches = sorted({tuple(int(x) for x in v.split(".")[:2]) for v in self.dd.versions()},
                                 reverse=True)
                if len(patches) >= KEPT_PATCHES:
                    self._cutoff = patches[KEPT_PATCHES - 1]
            except Exception:
                self.dd = None
        if self.dd is None:
            self.say("Aviso: sin Data Dragon no se calculan los picos por ítem ni se filtran parches.")

    def _ensure_items(self, game_version) -> None:
        patch = patch_of(game_version)
        if self.dd is None or not patch or patch in self._items_ready:
            return
        self._items_ready.add(patch)
        try:
            self.dd.prepare((game_version,), (), wait=ITEM_WAIT_S, minimap=False)
        except Exception:
            pass

    # ---- the loop
    def run(self) -> RunResult:
        """Download until a stop condition; always leaves rows, processed ids and state saved."""
        self._prepare()
        self.state.prune_seen(self.clock())
        self.state.totals["runs"] = self.state.totals.get("runs", 0) + 1
        reason, message = "interrumpido", ""
        try:
            while True:
                stop = self._stop_reason()
                if stop:
                    reason = stop
                    break
                self._wait_for_game()
                if self._stop_reason():
                    continue
                region = self._pick_region()
                if region is None:
                    reason = "agotado"
                    break
                try:
                    self._step(region)
                    self.errors = 0
                except InvalidApiKeyError:
                    reason, message = "clave", INVALID_KEY_MESSAGE
                    break
                except RiotConnectionError as exc:
                    reason, message = "conexion", str(exc)
                    break
                except RiotError as exc:
                    self.errors += 1
                    self._note_error(region, exc)
                    if self.errors >= MAX_ERRORS:
                        reason, message = "errores", str(exc)
                        break
        finally:
            self._finish(reason, message)
        return RunResult(reason, self.fetched, self.rows, self.clock() - self._started, self.paused_s, message)

    def _save(self, force=False) -> None:
        """Write descarga.json (at most every few seconds unless forced)."""
        if force or self.clock() - self._last_save >= STATE_SAVE_S:
            self._last_save = self.clock()
            self.state.save()

    def _finish(self, reason, message) -> None:
        try:
            self.flush()
        finally:
            if reason in ("clave", "conexion", "errores"):
                self.state.data["last_error"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                 "kind": reason, "message": message}
            self.state.data["last_run"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "stopped": reason,
                                           "fetched": self.fetched, "rows": self.rows}
            self._save(force=True)
            if self._since_rebuild:
                self.rebuild()

    def _stop_reason(self) -> str | None:
        if self.max_matches is not None and self.fetched >= self.max_matches:
            return "max"
        if self._deadline is not None and self.clock() >= self._deadline:
            return "horas"
        return None

    def _note_error(self, region, exc) -> None:
        self.state.data["last_error"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "riot",
                                         "region": region, "message": str(exc)}

    # ---- pause while playing
    def _phase(self):
        try:
            return self.phase_fn() if self.phase_fn else None
        except Exception:
            return None

    def _wait_for_game(self) -> None:
        if self.phase_fn is None or self.clock() - self._last_phase < PHASE_CHECK_S:
            return
        self._last_phase = self.clock()
        phase = self._phase()
        if phase not in PAUSE_PHASES:
            return
        self.flush()
        self._save(force=True)
        self.say("Pausado: el cliente de League está en partida o en selección de campeones; "
                 "sigo cuando termine (no se hace ninguna petición mientras tanto).")
        started = self.clock()
        while phase in PAUSE_PHASES:
            if self._deadline is not None and self.clock() >= self._deadline:
                break
            self.sleep(PHASE_POLL_S)
            phase = self._phase()
        self.paused_s += self.clock() - started
        self._last_phase = self.clock()
        self.say("Reanudando la descarga.")

    # ---- scheduling
    def _pick_region(self):
        """The region whose rate limiter is ready soonest (ties: the one with fewer matches this run)."""
        live = [n for n in self.apis if n not in self._exhausted]
        if not live:
            return None
        return min(live, key=lambda n: (round(self.apis[n].wait_estimate(), 3), self.by_region[n],
                                        list(self.apis).index(n)))

    def _step(self, name) -> None:
        """One unit of work in a region: pick a player, list their matches, or download one match."""
        rs, api = self.state.region(name), self.apis[name]
        cur = rs["current"]
        if cur is None:
            player = self._next_player(name, api, rs)
            if player is None:
                self._exhausted.add(name)
                return
            puuid, tier, champ = player
            cur = rs["current"] = {"puuid": puuid, "tier": tier, "champ": champ, "ids": None}
            self.state.data["seen"][_hash(puuid)] = self.clock()
            self._save()
        if cur["ids"] is None:
            try:
                ids = api.match_ids(cur["puuid"], MATCHES_PER_PLAYER, int(self.clock()) - WINDOW_DAYS * 86400)
            except NotFoundError:
                ids = []
            except (InvalidApiKeyError, RiotConnectionError):
                raise                               # the player stays: the next run lists their matches
            except RiotError:
                rs["current"] = None
                raise
            cur["ids"] = [i for i in ids if i not in self.known]
            self._save()
            return
        if cur["ids"]:
            self._process_match(name, api, rs, cur)
            return
        rs["current"] = None
        self._save()

    def _recently_seen(self, key) -> bool:
        t = self.state.data["seen"].get(key)
        return t is not None and self.clock() - t < RESAMPLE_S

    # ---- who to sample
    def _next_player(self, name, api, rs):
        """(puuid, tier, champion or None) of the next player of the region, or None when it has none."""
        while True:
            found = self._next_leaderboard_player(name, api, rs)
            if found is False:
                break
            if found is None:
                continue
            return found
        if self.solo_pool:
            return None
        empty = 0
        while empty < 2 * len(LEAGUE_ROTATION):         # a league (or page) can come back empty
            buffer = rs["league"]["buffer"]
            if not buffer:
                self._refill_league(name, api, rs)
                buffer = rs["league"]["buffer"]
                if not buffer:
                    empty += 1
                    continue
            puuid, tier = buffer.pop()
            if not self._recently_seen(_hash(puuid)):
                return puuid, tier, None
        return None

    def _next_leaderboard_player(self, name, api, rs):
        """Round-robin over the pool champions. A player tuple; None = skipped one, try again; False = none left."""
        now = self.clock()
        stale = {c for c in self.pool_order
                 if now - rs["lb_fetched"].get(str(c), -1e12) >= LB_REFRESH_S}
        ready = [c for c in self.pool_order if rs["lb_queue"].get(str(c)) or c in stale]
        if not ready or self.opgg is None:
            return False
        champ = min(ready, key=lambda c: (rs["lb_taken"].get(str(c), 0), self.pool_order.index(c)))
        queue = rs["lb_queue"].setdefault(str(champ), [])
        if not queue and champ in stale:
            self._fetch_leaderboard(name, rs, champ)
            return None
        if not queue:
            return False
        game_name, tag, tier = queue.pop(0)
        rs["lb_taken"][str(champ)] = rs["lb_taken"].get(str(champ), 0) + 1
        if self._recently_seen(_hash(name, game_name, tag)):
            return None
        try:
            puuid = api.puuid(game_name, tag)
        except NotFoundError:
            return None
        self.state.data["seen"][_hash(name, game_name, tag)] = self.clock()
        if self._recently_seen(_hash(puuid)):
            return None
        return puuid, tier, champ

    def _fetch_leaderboard(self, name, rs, champ) -> None:
        region = self.regions[name]
        try:
            players = self.opgg.leaderboard(region.opgg, champ)
            rs["lb_fetched"][str(champ)] = self.clock()
        except OpggError as exc:
            players = []
            rs["lb_fetched"][str(champ)] = self.clock() - LB_REFRESH_S + LB_RETRY_S
            self.state.data["last_error"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "opgg",
                                             "region": name, "message": str(exc)}
        rs["lb_queue"][str(champ)] = [list(p) for p in players]
        self._save()

    def _refill_league(self, name, api, rs) -> None:
        league = rs["league"]
        tier, division = LEAGUE_ROTATION[league["turn"] % len(LEAGUE_ROTATION)]
        league["turn"] += 1
        if division is None:
            data = api.top_league(tier)
            entries, tier_name = data.get("entries") or [], tier.upper()
        else:
            key = f"{tier}:{division}"
            top = league["max_page"].get(key, LEAGUE_PAGE_START)
            page = self.rng.randint(1, top)
            entries, tier_name = api.league_page(tier, division, page), tier
            if not entries and page > 1:
                league["max_page"][key] = page - 1
            elif len(entries) >= 200 and page == top:
                league["max_page"][key] = top + 5
        entries = [e for e in entries if e.get("puuid") and not e.get("inactive")]
        self.rng.shuffle(entries)
        league["buffer"] = [[e["puuid"], tier_name] for e in entries[:LEAGUE_BUFFER]]
        self._save()

    # ---- one match
    def _early_skip(self, info) -> str | None:
        if info.get("queueId") != QUEUE_SOLO:
            return "cola"
        patch = patch_of(info.get("gameVersion"))
        if self._cutoff and patch and tuple(int(x) for x in patch.split(".")) < self._cutoff:
            return "parche_antiguo"
        if is_remake(info):
            return "remake"
        dur = game_duration_seconds(info)
        if dur is None or dur < MIN_GAME_S:
            return "corta"
        return None

    def _skip(self, match_id, reason) -> None:
        self.skips[match_id] = [SOURCE, reason]
        self.known.add(match_id)
        counts = self.state.totals["skipped"]
        counts[reason] = counts.get(reason, 0) + 1

    def _process_match(self, name, api, rs, cur) -> None:
        match_id = cur["ids"][0]
        try:
            if match_id not in self.known:
                self._download_match(api, match_id, cur["tier"])
                self.fetched += 1
                self.by_region[name] += 1
                rs["fetched"] += 1
                self.state.totals["matches"] = self.state.totals.get("matches", 0) + 1
        except NotFoundError:
            self._skip(match_id, "no_encontrada")
        except (InvalidApiKeyError, RiotConnectionError):
            raise                                   # the match stays first in the queue for the next run
        except RiotError:
            cur["ids"].pop(0)                       # give up on this one; the run counts the error
            raise
        cur["ids"].pop(0)
        if len(self.pending) >= FLUSH_EVERY or len(self.skips) >= SKIPS_FLUSH_EVERY:
            self.flush()
        else:
            self._save()

    def _download_match(self, api, match_id, tier) -> None:
        match = api.match(match_id)
        info = match.get("info") or {}
        reason = self._early_skip(info)
        if reason:
            self._skip(match_id, reason)
            return
        try:
            timeline = api.timeline(match_id)
        except NotFoundError:
            self._skip(match_id, "sin_timeline")
            return
        if not ((timeline or {}).get("info") or {}).get("frames"):
            self._skip(match_id, "sin_timeline")
            return
        self._ensure_items(info.get("gameVersion"))
        self.known.add(match_id)
        self.pending.append((match_id, match, timeline, tier))

    # ---- writing
    def flush(self) -> None:
        """Rows of the held matches, then their ids (and the skipped ones) as processed."""
        if self.pending:
            by_tier: dict = {}
            for match_id, match, timeline, tier in self.pending:
                by_tier.setdefault(tier, []).append((match_id, match, timeline))
            legendary = samples.ddragon_legendary(self.dd) if self.dd is not None else None
            held = list(self.pending)
            totals = self.state.totals
            for tier, items in by_tier.items():
                done = samples.add_matches(self.store, items, source=SOURCE, tier=tier,
                                           legendary_for=legendary)
                self.rows += done["rows"]
                totals["rows"] = totals.get("rows", 0) + done["rows"]
                for reason, n in done["skipped"].items():
                    totals["skipped"][reason] = totals["skipped"].get(reason, 0) + n
                for champ, n in done["champions"].items():
                    if champ in self.pool:
                        totals["by_champion"][str(champ)] = totals["by_champion"].get(str(champ), 0) + n
            self._since_rebuild += len(held)
            self.pending = []
            self.report()
        if self.skips:
            self.store.mark_processed(self.skips)
            self.skips = {}
        self._save(force=True)
        if self._since_rebuild >= REBUILD_EVERY:
            self.rebuild()

    def rebuild(self) -> None:
        self._since_rebuild = 0
        self.store.rebuild()

    # ---- progress
    def active_hours(self) -> float:
        return max(1e-9, (self.clock() - self._started - self.paused_s) / 3600)

    def report(self) -> None:
        rate = self.fetched / self.active_hours() if self.fetched else 0
        per_region = " · ".join(f"{n.upper()} {self.by_region[n]}" for n in self.apis)
        text = (f"[{time.strftime('%H:%M:%S')}] {self.fetched} partidas ({rate:.0f}/h) · "
                f"{self.rows} filas nuevas ({self.state.totals.get('rows', 0)} en total) · {per_region}")
        low = self.coverage()[:3]
        if low:
            text += " · menos cubiertos: " + ", ".join(f"{n} {k}" for n, k in low)
        self.say(text)

    def coverage(self) -> list[tuple]:
        """[(champion name, rows)] for the pool champions, the least covered first."""
        counts = self.state.totals["by_champion"]
        rows = [(self.store.champion_name(c), counts.get(str(c), 0), c) for c in self.pool_order]
        rows.sort(key=lambda t: (t[1], self.pool_order.index(t[2])))
        return [(n, k) for n, k, _c in rows]


# ------------------------------------------------------------------ text
def format_pool(coverage) -> list[str]:
    return [f"  {name}: {rows}" for name, rows in coverage]


def run_summary(result: RunResult, state: DownloadState, coverage) -> list[str]:
    hours = max(1e-9, (result.elapsed_s - result.paused_s) / 3600)
    lines = [f"Descarga terminada: {STOP_TEXT.get(result.stopped, result.stopped)}."]
    if result.stopped in ("clave", "conexion", "errores") and result.message:
        lines.append(result.message)
    lines.append(f"Esta vez: {result.fetched} partidas ({result.fetched / hours:.0f}/h), "
                 f"{result.rows} filas nuevas, {result.elapsed_s / 60:.0f} min"
                 + (f" ({result.paused_s / 60:.0f} min en pausa)" if result.paused_s else "") + ".")
    if result.stopped in ("clave", "conexion", "errores"):
        lines.append("El progreso quedó guardado (data/lineas/descarga.json): vuelve a ejecutar "
                     "python -m lolan muestras para continuar.")
    if coverage:
        lines.append("Filas por campeón del pool (menos cubiertos primero):")
        lines += format_pool(coverage)
    return lines


def status_lines(state: DownloadState, store: samples.LineStore) -> list[str]:
    """Plain Spanish summary of descarga.json for `muestras --estado`."""
    data, totals = state.data, state.totals
    if not state.path.is_file():
        return ["Todavía no hay descarga: ejecuta python -m lolan muestras."]
    lines = [f"Partidas descargadas: {totals.get('matches', 0)} · filas: {totals.get('rows', 0)} · "
             f"ejecuciones: {totals.get('runs', 0)}"]
    last = data.get("last_run")
    if last:
        lines.append(f"Última ejecución: {last.get('when')} ({STOP_TEXT.get(last.get('stopped'), last.get('stopped'))}; "
                     f"{last.get('fetched', 0)} partidas, {last.get('rows', 0)} filas).")
    if totals.get("skipped"):
        lines.append("Omitidas: " + ", ".join(f"{k} {v}" for k, v in sorted(totals["skipped"].items(),
                                                                          key=lambda kv: -kv[1])))
    for name, rs in sorted(data.get("regions", {}).items()):
        queued = sum(len(q) for q in rs.get("lb_queue", {}).values())
        cur = rs.get("current")
        lines.append(f"Región {name.upper()}: {rs.get('fetched', 0)} partidas · cola del pool: {queued} "
                     f"jugadores · cola de ligas: {len(rs.get('league', {}).get('buffer', []))}"
                     + (f" · jugador en curso: {len(cur['ids'])} partidas pendientes" if cur else "")
)
    err = data.get("last_error")
    if err:
        lines.append(f"Último error ({err.get('when')}): {err.get('message')}")
    pool = {int(c): w for c, w in (data.get("pool") or {}).items() if str(c).isdigit()}
    if pool:
        counts = totals.get("by_champion", {})
        order = sorted(pool, key=lambda c: (counts.get(str(c), 0), -pool[c], c))
        lines.append("Filas por campeón del pool (menos cubiertos primero):")
        lines += [f"  {store.champion_name(c)}: {counts.get(str(c), 0)}" for c in order]
    return lines
