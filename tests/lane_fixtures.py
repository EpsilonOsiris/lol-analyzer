"""Synthetic lane-phase matches and timelines (hand-built frames, no real data)."""
from __future__ import annotations

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
MIN = 60_000
BLUE_MID, RED_MID = 3, 8


def champ_id(pid: int) -> int:
    return 100 + pid


def make_lane_match(match_id="LA1_100", duration=1200, queue=420, version="16.21.824.8524",
                    blue_win=True, names=None, puuids=None, drop=(), duplicate=(), remake=False,
                    created=1_700_000_000_000):
    """Ten players, pids 1-5 blue (TOP..UTILITY), 6-10 red. championId = 100 + pid.

    names / puuids: pid -> value overriding riotIdGameName / puuid. drop: pids with no
    teamPosition. duplicate: pids whose teamPosition is TOP (a second top on their team).
    """
    names, puuids = names or {}, puuids or {}
    parts = []
    for pid in range(1, 11):
        team = 100 if pid <= 5 else 200
        parts.append({
            "puuid": puuids.get(pid, f"puuid-{pid}"), "participantId": pid, "teamId": team,
            "teamPosition": "" if pid in drop else "TOP" if pid in duplicate else ROLES[(pid - 1) % 5],
            "championId": champ_id(pid), "championName": f"Champ{pid}",
            "riotIdGameName": names.get(pid, f"Jugador{pid}"),
            "win": blue_win if team == 100 else not blue_win,
        })
    if remake:
        for p in parts:
            p["gameEndedInEarlySurrender"] = True
    return {"metadata": {"matchId": match_id, "participants": [p["puuid"] for p in parts]},
            "info": {"gameCreation": created, "gameDuration": duration, "queueId": queue,
                     "gameVersion": version, "participants": parts}}


def make_lane_timeline(minutes=20, gold=None, xp=None, level=None, events=None):
    """Frames 0..minutes (one per minute). gold / xp / level: pid -> function(minute).

    Defaults: gold 500 + 300 m, xp 250 m, level 1 + m // 2.
    """
    gold, xp, level = gold or {}, xp or {}, level or {}
    events = events or []
    frames = []
    for m in range(minutes + 1):
        pfs = {}
        for pid in range(1, 11):
            pfs[str(pid)] = {
                "totalGold": gold.get(pid, lambda m: 500 + 300 * m)(m),
                "xp": xp.get(pid, lambda m: 250 * m)(m),
                "level": level.get(pid, lambda m: 1 + m // 2)(m),
            }
        frames.append({"timestamp": m * MIN, "participantFrames": pfs,
                       "events": [e for e in events if e["timestamp"] // MIN == m]})
    return {"info": {"frames": frames}}


def level_up(pid, level, ts):
    return {"type": "LEVEL_UP", "participantId": pid, "level": level, "timestamp": ts}


def bought(pid, item, ts):
    return {"type": "ITEM_PURCHASED", "participantId": pid, "itemId": item, "timestamp": ts}


def undo(pid, before, ts):
    return {"type": "ITEM_UNDO", "participantId": pid, "beforeId": before, "afterId": 0,
            "goldGain": 100, "timestamp": ts}


def mid_edge(extra_gold=100, extra_xp=0):
    """gold/xp functions that give the blue mid `extra_*` per minute more than the red mid."""
    return ({BLUE_MID: lambda m: 500 + (300 + extra_gold) * m},
            {BLUE_MID: lambda m: (250 + extra_xp) * m})


def row(**kw):
    """A lane row as the store keeps it (mid, champion 1 against 2 by default)."""
    base = {"match_id": "LA1_1", "source": "propia", "platform": "la1", "patch": "16.21", "tier": None,
            "queue": 420, "game_creation": 1, "role": "MIDDLE", "side": 100, "champ": 1, "opp": 2,
            "diff14": 500, "gold14": 400, "xp14": 100, "result": "win", "brackets": {},
            "first2": None, "first6": None, "series": [], "items": None, "win": 1, "minutes": 28.0,
            "person": None}
    base.update(kw)
    return base
