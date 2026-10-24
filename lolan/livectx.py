"""Personal context for the live page: the player's own history next to what is happening now.

Everything here is a neutral comparison with the player's OWN stored games (averages, the goals of
their improvement plan). It never says what to do.

The reference depends on the role being played: a laner compares CS, a jungler compares CS and kill
participation (and the early gold against the enemy JUNGLER), a support compares vision and kill
participation (CS says nothing about a support). Those two roles never fall back to "all your games",
which would mix in laner averages.
"""
from __future__ import annotations

import time

from . import deaths as dx
from .ddragon import norm
from .live import ROLE_LABELS

MIN_BASELINE_GAMES = 3
EARLY_S = dx.EARLY_MS / 1000.0            # "antes del minuto 14"
BASELINE_MINUTES = (7, 14, 21)
REFRESH_S = 10.0

# Plan goals that can be counted from the live events (the others need the timeline of a finished game).
LIVE_PLAN = {
    "early_deaths": lambda d: d["t"] < EARLY_S,
    "after_kill": lambda d: d["after_own_kill"],
    "outnumbered": lambda d: d["involved"] >= 3,
    "jungler_early": lambda d: d["t"] < EARLY_S and d["jungler_involved"],
}
SCOPE_LABELS = {"champion": "con {champion}", "role": "con tu rol ({role})", "all": "en todas tus partidas"}
STRICT_ROLES = ("JUNGLE", "UTILITY")      # their averages only make sense among games in that same role
ROLE_MODES = ("CLASSIC", "PRACTICETOOL")  # modes with Rift roles, where the history can name the role


def _avg(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def history_role(games, champion_id) -> str | None:
    """The role the player most often plays: with this champion when there are MIN_BASELINE_GAMES games
    of it, else overall. For a game whose client does not say the position. None without history."""
    wanted = norm(champion_id)
    for rows in ([g for g in games if wanted and norm(g.get("champion")) == wanted], list(games)):
        counts: dict[str, int] = {}
        for g in rows:
            if g.get("role"):
                counts[g["role"]] = counts.get(g["role"], 0) + 1
        if sum(counts.values()) >= MIN_BASELINE_GAMES:
            return max(counts, key=lambda r: (counts[r], r))
    return None


def baselines(games, champion_id, role, champion_name="", role_label="", strict=False) -> dict | None:
    """The player's averages for this champion; with fewer than MIN_BASELINE_GAMES games, for the role;
    and failing that, for all their games. None when there is nothing to compare with.

    `strict` (jungle, support): only games played in `role` count, so there is no "all games" fallback
    and the champion's games are the ones played in that role."""
    wanted = norm(champion_id)
    in_role = [g for g in games if role and g.get("role") == role]
    scopes = [
        ("champion", [g for g in (in_role if strict else games) if wanted and norm(g.get("champion")) == wanted]),
        ("role", in_role),
    ] + ([] if strict else [("all", list(games))])
    for scope, rows in scopes:
        usable = [g for g in rows if g.get("cs_14") is not None]
        if len(usable) < MIN_BASELINE_GAMES:
            continue
        return {
            "scope": scope,
            "scope_label": SCOPE_LABELS[scope].format(champion=champion_name or champion_id,
                                                      role=role_label or role),
            "games": len(usable),
            "cs": {m: _avg([g.get(f"cs_{m}") for g in rows]) for m in BASELINE_MINUTES},
            "cs_per_min": _avg([g.get("cs_per_min") for g in rows]),
            "gold_diff_14": _avg([g.get("gold_diff_14") for g in rows]),
            "kp": _avg([g.get("kp") for g in rows]),
            "vision_per_min": _avg([g.get("vision_per_min") for g in rows]),
        }
    return None


def reference_cs(base_cs: dict, seconds: float):
    """The player's average CS at this point of the game, interpolating between 0:00 and the
    7/14/21-minute averages. None after minute 21 or when an average is missing."""
    points = [(0.0, 0.0)]
    for m in BASELINE_MINUTES:
        if base_cs.get(m) is None:
            break
        points.append((m * 60.0, base_cs[m]))
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= seconds <= t1:
            return v0 + (v1 - v0) * (seconds - t0) / (t1 - t0)
    return None


def cs_context(state: dict, base: dict | None) -> dict | None:
    me = state.get("me")
    if not me:
        return None
    out = {"now": me["cs"], "clock": state["clock"], "cs_per_min": me["cs_per_min"], "has_base": base is not None}
    if base is None:
        return out
    ref = reference_cs(base["cs"], state["time"])
    points = []
    for m in BASELINE_MINUTES:
        cp = (me.get("checkpoints") or {}).get(str(m))
        points.append({"minute": m, "avg": base["cs"].get(m), "live": cp["cs"] if cp else None,
                       "reached": state["time"] >= m * 60})
    out.update({
        "reference": round(ref) if ref is not None else None, "scope_label": base["scope_label"],
        "games": base["games"], "points": points, "cs_per_min_avg": base["cs_per_min"],
        "gold_diff_14": base["gold_diff_14"],
    })
    return out


def kp_context(state: dict, base: dict | None) -> dict | None:
    """Kill participation so far next to the player's average (end-of-game, so only a rough guide)."""
    me = state.get("me")
    if not me:
        return None
    out = {"now": me.get("kp"), "kills": me["kills"], "assists": me["assists"],
           "team_kills": me.get("team_kills"), "has_base": base is not None and base.get("kp") is not None}
    if out["has_base"]:
        out.update(avg=base["kp"], scope_label=base["scope_label"], games=base["games"])
    return out


def vision_context(state: dict, base: dict | None) -> dict | None:
    """The support's vision score so far, with the enemy support's and the player's own average.

    No stored game has vision per minute, only the end-of-game score, so the reference is that game's
    vision per minute times the minutes played so far: an approximation (`approx`), flat over the game."""
    me = state.get("me")
    if not me:
        return None
    minutes = state["time"] / 60.0
    rival = next((p for p in state["teams"]["enemy"]["players"] if p["position"] == "UTILITY"), None)
    out = {"now": me["ward_score"], "clock": state["clock"],
           "per_min": round(me["ward_score"] / minutes, 2) if minutes >= 1 else None,
           "rival": {"champion_name": rival["champion_name"], "ward_score": rival["ward_score"]} if rival else None,
           "has_base": base is not None and base.get("vision_per_min") is not None, "approx": True}
    if out["has_base"]:
        out.update(avg_per_min=round(base["vision_per_min"], 2), reference=round(base["vision_per_min"] * minutes, 1),
                   scope_label=base["scope_label"], games=base["games"])
    return out


def plan_progress(plan: dict | None, state: dict) -> dict:
    """Live counts for the player's improvement-plan goals. Neutral: numbers, no advice."""
    if not plan or not plan.get("enough"):
        return {"enough": False, "games": (plan or {}).get("games", 0),
                "min_games": (plan or {}).get("min_games", dx.PLAN_MIN_GAMES), "goals": []}
    deaths = (state.get("me_events") or {}).get("deaths") or []
    goals = []
    for pr in plan["priorities"]:
        counter = LIVE_PLAN.get(pr["key"])
        goal = pr["goal"]
        item = {"key": pr["key"], "label": pr["label"], "goal": goal, "live": counter is not None,
                "goal_text": "ninguna" if goal == 0 else f"máximo {goal}"}
        if counter is not None:
            value = sum(1 for d in deaths if counter(d))
            item.update(value=value, within=value <= goal, text=f"{value} · meta: {item['goal_text']}")
        else:
            item.update(value=None, within=None, text="no se puede contar en vivo")
        goals.append(item)
    return {"enough": True, "games": plan["games"], "min_games": plan["min_games"], "goals": goals}


class LiveContext:
    """Builds the personal part of the live payload from the main player's stored games."""

    def __init__(self, get_data, clock=time.monotonic):
        self._get_data = get_data           # () -> web.Data of the main player, all queues
        self._clock = clock
        self._data = None
        self._data_at = -REFRESH_S
        self._plan = None
        self._plan_key = None
        self._base_cache: dict = {}

    def _refresh(self):
        now = self._clock()
        if self._data is not None and now - self._data_at < REFRESH_S:
            return self._data
        data = self._get_data()
        self._data, self._data_at = data, now
        key = (data.total, len(data.games)) if data else None
        if key != self._plan_key:
            self._plan_key = key
            self._base_cache = {}
            chrono = [data.analyses[g["match_id"]] for g in data.games if g["match_id"] in data.analyses]
            chrono.sort(key=lambda a: (a["created"] or 0, str(a["match_id"])))
            self._plan = dx.improvement_plan(chrono) if chrono else None
        return data

    def build(self, state: dict) -> dict | None:
        """{"cs", "plan", "role", ...} for the state, or None without a player of yours in the game.

        "role" is the position the game reports; when it does not (custom games, practice tool) and the
        mode has roles, the one the player most often plays ("role_source": "historial"). Jungle and
        support add "kp"; support adds "vision"."""
        me = state.get("me")
        if not me:
            return None
        data = self._refresh()
        games = data.games if data else []
        role, source = me["position"], ("juego" if me["position"] else None)
        if not role and state.get("mode") in ROLE_MODES:
            role = history_role(games, me["champion"]) or ""
            source = "historial" if role else None
        label = me["role_label"] or ROLE_LABELS.get(role, "")
        key = (norm(me["champion"]), role)
        if key not in self._base_cache:
            self._base_cache[key] = baselines(games, me["champion"], role, me["champion_name"], label,
                                              strict=role in STRICT_ROLES)
        base = self._base_cache[key]
        out = {"cs": cs_context(state, base), "plan": plan_progress(self._plan, state), "has_games": bool(games),
               "role": role or None, "role_source": source}
        if role in STRICT_ROLES:
            out["kp"] = kp_context(state, base)
        if role == "UTILITY":
            out["vision"] = vision_context(state, base)
        return out
