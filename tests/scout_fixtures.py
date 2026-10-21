"""A scripted Riot for the scouting tests: spectator game with fictional players (one hidden, one bot)."""
from __future__ import annotations

import copy
import json

from lolan.riot import NotFoundError, RiotError

NOW = 1_800_000_000          # "now" of the tests, in seconds
NOW_MS = NOW * 1000
DAY_MS = 86_400_000
ME = "Yo#LAN"
ME_PUUID = "P-me"

# championId -> (Data Dragon id, Spanish name)
CHAMPIONS = {
    222: ("Jinx", "Jinx"), 86: ("Garen", "Garen"), 113: ("Sejuani", "Sejuani"), 117: ("Lulu", "Lulu"),
    103: ("Ahri", "Ahri"), 58: ("Renekton", "Renekton"), 64: ("LeeSin", "Lee Sin"), 238: ("Zed", "Zed"),
    145: ("Kaisa", "Kai'Sa"), 412: ("Thresh", "Thresh"), 266: ("Aatrox", "Aatrox"), 99: ("Lux", "Lux"),
}
SPELLS = {4: ("SummonerFlash", "Destello"), 7: ("SummonerHeal", "Curación"), 14: ("SummonerDot", "Ignición"),
          11: ("SummonerSmite", "Aplastar"), 12: ("SummonerTeleport", "Teletransporte")}
PERKS = {8008: ("Paso Fugaz", "perk-images/Styles/Precision/LethalTempo/LethalTempoTemp.png"),
         8112: ("Electrocutar", "perk-images/Styles/Domination/Electrocute/Electrocute.png"),
         8010: ("Conquistador", "perk-images/Styles/Precision/Conqueror/Conqueror.png"),
         8000: ("Precisión", "perk-images/Styles/7201_Precision.png"),
         8100: ("Dominación", "perk-images/Styles/7200_Domination.png"),
         8200: ("Brujería", "perk-images/Styles/7202_Sorcery.png")}

# (riot id, puuid, championId, spells, keystone, style, substyle)
ALLIES = [
    (ME, ME_PUUID, 222, (4, 7), 8008, 8000, 8200),
    ("Rana Veloz#T002", "P-rana", 86, (4, 12), 8010, 8000, 8100),
    ("Epsilon Beta#LAN", "P-beta", 113, (4, 11), 8112, 8100, 8000),
    (None, None, 117, (4, 14), None, None, None),              # hidden player
    ("Mono Sabio#T004", "P-mono", 103, (4, 14), 8112, 8100, 8200),
]
ENEMIES = [
    ("Toro Rojo#T006", "P-toro", 58, (4, 12), 8010, 8000, 8100),
    ("Gato Negro#T007", "P-gato", 64, (4, 11), 8112, 8100, 8000),
    ("Pez Dorado#T008", "P-pez", 238, (4, 14), 8112, 8100, 8200),
    ("", "", 145, (4, 7), None, None, None),                   # bot
    ("Oso Pardo#T010", "P-oso", 412, (4, 14), 8010, 8000, 8200),
]


def _participant(team, row, bot=False):
    rid, puuid, champion, (s1, s2), key, style, sub = row
    p = {"teamId": team, "championId": champion, "spell1Id": s1, "spell2Id": s2, "bot": bot,
         "profileIconId": 1, "lastSelectedSkinIndex": 0}
    if rid is not None:
        p["riotId"] = rid
    if puuid is not None:
        p["puuid"] = puuid
    if key is not None:
        p["perks"] = {"perkIds": [key, 9111, 9104, 8014, 8139, 8135, 5008, 5008, 5001],
                      "perkStyle": style, "perkSubStyle": sub}
    return p


def spectator_game(game_id=7700001, queue=420, length=-20, extra_hidden_with_puuid=False):
    """spectator-v5 answer (blue team 100 = the viewer's, red team 200 = enemies)."""
    parts = [_participant(100, r) for r in ALLIES] + [
        _participant(200, r, bot=(r[0] == "")) for r in ENEMIES]
    if extra_hidden_with_puuid:        # puuid but no Riot ID: still treated as hidden
        parts[3]["puuid"] = "P-secret"
    return {"gameId": game_id, "gameMode": "CLASSIC", "gameType": "MATCHED_GAME", "mapId": 11,
            "gameQueueConfigId": queue, "gameStartTime": 0 if length < 0 else NOW_MS - length * 1000,
            "gameLength": length, "participants": parts,
            "bannedChampions": [
                {"championId": 266, "teamId": 100, "pickTurn": 1}, {"championId": 99, "teamId": 200, "pickTurn": 2},
                {"championId": -1, "teamId": 100, "pickTurn": 3}]}


def swap_teams(raw):
    """The same lobby seen from the other side (viewer on team 200)."""
    raw = copy.deepcopy(raw)
    for p in raw["participants"] + raw["bannedChampions"]:
        p["teamId"] = 300 - p["teamId"]
    return raw


def league(solo=None, flex=None):
    """league-v4 answer; each queue: (tier, rank, lp, wins, losses, hotStreak)."""
    out = []
    for qtype, e in (("RANKED_SOLO_5x5", solo), ("RANKED_FLEX_SR", flex)):
        if e:
            tier, rank, lp, w, l, hot = e
            out.append({"queueType": qtype, "tier": tier, "rank": rank, "leaguePoints": lp, "wins": w,
                        "losses": l, "hotStreak": hot})
    return out


def mastery(points, level=5, days_ago=3):
    return {"championLevel": level, "championPoints": points,
            "lastPlayTime": NOW_MS - days_ago * DAY_MS}


def api_match(match_id, puuid, champion_id, champion_name, win=True, kda=(5, 2, 7), created_days_ago=2,
              queue=420):
    """A match-v5 answer where `puuid` played `champion_id` (the others are filler)."""
    parts = []
    for i in range(10):
        parts.append({"puuid": f"filler-{match_id}-{i}", "teamId": 100 if i < 5 else 200, "win": i < 5,
                      "championName": "Ahri", "championId": 103, "kills": 1, "deaths": 1, "assists": 1})
    parts[0].update({"puuid": puuid, "championName": champion_name, "championId": champion_id, "win": win,
                     "kills": kda[0], "deaths": kda[1], "assists": kda[2]})
    return {"metadata": {"matchId": match_id}, "info": {
        "gameCreation": NOW_MS - created_days_ago * DAY_MS, "queueId": queue, "participants": parts}}


class StubLimiter:
    def __init__(self, world, host):
        self.world, self.host = world, host

    def wait_estimate(self):
        return self.world.waits.get(self.host, 0.0)


class World:
    """All of Riot for one test. Every call is logged as (name, argument)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.accounts = {ME: ME_PUUID}
        self.games: dict = {}
        self.leagues: dict = {}
        self.masteries: dict = {}
        self.ids: dict = {}
        self.matches: dict = {}
        self.waits: dict = {}
        self.fail: dict = {}                # call name -> exception to raise

    def client(self, host):
        return WorldClient(self, host)

    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def stock(self, game=None):
        """Plausible rank / mastery for everyone in `game` (solo queue only, fixed numbers)."""
        for rid, puuid, champion, *_ in ALLIES + ENEMIES:
            if not puuid:
                continue
            self.leagues[puuid] = league(solo=("GOLD", "II", 45, 60, 50, False))
            self.masteries[(puuid, champion)] = mastery(50_000)
            self.ids[puuid] = [f"LA1_{puuid}_{i}" for i in range(10)]
            for i in range(10):
                self.matches[f"LA1_{puuid}_{i}"] = api_match(f"LA1_{puuid}_{i}", puuid, 266, "Aatrox",
                                                             win=bool(i % 2))
        if game is not None:
            self.games[ME_PUUID] = game


class WorldClient:
    def __init__(self, world: World, host: str):
        self.world, self.host = world, host
        self.limiter = StubLimiter(world, host)

    def _do(self, name, arg):
        self.world.calls.append((name, arg))
        failure = self.world.fail.get(name)
        if failure is not None:
            exc = failure if isinstance(failure, Exception) else failure(arg)
            if exc is not None:
                raise exc

    def get_puuid(self, name, tag):
        self._do("account", f"{name}#{tag}")
        for rid, puuid in self.world.accounts.items():
            if rid.casefold() == f"{name}#{tag}".casefold():
                return puuid
        raise NotFoundError("404")

    def get_active_game(self, puuid):
        self._do("spectator", puuid)
        return self.world.games.get(puuid)

    def get_league_entries(self, puuid):
        self._do("league", puuid)
        return self.world.leagues.get(puuid, [])

    def get_champion_mastery(self, puuid, champion_id):
        self._do("mastery", (puuid, champion_id))
        return self.world.masteries.get((puuid, champion_id))

    def get_match_ids(self, puuid, count=10, type=None, queue=None):
        self._do("ids", puuid)
        return list(self.world.ids.get(puuid, []))[:count]

    def get_match(self, match_id):
        self._do("match", match_id)
        if match_id not in self.world.matches:
            raise NotFoundError("404")
        return copy.deepcopy(self.world.matches[match_id])


def riot_error(msg="boom"):
    return RiotError(msg)


PNG_BYTES = bytes([0x89]) + b"PNG"


def seed_ddragon(root, icons=True, version="16.20.1"):
    """A Data Dragon cache like the one a previous run leaves behind: names for every fixture id."""
    base = root / "data" / "ddragon"
    data = base / version / "data" / "es_MX"
    data.mkdir(parents=True, exist_ok=True)
    (base / "versions.json").write_text(json.dumps([version]), encoding="utf-8")
    (data / "champion.json").write_text(json.dumps({"data": {
        cid: {"id": cid, "key": str(key), "name": name} for key, (cid, name) in CHAMPIONS.items()}}),
        encoding="utf-8")
    (data / "summoner.json").write_text(json.dumps({"data": {
        sid: {"id": sid, "key": str(key), "name": name, "image": {"full": f"{sid}.png"}}
        for key, (sid, name) in SPELLS.items()}}), encoding="utf-8")
    trees = {8000: "Precisión", 8100: "Dominación", 8200: "Brujería"}
    (data / "runesReforged.json").write_text(json.dumps([
        {"id": tid, "key": name, "name": name, "icon": PERKS[tid][1], "slots": [{"runes": [
            {"id": rid, "key": n, "name": n, "icon": icon} for rid, (n, icon) in PERKS.items() if rid not in trees]}]}
        for tid, name in trees.items()]), encoding="utf-8")
    if icons:
        for cid, _name in CHAMPIONS.values():
            (base / version / "img" / "champion").mkdir(parents=True, exist_ok=True)
            (base / version / "img" / "champion" / f"{cid}.png").write_bytes(PNG_BYTES)
        for sid, _name in SPELLS.values():
            (base / version / "img" / "spell").mkdir(parents=True, exist_ok=True)
            (base / version / "img" / "spell" / f"{sid}.png").write_bytes(PNG_BYTES)
        for _n, icon in PERKS.values():
            path = base / version / "img" / "rune" / icon
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG_BYTES)
    return base
