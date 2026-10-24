"""Analysis over all analyzable games: summaries, win/loss drivers, report cards, trends."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from .metrics import extract_game_metrics

MIN_CHAMPION_GAMES = 5
MIN_REFERENCE_GAMES = 5
SESSION_GAP_MIN = 45

# Report-card thresholds
STRONG_PCT = 75
WEAK_PCT = 25
GOLD_DIFF_14_THRESHOLD = 500
CS_DIFF_14_THRESHOLD = 15
XP_DIFF_14_THRESHOLD = 500
CS_PER_MIN_GAP = 1.0
EARLY_DEATHS_WEAK = 2
SOLO_DEATHS_WEAK = 2
SOLO_KILLS_STRONG = 2
# Support report-card thresholds
VISION_PER_MIN_GAP = 0.5
SUPPORT_KP_STRONG = 0.75
SUPPORT_KP_WEAK = 0.40
SAVES_STRONG = 2
NO_CONTROL_WARD_MIN = 15
# Jungle report-card thresholds
ENEMY_JUNGLE_STRONG = 12
ENEMY_JUNGLE_WEAK = 20          # what the enemy jungler took from YOUR jungle
EARLY_KA_STRONG = 7
EARLY_KA_WEAK = 1
LANE_ROLES = ("TOP", "MIDDLE", "BOTTOM")
ROLE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")    # same order as comps.ROLES

# key -> (Spanish label, good direction (+1 higher is better, -1 lower), format kind)
METRICS = {
    "kda": ("KDA", 1, "num2"),
    "kills": ("Kills", 1, "num1"),
    "deaths": ("Muertes", -1, "num1"),
    "assists": ("Asistencias", 1, "num1"),
    "deaths_per_10": ("Muertes por 10 min", -1, "num2"),
    "cs_per_min": ("CS por minuto", 1, "num2"),
    "gold_per_min": ("Oro por minuto", 1, "int"),
    "damage_per_min": ("Daño por minuto", 1, "int"),
    "damage_share": ("% del daño del equipo", 1, "pct"),
    "kp": ("Participación en kills", 1, "pct"),
    "vision_per_min": ("Visión por minuto", 1, "num2"),
    "control_wards": ("Wards de control", 1, "num1"),
    "solo_kills": ("Solo kills", 1, "num1"),
    "solo_deaths": ("Muertes en solitario", -1, "num1"),
    "early_deaths": ("Muertes antes del min 14", -1, "num1"),
    "deaths_enemy_half": ("Muertes en mitad enemiga", -1, "num1"),
    "cs_first10": ("CS en los primeros 10 min", 1, "num1"),
    "max_cs_adv": ("Ventaja máxima de CS en línea", 1, "num1"),
    "cs_diff_7": ("Dif. de CS @7", 1, "diff"),
    "cs_diff_14": ("Dif. de CS @14", 1, "diff"),
    "gold_diff_7": ("Dif. de oro @7", 1, "diff"),
    "gold_diff_14": ("Dif. de oro @14", 1, "diff"),
    "gold_diff_21": ("Dif. de oro @21", 1, "diff"),
    "xp_diff_14": ("Dif. de XP @14", 1, "diff"),
    "gold_diff_end": ("Dif. de oro final", 1, "diff"),
    "cs_diff_end": ("Dif. de CS final", 1, "diff"),
    "skillshots_dodged": ("Habilidades esquivadas", 1, "num1"),
    "skillshots_hit": ("Habilidades acertadas", 1, "num1"),
    "first_blood": ("Participación en primera sangre", 1, "pct"),
    # support
    "assists_per_10": ("Asistencias por 10 min", 1, "num1"),
    "wards_per_min": ("Wards puestos por minuto", 1, "num2"),
    "ward_takedowns_per_min": ("Wards destruidos por minuto", 1, "num2"),
    "heal_shield_per_min": ("Curación y escudos por minuto", 1, "int"),
    "cc_time_per_min": ("Tiempo de control por minuto (s)", 1, "num2"),
    "immobilizations_per_10": ("Inmovilizaciones por 10 min", 1, "num1"),
    "saves": ("Aliados salvados", 1, "num1"),
    "support_quest": ("Misión de soporte a tiempo", 1, "yesno"),
    # jungle
    "jungle_cs_10": ("CS de jungla antes del min 10", 1, "num1"),
    "enemy_jungle_cs": ("Monstruos de la jungla rival", 1, "num1"),
    "scuttle_crabs": ("Cangrejos", 1, "num1"),
    "epic_takedowns": ("Dragones, heraldos y barones", 1, "num1"),
    "epic_steals": ("Objetivos épicos robados", 1, "num1"),
    "ka_15": ("Kills y asistencias antes del min 15", 1, "num1"),
    "early_ka": ("Kills y asistencias antes del min 14", 1, "num1"),
}

# Metrics evaluated for "what decides your games" on a laner's champion (see LINE_PROFILES).
INSIGHT_METRICS = [
    "kda", "kills", "deaths", "assists", "deaths_per_10", "cs_per_min", "gold_per_min",
    "damage_per_min", "damage_share", "kp", "vision_per_min", "control_wards", "solo_kills",
    "solo_deaths", "early_deaths", "deaths_enemy_half", "cs_first10", "max_cs_adv",
    "cs_diff_7", "cs_diff_14", "gold_diff_7", "gold_diff_14", "xp_diff_14",
    "skillshots_dodged", "skillshots_hit", "first_blood",
]

# Metrics shown on a laner's report card; value is compared with the lane opponent when
# an opponent counterpart exists.
CARD_METRICS = [
    ("kda", "opp_kda"),
    ("deaths_per_10", None),
    ("cs_per_min", "opp_cs_per_min"),
    ("gold_per_min", "opp_gold_per_min"),
    ("damage_per_min", "opp_damage_per_min"),
    ("damage_share", None),
    ("kp", None),
    ("vision_per_min", "opp_vision_per_min"),
    ("cs_diff_14", None),
    ("gold_diff_14", None),
    ("xp_diff_14", None),
    ("early_deaths", None),
    ("solo_deaths", None),
    ("deaths_enemy_half", None),
    ("solo_kills", None),
]

# Short headers of the stats shown in tables and tiles.
COLUMN_LABELS = {
    "kda": "KDA", "cs_per_min": "CS/min", "deaths_per_10": "Muertes/10 min", "gold_diff_14": "Dif. oro @14",
    "vision_per_min": "Visión/min", "kp": "Particip. en kills", "heal_shield_per_min": "Curación y escudos/min",
    "jungle_cs_10": "CS de jungla @10", "epic_takedowns": "Objetivos épicos", "wards_per_min": "Wards/min",
}
# The stat that tells a clan member's line apart from a laner's (the clan table has CS/min and Visión/min).
CLAN_LINE_STAT = {"support": "heal_shield_per_min", "jungle": "jungle_cs_10"}

QUEUE_NAMES = {
    400: "Normal (draft)",
    420: "Clasificatoria Solo/Dúo",
    430: "Normal (a ciegas)",
    440: "Clasificatoria Flexible",
    490: "Partida rápida",
}
ROLE_NAMES = {
    "TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Soporte",
    "": "Sin rol",
}
WEEKDAY_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def queue_name(queue_id) -> str:
    return QUEUE_NAMES.get(queue_id, f"Cola {queue_id}")


def role_name(role) -> str:
    return ROLE_NAMES.get(role or "", role or "Sin rol")


# ---------------------------------------------------------------- formatting
def fmt_value(key: str, value) -> str:
    if value is None:
        return "-"
    kind = METRICS.get(key, ("", 1, "num2"))[2]
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "int":
        return f"{value:.0f}"
    if kind == "diff":
        return f"{value:+.0f}"
    if kind == "num1":
        return f"{value:.1f}"
    if kind == "yesno":
        return "Sí" if value else "No"
    return f"{value:.2f}"


def fmt_pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def fmt_minutes(duration_min) -> str:
    if duration_min is None:
        return "-"
    total = int(round(duration_min * 60))
    return f"{total // 60}:{total % 60:02d}"


# ------------------------------------------------------------ stat helpers
def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(values):
    vals = [v for v in values if v is not None]
    return statistics.stdev(vals) if len(vals) >= 2 else None


def effect_size(win_values, loss_values):
    """(mean_win - mean_loss) / pooled std. None if a group has < 2 values or no spread."""
    a = [v for v in win_values if v is not None]
    b = [v for v in loss_values if v is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled == 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / pooled


def percentile_rank(value, population):
    """Percentage (0-100) of `population` below `value`; ties count half (mid-rank)."""
    vals = [v for v in population if v is not None]
    if value is None or not vals:
        return None
    less = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return (less + 0.5 * equal) / len(vals) * 100.0


def normalize_name(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


# ---------------------------------------------------------------- loading
def add_session_info(games) -> None:
    """Annotate games (sorted by time) with session_id / session_game.

    A session is a run of games where each starts less than SESSION_GAP_MIN minutes
    after the previous one ended.
    """
    session_id = 0
    position = 0
    prev_end = None
    for g in games:
        start = g.get("game_creation")
        gap_min = None
        if prev_end is not None and start is not None:
            gap_min = (start - prev_end) / 60000.0
        if prev_end is None or gap_min is None or gap_min >= SESSION_GAP_MIN:
            session_id += 1
            position = 1
        else:
            position += 1
        g["session_id"] = session_id
        g["session_game"] = position
        if start is not None:
            prev_end = start + (g.get("duration_s") or 0) * 1000


def load_games(store, puuid: str, include_all_queues: bool = False, queue: int | None = None,
               on_match=None):
    """Flat metric rows for every analyzable game of `puuid`, oldest first (optionally one queue).

    The player is always explicit: nothing here guesses whose games to analyze. Only that
    player's matches are read from the store. `on_match(row, match, timeline)`, if given, is called
    for every kept game while its JSON is already parsed (so extra analyses need no second parse).
    """
    if not puuid:
        return []
    games = []
    for _match_id, match, timeline in store.iter_matches(puuid):
        row = extract_game_metrics(match, timeline, puuid)
        if row is None or row["remake"]:
            continue
        if not include_all_queues and not row["analyzable"]:
            continue
        if queue is not None and row["queue_id"] != queue:
            continue
        if on_match is not None:
            on_match(row, match, timeline)
        games.append(row)
    games.sort(key=lambda g: (g.get("game_creation") or 0, str(g.get("match_id"))))
    add_session_info(games)
    return games


# ----------------------------------------------------------------- summary
# Averaged stats that only mean something in some lines: the games of other roles are left out of
# the mean (so CS/min is never averaged with a support's, nor the jungle's CS@10 with a laner's).
# The role "" (no position in the match) counts as a laner, like everywhere else.
_LANERS = LANE_ROLES + ("",)
STAT_ROLES = {
    "cs_per_min": _LANERS, "cs_diff_14": _LANERS,
    "gold_diff_14": _LANERS + ("JUNGLE",), "xp_diff_14": _LANERS + ("JUNGLE",),
    "wards_per_min": ("UTILITY",), "ward_takedowns_per_min": ("UTILITY",),
    "heal_shield_per_min": ("UTILITY",), "cc_time_per_min": ("UTILITY",),
    "immobilizations_per_10": ("UTILITY",), "saves": ("UTILITY",), "control_wards": ("UTILITY", "JUNGLE"),
    "jungle_cs_10": ("JUNGLE",), "enemy_jungle_cs": ("JUNGLE",), "scuttle_crabs": ("JUNGLE",),
    "epic_takedowns": ("JUNGLE",), "epic_steals": ("JUNGLE",), "ka_15": ("JUNGLE",), "early_ka": ("JUNGLE",),
}


def group_stats(games) -> dict:
    n = len(games)
    wins = sum(g["win"] for g in games)
    deaths = sum(g["deaths"] for g in games)
    out = {
        "games": n,
        "wins": wins,
        "winrate": wins / n if n else None,
        "kda": (sum(g["kills"] + g["assists"] for g in games) / max(1, deaths)) if n else None,
        "gold_per_min": mean(g.get("gold_per_min") for g in games),
        "deaths_per_10": mean(g.get("deaths_per_10") for g in games),
        "kp": mean(g.get("kp") for g in games),
        "vision_per_min": mean(g.get("vision_per_min") for g in games),
        "assists_per_10": mean(g.get("assists_per_10") for g in games),
    }
    for key, roles in STAT_ROLES.items():
        out[key] = mean(g.get(key) for g in games if (g.get("role") or "") in roles)
    return out


def _grouped(games, keyfunc, labelfunc=str):
    buckets = defaultdict(list)
    for g in games:
        buckets[keyfunc(g)].append(g)
    rows = []
    for key, gs in buckets.items():
        row = group_stats(gs)
        row["key"] = key
        row["label"] = labelfunc(key)
        rows.append(row)
    rows.sort(key=lambda r: (-r["games"], str(r["label"])))
    return rows


def summarize(games) -> dict:
    return {
        "overall": group_stats(games),
        "by_queue": _grouped(games, lambda g: g.get("queue_id"), queue_name),
        "by_role": _grouped(games, lambda g: g.get("role") or "", role_name),
        "by_champion": _grouped(games, lambda g: g.get("champion") or "?"),
    }


# ------------------------------------------------- per-champion win drivers
def games_for_champion(games, champion):
    wanted = normalize_name(champion)
    return [g for g in games if normalize_name(g.get("champion")) == wanted]


def champion_insights(games, champion, min_games=MIN_CHAMPION_GAMES, top_n=5) -> dict:
    gs = games_for_champion(games, champion)
    wins = [g for g in gs if g["win"]]
    losses = [g for g in gs if not g["win"]]
    result = {
        "champion": gs[0]["champion"] if gs else champion,
        "games": len(gs),
        "wins": len(wins),
        "losses": len(losses),
        "stats": group_stats(gs) if gs else None,
        "enough": len(gs) >= min_games and len(wins) >= 2 and len(losses) >= 2,
        "higher_in_wins": [],
        "higher_in_losses": [],
    }
    if not result["enough"]:
        return result
    entries = []
    for key in profile_for_games(gs).insights:
        wv = [g.get(key) for g in wins]
        lv = [g.get(key) for g in losses]
        d = effect_size(wv, lv)
        if d is None:
            continue
        entries.append({
            "key": key, "label": METRICS[key][0], "effect": d,
            "mean_win": mean(wv), "mean_loss": mean(lv),
        })
    result["higher_in_wins"] = sorted(
        (e for e in entries if e["effect"] > 0), key=lambda e: -e["effect"])[:top_n]
    result["higher_in_losses"] = sorted(
        (e for e in entries if e["effect"] < 0), key=lambda e: e["effect"])[:top_n]
    return result


# ------------------------------------------------------------- report card
def _reference_values(game, games, key):
    """(values, scope, n): player's own distribution on the same champion, else role, else all.

    Support and jungle games are only compared with games of the same role (their metrics mean
    nothing for a laner), and never fall back to "all games".
    """
    others = [g for g in games if g is not game and g.get("match_id") != game.get("match_id")]
    laner = profile_for_role(game.get("role")).key == "lane"
    if not laner:
        others = [g for g in others if g.get("role") == game.get("role")]
    scopes = [
        ("campeón", [g for g in others if g.get("champion") == game.get("champion")]),
        ("rol", [g for g in others if game.get("role") and g.get("role") == game.get("role")]),
    ]
    if laner:
        scopes.append(("todas las partidas", others))
    for scope, pool in scopes:
        vals = [g.get(key) for g in pool if g.get(key) is not None]
        if len(vals) >= MIN_REFERENCE_GAMES:
            return vals, scope, len(vals)
    return [], None, 0


# ------------------------------------------------------------ strength / weakness rules
# Each rule function reads a game row and fills the {key: text} dicts of strengths / weaknesses.
def _rules_lane(g, strengths, weaknesses) -> None:
    gd = g.get("gold_diff_14")
    if gd is not None:
        if gd >= GOLD_DIFF_14_THRESHOLD:
            strengths["gold_diff_14"] = f"Ventaja de oro de {gd:+.0f} al minuto 14 sobre tu rival de línea."
        elif gd <= -GOLD_DIFF_14_THRESHOLD:
            weaknesses["gold_diff_14"] = f"Desventaja de oro de {gd:+.0f} al minuto 14 frente a tu rival de línea."
    cd = g.get("cs_diff_14")
    if cd is not None:
        if cd >= CS_DIFF_14_THRESHOLD:
            strengths["cs_diff_14"] = f"Llevabas {cd:+.0f} CS sobre tu rival al minuto 14."
        elif cd <= -CS_DIFF_14_THRESHOLD:
            weaknesses["cs_diff_14"] = f"Estabas {cd:+.0f} CS contra tu rival al minuto 14."
    else:
        mine, theirs = g.get("cs_per_min"), g.get("opp_cs_per_min")
        if mine is not None and theirs is not None:
            if mine - theirs >= CS_PER_MIN_GAP:
                strengths["cs_per_min"] = f"Farmeaste {mine - theirs:+.1f} CS/min más que tu rival."
            elif theirs - mine >= CS_PER_MIN_GAP:
                weaknesses["cs_per_min"] = f"Farmeaste {mine - theirs:+.1f} CS/min frente a tu rival."
    xd = g.get("xp_diff_14")
    if xd is not None:
        if xd >= XP_DIFF_14_THRESHOLD:
            strengths["xp_diff_14"] = f"Ventaja de experiencia de {xd:+.0f} al minuto 14."
        elif xd <= -XP_DIFF_14_THRESHOLD:
            weaknesses["xp_diff_14"] = f"Desventaja de experiencia de {xd:+.0f} al minuto 14."


def _rules_support(g, strengths, weaknesses) -> None:
    vision, theirs = g.get("vision_per_min"), g.get("opp_vision_per_min")
    if vision is not None and theirs is not None:
        if vision - theirs >= VISION_PER_MIN_GAP:
            strengths["vision_per_min"] = (
                f"Visión de {vision:.1f}/min, {vision - theirs:+.1f} sobre el soporte rival.")
        elif theirs - vision >= VISION_PER_MIN_GAP:
            weaknesses["vision_per_min"] = (
                f"Visión de {vision:.1f}/min, {vision - theirs:+.1f} frente al soporte rival.")
    kp = g.get("kp")
    if kp is not None:
        if kp >= SUPPORT_KP_STRONG:
            strengths["kp"] = f"Participaste en el {kp * 100:.0f}% de las kills de tu equipo."
        elif kp <= SUPPORT_KP_WEAK:
            weaknesses["kp"] = f"Solo participaste en el {kp * 100:.0f}% de las kills de tu equipo."
    saves = g.get("saves")
    if saves is not None and saves >= SAVES_STRONG:
        strengths["saves"] = f"Salvaste a un aliado de morir {saves:.0f} veces."
    if g.get("control_wards") == 0 and (g.get("duration_min") or 0) >= NO_CONTROL_WARD_MIN:
        weaknesses["control_wards"] = "No pusiste ningún ward de control."
    if g.get("support_quest") == 0:
        weaknesses["support_quest"] = "No completaste a tiempo la misión de soporte."


def _rules_jungle(g, strengths, weaknesses) -> None:
    gd = g.get("gold_diff_14")
    if gd is not None:
        if gd >= GOLD_DIFF_14_THRESHOLD:
            strengths["gold_diff_14"] = f"Ventaja de oro de {gd:+.0f} al minuto 14 frente al jungla rival."
        elif gd <= -GOLD_DIFF_14_THRESHOLD:
            weaknesses["gold_diff_14"] = f"Desventaja de oro de {gd:+.0f} al minuto 14 frente al jungla rival."
    xd = g.get("xp_diff_14")
    if xd is not None:
        if xd >= XP_DIFF_14_THRESHOLD:
            strengths["xp_diff_14"] = f"Ventaja de experiencia de {xd:+.0f} al minuto 14 frente al jungla rival."
        elif xd <= -XP_DIFF_14_THRESHOLD:
            weaknesses["xp_diff_14"] = f"Desventaja de experiencia de {xd:+.0f} al minuto 14 frente al jungla rival."
    stolen = g.get("enemy_jungle_cs")
    if stolen is not None and stolen >= ENEMY_JUNGLE_STRONG:
        strengths["enemy_jungle_cs"] = f"Robaste {stolen:.0f} monstruos de la jungla rival."
    taken = g.get("opp_enemy_jungle_cs")
    if taken is not None and taken >= ENEMY_JUNGLE_WEAK:
        weaknesses["enemy_jungle_cs"] = f"El jungla rival te robó {taken:.0f} monstruos de tu jungla."
    ka, minute = g.get("early_ka"), 14
    if ka is None:
        ka, minute = g.get("ka_15"), 15
    if ka is not None:
        if ka >= EARLY_KA_STRONG:
            strengths["early_ka"] = f"Participaste en {ka:.0f} kills antes del minuto {minute}."
        elif ka <= EARLY_KA_WEAK and (g.get("duration_min") or 0) >= minute:
            weaknesses["early_ka"] = (f"Solo participaste en {ka:.0f} kill antes del minuto {minute}."
                                      if ka else f"No participaste en ninguna kill antes del minuto {minute}.")
    steals = g.get("epic_steals")
    if steals:
        strengths["epic_steals"] = f"Robaste {steals:.0f} {'objetivo épico' if steals == 1 else 'objetivos épicos'}."


def _rules_common(g, strengths, weaknesses) -> None:
    early = g.get("early_deaths")
    if early is not None:
        if early >= EARLY_DEATHS_WEAK:
            weaknesses["early_deaths"] = f"{early} muertes antes del minuto 14."
        elif early == 0 and (g.get("duration_min") or 0) >= 14:
            strengths["early_deaths"] = "Sin muertes antes del minuto 14."
    solo = g.get("solo_deaths")
    if solo is not None and solo >= SOLO_DEATHS_WEAK:
        weaknesses["solo_deaths"] = f"{solo} muertes en solitario (sin asistencias del equipo rival)."
    sk = g.get("solo_kills")
    if sk is not None and sk >= SOLO_KILLS_STRONG:
        strengths["solo_kills"] = f"{sk:.0f} solo kills."
    eh, deaths = g.get("deaths_enemy_half"), g.get("deaths") or 0
    if eh is not None and (eh >= 3 or (eh >= 2 and deaths and eh / deaths >= 0.6)):
        what = "invasiones o sobreextensión" if g.get("role") == "JUNGLE" else "sobreextensión"
        weaknesses["deaths_enemy_half"] = (
            f"{eh} de tus {deaths} muertes fueron en la mitad del mapa enemiga ({what}).")
    if g.get("first_blood") == 1 and g.get("has_timeline"):
        strengths["first_blood"] = "Participaste en la primera sangre."


# ----------------------------------------------------------------- line profiles
@dataclass(frozen=True)
class LineProfile:
    """What is measured and judged for one kind of line (a single declarative table below)."""
    key: str
    label: str
    rival: str              # who the opponent column / sentence refers to
    card: tuple             # report card rows: (metric, opponent's counterpart or None)
    neutral: frozenset      # card rows shown for information, with no strength / weakness verdict
    insights: tuple         # metrics ranked in "what decides your games" on a champion
    rules: tuple            # strength / weakness rule functions, in the order their texts are listed
    columns: tuple          # stats of the group tables (by role / queue / champion)
    game_cols: tuple        # stats shown on each row of a games list
    tiles: tuple            # summary tiles between KDA and deaths per 10 min
    averages: tuple         # rows of the champion page's averages table
    note: str = ""          # one line about what this line leaves out of the numbers


_COMMON_INSIGHTS = ("kda", "deaths", "assists", "deaths_per_10", "kp", "vision_per_min", "control_wards",
                    "solo_deaths", "early_deaths", "deaths_enemy_half", "skillshots_dodged",
                    "skillshots_hit", "first_blood")
LINE_PROFILES = {
    # Top, mid and ADC (and games without a position): the laner's yardstick.
    "lane": LineProfile(
        key="lane", label="Carril", rival="Rival de línea",
        card=tuple(CARD_METRICS), neutral=frozenset(), insights=tuple(INSIGHT_METRICS),
        rules=(_rules_lane, _rules_common),
        columns=("kda", "cs_per_min", "deaths_per_10", "gold_diff_14"),
        game_cols=("cs_per_min", "gold_diff_14"),
        tiles=("cs_per_min",),
        averages=("kda", "cs_per_min", "gold_per_min", "deaths_per_10", "kp", "vision_per_min",
                  "gold_diff_14", "cs_diff_14", "xp_diff_14")),
    "support": LineProfile(
        key="support", label="Soporte", rival="Soporte rival",
        card=(("kda", "opp_kda"), ("deaths_per_10", None), ("kp", None), ("assists_per_10", None),
              ("vision_per_min", "opp_vision_per_min"), ("wards_per_min", "opp_wards_per_min"),
              ("ward_takedowns_per_min", "opp_ward_takedowns_per_min"), ("control_wards", None),
              ("heal_shield_per_min", "opp_heal_shield_per_min"), ("cc_time_per_min", "opp_cc_time_per_min"),
              ("immobilizations_per_10", None), ("saves", None), ("support_quest", None),
              ("early_deaths", None), ("solo_deaths", None), ("deaths_enemy_half", None),
              ("damage_per_min", "opp_damage_per_min"), ("damage_share", None)),
        neutral=frozenset({"damage_per_min", "damage_share"}),
        insights=("kda", "deaths", "assists", "assists_per_10", "deaths_per_10", "kp", "vision_per_min",
                  "wards_per_min", "ward_takedowns_per_min", "control_wards", "heal_shield_per_min",
                  "cc_time_per_min", "immobilizations_per_10", "saves", "solo_deaths", "early_deaths",
                  "deaths_enemy_half", "skillshots_dodged", "skillshots_hit", "first_blood"),
        rules=(_rules_support, _rules_common),
        columns=("kda", "vision_per_min", "kp", "heal_shield_per_min", "deaths_per_10"),
        game_cols=("vision_per_min", "kp", "heal_shield_per_min"),
        tiles=("vision_per_min", "kp"),
        averages=("kda", "assists_per_10", "kp", "vision_per_min", "wards_per_min", "ward_takedowns_per_min",
                  "control_wards", "heal_shield_per_min", "cc_time_per_min", "immobilizations_per_10",
                  "saves", "deaths_per_10"),
        note="En soporte no se juzgan CS ni daño: se miden visión, participación, curación y control."),
    "jungle": LineProfile(
        key="jungle", label="Jungla", rival="Jungla rival",
        card=(("kda", "opp_kda"), ("deaths_per_10", None), ("kp", None),
              ("jungle_cs_10", "opp_jungle_cs_10"), ("enemy_jungle_cs", "opp_enemy_jungle_cs"),
              ("scuttle_crabs", "opp_scuttle_crabs"), ("epic_takedowns", "opp_epic_takedowns"),
              ("epic_steals", None), ("ka_15", None), ("early_ka", None), ("first_blood", None),
              ("gold_per_min", "opp_gold_per_min"), ("damage_per_min", "opp_damage_per_min"),
              ("vision_per_min", "opp_vision_per_min"), ("gold_diff_14", None), ("xp_diff_14", None),
              ("early_deaths", None), ("solo_deaths", None), ("deaths_enemy_half", None)),
        neutral=frozenset(),
        insights=("kda", "kills", "deaths", "assists", "deaths_per_10", "gold_per_min", "damage_per_min",
                  "damage_share", "kp", "vision_per_min", "control_wards", "solo_kills", "solo_deaths",
                  "early_deaths", "deaths_enemy_half", "jungle_cs_10", "enemy_jungle_cs", "scuttle_crabs",
                  "epic_takedowns", "epic_steals", "ka_15", "early_ka", "gold_diff_7", "gold_diff_14",
                  "xp_diff_14", "skillshots_dodged", "skillshots_hit", "first_blood"),
        rules=(_rules_jungle, _rules_common),
        columns=("kda", "jungle_cs_10", "epic_takedowns", "kp", "gold_diff_14", "deaths_per_10"),
        game_cols=("jungle_cs_10", "kp", "gold_diff_14"),
        tiles=("jungle_cs_10", "epic_takedowns"),
        averages=("kda", "jungle_cs_10", "enemy_jungle_cs", "scuttle_crabs", "epic_takedowns", "ka_15",
                  "gold_per_min", "deaths_per_10", "kp", "vision_per_min", "gold_diff_14", "xp_diff_14"),
        note="En jungla no se juzgan CS por minuto ni diferencias de CS: el rival es el jungla enemigo."),
    # Several kinds of line in the same list: only what means the same in all of them.
    "mixed": LineProfile(
        key="mixed", label="Todas las líneas", rival="Rival de línea",
        card=(("kda", "opp_kda"), ("deaths_per_10", None), ("kp", None),
              ("vision_per_min", "opp_vision_per_min"), ("early_deaths", None), ("solo_deaths", None),
              ("deaths_enemy_half", None)),
        neutral=frozenset(), insights=_COMMON_INSIGHTS, rules=(_rules_common,),
        columns=("kda", "cs_per_min", "vision_per_min", "kp", "deaths_per_10", "gold_diff_14"),
        game_cols=("cs_per_min", "vision_per_min", "kp"),
        tiles=("kp",),
        averages=("kda", "kp", "vision_per_min", "deaths_per_10"),
        note="Mezcla líneas distintas: CS por minuto y diferencias de línea solo cuentan las partidas de carril."),
}
_PROFILE_OF_ROLE = {"JUNGLE": "jungle", "UTILITY": "support"}


def profile_for_role(role) -> LineProfile:
    return LINE_PROFILES[_PROFILE_OF_ROLE.get(role or "", "lane")]


def profile_for_games(games) -> LineProfile:
    """The profile every game shares, or the mixed one when the list spans several kinds of line."""
    keys = {profile_for_role(g.get("role")).key for g in games}
    if not keys:
        return LINE_PROFILES["lane"]
    return LINE_PROFILES[keys.pop()] if len(keys) == 1 else LINE_PROFILES["mixed"]


def main_line_of(games) -> str | None:
    """The role played most in `games` (ties go to the earlier one of ROLE_ORDER); None without games."""
    counts = Counter(g["role"] for g in games if g.get("role") in ROLE_ORDER)
    if not counts:
        return None
    return max(counts, key=lambda role: (counts[role], -ROLE_ORDER.index(role)))


def row_stat(game, key):
    """A game's value of `key`, or None where the stat means nothing for the role it was played in
    (CS/min of a support, jungle CS@10 of a laner)."""
    roles = STAT_ROLES.get(key)
    if roles is not None and (game.get("role") or "") not in roles:
        return None
    return game.get(key)


def games_in_line(games, line):
    """The games played in `line` (a role), or all of them when `line` is None ("todas")."""
    return list(games) if line is None else [g for g in games if g.get("role") == line]


def resolve_line(choice, games) -> str | None:
    """The role an aggregate page shows: `choice` is "auto" (the one played most in `games`), "todas"
    (None: every game) or a role."""
    if choice == "todas":
        return None
    return main_line_of(games) if choice in (None, "auto") else choice


def _rule_flags(g) -> tuple[dict, dict]:
    """Hard-coded strength/weakness rules of the game's line; returns ({key: text}, {key: text})."""
    strengths, weaknesses = {}, {}
    for rule in profile_for_role(g.get("role")).rules:
        rule(g, strengths, weaknesses)
    return strengths, weaknesses


def game_report_card(game, games) -> dict:
    profile = profile_for_role(game.get("role"))
    rows = []
    for key, opp_key in profile.card:
        value = game.get(key)
        if value is None:
            continue
        label, direction, _kind = METRICS[key]
        neutral = key in profile.neutral
        ref, scope, n = ([], None, 0) if neutral else _reference_values(game, games, key)
        pct = percentile_rank(value, ref) if ref else None
        goodness = None if pct is None else (pct if direction > 0 else 100 - pct)
        verdict = None
        if goodness is not None:
            verdict = "fortaleza" if goodness >= STRONG_PCT else "debilidad" if goodness <= WEAK_PCT else "normal"
        opp_value = game.get(opp_key) if opp_key else None
        rows.append({
            "key": key, "label": label, "value": value, "fmt": fmt_value(key, value),
            "opp_value": opp_value, "opp_fmt": fmt_value(key, opp_value) if opp_value is not None else "-",
            "percentile": pct, "goodness": goodness, "scope": scope, "n": n, "verdict": verdict,
            "neutral": neutral,
        })

    rule_strengths, rule_weaknesses = _rule_flags(game)
    strengths = [{"key": k, "label": METRICS[k][0], "text": t} for k, t in rule_strengths.items()]
    weaknesses = [{"key": k, "label": METRICS[k][0], "text": t} for k, t in rule_weaknesses.items()]

    flagged = set(rule_strengths) | set(rule_weaknesses)
    pct_strong = sorted((r for r in rows if r["verdict"] == "fortaleza" and r["key"] not in flagged),
                        key=lambda r: -r["goodness"])[:4]
    pct_weak = sorted((r for r in rows if r["verdict"] == "debilidad" and r["key"] not in flagged),
                      key=lambda r: r["goodness"])[:4]
    for r in pct_strong:
        strengths.append({"key": r["key"], "label": r["label"], "text": _pct_text(r)})
    for r in pct_weak:
        weaknesses.append({"key": r["key"], "label": r["label"], "text": _pct_text(r)})

    card = {
        "match_id": game.get("match_id"),
        "champion": game.get("champion"),
        "role": game.get("role"),
        "win": bool(game.get("win")),
        "date": game.get("date"),
        "queue_id": game.get("queue_id"),
        "duration_min": game.get("duration_min"),
        "opp_champion": game.get("opp_champion"),
        "rival_label": profile.rival,
        "kda_text": f"{game['kills']}/{game['deaths']}/{game['assists']}",
        "has_timeline": bool(game.get("has_timeline")),
        "rows": rows,
        "strengths": strengths,
        "weaknesses": weaknesses,
    }
    card["text"] = render_card_text(card)
    return card


def _pct_text(r) -> str:
    # `goodness` is the percentile in the "good" direction, so the wording is the same
    # for metrics where lower is better.
    scope = {
        "campeón": "tus partidas con este campeón",
        "rol": "tus partidas en el mismo rol",
    }.get(r["scope"], "todas tus partidas")
    if r["goodness"] >= 50:
        verdict, pct = "mejor", r["goodness"]
    else:
        verdict, pct = "peor", 100 - r["goodness"]
    return f"{r['label']}: {r['fmt']} ({verdict} que el {pct:.0f}% de {scope})."


def render_card_text(card) -> str:
    res = "Victoria" if card["win"] else "Derrota"
    lines = [
        f"Partida {card['match_id']} - {card['champion']} ({role_name(card['role'])}) - {res} - "
        f"{fmt_minutes(card['duration_min'])} - KDA {card['kda_text']}",
    ]
    if card.get("opp_champion"):
        lines.append(f"{card['rival_label']}: {card['opp_champion']}")
    if not card["has_timeline"]:
        lines.append("Nota: esta partida no tiene timeline; faltan métricas por minuto y por fase.")
    lines.append("Fortalezas:")
    lines += [f"  - {s['text']}" for s in card["strengths"]] or ["  - (ninguna destacada)"]
    lines.append("Debilidades:")
    lines += [f"  - {w['text']}" for w in card["weaknesses"]] or ["  - (ninguna destacada)"]
    return "\n".join(lines)


# ------------------------------------------------------------------ trends
def trend_by_month(games) -> list[dict]:
    rows = _grouped([g for g in games if g.get("month")], lambda g: g["month"])
    rows.sort(key=lambda r: r["key"])
    return rows


def trend_by_weekday(games) -> list[dict]:
    rows = _grouped([g for g in games if g.get("weekday") is not None],
                    lambda g: g["weekday"], lambda k: WEEKDAY_NAMES[k])
    rows.sort(key=lambda r: r["key"])
    return rows


def trend_by_session_game(games, only=None) -> dict:
    """Winrate by game number within a session, and right after a win / a loss.

    `games` is the whole sequence the sessions are made of; `only` (a set of match ids) restricts
    what is counted to some of them (one line) without breaking who played right after whom.
    """
    capped = [g for g in games if g.get("session_game")]
    counted = [g for g in capped if only is None or g.get("match_id") in only]
    by_pos = _grouped(counted, lambda g: min(g["session_game"], 5),
                      lambda k: f"Partida {k}" if k < 5 else "Partida 5 o más")
    by_pos.sort(key=lambda r: r["key"])

    after_win, after_loss = [], []
    prev = None
    for g in capped:
        if (prev is not None and g["session_id"] == prev["session_id"]
                and (only is None or g.get("match_id") in only)):
            (after_win if prev["win"] else after_loss).append(g)
        prev = g
    return {
        "by_position": by_pos,
        "after_win": group_stats(after_win) if after_win else None,
        "after_loss": group_stats(after_loss) if after_loss else None,
        "sessions": len({g["session_id"] for g in counted}),
    }


# -------------------------------------------------------------------- clan
MIN_PAIR_GAMES = 3


def _most_common(values):
    """(value, count) of the most frequent non-empty value; ties go to the alphabetically first."""
    counts = Counter(v for v in values if v)
    if not counts:
        return None, 0
    value, n = min(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return value, n


def player_overview(games) -> dict:
    """One row of the clan comparison table for a player's games (in one queue)."""
    row = group_stats(games)
    row["solo_deaths_pg"] = mean(g.get("solo_deaths") for g in games)
    row["early_deaths_pg"] = mean(g.get("early_deaths") for g in games)
    role, role_games = _most_common(g.get("role") for g in games)
    champ, champ_games = _most_common(g.get("champion") for g in games)
    row.update({
        "top_role": role_name(role) if role else None, "top_role_games": role_games,
        "top_champion": champ, "top_champion_games": champ_games,
    })
    profile = profile_for_games(games)
    row["profile"] = profile.key
    key = CLAN_LINE_STAT.get(profile.key)
    row["line_stat"] = {"key": key, "label": COLUMN_LABELS[key], "value": row[key]} if key else None
    return row


def pair_stats(games_by_player: dict, min_games: int = MIN_PAIR_GAMES) -> list[dict]:
    """Games where two players were on the SAME team, for every pair with `min_games` or more.

    `games_by_player` maps a player key to that player's analyzable games (same queue filter for
    everyone). Rows carry match_id, team_id and win, so no match JSON is needed. Each pair row has
    `a`, `b` (the keys), `games`, `wins`, `winrate` together, and each player's overall winrate.
    """
    index = {k: {g["match_id"]: g for g in gs if g.get("match_id") is not None}
             for k, gs in games_by_player.items()}
    overall = {k: group_stats(gs)["winrate"] for k, gs in games_by_player.items()}
    keys = list(games_by_player)
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            together = [index[a][m] for m in index[a].keys() & index[b].keys()
                        if index[a][m].get("team_id") is not None
                        and index[a][m].get("team_id") == index[b][m].get("team_id")]
            if len(together) < min_games:
                continue
            wins = sum(g["win"] for g in together)
            pairs.append({"a": a, "b": b, "games": len(together), "wins": wins,
                          "winrate": wins / len(together),
                          "winrate_a": overall[a], "winrate_b": overall[b]})
    pairs.sort(key=lambda r: (-r["games"], str(r["a"]), str(r["b"])))
    return pairs
