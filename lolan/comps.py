"""Team compositions: what the clan plays and which ADC to pick for a given lineup.

Everything here is a pure function over "records": one dict per analyzable Summoner's Rift
game of the MAIN player (see `build_records`). Nothing touches the network or the database.

The ADC framework comes from a coaching video by a Challenger support:
  * follow-up ADCs need engage on their team (bad first picks);
  * kiting ADCs shine when chased and want peel/enchanter supports;
  * flexible ADCs work with any support and are safe first picks.
Every classification comes from lolan/data/champion_classes.json (Riot's official class and
subclasses plus a proposed type per lane, see docs/arquetipos.md). The champions the video names
carry `source == "video"`; the others "oficial" (a rule on Riot's subclass), "criterio" (a manual
call), "datos" (decided by the Diamond+ sample) or "pendiente" (a judgment call waiting for the
data). The type `mago` exists in TOP and BOTTOM. The team-level "plan de pelea" is a heuristic of
our own. Everything is seen from the main player's seat: only games he played, with his team's result.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

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

ADC_LABELS = {"followup": "Follow-up", "kiting": "Kiting", "flexible": "Flexible", "mago": "Mago",
              "otro": "Sin clasificar"}
SUPPORT_LABELS = {"engage": "Engage", "peel": "Peel/enchanter", "mixto": "Engage + peel",
                  "poke": "Poke/mago", "otro": "Sin clasificar"}
ADC_ORDER = ("followup", "kiting", "flexible", "mago", "otro")
SUPPORT_ORDER = ("engage", "peel", "mixto", "poke", "otro")


# ---------------------------------------------------------- classification tables
# Single source: lolan/data/champion_classes.json (Riot's class and subclasses, plus a proposed
# type per lane; see docs/arquetipos.md). The tables are built from it when this module loads.
# Where an entry comes from (`source` of every table entry, shown in plain Spanish by SOURCE_LABELS):
#   video      the coaching video (only ADCs and supports, see VIDEO_ADC / VIDEO_SUPPORT); wins over the JSON
#   oficial    a rule applied to Riot's official subclass (basis "regla")
#   criterio   a manual mapping in the proposal, or a previous classification kept (basis "manual"/"actual")
#   datos      decided by the Diamond+ sample (basis "datos", written by `lolan arquetipos --decidir`)
#   pendiente  a judgment call that the data has to decide (PENDING_DATA); keeps the proposal's value
CLASSES_PATH = Path(__file__).with_name("data") / "champion_classes.json"
SOURCE_LABELS = {"video": "del video", "oficial": "clase oficial de Riot", "criterio": "criterio",
                 "datos": "por datos", "pendiente": "pendiente de datos"}
BASIS_SOURCE = {"regla": "oficial", "manual": "criterio", "actual": "criterio", "datos": "datos"}

# Keys are Data Dragon championName values (KSante, MonkeyKing, Kaisa, JarvanIV, FiddleSticks...).
VIDEO_ADC = {
    "followup": ["Kaisa", "Twitch", "Tristana", "Samira", "Lucian", "Draven"],
    "kiting": ["Vayne", "Zeri", "Ezreal", "Sivir"],
    "flexible": ["Ashe", "Jinx", "Aphelios", "Senna", "Varus", "Caitlyn", "KogMaw"],
}
# The video only names Taric, Tahm Kench and Braum (as peel).
VIDEO_SUPPORT = {"peel": ["Taric", "TahmKench", "Braum"]}

# Judgment calls that the Diamond+ data has to decide (`lolan arquetipos --decidir`): lane ->
# champions. Until the data decides, they keep the proposal's value and carry source "pendiente".
# Rules per lane in lolan/archetypes_data.py: JUNGLE early vs farm, TOP tanque / bruiser / split,
# MIDDLE asesino vs melee. TOP: every Diver (bruiser vs split) with a real presence in the lane,
# the manual calls (Specialists, Camille, Irelia...) plus Volibear, Zaahen and Singed. JUNGLE:
# Diana, Ekko, Hecarim and the manual early/farm calls. MIDDLE: Locke and the burst/skirmisher mids.
PENDING_DATA = {
    "TOP": frozenset({
        "Ambessa", "Camille", "Chogath", "Gangplank", "Gnar", "Irelia", "Kayle", "MonkeyKing", "Olaf",
        "Pantheon", "Quinn", "Renekton", "Singed", "Teemo", "Vayne", "Volibear", "Warwick", "Zaahen"}),
    "JUNGLE": frozenset({
        "Diana", "Ekko", "FiddleSticks", "Graves", "Hecarim", "Nidalee", "Quinn", "Teemo", "Trundle",
        "Zyra"}),
    "MIDDLE": frozenset({"Ahri", "Locke", "Sylas", "TwistedFate", "Zoe"}),
}
TABLE_LANES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def load_classes(path=CLASSES_PATH) -> dict:
    """The JSON of champion classes: {DataDragonId: {riot_class, ..., proposal: {lane: {...}}}}."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def table_key(name: str, entry: dict) -> str:
    """The name the match data uses for a champion (its alias when it has one: FiddleSticks)."""
    return (entry.get("aliases") or [name])[0]


def build_tables(classes, video_adc=None, video_support=None, pending=None) -> dict:
    """{lane: {champion: {"type", "source"}}} from the JSON content.

    Entries typed "otro" are left out (unclassified). The video lists override the JSON.
    """
    video_adc = VIDEO_ADC if video_adc is None else video_adc
    video_support = VIDEO_SUPPORT if video_support is None else video_support
    pending = PENDING_DATA if pending is None else pending
    tables = {lane: {} for lane in TABLE_LANES}
    for name, entry in classes.items():
        key = table_key(name, entry)
        for lane, prop in (entry.get("proposal") or {}).items():
            kind = prop.get("type")
            if lane not in tables or not kind or kind == "otro":
                continue
            if prop.get("basis") == "datos":
                source = "datos"
            elif key in pending.get(lane, ()):
                source = "pendiente"
            else:
                source = BASIS_SOURCE.get(prop.get("basis"), "criterio")
            tables[lane][key] = {"type": kind, "source": source}
    for lane, groups in (("BOTTOM", video_adc), ("UTILITY", video_support)):
        for kind, champions in groups.items():
            for champion in champions:
                tables[lane][champion] = {"type": kind, "source": "video"}
    return tables


CLASSES = load_classes()
_TABLES = build_tables(CLASSES)
ADC_CLASS = _TABLES["BOTTOM"]
SUPPORT_CLASS = _TABLES["UTILITY"]
# Same champion, different role -> possibly different type, so the other lines are keyed by role.
#   TOP:    tanque (tank / engage), bruiser (bruiser / juggernaut), split (duelist / split pusher),
#           mago (mage: Vladimir, Rumble, Jayce, Kennen...)
#   JUNGLE: early (early game / ganker), tanque (tank / engage), farm (carry / farm)
#   MIDDLE: control (control mage / artillery), asesino (assassin / roamer), melee (melee duelist)
# UTILITY and BOTTOM are SUPPORT_CLASS / ADC_CLASS above (the ADC also has "mago").
ROLE_CLASS = {lane: _TABLES[lane] for lane in ("TOP", "JUNGLE", "MIDDLE")}

# Clear hard engage in ANY role (used to count how much engage a team has). Deliberately short:
# divers and champions whose engage is only situational (Camille, Yone, Kled, Briar, Ambessa...)
# are left out. Supports classified as "engage" (Pyke) count too, see `engage_count`.
HARD_ENGAGE = frozenset({
    "Malphite", "JarvanIV", "Sejuani", "Zac", "Ornn", "Amumu", "Rell", "Leona", "Nautilus",
    "Alistar", "Rakan", "MonkeyKing", "Vi", "Diana", "Sion", "Kennen", "Galio", "Gragas",
    "Maokai", "Rammus", "Nocturne", "Hecarim", "Skarner", "Thresh", "Blitzcrank", "Poppy",
    "KSante", "Sett",
})

TOP_LABELS = {"tanque": "Tanque", "bruiser": "Bruiser/juggernaut", "split": "Duelista/split pusher",
              "mago": "Mago", "otro": "Sin clasificar"}
JUNGLE_LABELS = {"early": "Early/ganker", "tanque": "Tanque/engage", "farm": "Carry/farm",
                 "otro": "Sin clasificar"}
MIDDLE_LABELS = {"control": "Control/artillería", "asesino": "Asesino/roamer",
                 "melee": "Duelista melee", "otro": "Sin clasificar"}
# role -> {type: label}, and the order in which the types are listed.
ROLE_TYPE_LABELS = {"TOP": TOP_LABELS, "JUNGLE": JUNGLE_LABELS, "MIDDLE": MIDDLE_LABELS,
                    "UTILITY": SUPPORT_LABELS, "BOTTOM": ADC_LABELS}
ROLE_TYPE_ORDER = {"TOP": ("tanque", "bruiser", "split", "mago", "otro"),
                   "JUNGLE": ("early", "tanque", "farm", "otro"),
                   "MIDDLE": ("control", "asesino", "melee", "otro"),
                   "UTILITY": SUPPORT_ORDER, "BOTTOM": ADC_ORDER}
LINE_ROLES = ("TOP", "JUNGLE", "MIDDLE", "UTILITY", "BOTTOM")      # order of the per-line tables

# A neutral champion of each type (used when a recommendation is only a TYPE and we still need a
# champion to predict the team plan). Chosen so they carry no special plan signal of their own.
TYPE_REPRESENTATIVE = {
    "TOP": {"tanque": "Gnar", "bruiser": "Garen", "split": "Fiora", "mago": "Vladimir"},
    "JUNGLE": {"early": "LeeSin", "tanque": "Zac", "farm": "Viego"},
    "MIDDLE": {"control": "Orianna", "asesino": "Zed", "melee": "Yasuo"},
    "UTILITY": {"engage": "Leona", "peel": "Lulu", "mixto": "Thresh", "poke": "Lux"},
    "BOTTOM": {"followup": "Kaisa", "kiting": "Zeri", "flexible": "Jinx", "mago": "Syndra"},
}

# What a user may type -> Data Dragon name (normalized with `normalize_name`).
CHAMPION_ALIASES = {"wukong": "MonkeyKing", "renataglasc": "Renata", "nunuywillump": "Nunu",
                    "kogmaw": "KogMaw", "velkoz": "Velkoz", "ksante": "KSante", "jarvaniv": "JarvanIV",
                    "tahmkench": "TahmKench", "missfortune": "MissFortune"}

_CANONICAL = {normalize_name(c): c for c in (*ADC_CLASS, *SUPPORT_CLASS, *HARD_ENGAGE,
                                              *(c for table in ROLE_CLASS.values() for c in table))}
for _name, _entry in CLASSES.items():              # every champion of the JSON, and its API spelling
    for _spelling in (_name, *(_entry.get("aliases") or [])):
        _CANONICAL[normalize_name(_spelling)] = table_key(_name, _entry)
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
    """followup / kiting / flexible / mago, or "otro" when the champion is not classified."""
    entry = _lookup(ADC_CLASS, champion)
    return entry["type"] if entry else "otro"


def adc_source(champion) -> str | None:
    """Where the ADC's type comes from (see SOURCE_LABELS), None when not classified."""
    entry = _lookup(ADC_CLASS, champion)
    return entry["source"] if entry else None


def support_archetype(champion) -> str:
    """engage / peel / mixto / poke, or "otro" when the champion is not classified."""
    entry = _lookup(SUPPORT_CLASS, champion)
    return entry["type"] if entry else "otro"


def support_source(champion) -> str | None:
    entry = _lookup(SUPPORT_CLASS, champion)
    return entry["source"] if entry else None


def role_type(role: str, champion) -> str:
    """Type of `champion` when played in `role` (any of the five), or "otro" if not classified."""
    if role == "UTILITY":
        return support_archetype(champion)
    if role == "BOTTOM":
        return adc_archetype(champion)
    entry = _lookup(ROLE_CLASS.get(role, {}), champion)
    return entry["type"] if entry else "otro"


def role_source(role: str, champion) -> str | None:
    """Where the type comes from ("video", "oficial", "criterio", "datos", "pendiente"), None if not classified."""
    if role == "UTILITY":
        return support_source(champion)
    if role == "BOTTOM":
        return adc_source(champion)
    entry = _lookup(ROLE_CLASS.get(role, {}), champion)
    return entry["source"] if entry else None


def type_label(role: str, kind: str) -> str:
    return ROLE_TYPE_LABELS.get(role, {}).get(kind, kind)


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


# ------------------------------------------------------------------ team "plan de pelea"
PLAN_ORDER = ("engage", "proteger", "poke", "pick", "split", "mixto")
PLAN_LABELS = {"engage": "Engage", "proteger": "Proteger al carry", "poke": "Poke/asedio",
               "pick": "Pick", "split": "Split push", "mixto": "Mixto"}

# Signals used by `team_plan`. All of them are our own judgement (guides, not Riot data).
WARDENS = frozenset({"Shen", "TahmKench", "Braum"})              # peel anywhere on the map
POKE_MID = frozenset({"Xerath", "Ziggs", "Velkoz", "Lux", "Hwei", "Mel", "Corki"})   # artillery mids
POKE_CARRIES = frozenset({"Ezreal", "Varus", "Jayce", "Caitlyn"})                  # poke in any role
SCALERS = frozenset({"Kayle", "Kassadin", "Vayne"})              # hypercarries outside the ADC slot
CATCHERS = frozenset({"Thresh", "Blitzcrank", "Pyke", "Nautilus"})                 # pick supports

# Thresholds of the rules, in priority order (the first one that holds wins):
#   engage:   >= 3 hard engage, or >= 2 when the ADC is a follow-up
#   poke:     >= 2 poke sources
#   pick:     >= 2 pick sources
#   split:    a "split" top and at most SPLIT_MAX_ENGAGE hard engage
#   proteger: >= PEEL_MIN peel + >= 1 hypercarry + at most PROTEGER_MAX_ENGAGE hard engage
#   mixto:    none of the above
# Tuning (279 real games): with "proteger" first it swallowed 57% of the games, because ~3 of 4
# teams have a peel support and a flexible/kiting ADC. Putting the more specific identities first
# and "proteger" last leaves it as the default for "peel + scaling carry, nothing sharper":
# engage 7%, poke 6%, pick 18%, split 21%, proteger 27%, mixto 21%.
PEEL_MIN = 1
PROTEGER_MAX_ENGAGE = 1
ENGAGE_MIN = 3
ENGAGE_MIN_WITH_FOLLOWUP = 2
POKE_MIN = 2
SPLIT_MAX_ENGAGE = 1
PICK_MIN = 2


def team_signals(team) -> dict[str, list[str]]:
    """Per signal, the champions of `team` (dicts with champion/role) that contribute to it.

    engage: hard engage (existing list, plus engage supports) · peel: peel/mixed supports and
    Shen/Tahm Kench/Braum · poke: artillery mids, artillery mage ADCs (Ziggs, Hwei...), poke supports,
    Ezreal/Varus/Jayce/Caitlyn ·
    hyper: flexible/kiting ADC, or Kayle/Kassadin/Vayne at top or mid · split: split top (a top
    mage is not one) ·
    pick: asesino mid, early jungle, catcher supports (Thresh, Blitzcrank, Pyke, Nautilus) ·
    followup: a follow-up ADC. Each player counts at most once per signal.
    """
    sig = {k: [] for k in ("engage", "peel", "poke", "hyper", "split", "pick", "followup")}
    for m in team:
        champ, role = canonical_champion(m.get("champion")), m.get("role")
        if not champ:
            continue
        sup = support_archetype(champ) if role == "UTILITY" else None
        if is_hard_engage(champ, role):
            sig["engage"].append(champ)
        if sup in ("peel", "mixto") or champ in WARDENS:
            sig["peel"].append(champ)
        if ((role == "MIDDLE" and champ in POKE_MID) or sup == "poke" or champ in POKE_CARRIES
                or (role == "BOTTOM" and champ in POKE_MID and adc_archetype(champ) == "mago")):
            sig["poke"].append(champ)
        if role == "BOTTOM" and adc_archetype(champ) in ("flexible", "kiting"):
            sig["hyper"].append(champ)
        elif role in ("TOP", "MIDDLE") and champ in SCALERS:
            sig["hyper"].append(champ)
        if role == "BOTTOM" and adc_archetype(champ) == "followup":
            sig["followup"].append(champ)
        if role == "TOP" and role_type("TOP", champ) == "split":
            sig["split"].append(champ)
        if (role == "MIDDLE" and role_type("MIDDLE", champ) == "asesino")                 or (role == "JUNGLE" and role_type("JUNGLE", champ) == "early")                 or (role == "UTILITY" and champ in CATCHERS):
            sig["pick"].append(champ)
    return sig


def is_proteger(sig) -> bool:
    """Peel + a scaling carry and little engage (the condition of the "proteger" plan)."""
    return (len(sig["peel"]) >= PEEL_MIN and len(sig["hyper"]) >= 1
            and len(sig["engage"]) <= PROTEGER_MAX_ENGAGE)


def _names(champions) -> str:
    return ", ".join(champions)


def team_plan(team) -> tuple[str, list[str]]:
    """(plan, reasons) of a team: engage / proteger / poke / pick / split / mixto.

    `team`: dicts with `champion` and `role` (the 5 players of one side, main player included).
    The reasons are short Spanish sentences naming the champions behind the decision.
    """
    sig = team_signals(team)
    n = {k: len(v) for k, v in sig.items()}
    engage_needed = ENGAGE_MIN_WITH_FOLLOWUP if n["followup"] else ENGAGE_MIN

    if n["engage"] >= engage_needed:
        plan = "engage"
        reasons = [f"{n['engage']} campeones de engage duro ({_names(sig['engage'])})."]
        if n["followup"]:
            reasons.append(f"El ADC es follow-up ({_names(sig['followup'])}) y tiene a quién seguir.")
    elif n["poke"] >= POKE_MIN:
        plan = "poke"
        reasons = [f"{n['poke']} fuentes de poke/asedio ({_names(sig['poke'])})."]
    elif n["pick"] >= PICK_MIN:
        plan = "pick"
        reasons = [f"{n['pick']} campeones de pick/emboscada ({_names(sig['pick'])})."]
    elif n["split"] >= 1 and n["engage"] <= SPLIT_MAX_ENGAGE:
        plan = "split"
        reasons = [f"Top de split push ({_names(sig['split'])}) y poco engage duro ({n['engage']}): "
                   "se puede jugar 4v4 mientras el top presiona."]
    elif is_proteger(sig):
        plan = "proteger"
        reasons = [f"Hay un carry de escalado ({_names(sig['hyper'])}) y peel para cuidarlo "
                   f"({_names(sig['peel'])}).",
                   f"Poco engage duro ({n['engage']}), así que el equipo juega a proteger, no a entrar."]
    else:
        plan = "mixto"
        reasons = ["Ninguna señal domina: engage duro %d, peel %d, poke %d, pick %d, carry de "
                   "escalado %d, split %d." % (n["engage"], n["peel"], n["poke"], n["pick"],
                                               n["hyper"], n["split"])]
    return plan, reasons


def match_plans(participants, team_id) -> tuple[str, str]:
    """(plan of the side `team_id`, plan of the other side) from raw match participants."""
    ally, foe = [], []
    for p in participants or []:
        member = {"champion": p.get("championName") or "", "role": p.get("teamPosition") or ""}
        (ally if p.get("teamId") == team_id else foe).append(member)
    return team_plan(ally)[0], team_plan(foe)[0]


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
        enemy_team = [{"champion": p.get("championName") or "", "role": p.get("teamPosition") or ""}
                      for p in enemies]
        cc = [_num(p.get("timeCCingOthers")) for p in enemies]
        cc = [v for v in cc if v is not None]
        records.append({
            "match_id": row["match_id"], "queue_id": row["queue_id"], "date": row["date"],
            "game_creation": row["game_creation"], "duration_min": row["duration_min"],
            "win": row["win"], "gold_diff_14": row.get("gold_diff_14"), "deaths": row["deaths"],
            "main_champion": me.get("championName") or "", "main_role": me.get("teamPosition") or "",
            "team": team,
            "enemy_champions": [p.get("championName") or "" for p in enemies],
            "enemy_team": enemy_team,
            "plan": team_plan(team)[0], "enemy_plan": team_plan(enemy_team)[0],
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


def _adc(r) -> dict | None:
    """The teammate playing BOTTOM (only meaningful when the main player is the support)."""
    return next((m for m in r["team"] if m["role"] == "BOTTOM" and not m["is_main"]), None)


def _bottom_games(records):
    return [r for r in records if r["main_role"] == "BOTTOM"]


def main_line(records, default="BOTTOM") -> str:
    """The line the main player plays most in `records` (ties go to the earlier line of ROLES)."""
    counts = Counter(r["main_role"] for r in records if r["main_role"] in ROLES)
    if not counts:
        return default
    return max(counts, key=lambda role: (counts[role], -ROLES.index(role)))


def parse_line(text) -> str | None:
    """"auto" / top / jungla / mid / adc / soporte (or TOP ... UTILITY) -> "auto" or the role; None if unknown."""
    key = str(text or "").strip().casefold()
    if key in ("", "auto"):
        return "auto"
    from .samples import parse_role
    return parse_role(key)


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


def duo_support(records, min_games=MIN_GAMES) -> dict:
    """b'. Main as support: winrate by ADC person, by ADC champion and by ADC archetype."""
    games, no_adc = [], 0
    for r in _own_games(records, "UTILITY"):
        if _adc(r) is None:
            no_adc += 1
        else:
            games.append(r)
    by_champion = _grouped(games, lambda r: _adc(r)["champion"] or None, min_games)
    for row in by_champion:
        row["archetype"] = adc_archetype(row["key"])
        row["source"] = adc_source(row["key"])
    return {
        "games": len(games), "no_adc": no_adc,
        "by_person": _grouped(games, lambda r: _adc(r)["person"] or RANDOM, min_games),
        "by_champion": by_champion,
        "by_archetype": _grouped(games, lambda r: adc_archetype(_adc(r)["champion"]), min_games),
    }


def support_adc_matrix(records, min_games=MIN_GAMES) -> dict:
    """c'. Archetype of the main's support x archetype of the ADC: games and winrate per cell."""
    cells = defaultdict(list)
    for r in _own_games(records, "UTILITY"):
        adc = _adc(r)
        if adc is not None:
            cells[(support_archetype(r["main_champion"]), adc_archetype(adc["champion"]))].append(r)
    rows = []
    for sup in SUPPORT_ORDER:
        row_cells = [{"adc": a, **_stat(cells[(sup, a)], min_games)} if cells.get((sup, a))
                     else None for a in ADC_ORDER]
        rows.append({"support": sup, "games": sum(c["games"] for c in row_cells if c), "cells": row_cells})
    return {"support_types": SUPPORT_ORDER, "adc_types": ADC_ORDER, "rows": rows,
            "games": sum(len(v) for v in cells.values())}


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


def _member_in(r, role, include_main=True) -> dict | None:
    """The player of the main's team in `role` (the first one if the data repeats a role)."""
    return next((m for m in r["team"] if m["role"] == role and (include_main or not m["is_main"])),
                None)


def by_line(records, min_games=MIN_GAMES) -> list[dict]:
    """a. Per line (TOP, JUNGLE, MIDDLE, UTILITY, BOTTOM): the main's team WR by the ALLY's type.

    The main player is excluded from his own line (games where he plays that role are not
    counted there). For each line also, per clan person who played it: how often each type, and
    the team's WR with each type. Rows: {role, label, games, by_type, people}.
    """
    out = []
    for role in LINE_ROLES:
        items = []
        for r in records:
            ally = _member_in(r, role, include_main=False)
            if ally is not None:
                items.append((r, ally))
        order = ROLE_TYPE_ORDER[role]

        def typed(pairs):
            buckets = defaultdict(list)
            for r, m in pairs:
                buckets[role_type(role, m["champion"])].append(r)
            rows = [{"key": k, "label": type_label(role, k), **_stat(v, min_games),
                     "share": len(v) / len(pairs)} for k, v in buckets.items()]
            rows.sort(key=lambda x: (x["small"], -x["shrunk"], -x["games"], order.index(x["key"])
                                     if x["key"] in order else 9))
            return rows

        by_person = defaultdict(list)
        for r, m in items:
            if m["person"]:
                by_person[m["person"]].append((r, m))
        people = [{"person": person, **_stat([r for r, _m in pairs], min_games), "types": typed(pairs)}
                  for person, pairs in by_person.items()]
        people.sort(key=lambda x: (-x["games"], x["person"]))
        out.append({"role": role, "label": role_name(role), "games": len(items),
                    "by_type": typed(items) if items else [], "people": people})
    return out


# Pairs of lines whose type combination is analysed: (role a, role b, key, title).
PAIR_DEFS = (("JUNGLE", "MIDDLE", "jg_mid", "Jungla × Mid"),
             ("JUNGLE", "TOP", "jg_top", "Jungla × Top"),
             ("UTILITY", "BOTTOM", "sup_adc", "Soporte × ADC"))


def type_pairs(records, min_games=MIN_GAMES) -> dict:
    """b. Type of one line x type of another (jungle x mid, jungle x top, support x ADC).

    Unlike `adc_support_matrix` (only games where the main is the ADC), this counts every game
    of the main's team in which both lines are filled, whoever plays them (the main included).
    Each pair: {key, title, role_a, role_b, games, rows[{a, b, labels, ...stats}]}.
    """
    out = {}
    for role_a, role_b, key, title in PAIR_DEFS:
        buckets = defaultdict(list)
        for r in records:
            a, b = _member_in(r, role_a), _member_in(r, role_b)
            if a is not None and b is not None:
                buckets[(role_type(role_a, a["champion"]), role_type(role_b, b["champion"]))].append(r)
        rows = [{"a": ta, "b": tb, "a_label": type_label(role_a, ta), "b_label": type_label(role_b, tb),
                 **_stat(v, min_games)} for (ta, tb), v in buckets.items()]
        rows.sort(key=lambda x: (x["small"], -x["shrunk"], -x["games"], x["a"], x["b"]))
        out[key] = {"key": key, "title": title, "role_a": role_a, "role_b": role_b,
                    "games": sum(len(v) for v in buckets.values()), "rows": rows}
    return out


# Neighbour lines whose pair with the main's own line is analysed (the pairs of PAIR_DEFS without bot).
OWN_PARTNERS = {"JUNGLE": ("MIDDLE", "TOP"), "TOP": ("JUNGLE",), "MIDDLE": ("JUNGLE",)}


def own_pairs(records, main_role, min_games=MIN_GAMES) -> list[dict]:
    """The main's type in `main_role` x the type of the ally in a neighbour line (his own games only).

    One entry per partner line: {key, title, role_a (the main's), role_b, games, rows[{a, b, labels,
    ...stats}]}, rows ranked like `type_pairs`. Empty for a line without such pairs (the bot lane
    has its own duo tables).
    """
    out = []
    mine = _own_games(records, main_role)
    for other in OWN_PARTNERS.get(main_role, ()):
        buckets = defaultdict(list)
        for r in mine:
            ally = _member_in(r, other, include_main=False)
            if ally is not None:
                buckets[(role_type(main_role, _main(r)["champion"]), role_type(other, ally["champion"]))].append(r)
        rows = [{"a": ta, "b": tb, "a_label": type_label(main_role, ta), "b_label": type_label(other, tb),
                 **_stat(v, min_games)} for (ta, tb), v in buckets.items()]
        rows.sort(key=lambda x: (x["small"], -x["shrunk"], -x["games"], x["a"], x["b"]))
        out.append({"key": f"{main_role.lower()}_{other.lower()}",
                    "title": f"Tú en {role_name(main_role).lower()} × {role_name(other).lower()}",
                    "role_a": main_role, "role_b": other,
                    "games": sum(len(v) for v in buckets.values()), "rows": rows})
    return out


def plan_analysis(records, min_games=MIN_GAMES) -> dict:
    """c. Distribution of the main's team plans and WR per plan; own plan x enemy plan matrix."""
    usable = [r for r in records if r.get("plan")]
    total = len(usable)

    def distribution(field):
        rows = []
        for plan in PLAN_ORDER:
            recs = [r for r in usable if r[field] == plan]
            if recs:
                rows.append({"plan": plan, "label": PLAN_LABELS[plan], **_stat(recs, min_games),
                             "share": len(recs) / total})
        return rows

    cells = defaultdict(list)
    for r in usable:
        cells[(r["plan"], r["enemy_plan"])].append(r)
    rows = []
    for plan in PLAN_ORDER:
        row_cells = [{"enemy": e, **_stat(cells[(plan, e)], min_games)} if cells.get((plan, e)) else None
                     for e in PLAN_ORDER]
        rows.append({"plan": plan, "label": PLAN_LABELS[plan],
                     "games": sum(c["games"] for c in row_cells if c), "cells": row_cells})
    return {"games": total, "mine": distribution("plan"), "enemy": distribution("enemy_plan"),
            "matrix": {"plans": PLAN_ORDER, "rows": rows, "games": total}}


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


def most_common_lineup(records, main_role="BOTTOM") -> dict[str, str]:
    """role -> person of the most frequent clan lineup, to pre-fill the draft form.

    It looks at the games where the main player was in `main_role` (the draft recommends for
    that line); for the ADC, and only if there are none, at all games. A lineup from other games
    could put somebody else in the main's line, so any other line looks at his own games only.
    3+ clan members first, else 2+; {} if there is none.
    """
    own = _own_games(records, main_role)
    for pool in ((own, records) if main_role == "BOTTOM" else (own,)):
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


def analyze_all(records, min_games=MIN_GAMES, main_role="BOTTOM") -> dict:
    """Every table of the "Composiciones" page / report, for the given records.

    `main_role`: the line the tables are seen from. The bot-lane duo tables are the ADC's
    (`duo`, `matrix`, `engage`) or the support's (`duo`, `matrix`); the other lines get `own_pairs`.
    What does not apply to the line is None / empty.
    """
    main_role = _check_role(main_role)
    adc, sup = main_role == "BOTTOM", main_role == "UTILITY"
    return {
        "main_role": main_role,
        "games": len(records),
        "bottom_games": len(_bottom_games(records)),
        "own_games": len(_own_games(records, main_role)),
        "clan_games": sum(1 for r in records if _clan_members(r, include_main=False)),
        "overall": _stat(records, min_games),
        "pools": clan_pools(records, min_games),
        "duo": duo_bot(records, min_games) if adc else duo_support(records, min_games) if sup else None,
        "matrix": (adc_support_matrix(records, min_games) if adc
                   else support_adc_matrix(records, min_games) if sup else None),
        "engage": adc_engage_table(records, min_games) if adc else None,
        "own_pairs": own_pairs(records, main_role, min_games),
        "lines": by_line(records, min_games),
        "type_pairs": type_pairs(records, min_games),
        "plans": plan_analysis(records, min_games),
        "lineups": clan_lineups(records, min_games=min_games),
        "pairs": clan_pairs(records, min_games),
        "penetration": penetration_table(records, min_games),
        "cleanse": cleanse_terciles(records, min_games),
        "common_lineup": most_common_lineup(records, main_role),
        "min_games": min_games,
    }


# ------------------------------------------------------------------- recommender
# How well an ADC archetype fits a support archetype, from -1 (bad) to +1 (great).
FIT = {
    "followup": {"engage": 1.0, "mixto": 0.5, "peel": -0.4, "poke": -0.5, "otro": 0.0},
    "kiting": {"engage": -0.2, "mixto": 0.5, "peel": 1.0, "poke": 0.3, "otro": 0.0},
    "flexible": {s: 0.4 for s in SUPPORT_ORDER},
    # Own judgment, not from the video and not calibrated with data yet: a mage ADC (Ziggs, Lux,
    # Brand, Seraphine...) is fragile like a kiting ADC but does not need to be chased, so peel and
    # mixed supports fit a little, engage is neutral and a poke support doubles up (neutral).
    "mago": {"engage": 0.1, "mixto": 0.3, "peel": 0.3, "poke": 0.0, "otro": 0.0},
    "otro": {s: 0.0 for s in SUPPORT_ORDER},
}
# No idea who the support will be: follow-ups are bad first picks, flexible ones are safe.
FIT_UNKNOWN_SUPPORT = {"followup": -0.6, "kiting": 0.0, "flexible": 0.6, "mago": 0.0, "otro": 0.0}
# Extra fit from the hard-engage champions of the rest of the team (0, 1, 2, 3+).
ENGAGE_FIT = {
    "followup": {"0": -0.3, "1": 0.1, "2": 0.4, "3+": 0.5},
    "kiting": {"0": 0.0, "1": 0.0, "2": -0.2, "3+": -0.4},
    # Own judgment (not calibrated): a mage ADC gains slightly from teammates who start the fight.
    "mago": {"0": 0.0, "1": 0.0, "2": 0.1, "3+": 0.2},
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


def _lineup_engage(lineup, main_role="BOTTOM") -> int | None:
    """Engage count of the teammates already chosen; None when no champion was entered."""
    members = [{"champion": v["champion"], "role": role} for role, v in lineup.items()
               if role != main_role and v["champion"]]
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
    if adc_type == "mago":
        if profile["dist"] is None:
            return f"{champion} es un mago en el ADC: sin saber el soporte no hay una regla clara, encaje neutro."
        return (f"{_support_phrase(profile)}: encaje {'bueno' if arch_fit >= 0.3 else 'neutro'} "
                f"para {champion} (mago; regla propia sin calibrar).")
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
    if engage is None or adc_type not in ("followup", "kiting"):
        return None                       # the mage row of ENGAGE_FIT only adds to the score
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


def draft_notes(lineup, records, enemy=(), main_role="BOTTOM") -> list[str]:
    """Context lines for the draft form: what was assumed for the support, team engage, enemy CC.

    The support/ADC fit lines only make sense when the main player is the ADC; for any other
    `main_role` one line says so instead.
    """
    main_role = _check_role(main_role)
    lu = normalize_lineup(lineup)
    notes = []
    if main_role == "BOTTOM":
        profile = support_profile(records, lu)
        if profile["source"] == "champion":
            notes.append(f"Soporte: {_support_phrase(profile)}.")
        elif profile["source"] == "historial":
            notes.append(f"Soporte: {_support_phrase(profile)} (según sus partidas contigo).")
        else:
            notes.append("Soporte desconocido: se favorecen los ADC flexibles y los follow-up se "
                         "consideran malos first picks.")
    else:
        notes.append(f"Juegas {role_name(main_role)}: tus picks salen de tu historial en {role_name(main_role)}.")
    engage = _lineup_engage(lu, main_role)
    if engage is not None:
        notes.append(f"Engage duro en tu equipo (sin contarte): {engage}.")
    enemies = [canonical_champion(e) for e in enemy if str(e or "").strip()]
    foes = [e for e in enemies if is_hard_engage(e)]
    if len(foes) >= 3:
        notes.append(f"El rival tiene mucho engage/CC ({', '.join(foes)}): considera llevar Purificar.")
    return notes


def recommend(lineup, records, enemy=(), include_all=False, min_games=MIN_GAMES,
              max_reasons=3, main_role="BOTTOM") -> list[dict]:
    """Ranked ADC picks for the main player given the rest of the lineup.

    `lineup`: role -> (person, champion) for the main's team (the BOTTOM entry is ignored: it is
    the main player). `enemy`: optional enemy champion names (not used for the score, see
    `draft_notes`). The pool is the champions the main played as ADC at least `min_games` times;
    `include_all` adds every classified ADC. Returns dicts with champion, score (0-100), reasons.

    The ranking is the main's own pick only when he is the ADC: for any other `main_role` it is
    empty (his picks for that line come from `recommend_draft`).
    """
    del enemy  # accepted for the API; the video framework has no champion-level enemy rule
    if _check_role(main_role) != "BOTTOM":
        return []
    lu = normalize_lineup(lineup)
    profile = support_profile(records, lu)
    engage = _lineup_engage(lu, main_role)
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


# --------------------------------------------------- generalized draft recommender
# For every line without a fixed champion the form recommends up to MAX_PICKS picks:
#   * a CLAN person was chosen: that person's champions in the role (>= `min_games` games in the
#     DB, from every game where they appear with the main player);
#   * "Aleatorio" (or a person without enough games): TYPES of that role, never specific champions.
# Score = weighted average of values in [-1, 1] (same idea as the ADC recommender):
#   rules      weight 1 (constant): framework of each line (see `_line_rules`) + the team plan bonus
#   champion   weight 2n/(n+5), n = games of that person with that champion in that role
#              (heavier than the ADC recommender's 2n/(n+10): the person's own record on a
#              champion should move the ranking more than a generic type rule)
#   type       weight 2n/(n+TYPE_K), n = games of the main's team with that ally type in that role;
#              only if n >= MIN_DATA_GAMES (10)
#   pair       weight p*2n/(n+PAIR_K) per type of the neighbouring line (jg x mid, jg x top,
#              sup x adc), p = how likely that neighbour type is; only if n >= MIN_DATA_GAMES
# With little data the rules dominate; with 10+ games the results start to weigh as much.
DRAFT_ROLES = ("TOP", "JUNGLE", "MIDDLE", "UTILITY")     # the other four lines; the main is the ADC
MIN_DATA_GAMES = 10
PERSON_CHAMPION_K = 5          # weight 2n/(n+5) of the person's record on a champion
UNCOMFORTABLE_WR = 0.45        # shrunk WR below this adds a caution reason
UNCOMFORTABLE_PENALTY = 0.2    # and subtracts this from the final signal (scale -1..1)
TYPE_K = 20
PAIR_K = 20
MAX_PICKS = 3
# Extra score for the plan the team would end up with (nothing for "mixto"), see `_plan_bonus`.
PLAN_BONUS = {"engage": 0.3, "proteger": 0.4, "poke": 0.3, "pick": 0.3, "split": 0.2, "mixto": 0.0}
PLAN_KEEP_BONUS = 0.2          # the pick keeps the plan the fixed picks already point to
PROTEGER_KEEP_BONUS = 0.3      # ADC hypercarry + peel are locked and the pick keeps "proteger" alive
# Framework values per jungle type (multiplied by how likely the condition is).
JUNGLE_HYPER = {"farm": 0.6, "tanque": 0.4, "early": -0.2}      # team has a scaling hypercarry
JUNGLE_ASESINO_MID = {"early": 0.6, "tanque": -0.6, "farm": 0.0}


def _prob(slot, pred) -> float:
    return sum(p for champion, p in slot["champs"] if pred(champion))


def _union(*ps) -> float:
    out = 1.0
    for p in ps:
        out *= 1 - _clamp(p, 0.0, 1.0)
    return 1 - out


def _check_role(main_role) -> str:
    if main_role not in ROLES:
        raise ValueError(f"main_role debe ser una de {ROLES}, no {main_role!r}")
    return main_role


def _own_games(records, role):
    """Games where the main player played `role`."""
    return [r for r in records if r["main_role"] == role]


def _slots(records, lu, main_role="BOTTOM") -> dict:
    """Per role: what we assume about who/what will be there.

    {"kind": "champion" | "historial" | "desconocido", "champs": [(champion, probability)]}.
    A typed champion is certain; a clan person without champion is their history in that role;
    the main's slot is his own history in `main_role` (he is the one picking it). A champion
    typed for the main's slot counts only when he is not the ADC (legacy: the ADC form has none).
    """
    slots = {}
    for role in ROLES:
        v = lu[role]
        slot, hist = {"kind": "desconocido", "champs": []}, None
        if role == main_role and (main_role == "BOTTOM" or not v["champion"]):
            hist = Counter(r["main_champion"] for r in _own_games(records, role) if r["main_champion"])
        elif v["champion"]:
            slot = {"kind": "champion", "champs": [(v["champion"], 1.0)]}
        elif v["person"]:
            hist = Counter(m["champion"] for r in records for m in r["team"]
                           if m["role"] == role and m["person"] == v["person"] and m["champion"])
        if hist:
            total = sum(hist.values())
            slot = {"kind": "historial", "champs": [(c, n / total) for c, n in sorted(hist.items())]}
        slots[role] = slot
    return slots


def _p_type(slots, role, *types) -> float:
    return _prob(slots[role], lambda c: role_type(role, c) in types)


def _p_hyper(slots) -> float:
    """Chance that the team has a scaling hypercarry (flexible/kiting ADC, Kayle/Kassadin/Vayne)."""
    return _union(_p_type(slots, "BOTTOM", "flexible", "kiting"),
                  _prob(slots["TOP"], lambda c: c in SCALERS),
                  _prob(slots["MIDDLE"], lambda c: c in SCALERS))


def _p_engage(slots, *roles) -> float:
    return _union(*(_prob(slots[r], lambda c, r=r: is_hard_engage(c, r)) for r in roles))


def _line_rules(role, kind, slots) -> list[tuple[float, str | None]]:
    """Framework values of a candidate of type `kind` in `role`: [(value, reason)]."""
    label = type_label(role, kind).lower()
    parts = []
    if role == "JUNGLE":
        hyper, ase = _p_hyper(slots), _p_type(slots, "MIDDLE", "asesino")
        v = JUNGLE_HYPER.get(kind, 0.0) * hyper
        text = None
        if abs(v) >= 0.15:
            text = (f"Hay un carry de escalado en tu equipo: un jungla {label} lo acompaña bien."
                    if v > 0 else "Con un carry de escalado en el equipo, un jungla early aporta menos.")
        parts.append((v, text))
        v = JUNGLE_ASESINO_MID.get(kind, 0.0) * ase
        text = None
        if abs(v) >= 0.15:
            text = ("Tu mid es asesino/roamer: un jungla early le da las rotaciones que necesita."
                    if v > 0 else "Tu mid es asesino/roamer y un jungla tanque no le sirve: prefiere un early.")
        parts.append((v, text))
    elif role == "MIDDLE":
        if kind == "control":
            parts.append((0.5, "Un mid de control/artillería es el pick seguro: no depende del resto del equipo."))
        elif kind == "asesino":
            early, tank = _p_type(slots, "JUNGLE", "early"), _p_type(slots, "JUNGLE", "tanque")
            v = -0.1 + 0.7 * early - 0.5 * tank
            text = ("Un asesino/roamer funciona mejor con una jungla early que juegue con él." if v > 0.2
                    else "Un asesino/roamer pide una jungla early; con otro tipo de jungla rinde menos."
                    if v < -0.1 else None)
            parts.append((v, text))
        elif kind == "melee":
            eng = _p_engage(slots, "TOP", "JUNGLE", "UTILITY")
            v = -0.3 + 0.8 * eng
            text = ("Un duelista melee necesita engage a quien seguir, y tu equipo lo tiene." if v > 0.2
                    else "Un duelista melee necesita engage a quien seguir y aún no lo tienes asegurado.")
            parts.append((v, text))
    elif role == "TOP":
        frontline = _union(_p_type(slots, "JUNGLE", "tanque"), _p_type(slots, "UTILITY", "engage"))
        if kind == "tanque":
            v = 0.1 + 0.5 * (1 - frontline)
            text = ("Tu equipo no tiene frontline asegurada: un top tanque la aporta." if v >= 0.4
                    else "Ya hay frontline (jungla o soporte): un top tanque suma menos." if v < 0.3 else None)
            parts.append((v, text))
        elif kind == "bruiser":
            parts.append((0.3, "Un bruiser es el pick seguro del top: aguanta y pega."))
        elif kind == "split":
            # own judgment: a mage ADC also zones the enemy while the top is away
            hold = _union(_p_type(slots, "UTILITY", "peel", "mixto"), _p_type(slots, "MIDDLE", "control"),
                          _p_type(slots, "BOTTOM", "mago"))
            v = -0.4 + 0.9 * hold
            text = ("El resto puede aguantar el 4v4 (peel o control), así que el split push es viable."
                    if v > 0.2 else "El split push solo funciona si el resto aguanta 4v4 (peel o control) "
                    "y eso no está asegurado.")
            parts.append((v, text))
        elif kind == "mago":
            # own judgment, kept neutral until it is calibrated with data
            parts.append((0.1, "Un top mago no depende del resto del equipo: pick neutro, sin una regla clara."))
    elif role == "UTILITY":
        adc = slots["BOTTOM"]
        if adc["champs"]:
            fit = sum(p * FIT[adc_archetype(c)][kind] for c, p in adc["champs"])
            share = Counter()
            for c, p in adc["champs"]:
                share[adc_archetype(c)] += p
            dominant = max(share, key=lambda a: (share[a], a))
            text = None
            if abs(fit) >= 0.2 and dominant != "otro":
                text = (f"Tu ADC suele ser {ADC_LABELS[dominant].lower()}: un soporte {label} "
                        f"{'encaja bien' if fit > 0 else 'encaja peor'}.")
            parts.append((fit, text))
    return parts


def _members(slots, role=None, champion=None, adc=None) -> list[dict]:
    """Team for the plan prediction: typed champions + the candidate + the assumed ADC."""
    members = [{"champion": s["champs"][0][0], "role": r} for r, s in slots.items()
               if s["kind"] == "champion" and r != role]
    if role and champion:
        members.append({"champion": champion, "role": role})
    if adc and role != "BOTTOM":
        members.append({"champion": adc, "role": "BOTTOM"})
    return members


def _plan_bonus(members, base_members) -> tuple[float, str, str | None]:
    """(bonus, plan, reason): extra score for the plan the team ends up with."""
    plan = team_plan(members)[0]
    bonus, text = PLAN_BONUS[plan], None
    if is_proteger(team_signals(members)) and is_proteger(team_signals(base_members)):
        bonus += PROTEGER_KEEP_BONUS
        text = "Tu ADC de escalado y el peel ya están: este pick mantiene vivo el plan de proteger al carry."
    elif plan != "mixto" and plan == team_plan(base_members)[0]:
        bonus += PLAN_KEEP_BONUS
        text = f"Refuerza el plan «{PLAN_LABELS[plan]}» que ya apuntan tus picks."
    elif bonus >= 0.3:
        text = f"Con este pick el equipo quedaría con plan «{PLAN_LABELS[plan]}»."
    return bonus, plan, text


# role -> [(pair key, neighbour role, is the candidate the first element of the pair)]
PAIR_RELATIONS = {"TOP": [("jg_top", "JUNGLE", False)],
                  "JUNGLE": [("jg_mid", "MIDDLE", True), ("jg_top", "TOP", True)],
                  "MIDDLE": [("jg_mid", "JUNGLE", False)],
                  "UTILITY": [("sup_adc", "BOTTOM", True)]}


def _score_candidate(role, kind, champion, slots, tables, base_members, adc, stat, min_games,
                     person=None, is_main=False):
    """(raw score in [-1, 1], reasons, plan) of a type (champion None) or of a champion.

    `is_main`: the candidate is for the main player's own slot (reasons say "tú", not "esta persona").
    """
    champ_for_plan = champion or TYPE_REPRESENTATIVE.get(role, {}).get(kind)
    rule_parts = _line_rules(role, kind, slots)
    bonus, plan, plan_text = _plan_bonus(_members(slots, role, champ_for_plan, adc), base_members)
    rule = _clamp(sum(v for v, _t in rule_parts) + bonus)
    cands = [(abs(v), t) for v, t in rule_parts if t]
    if plan_text:
        cands.append((abs(bonus), plan_text))
    num, den = RULE_WEIGHT * rule, RULE_WEIGHT

    n = stat["games"] if stat else 0
    penalty = 0.0
    if n:
        w, sig = 2 * n / (n + PERSON_CHAMPION_K), _wr_signal(stat["shrunk"])
        num, den = num + w * sig, den + w
        if n >= min_games and stat["shrunk"] < UNCOMFORTABLE_WR:
            penalty = UNCOMFORTABLE_PENALTY
            who = "Vas" if is_main else f"{person or 'Esta persona'} va"
            cands.append((abs(sig) * w + 0.5,
                          f"{who} {stat['wins']}-{stat['losses']} con {champion}; poco cómodo."))
        elif n >= min_games:
            with_ = "contigo" if is_main else "con esta persona"
            cands.append((abs(sig) * w + 0.1,
                          f"{champion} en {role_name(role)} {with_}: {stat['wins']}-{stat['losses']} "
                          f"({stat['winrate'] * 100:.0f}%, ajustado {stat['shrunk'] * 100:.0f}%)."))

    line = tables["lines"].get(role, {}).get(kind)
    if line and line["games"] >= MIN_DATA_GAMES:
        w, sig = 2 * line["games"] / (line["games"] + TYPE_K), _wr_signal(line["shrunk"])
        num, den = num + w * sig, den + w
        cands.append((abs(sig) * w + 0.1,
                      f"Tu equipo con {type_label(role, kind).lower()} en {role_name(role)}: "
                      f"{line['wins']}-{line['losses']} ({line['winrate'] * 100:.0f}%, {line['games']} partidas)."))

    for pair_key, other, first in PAIR_RELATIONS.get(role, []):
        other_types = Counter()
        for c, p in slots[other]["champs"]:
            other_types[role_type(other, c)] += p
        ra, rb = next((a, b) for a, b, k, _t in PAIR_DEFS if k == pair_key)
        for o, p in other_types.items():
            pa, pb = (kind, o) if first else (o, kind)
            st = tables["pairs"].get(pair_key, {}).get((pa, pb))
            if not st or st["games"] < MIN_DATA_GAMES:
                continue
            w, sig = p * 2 * st["games"] / (st["games"] + PAIR_K), _wr_signal(st["shrunk"])
            num, den = num + w * sig, den + w
            cands.append((abs(sig) * w + 0.1,
                          f"{type_label(ra, pa)} + {type_label(rb, pb)}: {st['wins']}-{st['losses']} "
                          f"({st['winrate'] * 100:.0f}%, {st['games']} partidas)."))
    cands.sort(key=lambda c: -c[0])
    return _clamp(num / den - penalty), [t for _m, t in cands], plan


def _assumed_adc(slots, records, main_role) -> str | None:
    """The ADC the other lines' rules assume (None when there is none or it is already a typed member)."""
    if main_role == "BOTTOM":
        adc_hist = Counter(r["main_champion"] for r in _bottom_games(records) if r["main_champion"])
        return max(adc_hist, key=lambda c: (adc_hist[c], c)) if adc_hist else None
    slot = slots["BOTTOM"]
    if slot["kind"] != "historial":
        return None
    return max(slot["champs"], key=lambda cp: (cp[1], cp[0]))[0]


def recommend_draft(lineup, records, enemy=(), include_all=False, min_games=MIN_GAMES,
                    max_picks=MAX_PICKS, main_role="BOTTOM") -> dict:
    """Recommendations for EVERY line without a fixed champion, plus the resulting team plan.

    `lineup`: role -> (person, champion) as in `recommend` (the `main_role` entry is the main
    player). Returns {"roles": {role: {...}}, "plan": {...}}. Each role has `mode`: "fijo"
    (champion typed), "campeones" (picks from that person's pool), "tipos" (type suggestions:
    Aleatorio, or a person without enough games in the role) or "adc" (the existing ADC
    recommender); and `picks`, the top `max_picks` with 1-3 Spanish reasons each.

    `main_role`: the line the main player plays. The default "BOTTOM" is the original behaviour
    (his slot is the ADC ranking; `include_all` applies). For any other line his slot is a normal
    entry (`is_main` True) whose pool is his own games in that line, and BOTTOM behaves like any
    other teammate's line (typed champion, or the person's ADC history, with ADC types).
    """
    main_role = _check_role(main_role)
    lu = normalize_lineup(lineup)
    slots = _slots(records, lu, main_role)
    tables = {
        "lines": {x["role"]: {t["key"]: t for t in x["by_type"]} for x in by_line(records, min_games)},
        "pairs": {k: {(x["a"], x["b"]): x for x in v["rows"]}
                  for k, v in type_pairs(records, min_games).items()},
    }
    adc = _assumed_adc(slots, records, main_role)
    base_members = _members(slots, adc=adc)

    roles = {}
    for role in (DRAFT_ROLES if main_role == "BOTTOM" else LINE_ROLES):
        v = lu[role]
        is_main = role == main_role
        person = v["person"] or (main_person(records) if is_main else None)
        entry = {"role": role, "label": role_name(role), "person": person, "fixed": v["champion"],
                 "picks": [], "note": None, "mode": "tipos"}
        if is_main:
            entry["is_main"] = True        # only when the main is not the ADC: the original output has no such key
        roles[role] = entry
        if v["champion"]:
            kind = role_type(role, v["champion"])
            entry.update(mode="fijo", fixed_type=kind, fixed_type_label=type_label(role, kind))
            continue
        pool = {}
        if is_main:
            by = defaultdict(list)
            for r in _own_games(records, role):
                if r["main_champion"]:
                    by[r["main_champion"]].append(r)
            pool = {c: rs for c, rs in by.items() if len(rs) >= min_games}
        elif v["person"]:
            by = defaultdict(list)
            for r in records:
                for m in r["team"]:
                    if m["role"] == role and m["person"] == v["person"] and m["champion"]:
                        by[m["champion"]].append(r)
            pool = {c: rs for c, rs in by.items() if len(rs) >= min_games}
        picks = []
        if pool:
            entry["mode"] = "campeones"
            for champion, recs in pool.items():
                st = _stat(recs, min_games)
                kind = role_type(role, champion)
                raw, reasons, plan = _score_candidate(role, kind, champion, slots, tables, base_members,
                                                      adc, st, min_games, person, is_main)
                picks.append({"kind": "campeon", "champion": champion, "type": kind,
                              "type_label": type_label(role, kind), "source": role_source(role, champion),
                              "score": round((raw + 1) * 50), "raw_score": raw,
                              "reasons": reasons[:3], "plan": plan, "plan_label": PLAN_LABELS[plan],
                              "games": st["games"], "wins": st["wins"], "winrate": st["winrate"],
                              "shrunk": st["shrunk"]})
        else:
            if is_main:
                entry["note"] = (f"Tienes menos de {min_games} partidas en {role_name(role)}: "
                                 "se sugieren tipos de campeón.")
            elif v["person"]:
                entry["note"] = (f"{v['person']} no tiene {min_games} partidas en {role_name(role)} "
                                 "contigo: se sugieren tipos de campeón.")
            for kind in (k for k in ROLE_TYPE_ORDER[role] if k != "otro"):
                raw, reasons, plan = _score_candidate(role, kind, None, slots, tables, base_members,
                                                      adc, None, min_games)
                picks.append({"kind": "tipo", "champion": None, "type": kind,
                              "type_label": type_label(role, kind), "source": None,
                              "score": round((raw + 1) * 50), "raw_score": raw,
                              "reasons": reasons[:3], "plan": plan, "plan_label": PLAN_LABELS[plan],
                              "games": 0, "wins": 0, "winrate": None, "shrunk": None})
        picks.sort(key=lambda x: (-x["raw_score"], -(x["shrunk"] or 0), x["champion"] or x["type"]))
        for pick in picks:
            if not pick["reasons"]:
                pick["reasons"] = ["Sin una regla clara ni datos suficientes: pick neutro."]
        entry["picks"] = picks[:max_picks]

    if main_role == "BOTTOM":
        adc_ranking = recommend(lineup, records, enemy, include_all=include_all, min_games=min_games)
        roles["BOTTOM"] = {
            "role": "BOTTOM", "label": role_name("BOTTOM"), "person": lu["BOTTOM"]["person"], "fixed": None,
            "mode": "adc", "note": None,
            "picks": [{"kind": "campeon", "champion": x["champion"], "type": x["adc_type"],
                       "type_label": ADC_LABELS[x["adc_type"]], "source": x["adc_source"],
                       "score": x["score"], "raw_score": x["raw_score"], "reasons": x["reasons"],
                       "plan": None, "plan_label": None, "games": x["games"], "wins": x["wins"],
                       "winrate": x["winrate"], "shrunk": x["shrunk"]} for x in adc_ranking[:max_picks]]}
    return {"roles": roles, "plan": _predicted_plan(roles, adc)}


def _predicted_plan(roles, adc) -> dict:
    """Plan of the team made of the typed champions + the best pick of every other line.

    A type-only pick stands in as TYPE_REPRESENTATIVE (a neutral champion of that type).
    """
    members, shown = [], []
    for role in LINE_ROLES:
        entry = roles[role]
        champion, text = None, None
        if entry.get("fixed"):
            champion = text = entry["fixed"]
        elif entry["picks"]:
            top = entry["picks"][0]
            champion = top["champion"] or TYPE_REPRESENTATIVE.get(role, {}).get(top["type"])
            text = top["champion"] or top["type_label"]
        elif role == "BOTTOM" and adc:
            champion = text = adc
        if champion:
            members.append({"champion": champion, "role": role})
            shown.append({"role": role, "label": role_name(role), "text": text})
    plan, reasons = team_plan(members)
    return {"plan": plan, "label": PLAN_LABELS[plan], "reasons": reasons, "members": shown}


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


CLASSIFICATION_NOTE = (
    "El tipo de cada campeón sale de la clase oficial de Riot (subclase), de un criterio nuestro cuando "
    "esa clase no decide, de los datos de partidas Diamante+ («por datos») o del video de coaching "
    "(«del video», solo ADC y soportes). Los marcados «pendiente de datos» son dudosos y esperan a que "
    "los datos los decidan. El plan de pelea es una heurística nuestra, no un dato de Riot.")


def source_tag(source) -> str:
    """", del video" / ", criterio" ... for the text reports; empty when the type has no source."""
    return f", {SOURCE_LABELS[source]}" if source in SOURCE_LABELS else ""


def _type_cell(row) -> str:
    return f"{row['label']} {_rec(row)}"


def _md_lines(a) -> list[str]:
    out = ["", "## Por línea (tus aliados, sin contarte)", "",
           "Winrate de tu equipo según el tipo del aliado en cada línea. Las partidas en las que tú "
           "juegas esa línea no cuentan en ella.", ""]
    for line in a["lines"]:
        out += [f"### {line['label']} · {line['games']} partidas", ""]
        if not line["by_type"]:
            out += ["Sin datos.", ""]
            continue
        out += _table(["Tipo", "Partidas", "% de partidas", "Récord", "WR ajustado"],
                      [[x["label"], x["games"], _pct(x["share"]), _rec(x), _pct(x["shrunk"])]
                       for x in line["by_type"]])
        if line["people"]:
            out += ["", "Por persona del clan en esa línea (tipos que juega y WR con cada uno):", ""]
            out += _table(["Persona", "Partidas", "Tipos: partidas, récord"],
                          [[p["person"], p["games"],
                            "; ".join(f"{t['label']} {t['games']}p ({_pct(t['share'])}) {_rec(t)}"
                                      for t in p["types"])] for p in line["people"]])
        out.append("")
    return out


def _md_type_pairs(a) -> list[str]:
    out = ["## Duplas de tipos", "",
           "Todas tus partidas con ambas líneas ocupadas (tú incluido si juegas una de ellas). "
           "Se ordena por WR ajustado.", ""]
    for pair in a["type_pairs"].values():
        out += [f"### {pair['title']} · {pair['games']} partidas", ""]
        out += _table([role_name(pair["role_a"]), role_name(pair["role_b"]), "Partidas",
                       "Récord", "WR ajustado"],
                      [[x["a_label"], x["b_label"], x["games"], _rec(x), _pct(x["shrunk"])]
                       for x in pair["rows"]]) if pair["rows"] else ["Sin datos."]
        out.append("")
    return out


def _md_duo_support(a) -> list[str]:
    duo = a["duo"]
    out = ["", "## Dúo bot (tú de soporte)", "", f"Partidas con ADC identificado: {duo['games']}.", ""]
    for title, rows, extra in (("Por persona de ADC", duo["by_person"], False),
                               ("Por campeón de ADC", duo["by_champion"], True),
                               ("Por arquetipo de ADC", duo["by_archetype"], False)):
        out += [f"### {title}", ""]
        body = []
        for r in rows:
            key = ADC_LABELS.get(r["key"], r["key"]) if title.endswith("arquetipo de ADC") else r["key"]
            if extra:
                key = f"{key} ({ADC_LABELS[r['archetype']].lower()}{source_tag(r['source'])})"
            body.append([key, r["games"], _rec(r), _pct(r["shrunk"]), _signed(r["gold_diff_14"]),
                         "-" if r["deaths"] is None else f"{r['deaths']:.1f}"])
        out += _table(["ADC", "Partidas", "Récord", "WR ajustado", "Dif. oro @14", "Muertes"], body) \
            if body else ["Sin datos."]
        out.append("")
    out += ["## Matriz soporte x ADC", ""]
    m = a["matrix"]
    out += _table(["Soporte \\ ADC", *[ADC_LABELS[t] for t in m["adc_types"]]],
                  [[SUPPORT_LABELS[r["support"]], *[(_rec(c) + f" n={c['games']}") if c else "-" for c in r["cells"]]]
                   for r in m["rows"] if r["games"]]) if m["games"] else ["Sin datos."]
    return out


def _md_own_pairs(a) -> list[str]:
    out = []
    for pair in a["own_pairs"]:
        out += ["", f"## {pair['title']}", "",
                f"Tu tipo de campeón x el de tu aliado en esa línea, solo en tus partidas de "
                f"{role_name(pair['role_a'])}: {pair['games']}.", ""]
        out += _table([f"{role_name(pair['role_a'])} (tú)", role_name(pair["role_b"]), "Partidas", "Récord",
                       "WR ajustado"],
                      [[x["a_label"], x["b_label"], x["games"], _rec(x), _pct(x["shrunk"])]
                       for x in pair["rows"]]) if pair["rows"] else ["Sin datos."]
    return out


def _summary_duo_support(a) -> list[str]:
    duo = a["duo"]
    out = ["", "Dúo bot, por ADC (WR ajustado):"]
    for r in duo["by_person"][:5]:
        out.append(f"  {r['key']:<22} {_rec(r):<14} ajustado {_pct(r['shrunk'])} · {r['games']} partidas")
    if duo["by_champion"]:
        out += ["Mejores campeones de ADC:"]
        for r in duo["by_champion"][:5]:
            out.append(f"  {r['key']:<22} {_rec(r):<14} ajustado {_pct(r['shrunk'])} "
                       f"({ADC_LABELS[r['archetype']].lower()})")
    m = a["matrix"]
    out += ["", "Matriz soporte x ADC (récord):"]
    if m["games"]:
        out.append("  " + " " * 15 + "".join(f"{ADC_LABELS[t]:<18}" for t in m["adc_types"]))
        for r in m["rows"]:
            if r["games"]:
                out.append("  " + f"{SUPPORT_LABELS[r['support']].split('/')[0]:<15}" +
                           "".join(f"{(_rec(c) if c else '-'):<18}" for c in r["cells"]))
    else:
        out.append("  Sin datos.")
    return out


def _summary_own_pairs(a) -> list[str]:
    out = []
    for pair in a["own_pairs"]:
        rows = [x for x in pair["rows"] if not x["small"] and "otro" not in (x["a"], x["b"])][:4]
        out += ["", f"{pair['title']} (mejores por WR ajustado):"]
        out += [f"  {x['a_label'] + ' + ' + x['b_label']:<46} {_rec(x):<14} ajustado {_pct(x['shrunk'])} "
                f"· {x['games']} partidas" for x in rows] or ["  Sin datos."]
    return out


def _md_plans(a) -> list[str]:
    plans = a["plans"]
    out = ["## Plan de pelea", "",
           "Plan de tu equipo en cada partida (regla en `comps.team_plan`: engage, poke, pick, split, "
           "proteger al carry; «mixto» si ninguna domina).", ""]
    if not plans["games"]:
        return out + ["Sin datos.", ""]
    for title, rows in (("Tu equipo", plans["mine"]), ("El equipo rival", plans["enemy"])):
        out += [f"### {title}", ""]
        out += _table(["Plan", "Partidas", "% de partidas", "Récord", "WR ajustado"],
                      [[x["label"], x["games"], _pct(x["share"]), _rec(x), _pct(x["shrunk"])]
                       for x in rows])
        out.append("")
    m = plans["matrix"]
    out += ["### Tu plan × plan rival (récord de tu equipo)", ""]
    out += _table(["Tu plan \\ rival", *[PLAN_LABELS[p] for p in m["plans"]]],
                  [[r["label"], *[(_rec(c) + f" n={c['games']}") if c else "-" for c in r["cells"]]]
                   for r in m["rows"] if r["games"]])
    out.append("")
    return out


def _pick_name(pick) -> str:
    if pick["kind"] == "tipo":
        return f"Tipo: {pick['type_label']}"
    return f"{pick['champion']} ({pick['type_label'].lower()}{source_tag(pick['source'])})"


def _draft_order(draft) -> list[str]:
    """Lines of a draft in display order: the main's own line first unless he is the ADC."""
    mine = next((role for role, e in draft["roles"].items() if e.get("is_main")), None)
    return ([mine] + [r for r in LINE_ROLES if r != mine]) if mine else list(LINE_ROLES)


def _md_draft(draft) -> list[str]:
    out = ["", "## Recomendación por línea", ""]
    for role in _draft_order(draft):
        e = draft["roles"].get(role)
        if not e:
            continue
        who = f" · {e['person']}" if e.get("person") else ""
        label = e["label"] + (" (tú)" if e.get("is_main") else "")
        if e["mode"] == "fijo":
            out.append(f"- **{label}**{who}: {e['fixed']} ({e['fixed_type_label'].lower()}), ya escogido.")
            continue
        out.append(f"- **{label}**{who}" + (f" ({e['note']})" if e.get("note") else ""))
        if not e["picks"]:
            out.append("  - Sin sugerencias.")
        for i, x in enumerate(e["picks"], 1):
            out.append(f"  {i}. {_pick_name(x)} · puntaje {x['score']}"
                       + (f" · plan {x['plan_label']}" if x.get("plan_label") else ""))
            out += [f"     - {t}" for t in x["reasons"]]
    plan = draft["plan"]
    out += ["", f"Plan resultante con las mejores sugerencias: **{plan['label']}** "
                f"({', '.join(m['label'] + ': ' + m['text'] for m in plan['members'])}).", ""]
    out += [f"- {t}" for t in plan["reasons"]]
    return out


def _summary_duo_adc(a) -> list[str]:
    duo = a["duo"]
    out = ["", "Dúo bot, por soporte (WR ajustado):"]
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
    return out


def _summary_lines(a) -> list[str]:
    out = ["", "Por línea (WR de tu equipo según el tipo del aliado):"]
    for line in a["lines"]:
        rows = [x for x in line["by_type"] if x["key"] != "otro"][:3]
        out.append(f"  {line['label']:<8}" + (" · ".join(_type_cell(x) for x in rows) if rows else "sin datos"))
    return out


def _summary_pairs(a) -> list[str]:
    out = []
    for key in ("jg_mid", "jg_top"):
        pair = a["type_pairs"][key]
        rows = [x for x in pair["rows"] if not x["small"] and "otro" not in (x["a"], x["b"])][:4]
        out += ["", f"Duplas {pair['title']} (mejores por WR ajustado):"]
        out += [f"  {x['a_label'] + ' + ' + x['b_label']:<46} {_rec(x):<14} ajustado {_pct(x['shrunk'])} "
                f"· {x['games']} partidas" for x in rows] or ["  Sin datos."]
    return out


def _summary_plans(a) -> list[str]:
    plans = a["plans"]
    out = ["", "Plan de pelea de tu equipo:"]
    if not plans["games"]:
        return out + ["  Sin datos."]
    out += [f"  {x['label']:<18} {x['games']:>3} partidas ({_pct(x['share'])}) · {_rec(x)} · "
            f"ajustado {_pct(x['shrunk'])}" for x in plans["mine"]]
    best = sorted((c | {"mine": r["label"], "foe": PLAN_LABELS[c["enemy"]]}
                   for r in plans["matrix"]["rows"] for c in r["cells"] if c and not c["small"]),
                  key=lambda c: (-c["shrunk"], -c["games"]))
    if best:
        out.append("  Mejores cruces (tu plan vs plan rival): " + "; ".join(
            f"{c['mine']} vs {c['foe']} {_rec(c)}" for c in best[:3]))
        out.append("  Peores cruces: " + "; ".join(
            f"{c['mine']} vs {c['foe']} {_rec(c)}" for c in best[::-1][:3]))
    return out


def _summary_draft(draft) -> list[str]:
    out = ["Recomendación por línea (alineación más común):"]
    for role in _draft_order(draft):
        e = draft["roles"].get(role)
        if not e or e["mode"] == "adc":         # the ADC ranking is printed on its own
            continue
        who = f" ({e['person']})" if e.get("person") else ""
        label = e["label"] + (" (tú)" if e.get("is_main") else "")
        if e["mode"] == "fijo":
            out.append(f"  {label}{who}: {e['fixed']} (ya escogido)")
            continue
        out.append(f"  {label}{who}:")
        for i, x in enumerate(e["picks"], 1):
            out.append(f"    {i}. {_pick_name(x)} (puntaje {x['score']})")
            out += [f"       - {x['reasons'][0]}"] if x["reasons"] else []
    plan = draft["plan"]
    out.append(f"  Plan resultante: {plan['label']} - {plan['reasons'][0]}")
    return out


def render_markdown(a: dict, ranking=None, lineup=None, cola_text="todas las colas",
                    notes=None, draft=None) -> str:
    """Full report with every analysis (written to reports/composiciones.md)."""
    mg = a["min_games"]
    ml = a.get("main_role", "BOTTOM")
    out = [f"# Composiciones ({cola_text})", "",
           f"Partidas analizadas: {a['games']} · como {role_name(ml)}: {a.get('own_games', a['bottom_games'])} · "
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

    if ml == "BOTTOM":
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
                    key = f"{key} ({SUPPORT_LABELS[r['archetype']].lower()}{source_tag(r['source'])})"
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

    elif ml == "UTILITY":
        out += _md_duo_support(a)
    else:
        out += _md_own_pairs(a)

    out += _md_lines(a)
    out += _md_type_pairs(a)
    out += _md_plans(a)

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
    if ml != "UTILITY":                 # a support does not build armor penetration
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
                       f"{source_tag(x['adc_source'])}) · puntaje {x['score']}")
            out += [f"   - {t}" for t in x["reasons"]]
    elif draft is not None and (lineup or notes):
        out += ["", f"## Draft como {role_name(ml)}", ""]
        if lineup:
            out.append("Alineación: " + _lineup_text([(p, r) for r, p in lineup.items()]) + ".")
        out += [f"- {n}" for n in notes or []]
    if draft is not None:
        out += _md_draft(draft)
    out += ["", "## Sobre las clasificaciones", "", CLASSIFICATION_NOTE]
    return "\n".join(out) + "\n"


def render_summary(a: dict, ranking=None, lineup=None, notes=None, cola_text="todas las colas",
                   draft=None) -> str:
    """Concise terminal summary: top duos, matrix and recommendations for the common lineup."""
    mg = a["min_games"]
    ml = a.get("main_role", "BOTTOM")
    out = [f"Composiciones ({cola_text}): {a['games']} partidas, {a.get('own_games', a['bottom_games'])} "
           f"como {role_name(ml)}, {a['clan_games']} con otro miembro del clan. (* = menos de {mg} partidas)"]
    if ml == "BOTTOM":
        out += _summary_duo_adc(a)
    elif ml == "UTILITY":
        out += _summary_duo_support(a)
    else:
        out += _summary_own_pairs(a)
    out += _summary_lines(a)
    out += _summary_pairs(a)
    out += _summary_plans(a)
    if ranking is not None or (draft is not None and ml != "BOTTOM"):
        out += ["", "Alineación más común del clan: " + (_lineup_text([(p, r) for r, p in lineup.items()])
                                                         if lineup else "(ninguna)")]
        out += [f"  {n}" for n in notes or []]
    if ranking is not None:
        out.append("Recomendación de ADC:")
        if not ranking:
            out.append("  Ningún ADC con suficientes partidas.")
        for i, x in enumerate(ranking[:4], 1):
            out.append(f"  {i}. {x['champion']} (puntaje {x['score']})")
            out += [f"     - {t}" for t in x["reasons"]]
    if draft is not None:
        out += ["", *_summary_draft(draft)]
    return "\n".join(out)
