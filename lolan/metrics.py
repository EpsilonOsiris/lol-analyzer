"""Pure functions: (match JSON, timeline JSON or None, puuid) -> flat metrics dict.

Everything is written defensively: any field may be missing and the result is
None for that metric (or 0 for plain counters) rather than an exception.
"""
from __future__ import annotations

from datetime import datetime

SR_QUEUES = frozenset({400, 420, 430, 440, 490})
MIN_DURATION_S = 300
SNAPSHOT_MINUTES = (7, 14, 21, 28)
# (key suffix, start minute, end minute exclusive or None)
PHASES = (
    ("0_7", 0, 7),
    ("7_14", 7, 14),
    ("14_21", 14, 21),
    ("21_28", 21, 28),
    ("28p", 28, None),
)
PHASE_KEYS = tuple(p[0] for p in PHASES)
PHASE_LABELS = {"0_7": "0-7", "7_14": "7-14", "14_21": "14-21", "21_28": "21-28", "28p": "28+"}
MONSTER_KEYS = {
    "DRAGON": "dragons",
    "RIFTHERALD": "heralds",
    "BARON_NASHOR": "barons",
    "HORDE": "grubs",
}
# Map is ~15000 x 15000; blue (100) owns bottom-left, red (200) top-right.
MAP_DIAGONAL = 15000


# ---------------------------------------------------------------- helpers
def _num(value):
    """Return value if it is a real number, else None (bools become ints)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return None


def _int(value, default=0):
    n = _num(value)
    return int(n) if n is not None else default


def _div(a, b):
    if a is None or not b:
        return None
    return a / b


def _diff(a, b):
    if a is None or b is None:
        return None
    return a - b


def phase_key(timestamp_ms) -> str:
    minute = (timestamp_ms or 0) / 60000.0
    for key, _start, end in PHASES:
        if end is None or minute < end:
            return key
    return PHASES[-1][0]


def is_enemy_half(team_id, x, y) -> bool:
    """True if the point lies on the half of the map owned by the other team."""
    if x is None or y is None:
        return False
    if team_id == 100:
        return x + y > MAP_DIAGONAL
    if team_id == 200:
        return x + y < MAP_DIAGONAL
    return False


def game_duration_seconds(info: dict) -> float | None:
    dur = _num(info.get("gameDuration"))
    if dur is None:
        start, end = _num(info.get("gameStartTimestamp")), _num(info.get("gameEndTimestamp"))
        if start is not None and end is not None and end > start:
            return (end - start) / 1000.0
        return None
    # Very old matches report milliseconds when gameEndTimestamp is absent.
    if dur > 20000 and not info.get("gameEndTimestamp"):
        return dur / 1000.0
    return float(dur)


def is_remake(info: dict) -> bool:
    dur = game_duration_seconds(info)
    if dur is not None and dur < MIN_DURATION_S:
        return True
    if info.get("gameEndedInEarlySurrender"):
        return True
    return any(p.get("gameEndedInEarlySurrender") for p in info.get("participants") or [])


def find_participant(participants, puuid):
    for p in participants or []:
        if p.get("puuid") == puuid:
            return p
    return None


def find_lane_opponent(participants, me):
    role = me.get("teamPosition")
    if not role:
        return None
    for p in participants or []:
        if p.get("teamId") != me.get("teamId") and p.get("teamPosition") == role:
            return p
    return None


def _player_rates(p: dict, dur_min) -> dict:
    kills, deaths, assists = _int(p.get("kills")), _int(p.get("deaths")), _int(p.get("assists"))
    cs = _int(p.get("totalMinionsKilled")) + _int(p.get("neutralMinionsKilled"))
    return {
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": (kills + assists) / max(1, deaths),
        "cs": cs,
        "cs_per_min": _div(cs, dur_min),
        "gold": _int(p.get("goldEarned")),
        "gold_per_min": _div(_int(p.get("goldEarned")), dur_min),
        "damage": _int(p.get("totalDamageDealtToChampions")),
        "damage_per_min": _div(_int(p.get("totalDamageDealtToChampions")), dur_min),
        "damage_taken": _int(p.get("totalDamageTaken")),
        "vision_score": _int(p.get("visionScore")),
        "vision_per_min": _div(_int(p.get("visionScore")), dur_min),
        "deaths_per_10": _div(deaths * 10.0, dur_min),
    }


def _challenge(p: dict, key):
    ch = p.get("challenges")
    if not isinstance(ch, dict):
        return None
    return _num(ch.get(key))


# -------------------------------------------------------- timeline metrics
def _counter_keys() -> list[str]:
    """Event-based counters (0 when a timeline exists, None when it does not)."""
    keys = []
    for ph in PHASE_KEYS:
        keys += [f"kills_{ph}", f"deaths_{ph}", f"assists_{ph}", f"wards_{ph}",
                 f"obj_own_{ph}", f"obj_enemy_{ph}",
                 f"buildings_taken_{ph}", f"buildings_lost_{ph}"]
    keys += ["early_deaths", "solo_deaths", "deaths_enemy_half",
             "first_blood", "first_blood_victim"]
    for mk in MONSTER_KEYS.values():
        keys += [f"{mk}_own", f"{mk}_enemy"]
    return keys


def timeline_metric_keys() -> list[str]:
    keys = []
    for m in SNAPSHOT_MINUTES:
        keys += [f"cs_{m}", f"gold_{m}", f"xp_{m}", f"level_{m}",
                 f"cs_diff_{m}", f"gold_diff_{m}", f"xp_diff_{m}"]
    return keys + _counter_keys()


def _frame_for_minute(frames, minute):
    """First frame at/after the given minute (5 s tolerance); None if not reached."""
    target = minute * 60000
    for fr in frames:
        ts = _num(fr.get("timestamp"))
        if ts is None:
            continue
        if ts >= target - 5000:
            return fr if ts <= target + 30000 else None
    return None


def _participant_frame(frame, pid):
    if frame is None or pid is None:
        return None
    pfs = frame.get("participantFrames") or {}
    pf = pfs.get(str(pid))
    if pf is None:
        pf = pfs.get(pid)
    return pf if isinstance(pf, dict) else None


def _frame_stats(pf):
    if pf is None:
        return None
    return {
        "cs": _int(pf.get("minionsKilled")) + _int(pf.get("jungleMinionsKilled")),
        "gold": _num(pf.get("totalGold")),
        "xp": _num(pf.get("xp")),
        "level": _num(pf.get("level")),
    }


def _team_of(pid, team_by_pid):
    if pid in team_by_pid:
        return team_by_pid[pid]
    if isinstance(pid, int) and 1 <= pid <= 10:
        return 100 if pid <= 5 else 200
    return None


def _timeline_metrics(timeline, pid, team_id, opp_pid, team_by_pid) -> dict:
    out = {k: None for k in timeline_metric_keys()}
    frames = ((timeline or {}).get("info") or {}).get("frames") or []
    if not frames or pid is None:
        return out

    # Snapshots at fixed minutes (only if the game reached that minute).
    for m in SNAPSHOT_MINUTES:
        fr = _frame_for_minute(frames, m)
        mine = _frame_stats(_participant_frame(fr, pid))
        if mine is None:
            continue
        theirs = _frame_stats(_participant_frame(fr, opp_pid))
        out[f"cs_{m}"], out[f"gold_{m}"] = mine["cs"], mine["gold"]
        out[f"xp_{m}"], out[f"level_{m}"] = mine["xp"], mine["level"]
        if theirs is not None:
            out[f"cs_diff_{m}"] = _diff(mine["cs"], theirs["cs"])
            out[f"gold_diff_{m}"] = _diff(mine["gold"], theirs["gold"])
            out[f"xp_diff_{m}"] = _diff(mine["xp"], theirs["xp"])

    # Event counters start at zero once we know a timeline exists.
    for k in _counter_keys():
        out[k] = 0

    champion_kills = []
    for fr in frames:
        for ev in fr.get("events") or []:
            ts = _num(ev.get("timestamp"))
            if ts is None:
                continue
            etype = ev.get("type")
            ph = phase_key(ts)
            if etype == "CHAMPION_KILL":
                champion_kills.append(ev)
            elif etype == "WARD_PLACED":
                if ev.get("creatorId") == pid:
                    out[f"wards_{ph}"] += 1
            elif etype == "ELITE_MONSTER_KILL":
                killer_team = ev.get("killerTeamId") or _team_of(ev.get("killerId"), team_by_pid)
                if killer_team not in (100, 200):
                    continue
                side = "own" if killer_team == team_id else "enemy"
                out[f"obj_{side}_{ph}"] += 1
                mk = MONSTER_KEYS.get(ev.get("monsterType"))
                if mk:
                    out[f"{mk}_{side}"] += 1
            elif etype == "BUILDING_KILL":
                # teamId is the team that OWNED the destroyed building.
                owner = ev.get("teamId")
                if owner not in (100, 200):
                    continue
                if owner == team_id:
                    out[f"buildings_lost_{ph}"] += 1
                else:
                    out[f"buildings_taken_{ph}"] += 1

    champion_kills.sort(key=lambda e: _num(e.get("timestamp")) or 0)
    for idx, ev in enumerate(champion_kills):
        ph = phase_key(_num(ev.get("timestamp")))
        killer = ev.get("killerId")
        victim = ev.get("victimId")
        assists = ev.get("assistingParticipantIds") or []
        if idx == 0:
            if killer == pid or pid in assists:
                out["first_blood"] = 1
            if victim == pid:
                out["first_blood_victim"] = 1
        if killer == pid:
            out[f"kills_{ph}"] += 1
        if pid in assists:
            out[f"assists_{ph}"] += 1
        if victim == pid:
            out[f"deaths_{ph}"] += 1
            # Solo death: a champion killed us and nobody assisted.
            if isinstance(killer, int) and 1 <= killer <= 10 and not assists:
                out["solo_deaths"] += 1
            pos = ev.get("position") or {}
            if is_enemy_half(team_id, _num(pos.get("x")), _num(pos.get("y"))):
                out["deaths_enemy_half"] += 1

    out["early_deaths"] = out["deaths_0_7"] + out["deaths_7_14"]
    return out


# ------------------------------------------------------------- main entry
def extract_game_metrics(match, timeline, puuid):
    """Flat dict of metrics for `puuid` in this match, or None if they did not play it."""
    match = match or {}
    info = match.get("info") or {}
    participants = info.get("participants") or []
    me = find_participant(participants, puuid)
    if me is None:
        return None

    duration_s = game_duration_seconds(info)
    dur_min = duration_s / 60.0 if duration_s else None
    queue_id = info.get("queueId")
    remake = is_remake(info)
    team_id = me.get("teamId")
    pid = me.get("participantId")
    opp = find_lane_opponent(participants, me)
    team_by_pid = {p.get("participantId"): p.get("teamId") for p in participants}

    created = _num(info.get("gameCreation"))
    when = datetime.fromtimestamp(created / 1000.0) if created else None
    version = str(info.get("gameVersion") or "")
    patch = ".".join(version.split(".")[:2]) if version else None

    row = {
        "match_id": (match.get("metadata") or {}).get("matchId") or info.get("gameId"),
        "game_creation": created,
        "date": when.isoformat(timespec="minutes") if when else None,
        "month": when.strftime("%Y-%m") if when else None,
        "weekday": when.weekday() if when else None,
        "queue_id": queue_id,
        "patch": patch,
        "duration_s": duration_s,
        "duration_min": dur_min,
        "champion": me.get("championName"),
        "role": me.get("teamPosition") or "",
        "team_id": team_id,
        "participant_id": pid,
        "win": 1 if me.get("win") else 0,
        "remake": remake,
        "analyzable": (not remake) and queue_id in SR_QUEUES,
        "has_timeline": 1 if timeline else 0,
    }
    row.update(_player_rates(me, dur_min))
    # Team "plan de pelea" of both sides (comps imports this module, hence the late import).
    if row["analyzable"]:
        from .comps import match_plans
        row["team_plan"], row["enemy_plan"] = match_plans(participants, team_id)
    else:
        row["team_plan"] = row["enemy_plan"] = None

    team = [p for p in participants if p.get("teamId") == team_id]
    team_kills = sum(_int(p.get("kills")) for p in team)
    team_damage = sum(_int(p.get("totalDamageDealtToChampions")) for p in team)
    row["kp"] = _div(row["kills"] + row["assists"], team_kills)
    row["damage_share"] = _div(row["damage"], team_damage)
    row["wards_placed"] = _int(me.get("wardsPlaced"))
    row["wards_killed"] = _int(me.get("wardsKilled"))
    row["control_wards"] = _int(me.get("detectorWardsPlaced"))

    row["solo_kills"] = _challenge(me, "soloKills")
    row["cs_first10"] = _challenge(me, "laneMinionsFirst10Minutes")
    row["max_cs_adv"] = _challenge(me, "maxCsAdvantageOnLaneOpponent")
    row["skillshots_dodged"] = _challenge(me, "skillshotsDodged")
    row["skillshots_hit"] = _challenge(me, "skillshotsHit")

    # Lane opponent (end-of-game comparison).
    if opp is not None:
        o = _player_rates(opp, dur_min)
        row.update({
            "opp_champion": opp.get("championName"),
            "opp_kda": o["kda"],
            "opp_cs_per_min": o["cs_per_min"],
            "opp_gold_per_min": o["gold_per_min"],
            "opp_damage_per_min": o["damage_per_min"],
            "opp_vision_per_min": o["vision_per_min"],
            "gold_diff_end": row["gold"] - o["gold"],
            "cs_diff_end": row["cs"] - o["cs"],
            "damage_diff": row["damage"] - o["damage"],
            "vision_diff": row["vision_score"] - o["vision_score"],
        })
    else:
        row.update({k: None for k in (
            "opp_champion", "opp_kda", "opp_cs_per_min", "opp_gold_per_min",
            "opp_damage_per_min", "opp_vision_per_min", "gold_diff_end",
            "cs_diff_end", "damage_diff", "vision_diff")})

    tl = _timeline_metrics(timeline, pid, team_id, opp.get("participantId") if opp else None, team_by_pid)
    if not timeline:
        # Without a timeline, fall back to the participant flags for first blood.
        tl["first_blood"] = 1 if (me.get("firstBloodKill") or me.get("firstBloodAssist")) else 0
    row.update(tl)
    return row
