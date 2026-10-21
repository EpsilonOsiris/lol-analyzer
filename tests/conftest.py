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


def make_timeline(minutes=16, events=None, my_pid=ME_PID, opp_pid=OPP_PID, tweak=None):
    """Frames 0..minutes. Me: gold 500+400m, cs 8m, xp 280m. Opp: gold 500+300m, cs 6m, xp 250m.

    `tweak(pfs, minute)` may edit the participantFrames of each minute (position, level, gold...).
    """
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
                "position": {"x": 7000, "y": 7000}, "currentGold": 300,
            }
        if tweak:
            tweak(pfs, m)
        frames.append({
            "timestamp": m * 60_000, "participantFrames": pfs,
            "events": [e for e in events if e["timestamp"] // 60_000 == m],
        })
    return {"info": {"frames": frames}}


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_ddragon_network(monkeypatch):
    """Tests never reach Riot's CDN: the default fetcher always fails (inject a fake to test downloads)."""
    from lolan import ddragon

    def refuse(url):
        raise ddragon.FetchError("red desactivada en las pruebas")

    monkeypatch.setattr(ddragon, "default_fetch", refuse)


# ------------------------------------------------------------ composition helpers
def make_comp_match(match_id="LA1_1", blue=None, red=None, win=True, queue=440, duration=1800,
                    created=BASE_TIME_MS, main_role="BOTTOM", spells=(4, 14), enemy_cc=None):
    """A match for the compositions feature.

    `blue`: role -> (puuid, riotIdGameName, championName) for the main player's team (the main
    player is ME_PUUID in `main_role`; give an entry for it to set name/champion). `red`: role ->
    championName. `enemy_cc`: timeCCingOthers of red's TOP (the rest have 10).
    """
    blue = dict(blue or {})
    my_pid = ROLES.index(main_role) + 1
    match = make_match(match_id, my_pid=my_pid, win=win, duration=duration, queue=queue,
                       created=created)
    parts = match["info"]["participants"]
    for role, (puuid, name, champion) in blue.items():
        p = parts[ROLES.index(role)]
        p.update({"puuid": puuid, "riotIdGameName": name, "championName": champion})
    for i, role in enumerate(ROLES):
        if parts[i]["puuid"] == ME_PUUID:
            parts[i].setdefault("riotIdGameName", "Epsilon Me")
            parts[i]["summoner1Id"], parts[i]["summoner2Id"] = spells
    for role, champion in (red or {}).items():
        parts[5 + ROLES.index(role)]["championName"] = champion
    for p in parts[5:]:
        p["timeCCingOthers"] = 10
    if enemy_cc is not None:
        parts[5]["timeCCingOthers"] = enemy_cc
    match["metadata"]["participants"] = [p["puuid"] for p in parts]
    return match


def set_final_stats(timeline, pid, **stats):
    """Put championStats on the last frame of a timeline for one participant."""
    pf = timeline["info"]["frames"][-1]["participantFrames"][str(pid)]
    pf.setdefault("championStats", {}).update(stats)
    return timeline


# ------------------------------------------------------------ Riot "apps" (API key change)
FRIEND_PUUID = "puuid-4"      # blue-side teammate in every make_match game


def encode_for_app(match, app):
    """Copy of a match (logical puuids) as Riot would serve it to `app`: every puuid gets '@app'."""
    import copy
    match = copy.deepcopy(match)
    for p in match["info"]["participants"]:
        p["puuid"] = f"{p['puuid']}@{app}"
    match["metadata"]["participants"] = [p["puuid"] for p in match["info"]["participants"]]
    return match


def world_matches(count=6, friend_in=range(4)):
    """match_id -> logical match. Games 0-3 are solo queue, the rest flex; the friend plays in
    `friend_in`; ME_PUUID plays in all."""
    world = {}
    for i in range(count):
        m = make_match(f"LA1_{i}", queue=420 if i < 4 else 440, win=bool(i % 2), kills=i,
                       created=BASE_TIME_MS + i * 3_600_000)
        if i not in friend_in:
            for p in m["info"]["participants"]:
                if p["puuid"] == FRIEND_PUUID:
                    p["puuid"] = "puuid-44"
            m["metadata"]["participants"] = [p["puuid"] for p in m["info"]["participants"]]
        world[f"LA1_{i}"] = m
    return world


class AppRiot:
    """Stands in for RiotClient talking to one Riot app: puuids are '<logical id>@<app>', and a
    puuid issued to another app is rejected like Riot does (PuuidFromOtherAppError)."""

    ACCOUNTS = {"Yo#LAN": ME_PUUID, "Amigo#LAN": FRIEND_PUUID}

    def __init__(self, app, world, accounts=None, missing_matches=(), missing_timelines=(),
                 fail_key_after=None, gate=None):
        self.app = app
        self.world = world
        self.accounts = dict(self.ACCOUNTS if accounts is None else accounts)
        self.missing_matches = set(missing_matches)
        self.missing_timelines = set(missing_timelines)
        self.fail_key_after = fail_key_after       # matches served before the key "expires"
        self.gate = gate                           # threading.Event the match download waits for
        self.calls = []

    def enc(self, logical):
        return f"{logical}@{self.app}"

    def get_puuid(self, name, tag):
        from lolan.riot import NotFoundError
        self.calls.append(("get_puuid", f"{name}#{tag}"))
        logical = self.accounts.get(f"{name}#{tag}")
        if logical is None:
            raise NotFoundError("404")
        return self.enc(logical)

    def get_riot_id(self, puuid):
        from lolan.riot import PUUID_OTHER_APP_MESSAGE, NotFoundError, PuuidFromOtherAppError
        self.calls.append(("get_riot_id", puuid))
        if not puuid.endswith(f"@{self.app}"):
            raise PuuidFromOtherAppError(PUUID_OTHER_APP_MESSAGE)
        logical = puuid[:-len(f"@{self.app}")]
        for riot_id, value in self.accounts.items():
            if value == logical:
                return riot_id
        raise NotFoundError("404")

    def list_match_ids(self, puuid, start_time=None, queue=None):
        return list(self.world)

    def get_match(self, match_id):
        from lolan.riot import InvalidApiKeyError, NotFoundError
        if self.gate is not None:
            self.gate.wait(10)
        served = sum(1 for kind, _ in self.calls if kind == "get_match")
        if self.fail_key_after is not None and served >= self.fail_key_after:
            raise InvalidApiKeyError("expired")
        self.calls.append(("get_match", match_id))
        if match_id in self.missing_matches or match_id not in self.world:
            raise NotFoundError("404")
        return encode_for_app(self.world[match_id], self.app)

    def get_timeline(self, match_id):
        from lolan.riot import NotFoundError
        self.calls.append(("get_timeline", match_id))
        if match_id in self.missing_timelines:
            raise NotFoundError("404")
        timeline = make_timeline()
        timeline["metadata"] = {"participants": encode_for_app(
            self.world[match_id], self.app)["metadata"]["participants"]}
        return timeline

    def fetched(self, kind="get_match"):
        return [arg for k, arg in self.calls if k == kind]


def seed_app_store(store, world, app="A", friend=True):
    """A database as an older install under `app` would have left it."""
    old = AppRiot(app, world)
    store.add_player(old.enc(ME_PUUID), "Yo#LAN", is_main=True)
    if friend:
        store.add_player(old.enc(FRIEND_PUUID), "Amigo#LAN")
    for match_id in world:
        store.save_match(match_id, old.get_match(match_id), old.get_timeline(match_id))
    return old
