"""Synthetic match + timeline builders (no network, no real data)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ME_PUUID = "puuid-me"
ME_PID = 3       # blue-side mid
OPP_PID = 8      # red-side mid
BASE_TIME_MS = 1_700_000_000_000


def make_match(match_id="LA1_1", my_pid=ME_PID, win=True, duration=1000, queue=420,
               created=BASE_TIME_MS, champion="Ahri", opp_champion="Zed", challenges=True,
               kills=1, deaths=2, assists=1, drop_opp_position=False):
    participants = []
    for pid in range(1, 11):
        team = 100 if pid <= 5 else 200
        role = ROLES[(pid - 1) % 5]
        p = {
            "puuid": ME_PUUID if pid == my_pid else f"puuid-{pid}",
            "participantId": pid,
            "teamId": team,
            "teamPosition": role,
            "championName": champion if pid == my_pid else (opp_champion if pid == OPP_PID else f"Champ{pid}"),
            "win": win if team == 100 else not win,
            "kills": 2, "deaths": 2, "assists": 3,
            "totalMinionsKilled": 100, "neutralMinionsKilled": 0,
            "goldEarned": 8000, "totalDamageDealtToChampions": 10000, "totalDamageTaken": 12000,
            "visionScore": 15, "wardsPlaced": 8, "wardsKilled": 1, "detectorWardsPlaced": 2,
        }
        participants.append(p)
    me = participants[my_pid - 1]
    me.update({
        "kills": kills, "deaths": deaths, "assists": assists,
        "totalMinionsKilled": 120, "neutralMinionsKilled": 0, "goldEarned": 9000,
        "totalDamageDealtToChampions": 15000, "visionScore": 20, "wardsPlaced": 10,
        "wardsKilled": 2, "detectorWardsPlaced": 3,
    })
    if challenges:
        me["challenges"] = {"soloKills": 1, "laneMinionsFirst10Minutes": 70,
                            "maxCsAdvantageOnLaneOpponent": 12.5,
                            "skillshotsDodged": 30, "skillshotsHit": 40}
    opp = participants[OPP_PID - 1]
    opp.update({"totalMinionsKilled": 100, "goldEarned": 8000, "visionScore": 12})
    if drop_opp_position:
        for p in participants:
            if p["participantId"] == OPP_PID:
                p["teamPosition"] = ""
    return {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in participants]},
        "info": {
            "gameCreation": created, "gameDuration": duration, "queueId": queue,
            "gameVersion": "14.5.123.456", "participants": participants,
        },
    }


def ev_kill(ts, killer, victim, assists=(), x=5000, y=5000):
    return {"type": "CHAMPION_KILL", "timestamp": ts, "killerId": killer, "victimId": victim,
            "assistingParticipantIds": list(assists), "position": {"x": x, "y": y}}


DEFAULT_EVENTS = [
    # 2:00 opp kills me with an assist, on the enemy half (x+y > 15000): first blood for them.
    ev_kill(120_000, 8, 3, [9], x=10000, y=8000),
    # 5:30 I kill 7 (assist from 1).
    ev_kill(330_000, 3, 7, [1]),
    # 9:00 solo death on my own half.
    ev_kill(540_000, 6, 3, [], x=3000, y=4000),
    # 12:00 teammate kills 6, I assist.
    ev_kill(720_000, 1, 6, [3]),
    {"type": "ELITE_MONSTER_KILL", "timestamp": 600_000, "killerId": 4, "killerTeamId": 100,
     "monsterType": "DRAGON"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 930_000, "killerId": 9, "killerTeamId": 200,
     "monsterType": "HORDE"},
    {"type": "BUILDING_KILL", "timestamp": 780_000, "killerId": 2, "teamId": 200,
     "buildingType": "TOWER_BUILDING"},
    {"type": "BUILDING_KILL", "timestamp": 900_000, "killerId": 7, "teamId": 100,
     "buildingType": "TOWER_BUILDING"},
    {"type": "WARD_PLACED", "timestamp": 60_000, "creatorId": 3},
    {"type": "WARD_PLACED", "timestamp": 180_000, "creatorId": 3},
    {"type": "WARD_PLACED", "timestamp": 480_000, "creatorId": 3},
    {"type": "WARD_PLACED", "timestamp": 480_000, "creatorId": 8},
]


def make_timeline(minutes=16, events=None, my_pid=ME_PID, opp_pid=OPP_PID):
    """Frames 0..minutes. Me: gold 500+400m, cs 8m, xp 280m. Opp: gold 500+300m, cs 6m, xp 250m."""
    events = copy.deepcopy(DEFAULT_EVENTS if events is None else events)
    frames = []
    for m in range(minutes + 1):
        pfs = {}
        for pid in range(1, 11):
            if pid == my_pid:
                gold, cs, xp = 500 + 400 * m, 8 * m, 280 * m
            elif pid == opp_pid:
                gold, cs, xp = 500 + 300 * m, 6 * m, 250 * m
            else:
                gold, cs, xp = 500 + 300 * m, 6 * m, 250 * m
            pfs[str(pid)] = {
                "totalGold": gold, "xp": xp, "level": 1 + m // 2,
                "minionsKilled": cs, "jungleMinionsKilled": 0,
                "position": {"x": 7000, "y": 7000},
            }
        frames.append({
            "timestamp": m * 60_000, "participantFrames": pfs,
            "events": [e for e in events if e["timestamp"] // 60_000 == m],
        })
    return {"info": {"frames": frames}}
