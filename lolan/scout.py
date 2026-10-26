"""Scouting ("¿Quién está en mi partida?"): who is in a game, from Riot's Spectator API.

Given a game in progress (spectator-v5) this builds a report of both teams: champion, summoner
spells, runes, ranked rank, mastery on the champion, the player's recent ranked games and what OUR
database already knows about that Riot ID (games played with / against each other, at zero API cost).

Policy (Riot's developer rules), enforced here and in the page:
  * Only what Riot's API exposes about each account: rank, mastery, match history. Nothing about
    live cooldowns, no "dodge" and no advice about what to do.
  * A player that comes without puuid / Riot ID (hidden, streamer mode) is NEVER looked up or
    identified: only champion and summoner spells are shown ("Jugador oculto"). Bots are labeled.
  * Other players' data is limited to their public Riot ID; cached data expires after 6 h.
  * Puuids are internal: they never go into the report that leaves this module.

The module is split in: pure parsing / formatting, `LocalHistory` (our DB), `ScoutRun` (the request
plan with its budget) and `Assets` + `enrich` / `render_text` (names and icons from Data Dragon).
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time

from . import analyze as an
from .players import normalize_riot_id
from .riot import InvalidApiKeyError, RiotError

BUDGET = 45                     # most requests one scout may spend (spectator + account included)
MAX_WAIT_S = 60.0               # stop rather than wait longer than this for the rate limit
CACHE_TTL_S = 6 * 3600          # per (puuid, champion)
CACHE_PRUNE_S = 24 * 3600       # rows older than this are deleted
RECENT_IDS = 10                 # last ranked match ids looked at per player
ENEMY_DETAILS = 3               # match details downloaded per enemy (not already stored here)
ON_CHAMPION_SHOWN = 3
MAX_ERRORS = 3                  # Riot failures in a row before the scout stops asking
LOW_MASTERY = 10_000
MAIN_MASTERY = 100_000
LABEL_LOW = "poca experiencia con este campeón"
LABEL_MAIN = "main"
HIDDEN_LABEL = "Jugador oculto"
BOT_LABEL = "Bot"
DAY_MS = 86_400_000

TIER_NAMES = {
    "IRON": "Hierro", "BRONZE": "Bronce", "SILVER": "Plata", "GOLD": "Oro", "PLATINUM": "Platino",
    "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Maestro",
    "GRANDMASTER": "Gran Maestro", "CHALLENGER": "Retador",
}
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}
SOLO_QUEUE, FLEX_QUEUE = "RANKED_SOLO_5x5", "RANKED_FLEX_SR"
QUEUE_LABELS = {
    0: "Personalizada", 400: "Normal (draft)", 420: "Clasificatoria Solo/Dúo", 430: "Normal (a ciegas)",
    440: "Clasificatoria Flexible", 450: "ARAM", 480: "Juego rápido", 490: "Partida rápida",
    700: "Clash", 710: "Clasificatoria de 5", 1700: "Arena",
}
MODE_LABELS = {"CLASSIC": "Clásico", "ARAM": "ARAM", "CHERRY": "Arena", "PRACTICETOOL": "Herramienta de práctica"}
STOP_REASONS = {
    "presupuesto": "se llegó al máximo de {budget} peticiones por partida",
    "limite": "Riot pide esperar más de {wait} s (límite de peticiones de tu key)",
    "error": "Riot no respondió bien a varias peticiones seguidas",
}


# ----------------------------------------------------------------- small helpers
def key_fingerprint(key: str) -> str:
    """Short, one-way fingerprint of the API key: lets caches notice a key change without keeping the key."""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:12]


def _int(v, default=0) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def split_riot_id(text) -> tuple[str, str]:
    text = str(text or "").strip()
    if "#" not in text:
        return text, ""
    name, tag = text.rsplit("#", 1)
    return name.strip(), tag.strip()


def queue_label(queue_id, mode="") -> str:
    if queue_id in QUEUE_LABELS:
        return QUEUE_LABELS[queue_id]
    return MODE_LABELS.get(str(mode or "").upper(), str(mode or "").title()) or f"Cola {queue_id}"


def pts_text(points) -> str:
    """12345 -> "12,3k"; 1500000 -> "1,5M"; small numbers as they are."""
    n = _int(points)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M").replace(".", ",")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k").replace(".", ",")
    return str(n)


def days_text(days) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    return f"hace {days} días"


# --------------------------------------------------------------- spectator parsing
def _participant(p: dict, viewer_puuid: str, viewer_riot_id: str) -> dict:
    puuid = str(p.get("puuid") or "").strip()
    rid = str(p.get("riotId") or "").strip()
    bot = bool(p.get("bot"))
    is_viewer = bool(viewer_puuid) and puuid == viewer_puuid
    if is_viewer and not rid:
        rid = viewer_riot_id
    hidden = not bot and not (puuid and rid and "#" in rid)
    name, tag = split_riot_id(rid) if (rid and not hidden and not bot) else ("", "")
    out = {
        "champion_id": _int(p.get("championId")),
        "spell_ids": [_int(p.get("spell1Id")), _int(p.get("spell2Id"))],
        "keystone_id": None, "style_id": None, "substyle_id": None,
        "hidden": hidden, "bot": bot, "is_viewer": is_viewer, "clan": False,
        "riot_id": f"{name}#{tag}" if name and tag else None,
        "game_name": name or None, "tag": tag or None,
        "_puuid": puuid if (not hidden and not bot and name and tag) else "",
    }
    perks = p.get("perks") if isinstance(p.get("perks"), dict) else {}
    if not hidden and not bot:      # hidden players: champion and spells only
        ids = [i for i in (perks.get("perkIds") or []) if isinstance(i, int)]
        out["keystone_id"] = ids[0] if ids else None
        out["style_id"] = perks.get("perkStyle") if isinstance(perks.get("perkStyle"), int) else None
        out["substyle_id"] = perks.get("perkSubStyle") if isinstance(perks.get("perkSubStyle"), int) else None
    return out


def parse_spectator(raw, viewer_puuid: str, viewer_riot_id: str = "") -> dict | None:
    """Compact game from a spectator-v5 answer, split into the viewer's team ("ally") and the other
    ("enemy"); None when the answer is not a game."""
    if not isinstance(raw, dict):
        return None
    parts = [p for p in raw.get("participants") or [] if isinstance(p, dict)]
    if not parts:
        return None
    viewer = next((p for p in parts if viewer_puuid and p.get("puuid") == viewer_puuid), None)
    ally_team = _int((viewer or parts[0]).get("teamId"), 100)
    teams = {"ally": [], "enemy": []}
    for p in parts:
        side = "ally" if _int(p.get("teamId"), 100) == ally_team else "enemy"
        teams[side].append(_participant(p, viewer_puuid, viewer_riot_id))
    bans = {"ally": [], "enemy": []}
    for b in raw.get("bannedChampions") or []:
        if isinstance(b, dict) and _int(b.get("championId"), -1) > 0:
            side = "ally" if _int(b.get("teamId"), 100) == ally_team else "enemy"
            bans[side].append({"champion_id": _int(b["championId"]), "pick_turn": _int(b.get("pickTurn"))})
    for side in bans:
        bans[side].sort(key=lambda b: b["pick_turn"])
    mode = str(raw.get("gameMode") or "")
    queue_id = _int(raw.get("gameQueueConfigId"))
    return {
        "game_id": raw.get("gameId"), "mode": mode, "queue_id": queue_id,
        "queue_label": queue_label(queue_id, mode), "map_id": _int(raw.get("mapId")),
        "started_ms": _int(raw.get("gameStartTime")), "length_s": _int(raw.get("gameLength")),
        "teams": teams, "bans": bans, "viewer_found": viewer is not None,
    }


# ------------------------------------------------------------ rank and mastery
def rank_entry(entry: dict) -> dict:
    """One ranked queue, formatted: label "Oro II", "45 PL", winrate of the season."""
    tier = str(entry.get("tier") or "").upper()
    division = str(entry.get("rank") or "")
    wins, losses = _int(entry.get("wins")), _int(entry.get("losses"))
    games = wins + losses
    lp = _int(entry.get("leaguePoints"))
    label = TIER_NAMES.get(tier, tier.title())
    if tier and tier not in APEX_TIERS and division:
        label += f" {division}"
    winrate = wins / games if games else None
    return {"tier": tier, "division": division, "label": label, "lp": lp, "lp_text": f"{lp} PL",
            "wins": wins, "losses": losses, "games": games, "winrate": winrate,
            "winrate_text": an.fmt_pct(winrate), "record_text": f"{wins}V-{losses}D",
            "hot_streak": bool(entry.get("hotStreak"))}


def format_ranks(entries) -> dict:
    """{"solo": rank|None, "flex": rank|None} from league-v4 entries."""
    out = {"solo": None, "flex": None}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("queueType") == SOLO_QUEUE:
            out["solo"] = rank_entry(e)
        elif e.get("queueType") == FLEX_QUEUE:
            out["flex"] = rank_entry(e)
    return out


def mastery_info(m, now_ms: float) -> dict:
    """Mastery on the champion being played; `m` None = never played (404)."""
    if not isinstance(m, dict):
        return {"never": True, "level": 0, "points": 0, "points_text": "0", "days_since": None,
                "last_text": "nunca lo ha jugado", "label": LABEL_LOW, "tone": "low"}
    points, level = _int(m.get("championPoints")), _int(m.get("championLevel"))
    last = _int(m.get("lastPlayTime"))
    days = max(0, int((now_ms - last) // DAY_MS)) if last > 0 else None
    label = LABEL_LOW if points < LOW_MASTERY else LABEL_MAIN if points > MAIN_MASTERY else None
    return {"never": False, "level": level, "points": points, "points_text": pts_text(points),
            "days_since": days, "last_text": days_text(days), "label": label,
            "tone": {LABEL_LOW: "low", LABEL_MAIN: "main"}.get(label, "")}


# ------------------------------------------------------------------ local history
class LocalHistory:
    """What our own database knows about Riot IDs: games two people played in together or against.

    Matched by Riot ID (riotIdGameName#riotIdTagline, case-insensitive) in the stored
    info.participants, never by puuid (puuids change with the API key). Built from one pass over
    the stored matches and rebuilt only when their number changes.
    """

    def __init__(self, store_factory):
        self._store_factory = store_factory
        self._lock = threading.Lock()
        self._count = None
        self._by_key: dict = {}

    @staticmethod
    def _key(name, tag) -> tuple[str, str]:
        return (str(name or "").strip().casefold(), str(tag or "").strip().casefold())

    def _index(self):
        store = self._store_factory()
        try:
            count = store.count_matches()
            with self._lock:
                if count == self._count:
                    return self._by_key
            by_key: dict = {}
            for match_id, match in store.iter_match_json():
                info = (match or {}).get("info") or {}
                for p in info.get("participants") or []:
                    if not isinstance(p, dict) or not p.get("riotIdGameName"):
                        continue
                    by_key.setdefault(self._key(p["riotIdGameName"], p.get("riotIdTagline")), []).append({
                        "m": match_id, "t": p.get("teamId"), "w": bool(p.get("win")),
                        "c": p.get("championName") or "", "at": _int(info.get("gameCreation"))})
            with self._lock:
                self._count, self._by_key = count, by_key
            return by_key
        finally:
            store.close()

    def _entries(self, index, riot_id):
        name, tag = split_riot_id(riot_id)
        rows = list(index.get(self._key(name, tag), []))
        if tag:        # older rows may lack the tag
            rows += index.get(self._key(name, ""), [])
        return rows

    def lookup(self, viewer_riot_id: str, other_riot_id: str) -> dict | None:
        if not viewer_riot_id or not other_riot_id:
            return None
        if self._key(*split_riot_id(viewer_riot_id)) == self._key(*split_riot_id(other_riot_id)):
            return None
        index = self._index()
        mine = {e["m"]: e for e in self._entries(index, viewer_riot_id)}
        together = {"wins": 0, "losses": 0}
        against = {"wins": 0, "losses": 0}
        last = None
        for e in self._entries(index, other_riot_id):
            me = mine.get(e["m"])
            if me is None:
                continue
            bucket = together if me["t"] == e["t"] else against
            bucket["wins" if me["w"] else "losses"] += 1
            if last is None or e["at"] >= last["at"]:
                last = {"champion": e["c"], "relation": "together" if me["t"] == e["t"] else "against",
                        "win": me["w"], "at": e["at"]}
        games = sum(together.values()) + sum(against.values())
        if not games:
            return None
        return {"games": games, "together": together, "against": against, "last": last}


def history_text(history, viewer_label: str, champion_namer=None) -> str:
    """"Antes: 2V-1D contra ti · 3V-0D con ti" (viewer_label is "ti" for the main player)."""
    if not history:
        return ""
    bits = []
    a, t = history["against"], history["together"]
    if a["wins"] + a["losses"]:
        bits.append(f"{a['wins']}V-{a['losses']}D contra {viewer_label}")
    if t["wins"] + t["losses"]:
        bits.append(f"{t['wins']}V-{t['losses']}D con {viewer_label}")
    text = "Antes: " + " · ".join(bits)
    last = history.get("last")
    if last and last.get("champion") and champion_namer:
        text += f" (la última vez jugó {champion_namer(last['champion'])})"
    return text


# ----------------------------------------------------------------- API with budget
class BudgetStop(Exception):
    """No more requests this scout: `reason` is a key of STOP_REASONS."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ScoutApi:
    """The Riot calls a scout makes, counted against a budget and the rate limit.

    `platform` / `regional` are RiotClients (la1 / americas); every call first checks that the budget
    is not spent and that the host's rate limiter would not make it wait more than `max_wait`.
    """

    def __init__(self, platform, regional, budget=BUDGET, max_wait=MAX_WAIT_S):
        self.platform, self.regional = platform, regional
        self.budget, self.max_wait = budget, max_wait
        self.used = 0

    def _spend(self, client) -> None:
        if self.used >= self.budget:
            raise BudgetStop("presupuesto")
        limiter = getattr(client, "limiter", None)
        if limiter is not None and limiter.wait_estimate() > self.max_wait:
            raise BudgetStop("limite")
        self.used += 1

    def account(self, name, tag):
        self._spend(self.regional)
        return self.regional.get_puuid(name, tag)

    def active_game(self, puuid):
        self._spend(self.platform)
        return self.platform.get_active_game(puuid)

    def league(self, puuid):
        self._spend(self.platform)
        return self.platform.get_league_entries(puuid)

    def mastery(self, puuid, champion_id):
        self._spend(self.platform)
        return self.platform.get_champion_mastery(puuid, champion_id)

    def match_ids(self, puuid, count=RECENT_IDS):
        self._spend(self.regional)
        return self.regional.get_match_ids(puuid, count=count, type="ranked")

    def match(self, match_id):
        self._spend(self.regional)
        return self.regional.get_match(match_id)


class PuuidResolver:
    """Current-app puuids by Riot ID, remembered in memory per (Riot ID, key fingerprint).

    The key (never kept, only its fingerprint) decides: a puuid is only valid for the application
    that asked for it, so after a key change everything is resolved again.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict = {}

    def resolve(self, riot_id: str, key: str, fetch) -> str:
        """`fetch(name, tag) -> puuid` is only called when the pair is not cached."""
        name, tag = normalize_riot_id(riot_id)
        cache_key = (f"{name}#{tag}".casefold(), key_fingerprint(key))
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
        puuid = fetch(name, tag)
        with self._lock:
            self._cache[cache_key] = puuid
        return puuid

    def known(self, riot_id: str, key: str) -> bool:
        try:
            name, tag = normalize_riot_id(riot_id)
        except ValueError:
            return False
        with self._lock:
            return (f"{name}#{tag}".casefold(), key_fingerprint(key)) in self._cache


# --------------------------------------------------------------------- recent games
def game_from_participant(p: dict, info: dict) -> dict:
    return {"champion": p.get("championName") or "", "champion_id": _int(p.get("championId"), None),
            "win": bool(p.get("win")), "k": _int(p.get("kills")), "d": _int(p.get("deaths")),
            "a": _int(p.get("assists")), "at": _int(info.get("gameCreation")),
            "queue": _int(info.get("queueId"))}


def find_participant(match: dict, puuid: str = "", riot_id: str = ""):
    """The participant of a match, by puuid when given (fresh API data) else by Riot ID."""
    info = (match or {}).get("info") or {}
    parts = [p for p in info.get("participants") or [] if isinstance(p, dict)]
    if puuid:
        found = next((p for p in parts if p.get("puuid") == puuid), None)
        if found is not None:
            return found, info
    if riot_id:
        name, tag = split_riot_id(riot_id)
        want = (name.casefold(), tag.casefold())
        for p in parts:
            have = (str(p.get("riotIdGameName") or "").strip().casefold(),
                    str(p.get("riotIdTagline") or "").strip().casefold())
            if have[0] == want[0] and (not have[1] or have[1] == want[1]):
                return p, info
    return None, info


def recent_summary(recent, champion_id: int, now_ms: float) -> dict | None:
    """Wins / losses of the recent ranked games known, and the ones on the champion being played."""
    if not recent:
        return None
    games = [g for g in recent.get("games") or [] if isinstance(g, dict)]
    wins = sum(1 for g in games if g.get("win"))
    games.sort(key=lambda g: g.get("at") or 0, reverse=True)
    on = []
    for g in games:
        if g.get("champion_id") == champion_id and len(on) < ON_CHAMPION_SHOWN:
            days = max(0, int((now_ms - g["at"]) // DAY_MS)) if g.get("at") else None
            on.append({"win": bool(g["win"]), "kda": f"{g['k']}/{g['d']}/{g['a']}",
                       "days_since": days, "when_text": days_text(days)})
    return {"ids": _int(recent.get("ids")), "known": len(games), "wins": wins,
            "losses": len(games) - wins, "record_text": f"{wins}V-{len(games) - wins}D",
            "on_champion": on, "partial": not recent.get("complete", True)}


# ------------------------------------------------------------------------ the scout
class ScoutRun:
    """One scouting job: fills the cache and builds the report of a game.

    Order of work (the request budget decides how far it gets):
      1. rank + mastery of every visible player: enemies, then allies, then the viewer;
      2. the last RECENT_IDS ranked match ids of everyone but the viewer and clan members (enemies
         first); for enemies also up to ENEMY_DETAILS match details that are not stored here yet.
    Everything fetched is cached per (puuid, champion) for CACHE_TTL_S, so scouting the same lobby
    again, or the same people later, costs nothing.
    """

    def __init__(self, api: ScoutApi, store, history: LocalHistory, clan, *, viewer_riot_id: str,
                 viewer_is_main: bool, now=time.time, cache_ttl=CACHE_TTL_S, tracked_ids=()):
        self.api, self.store, self.history = api, store, history
        self.clan = clan
        self.viewer_riot_id, self.viewer_is_main = viewer_riot_id, viewer_is_main
        self._now = now
        self.cache_ttl = cache_ttl
        self.tracked = {t.casefold() for t in tracked_ids}
        self._stored_ids: set | None = None
        self.stopped: str | None = None
        self.errors = 0
        self.from_cache = 0
        self.entries: dict = {}          # id(participant) -> cache entry

    # ---- helpers
    def _is_clan(self, p) -> bool:
        if p["hidden"] or p["bot"] or not p["game_name"]:
            return False
        return (self.clan.matches_prefix(p["game_name"]) or p["game_name"].casefold() in self.clan.aliases
                or (p["riot_id"] or "").casefold() in self.tracked)

    def _stored(self) -> set:
        if self._stored_ids is None:
            self._stored_ids = self.store.existing_match_ids()
        return self._stored_ids

    def _guard(self, fn):
        """Run one Riot call; None if it failed (counted), BudgetStop / invalid key propagate."""
        try:
            result = fn()
            self.errors = 0
            return result, True
        except (BudgetStop, InvalidApiKeyError):
            raise
        except RiotError:
            self.errors += 1
            if self.errors >= MAX_ERRORS:
                raise BudgetStop("error")
            return None, False

    # ---- per player
    def _entry(self, p) -> dict:
        key = id(p)
        if key not in self.entries:
            cached = self.store.get_scout_cache(p["_puuid"], p["champion_id"], self.cache_ttl, self._now())
            fresh = {"parts": [], "ranks": None, "mastery": None, "recent": None}
            if cached and isinstance(cached.get("parts"), list):
                fresh.update({k: cached.get(k) for k in ("parts", "ranks", "mastery", "recent")})
                fresh["cached_parts"] = list(cached["parts"])
            self.entries[key] = fresh
        return self.entries[key]

    def _save(self, p, entry) -> None:
        data = {k: entry.get(k) for k in ("parts", "ranks", "mastery", "recent")}
        data["v"] = 1
        self.store.put_scout_cache(p["_puuid"], p["champion_id"], data, self._now())

    def _core(self, p) -> None:
        entry = self._entry(p)
        if "rank" not in entry["parts"]:
            data, ok = self._guard(lambda: self.api.league(p["_puuid"]))
            if ok:
                entry["ranks"] = format_ranks(data)
                entry["parts"].append("rank")
                self._save(p, entry)
        if "mastery" not in entry["parts"]:
            data, ok = self._guard(lambda: self.api.mastery(p["_puuid"], p["champion_id"]))
            if ok:
                entry["mastery"] = ({k: data.get(k) for k in ("championLevel", "championPoints", "lastPlayTime")}
                                    if isinstance(data, dict) else None)
                entry["parts"].append("mastery")
                self._save(p, entry)

    def _recent(self, p, enemy: bool) -> None:
        entry = self._entry(p)
        if "recent" in entry["parts"]:
            return
        ids, ok = self._guard(lambda: self.api.match_ids(p["_puuid"], RECENT_IDS))
        if not ok:
            return
        games, fetched, complete = [], 0, True
        stored = self._stored()
        try:
            for mid in ids:
                on_champion = sum(1 for g in games if g.get("champion_id") == p["champion_id"])
                if mid in stored:
                    got = self.store.get_match(mid)
                    part, info = find_participant(got[0], riot_id=p["riot_id"]) if got else (None, {})
                    if part is not None:
                        games.append(game_from_participant(part, info))
                    continue
                if not enemy:
                    continue
                if fetched >= ENEMY_DETAILS or on_champion >= ON_CHAMPION_SHOWN:
                    continue
                match, ok = self._guard(lambda mid=mid: self.api.match(mid))
                fetched += 1
                if not ok:
                    complete = False
                    continue
                part, info = find_participant(match, puuid=p["_puuid"])
                if part is not None:
                    games.append(game_from_participant(part, info))
        except BudgetStop:
            complete = False
            entry["recent"] = {"ids": len(ids), "games": games, "complete": False}
            self._save(p, entry)
            raise
        entry["recent"] = {"ids": len(ids), "games": games, "complete": complete}
        if complete:
            entry["parts"].append("recent")
        self._save(p, entry)

    # ---- the whole game
    def run(self, game: dict) -> dict:
        """Fill `game` (from parse_spectator) with the data of every visible player; the report."""
        players = [("enemy", p) for p in game["teams"]["enemy"]] + [("ally", p) for p in game["teams"]["ally"]]
        for _side, p in players:
            p["clan"] = self._is_clan(p) and not p["is_viewer"]
        lookup = [(s, p) for s, p in players if p["_puuid"]]
        order = ([x for x in lookup if x[0] == "enemy"]
                 + [x for x in lookup if x[0] == "ally" and not x[1]["is_viewer"]]
                 + [x for x in lookup if x[1]["is_viewer"]])
        for _s, p in lookup:
            had = self._entry(p)
            if had.get("cached_parts"):
                self.from_cache += 1 if {"rank", "mastery"} <= set(had["cached_parts"]) else 0
        try:
            for _side, p in order:
                self._core(p)
            for side, p in order:
                if p["is_viewer"] or p["clan"]:
                    continue
                self._recent(p, enemy=(side == "enemy"))
        except BudgetStop as stop:
            self.stopped = stop.reason
        return self._report(game, players)

    # ---- report
    def _report(self, game, players) -> dict:
        now = self._now()
        now_ms = now * 1000
        viewer_label = "ti" if self.viewer_is_main else split_riot_id(self.viewer_riot_id)[0]
        missing_core = missing_deep = 0
        for _side, p in players:
            p["status"] = "hidden" if p["hidden"] else "bot" if p["bot"] else "ok"
            p["rank"] = p["mastery"] = p["recent"] = p["history"] = None
            if not p["_puuid"]:
                continue
            entry = self._entry(p)
            parts = entry["parts"]
            if "rank" in parts:
                p["rank"] = entry["ranks"]
            if "mastery" in parts:
                p["mastery"] = mastery_info(entry["mastery"], now_ms)
            if entry.get("recent"):
                p["recent"] = recent_summary(entry["recent"], p["champion_id"], now_ms)
            if not ({"rank", "mastery"} <= set(parts)):
                missing_core += 1
                p["status"] = "partial"
            elif not (p["is_viewer"] or p["clan"]) and "recent" not in parts:
                missing_deep += 1
            if not p["is_viewer"]:
                p["history"] = self.history.lookup(self.viewer_riot_id, p["riot_id"])
            p["streak"] = bool(p["rank"] and any((r or {}).get("hot_streak") for r in p["rank"].values()))
        skipped = []
        reason = (STOP_REASONS.get(self.stopped, "").format(budget=self.api.budget, wait=int(self.api.max_wait))
                  if self.stopped else "")
        if missing_core:
            skipped.append(f"Rango o maestría de {missing_core} jugador{'es' if missing_core != 1 else ''}"
                           + (f": {reason}" if reason else ""))
        if missing_deep:
            skipped.append(f"Partidas recientes de {missing_deep} jugador{'es' if missing_deep != 1 else ''}"
                           + (f": {reason}" if reason else ""))
        report = {
            "version": 1, "created": int(now), "viewer_label": viewer_label,
            "scouted": {"riot_id": self.viewer_riot_id, "is_main": self.viewer_is_main},
            "game": {k: game[k] for k in ("game_id", "mode", "queue_id", "queue_label", "map_id",
                                          "started_ms", "length_s")},
            "teams": game["teams"], "bans": game["bans"],
            "requests": {"used": self.api.used, "budget": self.api.budget, "from_cache": self.from_cache,
                         "skipped": skipped, "stopped": self.stopped},
            "complete": not skipped,
        }
        return _strip_private(report)


def _strip_private(value):
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


# ------------------------------------------------------------------ names and icons
class Assets:
    """Spanish names and icon URLs from the Data Dragon cache (nothing is downloaded here)."""

    def __init__(self, dd, asset_url=None):
        self.dd = dd
        self.version = dd.latest() if dd is not None else None
        self._url = asset_url or (lambda rel: None)

    def champion(self, champion_id) -> tuple[str, str | None]:
        found = self.dd.champion_by_key(self.version, champion_id) if self.dd else None
        if found:
            return found[1], self._url(self.dd.icon_rel(self.version, found[0]))
        return f"Campeón {champion_id}", None

    def champion_by_name(self, api_name) -> str:
        return self.dd.champion_name(self.version, api_name) if self.dd else str(api_name)

    def spell(self, spell_id) -> dict:
        info = self.dd.spell_info(self.version, spell_id) if self.dd else None
        if info:
            return {"name": info["name"], "icon": self._url(info["icon_rel"])}
        return {"name": f"Hechizo {spell_id}" if spell_id else "?", "icon": None}

    def perk(self, perk_id) -> dict | None:
        if perk_id is None:
            return None
        info = self.dd.perk_info(self.version, perk_id) if self.dd else None
        if info:
            return {"name": info["name"], "icon": self._url(info["icon_rel"])}
        return {"name": f"Runa {perk_id}", "icon": None}


def wanted_assets(report) -> tuple[set, set, set]:
    """(champion ids, summoner spell ids, rune / tree ids) a report shows: for Data Dragon downloads."""
    champs, spells, perks = set(), set(), set()
    if not report:
        return champs, spells, perks
    for side in ("ally", "enemy"):
        for p in report["teams"][side]:
            champs.add(p["champion_id"])
            spells.update(i for i in p["spell_ids"] if i)
            perks.update(i for i in (p["keystone_id"], p["style_id"], p["substyle_id"]) if i)
        champs.update(b["champion_id"] for b in report["bans"][side])
    return champs, spells, perks


def enrich(report: dict, assets: Assets) -> dict:
    """Copy of the report with champion names, icons, spells, runes and the history sentence."""
    out = copy.deepcopy(report)
    viewer_label = out.get("viewer_label") or "ti"
    for side in ("ally", "enemy"):
        for p in out["teams"][side]:
            p["champion_name"], p["icon"] = assets.champion(p["champion_id"])
            p["spells"] = [assets.spell(i) for i in p["spell_ids"] if i]
            p["keystone"] = assets.perk(p["keystone_id"])
            p["secondary"] = assets.perk(p["substyle_id"])
            if p["hidden"]:
                p["display_name"] = HIDDEN_LABEL
            elif p["bot"]:
                p["display_name"] = BOT_LABEL
            else:
                p["display_name"] = p["riot_id"]
            p["history_text"] = history_text(p.get("history"), viewer_label, assets.champion_by_name)
        for b in out["bans"][side]:
            b["champion_name"], b["icon"] = assets.champion(b["champion_id"])
    _enrich_duelos(out.get("duelos"), assets)
    return out


def _enrich_duelos(duelos, assets: Assets) -> None:
    """Names and icons for the champions of the lane matchups (in place)."""
    if not isinstance(duelos, dict):
        return
    for lane in duelos.get("lanes") or []:
        for side in ("ally", "enemy"):
            lane[side]["champion_name"], lane[side]["icon"] = assets.champion(lane[side]["champion_id"])
    for duo in (duelos.get("duo") or {}).values():
        for key in ("adc", "support"):
            duo[f"{key}_name"], duo[f"{key}_icon"] = assets.champion(duo[f"{key}_id"])


# ------------------------------------------------------------------------- text
def _rank_line(rank) -> str:
    if not rank:
        return "sin clasificar"
    bits = []
    for key, tag in (("solo", "Solo"), ("flex", "Flex")):
        r = rank.get(key)
        if r:
            bits.append(f"{tag} {r['label']} {r['lp_text']} {r['winrate_text']} ({r['games']})"
                        + (" · racha de victorias" if r["hot_streak"] else ""))
    return " / ".join(bits) if bits else "sin clasificar"


def render_text(report: dict, assets: Assets) -> str:
    """Compact Spanish report for the terminal."""
    rep = enrich(report, assets)
    g = rep["game"]
    who = rep["scouted"]["riot_id"]
    lines = [f"Partida de {who}: {g['queue_label']}" + (f" · lleva {g['length_s'] // 60}:{g['length_s'] % 60:02d}"
                                                       if g.get("length_s", 0) > 0 else " · en la pantalla de carga")]
    for side, title in (("ally", "Tu equipo" if rep["scouted"]["is_main"] else "Su equipo"),
                        ("enemy", "Equipo enemigo")):
        lines += ["", title]
        for p in rep["teams"][side]:
            spells = "/".join(s["name"] for s in p["spells"])
            head = f"  {p['champion_name']:<14} {p['display_name']:<26}"
            if p["hidden"] or p["bot"]:
                lines.append(f"{head} {spells}")
                continue
            keystone = f" · {p['keystone']['name']}" if p.get("keystone") else ""
            tags = (" [tú]" if p["is_viewer"] else "") + (" [de tu clan]" if p["clan"] else "")
            lines.append(f"{head}{tags} {spells}{keystone}")
            detail = [f"rango: {_rank_line(p['rank'])}" if p["rank"] is not None else "rango: sin datos"]
            m = p["mastery"]
            if m is None:
                detail.append("maestría: sin datos")
            else:
                bit = "maestría: " + (f"{m['points_text']} pts, nivel {m['level']}" if not m["never"] else "nunca lo ha jugado")
                if m["last_text"] and not m["never"]:
                    bit += f", {m['last_text']}"
                if m["label"]:
                    bit += f" ({m['label']})"
                detail.append(bit)
            r = p.get("recent")
            if r and r["known"]:
                bit = f"últimas {r['known']} ranked: {r['record_text']}"
                if r["on_champion"]:
                    bit += " · con este campeón: " + ", ".join(
                        ("V" if x["win"] else "D") + f" {x['kda']}" for x in r["on_champion"])
                detail.append(bit)
            if p["history_text"]:
                detail.append(p["history_text"])
            lines.append("      " + " | ".join(detail))
    for side, title in (("ally", "Bans (tu equipo)"), ("enemy", "Bans (enemigos)")):
        if rep["bans"][side]:
            lines += ["", f"{title}: " + ", ".join(b["champion_name"] for b in rep["bans"][side])]
    r = rep["requests"]
    lines += ["", f"Peticiones a Riot usadas: {r['used']} de {r['budget']} (máximo por partida)"
              + (f"; {r['from_cache']} jugadores salieron de la caché de 6 h" if r["from_cache"] else "")]
    for s in r["skipped"]:
        lines.append(f"Omitido: {s}.")
    lines.append("Datos de Riot (Spectator). Solo se muestra información pública de las cuentas.")
    return "\n".join(lines)
