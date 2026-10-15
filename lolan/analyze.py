"""Analysis over all analyzable games: summaries, win/loss drivers, report cards, trends."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict

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
}

# Metrics evaluated for "what decides your games" on a champion.
INSIGHT_METRICS = [
    "kda", "kills", "deaths", "assists", "deaths_per_10", "cs_per_min", "gold_per_min",
    "damage_per_min", "damage_share", "kp", "vision_per_min", "control_wards", "solo_kills",
    "solo_deaths", "early_deaths", "deaths_enemy_half", "cs_first10", "max_cs_adv",
    "cs_diff_7", "cs_diff_14", "gold_diff_7", "gold_diff_14", "xp_diff_14",
    "skillshots_dodged", "skillshots_hit", "first_blood",
]

# Metrics shown on a game's report card; value is compared with the lane opponent when
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
def group_stats(games) -> dict:
    n = len(games)
    wins = sum(g["win"] for g in games)
    deaths = sum(g["deaths"] for g in games)
    return {
        "games": n,
        "wins": wins,
        "winrate": wins / n if n else None,
        "kda": (sum(g["kills"] + g["assists"] for g in games) / max(1, deaths)) if n else None,
        "cs_per_min": mean(g.get("cs_per_min") for g in games),
        "gold_per_min": mean(g.get("gold_per_min") for g in games),
        "deaths_per_10": mean(g.get("deaths_per_10") for g in games),
        "kp": mean(g.get("kp") for g in games),
        "vision_per_min": mean(g.get("vision_per_min") for g in games),
        "gold_diff_14": mean(g.get("gold_diff_14") for g in games),
        "cs_diff_14": mean(g.get("cs_diff_14") for g in games),
        "xp_diff_14": mean(g.get("xp_diff_14") for g in games),
    }


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
    for key in INSIGHT_METRICS:
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
    """(values, scope, n): player's own distribution on the same champion, else role, else all."""
    others = [g for g in games if g is not game and g.get("match_id") != game.get("match_id")]
    scopes = (
        ("campeón", [g for g in others if g.get("champion") == game.get("champion")]),
        ("rol", [g for g in others if game.get("role") and g.get("role") == game.get("role")]),
        ("todas las partidas", others),
    )
    for scope, pool in scopes:
        vals = [g.get(key) for g in pool if g.get(key) is not None]
        if len(vals) >= MIN_REFERENCE_GAMES:
            return vals, scope, len(vals)
    return [], None, 0


def _rule_flags(g) -> tuple[dict, dict]:
    """Hard-coded strength/weakness rules; returns ({key: text}, {key: text})."""
    strengths, weaknesses = {}, {}

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
        weaknesses["deaths_enemy_half"] = (
            f"{eh} de tus {deaths} muertes fueron en la mitad del mapa enemiga (sobreextensión).")
    if g.get("first_blood") == 1 and g.get("has_timeline"):
        strengths["first_blood"] = "Participaste en la primera sangre."
    return strengths, weaknesses


def game_report_card(game, games) -> dict:
    rows = []
    for key, opp_key in CARD_METRICS:
        value = game.get(key)
        if value is None:
            continue
        label, direction, _kind = METRICS[key]
        ref, scope, n = _reference_values(game, games, key)
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
        lines.append(f"Rival de línea: {card['opp_champion']}")
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


def trend_by_session_game(games) -> dict:
    """Winrate by game number within a session, and right after a win / a loss."""
    capped = [g for g in games if g.get("session_game")]
    by_pos = _grouped(capped, lambda g: min(g["session_game"], 5),
                      lambda k: f"Partida {k}" if k < 5 else "Partida 5 o más")
    by_pos.sort(key=lambda r: r["key"])

    after_win, after_loss = [], []
    prev = None
    for g in capped:
        if prev is not None and g["session_id"] == prev["session_id"]:
            (after_win if prev["win"] else after_loss).append(g)
        prev = g
    return {
        "by_position": by_pos,
        "after_win": group_stats(after_win) if after_win else None,
        "after_loss": group_stats(after_loss) if after_loss else None,
        "sessions": len({g["session_id"] for g in capped}),
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
