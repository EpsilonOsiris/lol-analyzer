"""Death, fight and objective analysis from a stored match + timeline (pure functions).

Positions are Riot map coordinates (0-14820 on both axes; the blue base, team 100, is at the
bottom-left and the red base, team 200, at the top-right). What Riot does not give is flagged as
approximate: players' positions between kills come from the once-per-minute frames, levels and gold
come from the last frame before the event, and wards have no position at all.

`analyze_game(..., detail=False)` is the light version cached for every game of a player (it feeds
the aggregates); `detail=True` adds the damage breakdown and the fights for a single game page.
"""
from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict

from .analyze import effect_size, role_name
from .ddragon import champion_display
from .metrics import _int, _num, find_lane_opponent, find_participant

MAP_SIZE = 14820
EARLY_MS = 14 * 60000                 # "antes del minuto 14"
BASE_BOX = 3800                       # a base is the corner square of this size
LANE_BAND = 2700                      # lanes hug the map edges: this wide
MID_BAND = 1300                       # |x - y| at most this much is the mid lane
RIVER_BAND = 1800                     # |x + y - MAP_SIZE| at most this much is the river
SIDE_NEUTRAL = 500                    # this close to the river diagonal counts as "centro"
UNSPENT_GOLD = 1000
AFTER_KILL_MS = 60_000
OBJECTIVE_WINDOW_MS = 45_000
WARD_WINDOW_MS = 90_000
CONTROL_WARD_WINDOW_MS = 180_000
CONTROL_WARD_ID = 2055
FIGHT_GAP_MS = 15_000
FIGHT_RADIUS = 3500
FIGHT_AFTERMATH_MS = 60_000
FAR_UNITS = 4000
MIN_MATCHUP_GAMES = 3
PITS = {"dragon": (9866, 4414), "baron": (5007, 10471), "herald": (5007, 10471)}
LOST_KINDS = ("dragon", "baron", "herald")
MONSTER_KINDS = {"DRAGON": "dragon", "BARON_NASHOR": "baron", "RIFTHERALD": "herald",
                 "HORDE": "grubs", "ATAKHAN": "atakhan"}
BUILDING_KINDS = {"TOWER_BUILDING": "tower", "INHIBITOR_BUILDING": "inhib"}
DRAGON_NAMES = {
    "AIR_DRAGON": "Dragón de las nubes", "EARTH_DRAGON": "Dragón de montaña",
    "FIRE_DRAGON": "Dragón infernal", "WATER_DRAGON": "Dragón del océano",
    "HEXTECH_DRAGON": "Dragón hextech", "CHEMTECH_DRAGON": "Dragón quimiotécnico",
    "ELDER_DRAGON": "Dragón anciano",
}
OBJECTIVE_NAMES = {"dragon": "Dragón", "baron": "Barón Nashor", "herald": "Heraldo de la Grieta",
                   "grubs": "Larvas del vacío", "atakhan": "Atakhan", "tower": "Torre",
                   "inhib": "Inhibidor"}
# "{team} {verb}" in the sentences about objectives near a death.
OBJECTIVE_DONE = {"dragon": "tomó un dragón", "baron": "tomó el Barón", "herald": "tomó el Heraldo",
                  "grubs": "tomó larvas del vacío", "atakhan": "derrotó a Atakhan",
                  "tower": "destruyó una torre", "inhib": "destruyó un inhibidor"}

# Base respawn wait by level (seconds); the time factor adds up to +50% as the game goes on.
BASE_RESPAWN_S = (10, 10, 12, 12, 14, 16, 20, 25, 28, 32.5, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5)

ZONE_LABELS = {"top": "Línea de top", "mid": "Línea de mid", "bot": "Línea de bot",
               "rio": "Río", "jungla": "Jungla", "base": "Base"}
ZONE_DE = {"top": "de la línea de top", "mid": "de la línea de mid", "bot": "de la línea de bot",
           "rio": "del río", "jungla": "de la jungla"}
SIDE_LABELS = {"propio": "tu lado", "enemigo": "lado enemigo", "centro": "centro"}


# ----------------------------------------------------------------- small helpers
def fmt_clock(ms) -> str:
    total = int((ms or 0) // 1000)
    return f"{total // 60}:{total % 60:02d}"


def join_es(items) -> str:
    items = [str(i) for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


def plural(n, one, many) -> str:
    return one if n == 1 else many


def _rate(num, den) -> dict:
    return {"n": num, "of": den, "pct": (num / den) if den else None}


def _team_fallback(pid):
    if isinstance(pid, int) and 1 <= pid <= 10:
        return 100 if pid <= 5 else 200
    return None


# ------------------------------------------------------------- map geometry
def zone_of(x, y):
    """(zone, corner) of a map point. zone: base / top / bot / mid / rio / jungla.

    `corner` is "azul" or "rojo" for a base and None otherwise. Lanes are bands along the map edges
    (top: left and top edges; bot: bottom and right edges), mid is the diagonal band, the river is
    the anti-diagonal band and everything else is jungle. The mid band wins where it meets the river.
    """
    if x is None or y is None:
        return None, None
    if x < BASE_BOX and y < BASE_BOX:
        return "base", "azul"
    if x > MAP_SIZE - BASE_BOX and y > MAP_SIZE - BASE_BOX:
        return "base", "rojo"
    if (x < LANE_BAND and y >= BASE_BOX) or (y > MAP_SIZE - LANE_BAND and x <= MAP_SIZE - BASE_BOX):
        return "top", None
    if (y < LANE_BAND and x >= BASE_BOX) or (x > MAP_SIZE - LANE_BAND and y <= MAP_SIZE - BASE_BOX):
        return "bot", None
    if abs(x - y) <= MID_BAND:
        return "mid", None
    if abs(x + y - MAP_SIZE) <= RIVER_BAND:
        return "rio", None
    return "jungla", None


def side_of(x, y, team_id, zone=None, corner=None) -> str:
    """"propio" / "enemigo" / "centro": which side of the river diagonal, from `team_id`'s view."""
    if zone == "base":
        mine = "azul" if team_id == 100 else "rojo"
        return "propio" if corner == mine else "enemigo"
    dev = (x + y) - MAP_SIZE            # negative = blue half of the map
    if abs(dev) <= SIDE_NEUTRAL and zone in ("rio", "mid"):
        return "centro"
    if team_id == 200:
        dev = -dev
    return "propio" if dev < 0 else "enemigo"


def place(x, y, team_id) -> dict:
    zone, corner = zone_of(x, y)
    side = side_of(x, y, team_id, zone, corner) if zone else None
    return {"zone": zone, "side": side, "label": place_label(zone, side)}


def place_label(zone, side) -> str:
    """Short label for tables: "Línea de bot · tu lado", "Base enemiga"."""
    if zone is None:
        return "Sin posición"
    if zone == "base":
        return "Tu base" if side == "propio" else "Base enemiga"
    return f"{ZONE_LABELS[zone]} · {SIDE_LABELS.get(side, '')}"


def place_phrase(zone, side) -> str:
    """"en tu lado del río", "en la base enemiga" for a sentence."""
    if zone is None:
        return "en un punto sin registro"
    if zone == "base":
        return "en tu base" if side == "propio" else "en la base enemiga"
    where = {"propio": "tu lado", "enemigo": "el lado enemigo", "centro": "el centro"}.get(side, "el mapa")
    return f"en {where} {ZONE_DE[zone]}"


def death_timer_s(level, minute) -> float:
    """Approximate respawn wait (seconds) for a champion of `level` dying at game minute `minute`."""
    base = BASE_RESPAWN_S[min(max(_int(level, 1), 1), 18) - 1]
    if minute <= 15:
        factor = 0.0
    elif minute <= 30:
        factor = (minute - 15) * 2 * 0.00425
    elif minute <= 45:
        factor = 0.1275 + (minute - 30) * 2 * 0.003
    else:
        factor = 0.2175 + (minute - 45) * 2 * 0.0145
    return base * (1 + min(factor, 0.5))


# --------------------------------------------------------------- parsed game
class _Game:
    """The match + timeline parsed once: participants, sorted frames and events."""

    def __init__(self, match, timeline, puuid):
        info = (match or {}).get("info") or {}
        self.info = info
        self.parts: dict[int, dict] = {}
        for p in info.get("participants") or []:
            pid = p.get("participantId")
            if isinstance(pid, int):
                self.parts[pid] = {
                    "pid": pid, "team": p.get("teamId") or _team_fallback(pid),
                    "champion": p.get("championName") or "?", "role": p.get("teamPosition") or "",
                    "name": p.get("riotIdGameName") or p.get("summonerName") or "",
                }
        me = find_participant(info.get("participants"), puuid)
        self.me = me.get("participantId") if me else None
        self.me_raw = me
        self.team = (self.parts.get(self.me) or {}).get("team")
        frames = [fr for fr in ((timeline or {}).get("info") or {}).get("frames") or []
                  if _num(fr.get("timestamp")) is not None]
        frames.sort(key=lambda fr: fr["timestamp"])
        self.frames = frames
        self.frame_ts = [fr["timestamp"] for fr in frames]
        self.events = sorted(
            (e for fr in frames for e in fr.get("events") or [] if _num(e.get("timestamp")) is not None),
            key=lambda e: e["timestamp"])
        self.kills = [e for e in self.events if e.get("type") == "CHAMPION_KILL"
                      and isinstance(e.get("victimId"), int) and 1 <= e["victimId"] <= 10]

    def team_of(self, pid):
        return (self.parts.get(pid) or {}).get("team") or _team_fallback(pid)

    def champ(self, pid) -> dict | None:
        p = self.parts.get(pid)
        return {"pid": pid, "champion": p["champion"], "role": p["role"], "team": p["team"]} if p else None

    def idx_before(self, t) -> int | None:
        i = bisect.bisect_right(self.frame_ts, t) - 1
        return i if i >= 0 else (0 if self.frames else None)

    def idx_nearest(self, t) -> int | None:
        if not self.frames:
            return None
        i = bisect.bisect_left(self.frame_ts, t)
        if i == 0:
            return 0
        if i >= len(self.frames):
            return len(self.frames) - 1
        return i if self.frame_ts[i] - t < t - self.frame_ts[i - 1] else i - 1

    def pframe(self, idx, pid) -> dict | None:
        if idx is None or pid is None:
            return None
        pfs = self.frames[idx].get("participantFrames") or {}
        pf = pfs.get(str(pid))
        if pf is None:
            pf = pfs.get(pid)
        return pf if isinstance(pf, dict) else None

    def pos_at(self, idx, pid):
        pos = (self.pframe(idx, pid) or {}).get("position") or {}
        x, y = _num(pos.get("x")), _num(pos.get("y"))
        return (int(x), int(y)) if x is not None and y is not None else None

    def positions_nearest(self, t) -> tuple[dict, int | None]:
        """({pid: (x, y)}, frame timestamp) from the frame closest to time `t` (approximate)."""
        idx = self.idx_nearest(t)
        if idx is None:
            return {}, None
        out = {}
        for pid in self.parts:
            pos = self.pos_at(idx, pid)
            if pos:
                out[pid] = pos
        return out, self.frame_ts[idx]


def _kill_team(game, ev):
    """Team credited with a champion kill (a tower/minion kill counts for the other team)."""
    killer = ev.get("killerId")
    if isinstance(killer, int) and 1 <= killer <= 10:
        return game.team_of(killer)
    victim_team = game.team_of(ev.get("victimId"))
    return {100: 200, 200: 100}.get(victim_team)


def _kill_pos(ev):
    pos = ev.get("position") or {}
    x, y = _num(pos.get("x")), _num(pos.get("y"))
    return (int(x), int(y)) if x is not None and y is not None else None


# --------------------------------------------------------------- ability names
SUMMONER_NAMES = {
    "summonerdot": "Ignición", "summonerflash": "Destello", "summonerexhaust": "Extenuación",
    "summonerheal": "Curación", "summonerbarrier": "Barrera", "summonerteleport": "Teletransporte",
    "summonerhaste": "Fantasmal", "summonerboost": "Purificar", "summonersmite": "Castigo",
}
ITEM_FALLBACKS = {
    "itemknightsvow": "Voto del caballero", "elixirofsorcery": "Elixir de hechicería",
    "itemredemption": "Redención", "hextechgunbladespell": "Hextech Gunblade",
}


class NoNames:
    """Stand-in when Data Dragon is not available: ability letters only."""

    def spell(self, champion, spell_name, slot):
        return None

    def passive(self, champion):
        return None

    def item(self, item_id):
        return None


def ability_label(entry: dict, champion, names=None) -> str:
    """Spanish label of one damage entry (victimDamageReceived / victimDamageDealt).

    Riot's slots are unreliable for anything that is not a plain Q/W/E/R, so the internal spell
    name decides first: unnamed effects, summoner spells, items, basic attacks and passives are
    recognised before falling back to the slot number.
    """
    names = names or NoNames()
    raw = str(entry.get("spellName") or "")
    low = raw.lower()
    slot = entry.get("spellSlot")
    if not low:
        return "Runas y otros efectos"
    if low == "burning":
        return "Quemadura"
    if low.startswith("summoner") or low.startswith("s5_summoner"):
        key = "summonersmite" if "smite" in low else low
        return SUMMONER_NAMES.get(key, "Hechizo de invocador")
    digits = "".join(ch for ch in low.split("_")[0] if ch.isdigit())
    if low in ITEM_FALLBACKS or (digits and low.startswith(digits) and ("active" in low)) or low.startswith("item"):
        name = names.item(digits) if digits else None
        return f"Objeto: {name or ITEM_FALLBACKS.get(low, 'activo')}"
    if entry.get("basic") or "basicattack" in low or "critattack" in low:
        return "Ataque básico"
    if low.endswith("passive") or slot == 63:
        name = names.passive(champion)
        return f"Pasiva · {name}" if name else "Pasiva"
    found = names.spell(champion, low, slot)
    if found:
        letter, name = found
        return f"{letter} · {name}" if name else f"Habilidad {letter}"
    if isinstance(slot, int) and 0 <= slot <= 3:
        return f"Habilidad {'QWER'[slot]}"
    return "Otra habilidad"


def _dmg_amounts(entry):
    return (_int(entry.get("physicalDamage")), _int(entry.get("magicDamage")), _int(entry.get("trueDamage")))


def _dominant(phys, mag, true) -> str:
    best = max((phys, "fisico"), (mag, "magico"), (true, "verdadero"))
    return best[1]


def _source_of(game, entry) -> tuple[str, str]:
    """(key, kind) of who dealt a damage entry: a champion (by participant id) or a group."""
    pid = entry.get("participantId")
    if isinstance(pid, int) and 1 <= pid <= 10:
        return f"p{pid}", "champion"
    kind = {"MINION": "minion", "TOWER": "tower", "MONSTER": "monster"}.get(entry.get("type"), "other")
    return kind, kind


SOURCE_LABELS = {"minion": "Minions", "tower": "Torres", "monster": "Monstruos de la jungla",
                 "other": "Otros (entorno)"}


def damage_totals(entries) -> dict:
    """Totals of damage entries by type and by kind of source (no names needed)."""
    phys = mag = true = champs = 0
    kinds = Counter()
    for e in entries or []:
        p, m, t = _dmg_amounts(e)
        total = p + m + t
        phys, mag, true = phys + p, mag + m, true + t
        pid = e.get("participantId")
        kind = "champion" if isinstance(pid, int) and 1 <= pid <= 10 else \
            {"MINION": "minion", "TOWER": "tower", "MONSTER": "monster"}.get(e.get("type"), "other")
        kinds[kind] += total
        if kind == "champion":
            champs += total
    return {"total": phys + mag + true, "phys": phys, "mag": mag, "true": true,
            "champions": champs, "by_kind": dict(kinds)}


def damage_breakdown(game, entries, names=None) -> list[dict]:
    """Sources of the damage received, biggest first, each with its abilities (Spanish labels)."""
    sources: dict[str, dict] = {}
    for e in entries or []:
        key, kind = _source_of(game, e)
        p, m, t = _dmg_amounts(e)
        if p + m + t <= 0:
            continue
        src = sources.get(key)
        if src is None:
            pid = e.get("participantId") if kind == "champion" else None
            who = game.champ(pid) if pid else None
            src = {"key": key, "kind": kind, "pid": pid,
                   "champion": who["champion"] if who else None,
                   "team": who["team"] if who else None,
                   "label": SOURCE_LABELS.get(kind) if kind != "champion" else None,
                   "phys": 0, "mag": 0, "true": 0, "abilities": {}}
            sources[key] = src
        src["phys"] += p
        src["mag"] += m
        src["true"] += t
        champion = src["champion"] or ""
        label = ability_label(e, champion, names) if kind == "champion" else SOURCE_LABELS[kind]
        ab = src["abilities"].setdefault(label, {"label": label, "phys": 0, "mag": 0, "true": 0})
        ab["phys"] += p
        ab["mag"] += m
        ab["true"] += t
    out = []
    for src in sources.values():
        abilities = []
        for ab in src["abilities"].values():
            ab["total"] = ab["phys"] + ab["mag"] + ab["true"]
            ab["type"] = _dominant(ab["phys"], ab["mag"], ab["true"])
            abilities.append(ab)
        abilities.sort(key=lambda a: -a["total"])
        src["abilities"] = abilities
        src["total"] = src["phys"] + src["mag"] + src["true"]
        out.append(src)
    out.sort(key=lambda s: -s["total"])
    return out


def damage_dealt_breakdown(game, entries) -> dict:
    """What the player hit before dying (victimDamageDealt), by target."""
    targets: dict[str, dict] = {}
    for e in entries or []:
        key, kind = _source_of(game, e)
        p, m, t = _dmg_amounts(e)
        if p + m + t <= 0:
            continue
        # In "dealt" entries participantId is the TARGET.
        tgt = targets.setdefault(key, {"key": key, "kind": kind, "total": 0,
                                       "champion": (game.champ(e.get("participantId")) or {}).get("champion")
                                       if kind == "champion" else None,
                                       "label": SOURCE_LABELS.get(kind) if kind != "champion" else None})
        tgt["total"] += p + m + t
    rows = sorted(targets.values(), key=lambda r: -r["total"])
    return {"total": sum(r["total"] for r in rows), "targets": rows,
            "champions": sum(r["total"] for r in rows if r["kind"] == "champion")}


# ----------------------------------------------------------- objectives / events
def _objective_events(game):
    """Objective events as {t, kind, by (team that took it), label, pos, subtype, killer}."""
    out = []
    for e in game.events:
        etype = e.get("type")
        if etype == "ELITE_MONSTER_KILL":
            kind = MONSTER_KINDS.get(e.get("monsterType"))
            if not kind:
                continue
            team = e.get("killerTeamId")
            if team not in (100, 200):
                team = game.team_of(e.get("killerId"))
            label = DRAGON_NAMES.get(e.get("monsterSubType"), OBJECTIVE_NAMES[kind]) if kind == "dragon" \
                else OBJECTIVE_NAMES[kind]
            out.append({"t": e["timestamp"], "kind": kind, "team": team, "label": label,
                        "killer": e.get("killerId"), "pos": _kill_pos(e)})
        elif etype == "BUILDING_KILL":
            kind = BUILDING_KINDS.get(e.get("buildingType"))
            owner = e.get("teamId")
            if not kind or owner not in (100, 200):
                continue
            out.append({"t": e["timestamp"], "kind": kind, "team": 300 - owner,
                        "label": OBJECTIVE_NAMES[kind], "killer": e.get("killerId"), "pos": _kill_pos(e)})
    return out


def _dead_intervals(game) -> dict[int, list[tuple[float, float]]]:
    """pid -> [(died_at_ms, back_at_ms)] using the approximate respawn timer."""
    out = defaultdict(list)
    for ev in game.kills:
        t = ev["timestamp"]
        victim = ev["victimId"]
        idx = game.idx_before(t)
        level = (game.pframe(idx, victim) or {}).get("level")
        out[victim].append((t, t + death_timer_s(level, t / 60000.0) * 1000))
    return out


def _is_dead(intervals, pid, t) -> bool:
    return any(a <= t < b for a, b in intervals.get(pid, ()))


def lost_objectives(game, intervals=None) -> list[dict]:
    """Enemy dragons / barons / heralds with where the player was when they fell."""
    if game.me is None:
        return []
    intervals = intervals if intervals is not None else _dead_intervals(game)
    out = []
    for ob in _objective_events(game):
        if ob["kind"] not in LOST_KINDS or ob["team"] in (None, game.team):
            continue
        t = ob["t"]
        pit = PITS[ob["kind"]]
        idx = game.idx_nearest(t)
        pos = game.pos_at(idx, game.me)
        dist = math.dist(pos, pit) if pos else None
        dead = _is_dead(intervals, game.me, t)
        died_ago = None
        for a, b in intervals.get(game.me, ()):
            if a <= t:
                died_ago = (t - a) / 1000.0 if t < b else died_ago
        recent = any(0 <= t - a <= OBJECTIVE_WINDOW_MS for a, _b in intervals.get(game.me, ()))
        allies = [p for p, v in game.parts.items() if v["team"] == game.team and p != game.me
                  and _is_dead(intervals, p, t)]
        # "lejos" is for a living player: a dead one's frame position says nothing about the pit.
        far = (not dead) and dist is not None and dist > FAR_UNITS
        status = "muerto" if dead else "lejos" if far else "cerca" if dist is not None else "sin datos"
        out.append({"t": t, "clock": fmt_clock(t), "kind": ob["kind"], "label": ob["label"],
                    "pit": pit, "player_pos": pos, "distance": dist, "far": far, "dead": dead,
                    "died_recently": recent, "dead_for_s": died_ago, "allies_dead": len(allies),
                    "status": status, "killer": ob["killer"]})
    return out


# ------------------------------------------------------------------- deaths
def _context(game) -> dict:
    parts = game.parts
    enemy = [p for p in parts.values() if p["team"] != game.team]
    jungler = next((p for p in enemy if p["role"] == "JUNGLE"), None)
    bot = [p for r in ("BOTTOM", "UTILITY") for p in enemy if p["role"] == r]
    adc = next((p for p in enemy if p["role"] == "BOTTOM"), None)
    support = next((p for p in enemy if p["role"] == "UTILITY"), None)
    opp = find_lane_opponent(game.info.get("participants"), game.me_raw) if game.me_raw else None
    return {
        "lane_opponent": opp.get("championName") if opp else None,
        "enemy_jungler": jungler["champion"] if jungler else None,
        "enemy_jungler_pid": jungler["pid"] if jungler else None,
        "enemy_bot": [p["champion"] for p in bot],
        "enemy_bot_pids": [p["pid"] for p in bot],
        "enemy_adc": adc["champion"] if adc else None,
        "enemy_adc_pid": adc["pid"] if adc else None,
        "enemy_support": support["champion"] if support else None,
        "enemy_support_pid": support["pid"] if support else None,
    }


def _nearby_objectives(game, objectives, t):
    rows = []
    for ob in objectives:
        dt = (ob["t"] - t) / 1000.0
        if abs(dt) <= OBJECTIVE_WINDOW_MS / 1000.0 and ob["team"] in (100, 200):
            rows.append({"kind": ob["kind"], "label": ob["label"],
                         "by": "aliado" if ob["team"] == game.team else "enemigo", "dt_s": round(dt)})
    rows.sort(key=lambda r: abs(r["dt_s"]))
    return rows


def _death_record(game, ev, n, ctx, objectives, detail, names):
    t = ev["timestamp"]
    pid = game.me
    pos = _kill_pos(ev)
    pl = place(*(pos or (None, None)), game.team)
    killer_id = ev.get("killerId")
    assists = [a for a in (ev.get("assistingParticipantIds") or [])
               if isinstance(a, int) and 1 <= a <= 10 and a != pid]
    is_champ = isinstance(killer_id, int) and 1 <= killer_id <= 10
    received = ev.get("victimDamageReceived") or []
    totals = damage_totals(received)

    if is_champ:
        kind = "campeon"
    else:
        by_kind = totals["by_kind"]
        top = max(((v, k) for k, v in by_kind.items() if k != "champion"), default=None)
        kind = {"tower": "torre", "minion": "minion", "monster": "monstruo"}.get(top[1] if top else "", "entorno")

    killer = game.champ(killer_id) if is_champ else None
    assisters = [game.champ(a) for a in assists if game.champ(a)]
    involved = ([killer] if killer else []) + assisters
    involved_pids = {p["pid"] for p in involved}
    jungler = ctx["enemy_jungler_pid"]
    jungler_involved = (jungler in involved_pids) if (jungler and kind == "campeon") else None

    idx = game.idx_before(t)
    mine = game.pframe(idx, pid) or {}
    theirs = game.pframe(idx, killer_id) if is_champ else None
    level = _num(mine.get("level"))
    klevel = _num((theirs or {}).get("level"))
    gold = _num(mine.get("currentGold"))
    total_gold = _num(mine.get("totalGold"))
    ktotal = _num((theirs or {}).get("totalGold"))

    # Own kill shortly before dying.
    since_kill = None
    for e in game.kills:
        if e["timestamp"] >= t:
            break
        if e.get("killerId") == pid and t - e["timestamp"] <= AFTER_KILL_MS:
            since_kill = (t - e["timestamp"]) / 1000.0

    wards = sum(1 for e in game.events
                if e["type"] == "WARD_PLACED" and e.get("creatorId") and game.team_of(e["creatorId"]) == game.team
                and t - WARD_WINDOW_MS <= e["timestamp"] < t)
    control = any(e["type"] == "ITEM_PURCHASED" and e.get("participantId") == pid
                  and e.get("itemId") == CONTROL_WARD_ID and t - CONTROL_WARD_WINDOW_MS <= e["timestamp"] <= t
                  for e in game.events)

    rec = {
        "n": n, "t": t, "clock": fmt_clock(t), "minute": t / 60000.0, "early": t < EARLY_MS,
        "pos": pos, "zone": pl["zone"], "side": pl["side"], "place": pl["label"],
        "place_phrase": place_phrase(pl["zone"], pl["side"]),
        "kind": kind, "killer": killer, "assisters": assisters, "enemies": len(involved),
        "enemy_jungler_involved": jungler_involved,
        "level": level, "killer_level": klevel,
        "level_diff": (level - klevel) if level is not None and klevel is not None else None,
        "current_gold": gold, "total_gold": total_gold, "killer_total_gold": ktotal,
        "gold_diff": (total_gold - ktotal) if total_gold is not None and ktotal is not None else None,
        "unspent_1000": (gold >= UNSPENT_GOLD) if gold is not None else None,
        "frame_ts": game.frame_ts[idx] if idx is not None else None,
        "since_own_kill_s": since_kill, "after_own_kill": since_kill is not None,
        "objectives_near": _nearby_objectives(game, objectives, t),
        "wards_allied_90s": wards, "control_ward_3min": control,
        "damage_received": totals, "fight": None,
    }
    if detail:
        rec["damage_sources"] = damage_breakdown(game, received, names)
        rec["damage_dealt"] = damage_dealt_breakdown(game, ev.get("victimDamageDealt") or [])
        positions, frame_ts = game.positions_nearest(t)
        rec["positions"] = positions
        rec["positions_frame_ts"] = frame_ts
    return rec


# -------------------------------------------------------------------- fights
def cluster_kills(kills, gap_ms=FIGHT_GAP_MS, radius=FIGHT_RADIUS) -> list[list[dict]]:
    """Group champion kills (sorted by time) into fights.

    A kill joins the open cluster (last kill at most `gap_ms` earlier) whose centroid is closest
    and within `radius` units; otherwise it starts a new cluster. Kills of two simultaneous fights
    in different places therefore stay apart.
    """
    clusters: list[dict] = []
    for k in kills:
        pos = _kill_pos(k)
        best, best_d = None, None
        for c in clusters:
            if k["timestamp"] - c["last"] > gap_ms:
                continue
            if pos is None or not c["n"]:
                d = 0.0
            else:
                d = math.dist(pos, (c["sx"] / c["n"], c["sy"] / c["n"]))
            if d <= radius and (best is None or d < best_d):
                best, best_d = c, d
        if best is None:
            best = {"kills": [], "sx": 0.0, "sy": 0.0, "n": 0, "last": k["timestamp"]}
            clusters.append(best)
        best["kills"].append(k)
        best["last"] = k["timestamp"]
        if pos:
            best["sx"] += pos[0]
            best["sy"] += pos[1]
            best["n"] += 1
    return [c["kills"] for c in clusters]


def _is_fight(cluster, me) -> bool:
    if len(cluster) >= 3:
        return True
    if len(cluster) < 2 or me is None:
        return False
    return any(k.get("killerId") == me or k.get("victimId") == me
               or me in (k.get("assistingParticipantIds") or []) for k in cluster)


def find_fights(game, objectives=None) -> list[dict]:
    objectives = objectives if objectives is not None else _objective_events(game)
    fights = []
    for cluster in cluster_kills(game.kills):
        if not _is_fight(cluster, game.me):
            continue
        start, end = cluster[0]["timestamp"], cluster[-1]["timestamp"]
        kills, team_kills = [], Counter()
        my_events = []
        for k in cluster:
            killer = k.get("killerId")
            assists = [a for a in (k.get("assistingParticipantIds") or []) if isinstance(a, int)]
            kteam = _kill_team(game, k)
            team_kills[kteam] += 1
            row = {"t": k["timestamp"], "clock": fmt_clock(k["timestamp"]),
                   "killer": killer if isinstance(killer, int) and killer > 0 else None,
                   "victim": k["victimId"], "assisters": assists, "pos": _kill_pos(k),
                   "killer_team": kteam, "victim_team": game.team_of(k["victimId"])}
            kills.append(row)
            if game.me is not None:
                what = ("murió" if k["victimId"] == game.me else "mató" if killer == game.me
                        else "asistió" if game.me in assists else None)
                if what:
                    my_events.append({"t": row["t"], "clock": row["clock"], "what": what, "pos": row["pos"]})
        mine, theirs = team_kills.get(game.team, 0), team_kills.get(300 - game.team if game.team else None, 0)
        result = "ganada" if mine > theirs else "perdida" if mine < theirs else "empatada"
        mid = (start + end) / 2
        positions, frame_ts = game.positions_nearest(mid)
        pts = [k["pos"] for k in kills if k["pos"]]
        center = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else None
        aftermath = [{"kind": o["kind"], "label": o["label"],
                      "by": "aliado" if o["team"] == game.team else "enemigo",
                      "dt_s": round((o["t"] - end) / 1000.0)}
                     for o in objectives
                     if o["team"] in (100, 200) and start <= o["t"] <= end + FIGHT_AFTERMATH_MS]
        first_victim = kills[0]["victim"]
        pl = place(*(center or (None, None)), game.team)
        fights.append({
            "start": start, "end": end, "clock": fmt_clock(start), "duration_s": round((end - start) / 1000.0),
            "kills": kills, "my_team_kills": mine, "enemy_team_kills": theirs, "result": result,
            "first_death": first_victim, "first_death_mine": game.team_of(first_victim) == game.team,
            "player_died": any(e["what"] == "murió" for e in my_events),
            "player_kills": sum(1 for e in my_events if e["what"] == "mató"),
            "player_assists": sum(1 for e in my_events if e["what"] == "asistió"),
            "player_involved": bool(my_events),
            "player_first": my_events[0] if my_events else None,
            "player_last": my_events[-1] if my_events else None,
            "positions": positions, "positions_frame_ts": frame_ts, "center": center,
            "place": pl["label"], "aftermath": aftermath,
        })
    for i, f in enumerate(fights, 1):
        f["id"] = i
    return fights


# ------------------------------------------------------------------ one game
def analyze_game(match, timeline, puuid, detail=True, names=None) -> dict | None:
    """Everything about the player's deaths in one game; None without a timeline or the player."""
    game = _Game(match, timeline, puuid)
    if game.me is None or not game.frames:
        return None
    info = game.info
    ctx = _context(game)
    objectives = _objective_events(game)
    intervals = _dead_intervals(game)
    deaths = [_death_record(game, ev, i, ctx, objectives, detail, names)
              for i, ev in enumerate((e for e in game.kills if e["victimId"] == game.me), 1)]
    me = game.parts[game.me]
    out = {
        "match_id": (match.get("metadata") or {}).get("matchId") or info.get("gameId"),
        "created": _num(info.get("gameCreation")), "game_version": info.get("gameVersion"),
        "win": bool((game.me_raw or {}).get("win")), "duration_s": _num(info.get("gameDuration")),
        "pid": game.me, "team": game.team, "champion": me["champion"], "role": me["role"],
        "participants": game.parts, "context": ctx, "deaths": deaths,
        "objectives_lost": lost_objectives(game, intervals),
    }
    if detail:
        fights = find_fights(game, objectives)
        out["fights"] = fights
        for f in fights:
            for k in f["kills"]:
                if k["victim"] == game.me:
                    for d in deaths:
                        if d["t"] == k["t"]:
                            d["fight"] = f["id"]
    return out


# ---------------------------------------------------------------- Spanish text
def describe_death(d: dict, game: dict, display=champion_display) -> list[str]:
    """The "trasfondo" of one death as short Spanish sentences (also the text version of the charts)."""
    ctx = game["context"]
    facts = []
    killer = d.get("killer")
    if d["kind"] == "campeon" and killer:
        text = f"Te mató {display(killer['champion'])} ({role_name(killer['role'])})"
        if d["assisters"]:
            text += " con ayuda de " + join_es(
                f"{display(a['champion'])} ({role_name(a['role'])})" for a in d["assisters"])
        else:
            text += " en solitario"
        jg = ctx.get("enemy_jungler")
        if jg:
            if d["enemy_jungler_involved"]:
                text += f"; su jungla ({display(jg)}) participó"
            else:
                text += f"; su jungla ({display(jg)}) no participó"
        facts.append(text + ".")
    else:
        facts.append({
            "torre": "Te mató una torre.", "minion": "Te mataron los minions.",
            "monstruo": "Te mató un monstruo de la jungla.",
        }.get(d["kind"], "Moriste sin que te matara un campeón (ejecución o efecto del entorno)."))

    level_txt = ""
    if d["level_diff"] is not None and killer:
        n = abs(int(d["level_diff"]))
        if d["level_diff"] < 0:
            level_txt = f"Tenías {n} {plural(n, 'nivel', 'niveles')} menos que {display(killer['champion'])}"
        elif d["level_diff"] > 0:
            level_txt = f"Tenías {n} {plural(n, 'nivel', 'niveles')} más que {display(killer['champion'])}"
        else:
            level_txt = f"Estabas al mismo nivel que {display(killer['champion'])}"
    if d["current_gold"] is not None and d["current_gold"] >= 100:
        gold = f"{int(d['current_gold'])} de oro sin gastar"
        facts.append(f"{level_txt} y {gold}." if level_txt else f"Tenías {gold}.")
    elif level_txt:
        facts.append(level_txt + ".")

    if d["after_own_kill"]:
        facts.append(f"Habías matado a alguien {int(round(d['since_own_kill_s']))} s antes.")
    facts.append(f"Moriste {d['place_phrase']}.")
    for ob in d["objectives_near"][:2]:
        who = "Tu equipo" if ob["by"] == "aliado" else "El equipo enemigo"
        when = (f"{abs(ob['dt_s'])} s después de tu muerte" if ob["dt_s"] > 0
                else f"{abs(ob['dt_s'])} s antes de tu muerte" if ob["dt_s"] < 0 else "justo cuando moriste")
        facts.append(f"{who} {OBJECTIVE_DONE[ob['kind']]} {when}.")
    w = d["wards_allied_90s"]
    facts.append(
        f"Tu equipo puso {w} {plural(w, 'ward', 'wards')} en los 90 s anteriores y "
        f"{'sí' if d['control_ward_3min'] else 'no'} habías comprado un ward de control en los últimos 3 minutos "
        "(aproximado: Riot no da la posición de los wards).")
    dmg = d["damage_received"]
    if dmg["total"]:
        pct = lambda v: round(v * 100 / dmg["total"])  # noqa: E731
        facts.append(
            f"Recibiste {dmg['total']} de daño ({pct(dmg['phys'])}% físico, {pct(dmg['mag'])}% mágico, "
            f"{pct(dmg['true'])}% verdadero); {pct(dmg['champions'])}% vino de campeones.")
    dealt = d.get("damage_dealt")
    if dealt is not None:
        if dealt["total"]:
            facts.append(f"Antes de morir hiciste {dealt['total']} de daño ({dealt['champions']} a campeones).")
        else:
            facts.append("Antes de morir no hiciste daño.")
    return facts


def describe_fight(f: dict, game: dict, display=champion_display) -> str:
    """One-line summary of a fight for the list."""
    res = {"ganada": "Ganó tu equipo", "perdida": "Perdió tu equipo", "empatada": "Empate"}[f["result"]]
    text = f"{res} {f['my_team_kills']}-{f['enemy_team_kills']}"
    first = game["participants"].get(f["first_death"])
    if first:
        text += f"; la primera baja fue {'de tu equipo' if f['first_death_mine'] else 'del equipo enemigo'} ({display(first['champion'])})"
    if f["player_died"]:
        text += "; moriste"
    elif f["player_involved"]:
        text += f"; participaste ({f['player_kills']} kills, {f['player_assists']} asistencias)"
    else:
        text += "; no participaste"
    return text + "."


# ----------------------------------------------------------------- aggregates
def _split(items, predicate=None, universe=None) -> dict:
    """{all, win, loss} rates of `predicate` among `items` ((win, death) pairs) in the universe."""
    out = {}
    for name, sel in (("all", None), ("win", True), ("loss", False)):
        pool = [d for w, d in items if (sel is None or w == sel) and (universe is None or universe(d))]
        out[name] = _rate(sum(1 for d in pool if predicate(d)) if predicate else len(pool), len(pool))
    return out


def deaths_of(analyses):
    """(win, death_record) pairs over a list of per-game analyses."""
    return [(a["win"], d) for a in analyses for d in a["deaths"]]


def _took_part(d, pid) -> bool:
    """Whether participant `pid` killed or assisted in death `d`."""
    people = ([d["killer"]] if d.get("killer") else []) + list(d.get("assisters") or [])
    return any(isinstance(p, dict) and p.get("pid") == pid for p in people)


def matchup_table(analyses, key_func, min_games=MIN_MATCHUP_GAMES, pid_func=None) -> list[dict]:
    """Games, deaths per game and winrate against each opponent key (rows with enough games).

    With `pid_func` (the opponent's participant id in each game) rows also count the player's
    deaths in which that opponent got the kill or an assist.
    """
    buckets = defaultdict(list)
    for a in analyses:
        key = key_func(a)
        if key:
            buckets[key].append(a)
    rows = []
    for key, games in buckets.items():
        if len(games) < min_games:
            continue
        deaths = sum(len(a["deaths"]) for a in games)
        wins = sum(1 for a in games if a["win"])
        row = {"key": key, "games": len(games), "wins": wins, "winrate": wins / len(games),
               "deaths": deaths, "deaths_pg": deaths / len(games)}
        if pid_func is not None:
            by_them = sum(1 for a in games for d in a["deaths"] if _took_part(d, pid_func(a)))
            row["by_them"], row["by_them_pg"] = by_them, by_them / len(games)
        rows.append(row)
    rows.sort(key=lambda r: (-r["games"], str(r["key"])))
    return rows


def _rival_table(analyses, role):
    """Matchup rows against the enemy in `role` ("jungler", "adc" or "support")."""
    champ, pid = f"enemy_{role}", f"enemy_{role}_pid"
    return matchup_table(
        analyses,
        lambda a: champion_display(a["context"].get(champ)) if a["context"].get(champ) else None,
        pid_func=lambda a: a["context"].get(pid))


def aggregate(analyses: list[dict]) -> dict:
    """Patterns over a player's games (each analysis from `analyze_game`), split wins vs losses."""
    items = deaths_of(analyses)
    champ = lambda d: d["kind"] == "campeon"  # noqa: E731
    zones = Counter((d["zone"], d["side"]) for _w, d in items if d["zone"])
    zone_rows = []
    for (zone, side), _n in zones.most_common():
        zone_rows.append({"zone": zone, "side": side, "label": place_label(zone, side),
                          **_split(items, lambda d, z=zone, s=side: d["zone"] == z and d["side"] == s)})
    objs = [(a["win"], o) for a in analyses for o in a["objectives_lost"] if o["kind"] in ("dragon", "baron")]
    known = lambda o: o["distance"] is not None  # noqa: E731
    objectives = {
        "total": len(objs),
        "dead": _split(objs, lambda o: o["dead"]),
        "far": _split(objs, lambda o: o["far"], known),
        "dead_or_far": _split(objs, lambda o: o["dead"] or o["far"], known),
        "died_recently": _split(objs, lambda o: o["died_recently"]),
    }
    wins = [a for a in analyses if a["win"]]
    losses = [a for a in analyses if not a["win"]]
    avg = lambda games: (sum(len(a["deaths"]) for a in games) / len(games)) if games else None  # noqa: E731
    return {
        "games": len(analyses), "wins": len(wins), "losses": len(losses), "deaths": len(items),
        "deaths_pg": {"all": avg(analyses), "win": avg(wins), "loss": avg(losses)},
        "jungler_early": _split(items, lambda d: d["enemy_jungler_involved"],
                                lambda d: d["early"] and d["enemy_jungler_involved"] is not None),
        "jungler_late": _split(items, lambda d: d["enemy_jungler_involved"],
                               lambda d: not d["early"] and d["enemy_jungler_involved"] is not None),
        "jungler_all": _split(items, lambda d: d["enemy_jungler_involved"],
                              lambda d: d["enemy_jungler_involved"] is not None),
        "enemies_1": _split(items, lambda d: d["enemies"] == 1, champ),
        "enemies_2": _split(items, lambda d: d["enemies"] == 2, champ),
        "enemies_3p": _split(items, lambda d: d["enemies"] >= 3, champ),
        "lower_level": _split(items, lambda d: d["level_diff"] < 0, lambda d: d["level_diff"] is not None),
        "unspent_1000": _split(items, lambda d: d["unspent_1000"], lambda d: d["unspent_1000"] is not None),
        "after_kill": _split(items, lambda d: d["after_own_kill"]),
        "enemy_side": _split(items, lambda d: d["side"] == "enemigo", lambda d: d["side"] is not None),
        "zones": zone_rows,
        "objectives": objectives,
        "junglers": _rival_table(analyses, "jungler"),
        "adcs": _rival_table(analyses, "adc"),
        "supports": _rival_table(analyses, "support"),
    }


def heat_points(analyses, phase="todas", result="todas") -> list[tuple[int, int]]:
    """Death positions, optionally only before/after minute 14 and only in wins/losses.

    Red-side games are mirrored so the player's own base is always bottom-left; otherwise
    "your side of the map" would be smeared across both halves.
    """
    pts = []
    for a in analyses:
        if result == "victorias" and not a["win"] or result == "derrotas" and a["win"]:
            continue
        red = a.get("team") == 200
        for d in a["deaths"]:
            if d["pos"] is None:
                continue
            if phase == "antes14" and not d["early"] or phase == "despues14" and d["early"]:
                continue
            x, y = d["pos"]
            pts.append((MAP_SIZE - x, MAP_SIZE - y) if red else (x, y))
    return pts


# --------------------------------------------------------------- improvement plan
def _count(a, pred):
    return sum(1 for d in a["deaths"] if pred(d))


# key, what is counted per game, goal text, goal (max allowed per game), value per game
PLAN_METRICS = (
    {"key": "after_kill", "label": "Muertes en el minuto siguiente a una kill tuya", "goal": 1,
     "goal_text": "Máximo 1 muerte en el minuto siguiente a una kill tuya",
     "value": lambda a: _count(a, lambda d: d["after_own_kill"])},
    {"key": "jungler_early", "label": "Muertes antes del min 14 con el jungla enemigo", "goal": 1,
     "goal_text": "Máximo 1 muerte antes del minuto 14 en la que participe el jungla enemigo",
     "value": lambda a: _count(a, lambda d: d["early"] and d["enemy_jungler_involved"])},
    {"key": "unspent", "label": "Muertes con 1000 de oro o más sin gastar", "goal": 0,
     "goal_text": "Ninguna muerte con 1000 de oro o más sin gastar (vuelve a base a comprar)",
     "value": lambda a: _count(a, lambda d: d["unspent_1000"])},
    {"key": "enemy_side", "label": "Muertes en el lado enemigo del mapa", "goal": 1,
     "goal_text": "Máximo 1 muerte en el lado enemigo del mapa",
     "value": lambda a: _count(a, lambda d: d["side"] == "enemigo")},
    {"key": "outnumbered", "label": "Muertes contra 3 o más rivales", "goal": 1,
     "goal_text": "Máximo 1 muerte contra 3 o más rivales",
     "value": lambda a: _count(a, lambda d: d["enemies"] >= 3)},
    {"key": "objectives", "label": "Dragones o barones enemigos con tu muerte o lejos de ti", "goal": 1,
     "goal_text": "Máximo 1 dragón o barón enemigo mientras estás muerto o lejos",
     "value": lambda a: sum(1 for o in a["objectives_lost"]
                            if o["kind"] in ("dragon", "baron") and (o["dead"] or o["far"]))},
    {"key": "early_deaths", "label": "Muertes antes del minuto 14", "goal": 1,
     "goal_text": "Máximo 1 muerte antes del minuto 14",
     "value": lambda a: _count(a, lambda d: d["early"])},
)
PLAN_MIN_GAMES = 8
PLAN_MIN_EFFECT = 0.1
PLAN_MIN_MISS = 0.2
PLAN_TOP = 3
PLAN_LAST = 10


def improvement_plan(analyses: list[dict], top_n=PLAN_TOP, last_n=PLAN_LAST) -> dict:
    """Top priorities from the candidates in PLAN_METRICS.

    For each candidate, per game: value = how many times it happened and "met" = value <= goal.
    score = effect * miss, where effect = (mean value in losses - mean in wins) / pooled std (how
    much it separates losses from wins; must be > 0.1) and miss = share of games that did not meet
    the goal (must be >= 20%). The `top_n` best scores win. `analyses` must be oldest first; each
    priority also reports met / not met for the last `last_n` games.
    """
    games = list(analyses)
    wins = [a for a in games if a["win"]]
    losses = [a for a in games if not a["win"]]
    if len(games) < PLAN_MIN_GAMES or len(wins) < 2 or len(losses) < 2:
        return {"enough": False, "games": len(games), "priorities": [], "min_games": PLAN_MIN_GAMES}
    ranked = []
    for spec in PLAN_METRICS:
        values = [spec["value"](a) for a in games]
        effect = effect_size([spec["value"](a) for a in losses], [spec["value"](a) for a in wins])
        met = [v <= spec["goal"] for v in values]
        miss = 1 - sum(met) / len(games)
        if effect is None or effect <= PLAN_MIN_EFFECT or miss < PLAN_MIN_MISS:
            continue
        met_wins = [a for a, ok in zip(games, met) if ok]
        not_met = [a for a, ok in zip(games, met) if not ok]
        recent = games[-last_n:]
        ranked.append({
            "key": spec["key"], "label": spec["label"], "goal_text": spec["goal_text"],
            "goal": spec["goal"], "effect": effect, "miss": miss, "score": effect * miss,
            "avg": sum(values) / len(values),
            "met_rate": _rate(sum(met), len(games)),
            "winrate_met": (sum(1 for a in met_wins if a["win"]) / len(met_wins)) if met_wins else None,
            "winrate_not_met": (sum(1 for a in not_met if a["win"]) / len(not_met)) if not_met else None,
            "last": [{"match_id": a["match_id"], "ok": spec["value"](a) <= spec["goal"],
                      "value": spec["value"](a), "win": a["win"], "champion": a["champion"]}
                     for a in recent],
        })
    ranked.sort(key=lambda r: -r["score"])
    return {"enough": True, "games": len(games), "priorities": ranked[:top_n], "min_games": PLAN_MIN_GAMES}
