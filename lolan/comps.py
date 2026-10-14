"""Team compositions: what the clan plays and which ADC to pick for a given lineup.

Everything here is a pure function over "records": one dict per analyzable Summoner's Rift
game of the MAIN player (see `build_records`). Nothing touches the network or the database.

The ADC framework comes from a coaching video by a Challenger support:
  * follow-up ADCs need engage on their team (bad first picks);
  * kiting ADCs shine when chased and want peel/enchanter supports;
  * flexible ADCs work with any support and are safe first picks.
Champions that the video names carry `source == "video"`; the ones added by us carry "propia".
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .analyze import mean, normalize_name, role_name
from .metrics import extract_game_metrics, find_participant
from .settings import DEFAULT_CLAN_PREFIX

# --------------------------------------------------------------------- constants
MIN_GAMES = 3                   # rows below this are shown greyed out / labelled "muestra pequeña"
PRIOR = 5                       # shrunk winrate = (wins + PRIOR) / (games + 2 * PRIOR)
TANKY_ARMOR = 200               # total armor that approximates ">100 bonus armor" (base is ~100 late)
MIN_PEN_DURATION_MIN = 20       # shorter games never reach a meaningful item state
CLEANSE_ID = 1                  # summoner spell id of Cleanse (Purificar)
RANDOM = "Aleatorio"            # label of a support who is not part of the clan
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
ENGAGE_BUCKETS = ("0", "1", "2", "3+")

ADC_LABELS = {"followup": "Follow-up", "kiting": "Kiting", "flexible": "Flexible", "otro": "Sin clasificar"}
SUPPORT_LABELS = {"engage": "Engage", "peel": "Peel/enchanter", "mixto": "Engage + peel",
                  "poke": "Poke/mago", "otro": "Sin clasificar"}
ADC_ORDER = ("followup", "kiting", "flexible", "otro")
SUPPORT_ORDER = ("engage", "peel", "mixto", "poke", "otro")


# ---------------------------------------------------------- classification tables
def _build(groups: dict) -> dict:
    """{type: {source: [champions]}} -> {champion: {"type": type, "source": source}}."""
    table = {}
    for kind, by_source in groups.items():
        for source, champions in by_source.items():
            for champion in champions:
                table[champion] = {"type": kind, "source": source}
    return table


# Keys are Data Dragon championName values (KSante, MonkeyKing, Kaisa, JarvanIV...).
ADC_CLASS = _build({
    "followup": {"video": ["Kaisa", "Twitch", "Tristana", "Samira", "Lucian", "Draven"],
                 "propia": ["MissFortune", "Kalista", "Nilah"]},
    "kiting": {"video": ["Vayne", "Zeri", "Ezreal", "Sivir"],
               "propia": ["Xayah"]},
    "flexible": {"video": ["Ashe", "Jinx", "Aphelios", "Senna", "Varus", "Caitlyn", "KogMaw"],
                 "propia": ["Jhin", "Yunara", "Smolder", "Corki", "Mel"]},
})

# The video only names Taric, Tahm Kench and Braum (as peel); the rest is our own classification.
SUPPORT_CLASS = _build({
    "engage": {"propia": ["Leona", "Nautilus", "Alistar", "Rell", "Rakan", "Blitzcrank", "Pyke",
                          "Maokai", "Poppy"]},
    "peel": {"video": ["Taric", "TahmKench", "Braum"],
             "propia": ["Lulu", "Janna", "Milio", "Nami", "Soraka", "Sona", "Yuumi", "Karma",
                        "Renata", "Seraphine"]},
    "mixto": {"propia": ["Thresh"]},
    "poke": {"propia": ["Lux", "Morgana", "Zyra", "Brand", "Xerath", "Velkoz", "Hwei", "Swain",
                        "Senna", "Neeko", "Zilean"]},
})

# Clear hard engage in ANY role (used to count how much engage a team has). Deliberately short:
# divers and champions whose engage is only situational (Camille, Yone, Kled, Briar, Ambessa...)
# are left out. Supports classified as "engage" (Pyke) count too, see `engage_count`.
HARD_ENGAGE = frozenset({
    "Malphite", "JarvanIV", "Sejuani", "Zac", "Ornn", "Amumu", "Rell", "Leona", "Nautilus",
    "Alistar", "Rakan", "MonkeyKing", "Vi", "Diana", "Sion", "Kennen", "Galio", "Gragas",
    "Maokai", "Rammus", "Nocturne", "Hecarim", "Skarner", "Thresh", "Blitzcrank", "Poppy",
    "KSante", "Sett",
})

# What a user may type -> Data Dragon name (normalized with `normalize_name`).
CHAMPION_ALIASES = {"wukong": "MonkeyKing", "renataglasc": "Renata", "nunuywillump": "Nunu",
                    "kogmaw": "KogMaw", "velkoz": "Velkoz", "ksante": "KSante", "jarvaniv": "JarvanIV",
                    "tahmkench": "TahmKench", "missfortune": "MissFortune"}

_CANONICAL = {normalize_name(c): c for c in (*ADC_CLASS, *SUPPORT_CLASS, *HARD_ENGAGE)}
_CANONICAL.update({k: v for k, v in CHAMPION_ALIASES.items()})


def canonical_champion(text, known=()) -> str:
    """Data Dragon name for something typed by hand ("kai'sa" -> "Kaisa"); the text itself if unknown."""
    text = str(text or "").strip()
    key = normalize_name(text)
    if not key:
        return ""
    if key in _CANONICAL:
        return _CANONICAL[key]
    for name in known:
        if normalize_name(name) == key:
            return name
    return text


def _lookup(table, champion):
    return table.get(canonical_champion(champion))


def adc_archetype(champion) -> str:
    """followup / kiting / flexible, or "otro" when the champion is not classified."""
    entry = _lookup(ADC_CLASS, champion)
    return entry["type"] if entry else "otro"


def adc_source(champion) -> str | None:
    """"video" or "propia" for a classified ADC, None otherwise."""
    entry = _lookup(ADC_CLASS, champion)
    return entry["source"] if entry else None


def support_archetype(champion) -> str:
    """engage / peel / mixto / poke, or "otro" when the champion is not classified."""
    entry = _lookup(SUPPORT_CLASS, champion)
    return entry["type"] if entry else "otro"


def support_source(champion) -> str | None:
    entry = _lookup(SUPPORT_CLASS, champion)
    return entry["source"] if entry else None


def is_hard_engage(champion, role: str | None = None) -> bool:
    """A hard-engage champion, or a support classified as "engage" (Pyke) when played as UTILITY."""
    name = canonical_champion(champion)
    if name in HARD_ENGAGE:
        return True
    return role == "UTILITY" and support_archetype(name) == "engage"


def engage_count(members) -> int:
    """How many of the team's champions are hard engage. `members`: dicts with champion/role."""
    return sum(1 for m in members if is_hard_engage(m.get("champion"), m.get("role")))


def engage_bucket(count: int) -> str:
    return "3+" if count >= 3 else str(count)


# ------------------------------------------------------------------- statistics
def shrunk_wr(wins: int, games: int, prior: int = PRIOR) -> float:
    """Winrate pulled towards 50% by `prior` virtual wins and losses: (wins + 5) / (games + 10)."""
    return (wins + prior) / (games + 2 * prior)


def _stat(recs, min_games=MIN_GAMES) -> dict:
    n = len(recs)
    wins = sum(1 for r in recs if r["win"])
    return {
        "games": n, "wins": wins, "losses": n - wins,
        "winrate": wins / n if n else None,
        "shrunk": shrunk_wr(wins, n),
        "small": n < min_games,
        "gold_diff_14": mean(r.get("gold_diff_14") for r in recs),
        "deaths": mean(r.get("deaths") for r in recs),
    }


def _grouped(recs, keyfunc, min_games=MIN_GAMES) -> list[dict]:
    """Rows {key, ...stats} ranked by shrunk winrate; small samples go last."""
    buckets = defaultdict(list)
    for r in recs:
        key = keyfunc(r)
        if key is not None:
            buckets[key].append(r)
    rows = [{"key": k, **_stat(v, min_games)} for k, v in buckets.items()]
    rows.sort(key=lambda x: (x["small"], -x["shrunk"], -x["games"], str(x["key"])))
    return rows


# --------------------------------------------------------------------- clan config
def parse_aliases(text, strict: bool = False) -> dict[str, str]:
    """"Cuenta Alterna=Jugador Principal;Otro=Principal" -> {"cuenta alterna": "Jugador Principal", ...}.

    Keys are case-folded game names; values keep their spelling (they are the display name).
    Lenient by default (bad items are skipped); with strict=True a ValueError says what is wrong.
    """
    result: dict[str, str] = {}
    for item in re.split(r"[;\n]", str(text or "")):
        item = item.strip()
        if not item:
            continue
        name, sep, target = item.partition("=")
        name, target = name.strip(), target.strip()
        if not sep or not name or not target:
            if strict:
                raise ValueError(
                    f"Alias mal escrito: «{item}». Usa el formato Cuenta=Persona, separados por "
                    "punto y coma (por ejemplo: Cuenta Alterna=Jugador Principal).")
            continue
        result[name.casefold()] = target
    return result


@dataclass
class ClanConfig:
    """Who counts as clan: game names starting with `prefix`, or any tracked account."""
    prefix: str = DEFAULT_CLAN_PREFIX
    aliases: dict = field(default_factory=dict)       # casefolded game name -> person label
    tracked: dict = field(default_factory=dict)       # puuid -> "Nombre#TAG" of registered players

    @classmethod
    def build(cls, prefix: str = "", aliases_text: str = "", tracked=None) -> "ClanConfig":
        return cls(prefix=(prefix or "").strip() or DEFAULT_CLAN_PREFIX,
                   aliases=parse_aliases(aliases_text), tracked=dict(tracked or {}))

    def matches_prefix(self, name) -> bool:
        return bool(name) and str(name).casefold().startswith(self.prefix.casefold())

    def person(self, name: str) -> str:
        """Display label of a game name after following the aliases (loops are cut)."""
        label, seen = name, set()
        while label.casefold() in self.aliases and label.casefold() not in seen:
            seen.add(label.casefold())
            label = self.aliases[label.casefold()]
        return label


# ------------------------------------------------------------------ record building
def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _member(p: dict, main_puuid: str) -> dict:
    return {
        "puuid": p.get("puuid") or "",
        "name": (p.get("riotIdGameName") or "").strip(),
        "champion": p.get("championName") or "",
        "role": p.get("teamPosition") or "",
        "is_main": p.get("puuid") == main_puuid,
        "person": None,
    }


def _penetration(timeline, main_pid, enemy_pids, duration_min):
    """Armor of the enemies and armor penetration of the main player at the timeline's last frame."""
    if duration_min is None or duration_min < MIN_PEN_DURATION_MIN or not timeline:
        return None
    frames = ((timeline.get("info") or {}).get("frames")) or []
    if not frames:
        return None
    pfs = frames[-1].get("participantFrames") or {}

    def stats(pid):
        pf = pfs.get(str(pid))
        if pf is None:
            pf = pfs.get(pid)
        return (pf or {}).get("championStats") if isinstance(pf, dict) else None

    armors = [_num((stats(pid) or {}).get("armor")) for pid in enemy_pids]
    armors = [a for a in armors if a is not None]
    mine = stats(main_pid)
    if not armors or not isinstance(mine, dict):
        return None
    pct = max(_num(mine.get("armorPenPercent")) or 0, _num(mine.get("bonusArmorPenPercent")) or 0)
    return {"max_armor": max(armors), "tanky": max(armors) >= TANKY_ARMOR,
            "pen_pct": pct, "lethality": _num(mine.get("armorPen")) or 0}


def build_records(matches, main_puuid: str, queue: int | None = None) -> list[dict]:
    """One record per analyzable game of the main player, oldest first.

    `matches` yields (match_id, match_json, timeline_json_or_None), e.g. `Store.iter_matches(puuid)`.
    Remakes and queues outside the Summoner's Rift list are skipped, like everywhere else.
    Members' `person` is filled in later by `resolve_people`.
    """
    records = []
    for _match_id, match, timeline in matches:
        row = extract_game_metrics(match, timeline, main_puuid)
        if row is None or row["remake"] or not row["analyzable"]:
            continue
        if queue is not None and row["queue_id"] != queue:
            continue
        participants = (match.get("info") or {}).get("participants") or []
        me = find_participant(participants, main_puuid)
        team_id = me.get("teamId")
        team = [_member(p, main_puuid) for p in participants if p.get("teamId") == team_id]
        enemies = [p for p in participants if p.get("teamId") != team_id]
        cc = [_num(p.get("timeCCingOthers")) for p in enemies]
        cc = [v for v in cc if v is not None]
        records.append({
            "match_id": row["match_id"], "queue_id": row["queue_id"], "date": row["date"],
            "game_creation": row["game_creation"], "duration_min": row["duration_min"],
            "win": row["win"], "gold_diff_14": row.get("gold_diff_14"), "deaths": row["deaths"],
            "main_champion": me.get("championName") or "", "main_role": me.get("teamPosition") or "",
            "team": team,
            "enemy_champions": [p.get("championName") or "" for p in enemies],
            "enemy_cc": sum(cc) if cc else None,
            "cleanse": CLEANSE_ID in (me.get("summoner1Id"), me.get("summoner2Id")),
            "pen": _penetration(timeline, me.get("participantId"),
                                [p.get("participantId") for p in enemies], row["duration_min"]),
        })
    records.sort(key=lambda r: (r["game_creation"] or 0, str(r["match_id"])))
    return records


def resolve_people(records, clan: ClanConfig) -> list[dict]:
    """Set `person` on every clan member (None for everybody else); returns `records`.

    A puuid is clan if it is tracked or any of its game names starts with the prefix. It is
    displayed with its most recent game name, after following the aliases, so two accounts
    aliased to each other become one person.
    """
    latest, any_clan = {}, set()
    for r in sorted(records, key=lambda r: r["game_creation"] or 0):
        for m in r["team"]:
            if m["name"]:
                latest[m["puuid"]] = m["name"]
                if clan.matches_prefix(m["name"]):
                    any_clan.add(m["puuid"])
    label = {}
    for puuid in {m["puuid"] for r in records for m in r["team"]}:
        tracked = puuid in clan.tracked
        if not tracked and puuid not in any_clan:
            continue
        name = latest.get(puuid) or clan.tracked.get(puuid, "").split("#")[0]
        if name:
            label[puuid] = clan.person(name)
    for r in records:
        for m in r["team"]:
            m["person"] = label.get(m["puuid"])
    return records


def filter_queue(records, queue: int | None) -> list[dict]:
    return [r for r in records if queue is None or r["queue_id"] == queue]


# ----------------------------------------------------------------- record helpers
def main_person(records) -> str | None:
    for r in records:
        for m in r["team"]:
            if m["is_main"]:
                return m["person"]
    return None


def _main(r) -> dict:
    return next(m for m in r["team"] if m["is_main"])


def _support(r) -> dict | None:
    """The teammate playing UTILITY (only meaningful when the main player is the ADC)."""
    return next((m for m in r["team"] if m["role"] == "UTILITY" and not m["is_main"]), None)


def _bottom_games(records):
    return [r for r in records if r["main_role"] == "BOTTOM"]


def _clan_members(r, include_main=True):
    return [m for m in r["team"] if m["person"] and (include_main or not m["is_main"])]


# ------------------------------------------------------------------------ analyses
def clan_pools(records, min_games=MIN_GAMES, top_champions=3) -> list[dict]:
    """a. Per clan person: games seen with the main player, winrate, roles, champions per role."""
    seen = defaultdict(list)
    for r in records:
        for m in r["team"]:
            if m["person"]:
                seen[m["person"]].append((r, m))
    rows = []
    for person, items in seen.items():
        row = {"person": person, "is_main": any(m["is_main"] for _r, m in items),
               **_stat([r for r, _m in items], min_games), "roles": []}
        roles = Counter(m["role"] for _r, m in items)
        for role, n in sorted(roles.items(), key=lambda kv: (-kv[1], kv[0])):
            in_role = [(r, m) for r, m in items if m["role"] == role]
            by_champ = defaultdict(list)
            for r, m in in_role:
                by_champ[m["champion"]].append(r)
            champs = [{"champion": c, **_stat(rs, min_games)} for c, rs in by_champ.items()]
            champs.sort(key=lambda x: (-x["games"], -x["shrunk"], x["champion"]))
            row["roles"].append({
                "role": role, "label": role_name(role), "games": n, "share": n / len(items),
                "champions": champs[:top_champions],
            })
        rows.append(row)
    rows.sort(key=lambda x: (-x["games"], x["person"]))
    return rows


def duo_bot(records, min_games=MIN_GAMES) -> dict:
    """b. Main as ADC: winrate by support person, by support champion and by support archetype."""
    games, no_support = [], 0
    for r in _bottom_games(records):
        if _support(r) is None:
            no_support += 1
        else:
            games.append(r)
    by_champion = _grouped(games, lambda r: _support(r)["champion"] or None, min_games)
    for row in by_champion:
        row["archetype"] = support_archetype(row["key"])
        row["source"] = support_source(row["key"])
    return {
        "games": len(games), "no_support": no_support,
        "by_person": _grouped(games, lambda r: _support(r)["person"] or RANDOM, min_games),
        "by_champion": by_champion,
        "by_archetype": _grouped(games, lambda r: support_archetype(_support(r)["champion"]),
                                 min_games),
    }


def adc_support_matrix(records, min_games=MIN_GAMES) -> dict:
    """c. Archetype of the main's ADC x archetype of the support: games and winrate per cell."""
    cells = defaultdict(list)
    for r in _bottom_games(records):
        sup = _support(r)
        if sup is not None:
            cells[(adc_archetype(r["main_champion"]), support_archetype(sup["champion"]))].append(r)
    rows = []
    for adc in ADC_ORDER:
        row_cells = [{"support": s, **_stat(cells[(adc, s)], min_games)} if cells.get((adc, s))
                     else None for s in SUPPORT_ORDER]
        rows.append({"adc": adc, "games": sum(c["games"] for c in row_cells if c), "cells": row_cells})
    return {"adc_types": ADC_ORDER, "support_types": SUPPORT_ORDER, "rows": rows,
            "games": sum(len(v) for v in cells.values())}


def adc_engage_table(records, min_games=MIN_GAMES) -> dict:
    """d. Archetype of the main's ADC x number of hard-engage champions on the main's team."""
    cells = defaultdict(list)
    for r in _bottom_games(records):
        others = [m for m in r["team"] if not m["is_main"]]
        cells[(adc_archetype(r["main_champion"]), engage_bucket(engage_count(others)))].append(r)
    rows = []
    for adc in ADC_ORDER:
        row_cells = [{"bucket": b, **_stat(cells[(adc, b)], min_games)} if cells.get((adc, b))
                     else None for b in ENGAGE_BUCKETS]
        rows.append({"adc": adc, "games": sum(c["games"] for c in row_cells if c), "cells": row_cells})
    return {"buckets": ENGAGE_BUCKETS, "rows": rows, "games": sum(len(v) for v in cells.values())}


def lineup_of(r) -> tuple:
    """Clan part of a game's team as a sorted tuple of (person, role)."""
    return tuple(sorted((m["person"], m["role"]) for m in _clan_members(r)))


def clan_lineups(records, min_members=3, min_games=MIN_GAMES, limit=8) -> list[dict]:
    """e1. Most frequent clan lineups (person -> role) in games with `min_members`+ clan members."""
    buckets = defaultdict(list)
    for r in records:
        if len(_clan_members(r)) >= min_members:
            buckets[lineup_of(r)].append(r)
    rows = [{"lineup": list(k), **_stat(v, min_games)} for k, v in buckets.items()]
    rows.sort(key=lambda x: (-x["games"], -x["shrunk"], str(x["lineup"])))
    for row in rows:
        row["lineup"].sort(key=lambda pr: (ROLES.index(pr[1]) if pr[1] in ROLES else 9, pr[0]))
    return rows[:limit]


def most_common_lineup(records) -> dict[str, str]:
    """role -> person of the most frequent clan lineup, to pre-fill the draft form.

    It looks at the games where the main player was the ADC (the draft recommends ADCs) and,
    only if there are none, at all games. 3+ clan members first, else 2+; {} if there is none.
    """
    for pool in (_bottom_games(records), records):
        for minimum in (3, 2):
            rows = clan_lineups(pool, min_members=minimum, limit=1)
            if rows:
                return {role: person for person, role in rows[0]["lineup"] if role}
    return {}


def clan_pairs(records, min_games=MIN_GAMES, top=5) -> dict:
    """e2. Pairs (person+champion, person+champion) that shared a team, best and worst by shrunk WR."""
    buckets = defaultdict(list)
    for r in records:
        members = sorted((m["person"], m["champion"]) for m in _clan_members(r))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                buckets[(a, b)].append(r)
    rows = [{"a": k[0], "b": k[1], **_stat(v, min_games)} for k, v in buckets.items()
            if len(v) >= min_games]
    rows.sort(key=lambda x: (-x["shrunk"], -x["games"], str(x["a"]), str(x["b"])))
    best = rows[:top]
    worst = [x for x in reversed(rows) if x not in best][:top]
    return {"best": best, "worst": worst, "total": len(rows)}


def penetration_table(records, min_games=MIN_GAMES) -> dict:
    """f. 2x2: an enemy with >= TANKY_ARMOR total armor (yes/no) x main had % armor pen (yes/no)."""
    cells = defaultdict(list)
    eligible = [r for r in records if r.get("pen")]
    for r in eligible:
        cells[(r["pen"]["tanky"], r["pen"]["pen_pct"] > 0)].append(r)
    rows = []
    for tanky in (True, False):
        rows.append({
            "tanky": tanky,
            "with_pen": {**_stat(cells.get((tanky, True), []), min_games)},
            "without_pen": {**_stat(cells.get((tanky, False), []), min_games)},
        })
    only_lethality = sum(1 for r in eligible if r["pen"]["tanky"] and r["pen"]["pen_pct"] <= 0
                         and r["pen"]["lethality"] > 0)
    return {"rows": rows, "eligible": len(eligible), "excluded": len(records) - len(eligible),
            "tanky_games": sum(1 for r in eligible if r["pen"]["tanky"]),
            "tanky_only_lethality": only_lethality}


def cleanse_terciles(records, min_games=MIN_GAMES) -> dict:
    """g. Enemy team's total CC time split into terciles over the main's games; WR with/without Cleanse."""
    usable = [r for r in records if r.get("enemy_cc") is not None]
    out = {"games": len(usable), "buckets": [], "cleanse_games": sum(1 for r in usable if r["cleanse"])}
    if len(usable) < 3:
        return out
    values = sorted(r["enemy_cc"] for r in usable)
    n = len(values)
    q1 = values[math.ceil(n / 3) - 1]
    q2 = values[math.ceil(2 * n / 3) - 1]

    def tercile(v):
        return 0 if v <= q1 else 1 if v <= q2 else 2

    names = ("CC bajo", "CC medio", "CC alto")
    for idx, name in enumerate(names):
        recs = [r for r in usable if tercile(r["enemy_cc"]) == idx]
        if not recs:
            continue
        out["buckets"].append({
            "name": name, "min": min(r["enemy_cc"] for r in recs),
            "max": max(r["enemy_cc"] for r in recs), "games": len(recs),
            "with_cleanse": _stat([r for r in recs if r["cleanse"]], min_games),
            "without_cleanse": _stat([r for r in recs if not r["cleanse"]], min_games),
        })
    return out


def analyze_all(records, min_games=MIN_GAMES) -> dict:
    """Every table of the "Composiciones" page / report, for the given records."""
    return {
        "games": len(records),
        "bottom_games": len(_bottom_games(records)),
        "clan_games": sum(1 for r in records if _clan_members(r, include_main=False)),
        "overall": _stat(records, min_games),
        "pools": clan_pools(records, min_games),
        "duo": duo_bot(records, min_games),
        "matrix": adc_support_matrix(records, min_games),
        "engage": adc_engage_table(records, min_games),
        "lineups": clan_lineups(records, min_games=min_games),
        "pairs": clan_pairs(records, min_games),
        "penetration": penetration_table(records, min_games),
        "cleanse": cleanse_terciles(records, min_games),
        "common_lineup": most_common_lineup(records),
        "min_games": min_games,
    }


# ------------------------------------------------------------------- recommender
# How well an ADC archetype fits a support archetype, from -1 (bad) to +1 (great).
FIT = {
    "followup": {"engage": 1.0, "mixto": 0.5, "peel": -0.4, "poke": -0.5, "otro": 0.0},
    "kiting": {"engage": -0.2, "mixto": 0.5, "peel": 1.0, "poke": 0.3, "otro": 0.0},
    "flexible": {s: 0.4 for s in SUPPORT_ORDER},
    "otro": {s: 0.0 for s in SUPPORT_ORDER},
}
# No idea who the support will be: follow-ups are bad first picks, flexible ones are safe.
FIT_UNKNOWN_SUPPORT = {"followup": -0.6, "kiting": 0.0, "flexible": 0.6, "otro": 0.0}
# Extra fit from the hard-engage champions of the rest of the team (0, 1, 2, 3+).
ENGAGE_FIT = {
    "followup": {"0": -0.3, "1": 0.1, "2": 0.4, "3+": 0.5},
    "kiting": {"0": 0.0, "1": 0.0, "2": -0.2, "3+": -0.4},
}
# Weights of the final score (a weighted average of values in [-1, 1]):
#   rule fit   weight 1 (constant)
#   champion   weight 2n/(n+10) with n = the main's games with that ADC  (0 -> 0, 10 -> 1, 30 -> 1.5)
#   duo        weight 2n/(n+6)  with n = games with that support person + that ADC, only if n >= 3
# So with little data the framework rules dominate; with plenty of games the results do.
RULE_WEIGHT = 1.0
CHAMPION_K = 10
DUO_K = 6
WR_SCALE = 4        # a shrunk winrate of 62.5% (or 37.5%) maps to +1 (-1)


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _wr_signal(shrunk):
    return _clamp((shrunk - 0.5) * WR_SCALE)


def normalize_lineup(lineup) -> dict:
    """{role: {"person", "champion"}} from role -> (person, champion) | {person, champion} | None."""
    out = {}
    for role in ROLES:
        value = (lineup or {}).get(role)
        if isinstance(value, dict):
            person, champion = value.get("person"), value.get("champion")
        elif isinstance(value, (list, tuple)):
            person, champion = (list(value) + [None, None])[:2]
        else:
            person, champion = None, None
        person = str(person).strip() if person else None
        out[role] = {"person": None if person in (None, "", RANDOM) else person,
                     "champion": canonical_champion(champion) or None}
    return out


def support_profile(records, lineup) -> dict:
    """What we assume about the support slot: its champion, or the person's history, or nothing."""
    sup = normalize_lineup(lineup)["UTILITY"]
    if sup["champion"]:
        arch = support_archetype(sup["champion"])
        return {"source": "champion", "person": sup["person"], "dist": {arch: 1.0},
                "champions": [(sup["champion"], arch)]}
    if sup["person"]:
        hist = Counter(m["champion"] for r in records for m in r["team"]
                       if m["role"] == "UTILITY" and m["person"] == sup["person"] and m["champion"])
        if hist:
            total = sum(hist.values())
            dist = Counter()
            for champion, n in hist.items():
                dist[support_archetype(champion)] += n / total
            top = [(c, support_archetype(c)) for c, _n in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
            return {"source": "historial", "person": sup["person"], "dist": dict(dist), "champions": top}
    return {"source": "desconocido", "person": sup["person"], "dist": None, "champions": []}


def _lineup_engage(lineup) -> int | None:
    """Engage count of the teammates already chosen; None when no champion was entered."""
    members = [{"champion": v["champion"], "role": role} for role, v in lineup.items()
               if role != "BOTTOM" and v["champion"]]
    return engage_count(members) if members else None


def _rule_parts(adc_type, profile, engage) -> tuple[float, float]:
    """(archetype fit, engage fit) of an ADC type for the assumed support and team engage."""
    if profile["dist"] is None:
        arch = FIT_UNKNOWN_SUPPORT[adc_type]
    else:
        arch = sum(p * FIT[adc_type][a] for a, p in profile["dist"].items())
    eng = 0.0
    if engage is not None and adc_type in ENGAGE_FIT:
        eng = ENGAGE_FIT[adc_type][engage_bucket(engage)]
    return arch, eng


_GOOD_FOR = {
    "engage": "los ADC follow-up y flexibles rinden mejor",
    "peel": "los ADC de kiting y flexibles rinden mejor",
    "mixto": "sirve tanto a los ADC follow-up como a los de kiting",
    "poke": "pide un ADC flexible que aguante la línea",
    "otro": "no hay una regla clara para ese soporte",
}


def _support_phrase(profile) -> str:
    who = profile["person"]
    champs = profile["champions"]
    if profile["source"] == "champion":
        c, a = champs[0]
        return f"{who + ' con ' if who else ''}{c} ({SUPPORT_LABELS[a].lower()})"
    parts = " y ".join(f"{c} ({SUPPORT_LABELS[a].lower()})" for c, a in champs)
    return f"{who} suele jugar {parts}"


def _arch_reason(champion, adc_type, profile, arch_fit) -> str | None:
    label = ADC_LABELS[adc_type].lower()
    if adc_type == "otro":
        return None
    if adc_type == "flexible":
        return f"{champion} es flexible: funciona con cualquier soporte y es un first pick seguro."
    if profile["dist"] is None:
        if adc_type == "followup":
            return (f"{champion} es follow-up: sin saber el soporte es un mal first pick, "
                    "mejor guárdalo para cuando haya engage.")
        return f"{champion} es de kiting: brilla si lo persiguen, pero necesita un soporte de peel."
    dominant = max(profile["dist"], key=lambda a: profile["dist"][a])
    phrase = _support_phrase(profile)
    if arch_fit >= 0.3:
        return f"{phrase}: {_GOOD_FOR[dominant]} ({champion} es {label})."
    if arch_fit <= -0.2:
        return f"{phrase}: {champion} es {label} y encaja peor con ese soporte."
    return f"{phrase}: encaje neutro para {champion} ({label})."


def _engage_reason(champion, adc_type, engage) -> str | None:
    if engage is None or adc_type not in ENGAGE_FIT:
        return None
    n = engage_bucket(engage)
    plural = "campeón de engage duro" if engage == 1 else "campeones de engage duro"
    if adc_type == "followup":
        if engage >= 2:
            return f"Tu equipo ya tiene {engage} {plural}: un follow-up como {champion} tiene a quién seguir."
        if engage == 0:
            return f"Tu equipo no tiene engage duro y {champion} es follow-up: le faltaría a quién seguir."
        return None
    if engage >= 2:
        return f"Tu equipo ya tiene {n} {plural}: {champion} (kiting) pierde valor con un equipo de engage puro."
    return None


def draft_notes(lineup, records, enemy=()) -> list[str]:
    """Context lines for the draft form: what was assumed for the support, team engage, enemy CC."""
    lu = normalize_lineup(lineup)
    profile = support_profile(records, lu)
    notes = []
    if profile["source"] == "champion":
        notes.append(f"Soporte: {_support_phrase(profile)}.")
    elif profile["source"] == "historial":
        notes.append(f"Soporte: {_support_phrase(profile)} (según sus partidas contigo).")
    else:
        notes.append("Soporte desconocido: se favorecen los ADC flexibles y los follow-up se "
                     "consideran malos first picks.")
    engage = _lineup_engage(lu)
    if engage is not None:
        notes.append(f"Engage duro en tu equipo (sin contarte): {engage}.")
    enemies = [canonical_champion(e) for e in enemy if str(e or "").strip()]
    foes = [e for e in enemies if is_hard_engage(e)]
    if len(foes) >= 3:
        notes.append(f"El rival tiene mucho engage/CC ({', '.join(foes)}): considera llevar Purificar.")
    return notes


def recommend(lineup, records, enemy=(), include_all=False, min_games=MIN_GAMES,
              max_reasons=3) -> list[dict]:
    """Ranked ADC picks for the main player given the rest of the lineup.

    `lineup`: role -> (person, champion) for the main's team (the BOTTOM entry is ignored: it is
    the main player). `enemy`: optional enemy champion names (not used for the score, see
    `draft_notes`). The pool is the champions the main played as ADC at least `min_games` times;
    `include_all` adds every classified ADC. Returns dicts with champion, score (0-100), reasons.
    """
    del enemy  # accepted for the API; the video framework has no champion-level enemy rule
    lu = normalize_lineup(lineup)
    profile = support_profile(records, lu)
    engage = _lineup_engage(lu)
    sup_person = lu["UTILITY"]["person"]

    bottom = _bottom_games(records)
    by_champion = defaultdict(list)
    for r in bottom:
        by_champion[r["main_champion"]].append(r)
    candidates = {c for c, rs in by_champion.items() if len(rs) >= min_games}
    if include_all:
        candidates |= set(ADC_CLASS) | set(by_champion)

    ranking = []
    for champion in candidates:
        recs = by_champion.get(champion, [])
        st = _stat(recs, min_games)
        adc_type = adc_archetype(champion)
        arch_fit, engage_fit = _rule_parts(adc_type, profile, engage)
        rule = _clamp(arch_fit + engage_fit)
        n = st["games"]
        w_champ = 2 * n / (n + CHAMPION_K)
        s_champ = _wr_signal(st["shrunk"]) if n else 0.0

        duo_recs = [r for r in recs if sup_person and (_support(r) or {}).get("person") == sup_person]
        duo = _stat(duo_recs, min_games) if duo_recs else None
        w_duo = 2 * duo["games"] / (duo["games"] + DUO_K) if duo and duo["games"] >= min_games else 0.0
        s_duo = _wr_signal(duo["shrunk"]) if w_duo else 0.0

        total_w = RULE_WEIGHT + w_champ + w_duo
        score = (RULE_WEIGHT * rule + w_champ * s_champ + w_duo * s_duo) / total_w

        cands = []        # (magnitude, text): the strongest 1-3 are shown
        text = _arch_reason(champion, adc_type, profile, arch_fit)
        if text:
            cands.append((abs(arch_fit) * RULE_WEIGHT + 0.05, text))
        text = _engage_reason(champion, adc_type, engage)
        if text:
            cands.append((abs(engage_fit) * RULE_WEIGHT, text))
        if w_duo:
            cands.append((abs(s_duo) * w_duo + 0.1,
                          f"Con {champion} y {sup_person} de soporte: {duo['wins']}-{duo['losses']} "
                          f"({duo['winrate'] * 100:.0f}%)."))
        if n >= min_games:
            cands.append((abs(s_champ) * w_champ + 0.1,
                          f"Tu historial de ADC con {champion}: {st['wins']}-{st['losses']} "
                          f"({st['winrate'] * 100:.0f}%, ajustado {st['shrunk'] * 100:.0f}%)."))
        elif n:
            cands.append((0.0, f"Solo {n} partida{'s' if n != 1 else ''} tuyas con {champion}: muestra pequeña."))
        else:
            cands.append((0.0, "No lo has jugado de ADC: se recomienda solo por el encaje del equipo."))
        cands.sort(key=lambda c: -c[0])
        reasons = [t for _m, t in cands[:max_reasons]]

        ranking.append({
            "champion": champion, "score": round((score + 1) * 50), "raw_score": score,
            "reasons": reasons, "adc_type": adc_type, "adc_source": adc_source(champion),
            "games": n, "wins": st["wins"], "winrate": st["winrate"], "shrunk": st["shrunk"],
            "duo_games": duo["games"] if duo else 0,
            "in_pool": n >= min_games,
        })
    ranking.sort(key=lambda x: (-x["raw_score"], -x["shrunk"], x["champion"]))
    return ranking


# ------------------------------------------------------------------ text rendering
def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _rec(s) -> str:
    """"9-4 (69%)" with a small-sample marker."""
    if not s or not s["games"]:
        return "-"
    return f"{s['wins']}-{s['losses']} ({_pct(s['winrate'])}){' *' if s['small'] else ''}"


def _signed(v) -> str:
    return "-" if v is None else f"{v:+.0f}"


def _lineup_text(lineup) -> str:
    return ", ".join(f"{p} ({role_name(r)})" for p, r in lineup)


def _table(headers, rows) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def render_markdown(a: dict, ranking=None, lineup=None, cola_text="todas las colas",
                    notes=None) -> str:
    """Full report with every analysis (written to reports/composiciones.md)."""
    mg = a["min_games"]
    out = [f"# Composiciones ({cola_text})", "",
           f"Partidas analizadas: {a['games']} · como ADC: {a['bottom_games']} · "
           f"con algún otro miembro del clan en tu equipo: {a['clan_games']}.", "",
           f"Los winrates con * tienen menos de {mg} partidas (muestra pequeña). El ranking usa el "
           f"winrate ajustado (victorias + {PRIOR}) / (partidas + {2 * PRIOR}); el winrate crudo "
           "se muestra siempre.", ""]

    out += ["## Pools del clan", ""]
    if a["pools"]:
        for p in a["pools"]:
            roles = "; ".join(
                f"{r['label']} {r['games']} (" + ", ".join(
                    f"{c['champion']} {c['games']}p {_pct(c['winrate'])}" for c in r["champions"]) + ")"
                for r in p["roles"])
            out.append(f"- **{p['person']}**{' (tú)' if p['is_main'] else ''}: {p['games']} partidas, "
                       f"WR {_pct(p['winrate'])}{' *' if p['small'] else ''}. {roles}")
    else:
        out.append("Sin datos.")

    duo = a["duo"]
    out += ["", "## Dúo bot (tú de ADC)", "", f"Partidas con soporte identificado: {duo['games']}.", ""]
    for title, rows, extra in (("Por persona de soporte", duo["by_person"], False),
                               ("Por campeón de soporte", duo["by_champion"], True),
                               ("Por arquetipo de soporte", duo["by_archetype"], False)):
        out += [f"### {title}", ""]
        body = []
        for r in rows:
            key = SUPPORT_LABELS.get(r["key"], r["key"]) if title.endswith("arquetipo de soporte") else r["key"]
            if extra:
                key = f"{key} ({SUPPORT_LABELS[r['archetype']].lower()}{', propia' if r['source'] == 'propia' else ''})"
            body.append([key, r["games"], _rec(r), _pct(r["shrunk"]), _signed(r["gold_diff_14"]),
                         "-" if r["deaths"] is None else f"{r['deaths']:.1f}"])
        out += _table(["Soporte", "Partidas", "Récord", "WR ajustado", "Dif. oro @14", "Muertes"], body) \
            if body else ["Sin datos."]
        out.append("")

    out += ["## Matriz ADC x soporte", ""]
    m = a["matrix"]
    out += _table(["ADC \\ Soporte", *[SUPPORT_LABELS[s] for s in m["support_types"]]],
                  [[ADC_LABELS[r["adc"]], *[(_rec(c) + f" n={c['games']}") if c else "-" for c in r["cells"]]]
                   for r in m["rows"] if r["games"]]) if m["games"] else ["Sin datos."]

    out += ["", "## ADC x engage duro en tu equipo", ""]
    e = a["engage"]
    out += _table(["ADC \\ Engage", *e["buckets"]],
                  [[ADC_LABELS[r["adc"]], *[(_rec(c) + f" n={c['games']}") if c else "-" for c in r["cells"]]]
                   for r in e["rows"] if r["games"]]) if e["games"] else ["Sin datos."]

    out += ["", "## Alineaciones del clan (3 o más miembros)", ""]
    out += _table(["Alineación", "Partidas", "Récord", "WR ajustado"],
                  [[_lineup_text(r["lineup"]), r["games"], _rec(r), _pct(r["shrunk"])]
                   for r in a["lineups"]]) if a["lineups"] else ["Sin datos."]
    pairs = a["pairs"]
    out += ["", "### Mejores y peores parejas (persona + campeón)", ""]
    if pairs["best"]:
        body = [["Mejor", f"{x['a'][0]} ({x['a'][1]}) + {x['b'][0]} ({x['b'][1]})", x["games"], _rec(x),
                 _pct(x["shrunk"])] for x in pairs["best"]]
        body += [["Peor", f"{x['a'][0]} ({x['a'][1]}) + {x['b'][0]} ({x['b'][1]})", x["games"], _rec(x),
                  _pct(x["shrunk"])] for x in pairs["worst"]]
        out += _table(["", "Pareja", "Partidas", "Récord", "WR ajustado"], body)
    else:
        out.append(f"Ninguna pareja llega a {mg} partidas juntos.")

    pen = a["penetration"]
    out += ["", "## Itemización: penetración de armadura", "",
            f"Partidas de 20+ min con timeline: {pen['eligible']} (excluidas: {pen['excluded']}). "
            f"«Enemigo tanque» = algún rival con {TANKY_ARMOR}+ de armadura total al final, una "
            "aproximación a «más de 100 de armadura adicional».", ""]
    out += _table(["", "Con % pen. de armadura", "Sin % pen. de armadura"],
                  [["Enemigo tanque" if r["tanky"] else "Sin enemigo tanque", _rec(r["with_pen"]) +
                    f" n={r['with_pen']['games']}", _rec(r["without_pen"]) + f" n={r['without_pen']['games']}"]
                   for r in pen["rows"]]) if pen["eligible"] else ["Sin datos."]

    cl = a["cleanse"]
    out += ["", "## Purificar vs CC enemigo", "",
            f"CC total del equipo rival (segundos) en terciles de tus partidas. Partidas: {cl['games']}, "
            f"con Purificar: {cl['cleanse_games']}.", ""]
    out += _table(["Tercil", "Rango CC", "Partidas", "Con Purificar", "Sin Purificar"],
                  [[b["name"], f"{b['min']:.0f}-{b['max']:.0f}", b["games"], _rec(b["with_cleanse"]),
                    _rec(b["without_cleanse"])] for b in cl["buckets"]]) if cl["buckets"] else ["Sin datos."]

    if ranking is not None:
        out += ["", "## Recomendación de ADC", ""]
        if lineup:
            out.append("Alineación: " + _lineup_text([(p, r) for r, p in lineup.items()]) + ".")
        out += [f"- {n}" for n in notes or []]
        out.append("")
        for i, x in enumerate(ranking[:6], 1):
            out.append(f"{i}. **{x['champion']}** ({ADC_LABELS[x['adc_type']].lower()}"
                       f"{', propia' if x['adc_source'] == 'propia' else ''}) · puntaje {x['score']}")
            out += [f"   - {t}" for t in x["reasons"]]
    return "\n".join(out) + "\n"


def render_summary(a: dict, ranking=None, lineup=None, notes=None, cola_text="todas las colas") -> str:
    """Concise terminal summary: top duos, matrix and recommendations for the common lineup."""
    mg = a["min_games"]
    out = [f"Composiciones ({cola_text}): {a['games']} partidas, {a['bottom_games']} como ADC, "
           f"{a['clan_games']} con otro miembro del clan. (* = menos de {mg} partidas)"]
    duo = a["duo"]
    out += ["", "Dúo bot, por soporte (WR ajustado):"]
    for r in duo["by_person"][:5]:
        out.append(f"  {r['key']:<22} {_rec(r):<14} ajustado {_pct(r['shrunk'])} · {r['games']} partidas")
    if duo["by_champion"]:
        out += ["Mejores campeones de soporte:"]
        for r in duo["by_champion"][:5]:
            out.append(f"  {r['key']:<22} {_rec(r):<14} ajustado {_pct(r['shrunk'])} "
                       f"({SUPPORT_LABELS[r['archetype']].lower()})")
    m = a["matrix"]
    out += ["", "Matriz ADC x soporte (récord):"]
    if m["games"]:
        out.append("  " + " " * 15 + "".join(f"{SUPPORT_LABELS[s].split('/')[0]:<18}" for s in m["support_types"]))
        for r in m["rows"]:
            if r["games"]:
                out.append("  " + f"{ADC_LABELS[r['adc']]:<15}" +
                           "".join(f"{(_rec(c) if c else '-'):<18}" for c in r["cells"]))
    else:
        out.append("  Sin datos.")
    if ranking is not None:
        out += ["", "Alineación más común del clan: " + (_lineup_text([(p, r) for r, p in lineup.items()])
                                                         if lineup else "(ninguna)")]
        out += [f"  {n}" for n in notes or []]
        out.append("Recomendación de ADC:")
        if not ranking:
            out.append("  Ningún ADC con suficientes partidas.")
        for i, x in enumerate(ranking[:4], 1):
            out.append(f"  {i}. {x['champion']} (puntaje {x['score']})")
            out += [f"     - {t}" for t in x["reasons"]]
    return "\n".join(out)
