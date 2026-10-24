"""Realistic Live Client Data API payloads (documented shapes, fictional players)."""
from __future__ import annotations

import copy

ME = "Zorro Azul#T001"          # the active player: ORDER (blue) bottom lane
BLUE = [  # (riotId, champion id, display name, position)
    ("Rana Veloz#T002", "Garen", "Garen", "TOP"),
    ("Lince Gris#T003", "Sejuani", "Sejuani", "JUNGLE"),
    ("Mono Sabio#T004", "Ahri", "Ahri", "MIDDLE"),
    (ME, "Jinx", "Jinx", "BOTTOM"),
    ("Luna Chica#T005", "Lulu", "Lulu", "UTILITY"),
]
RED = [
    ("Toro Rojo#T006", "Renekton", "Renekton", "TOP"),
    ("Gato Negro#T007", "LeeSin", "Lee Sin", "JUNGLE"),
    ("Pez Dorado#T008", "Zed", "Zed", "MIDDLE"),
    ("Cuervo Alto#T009", "Kaisa", "Kai'Sa", "BOTTOM"),
    ("Oso Pardo#T010", "Thresh", "Thresh", "UTILITY"),
]


def item(item_id, name, count=1, slot=0):
    return {"itemID": item_id, "displayName": name, "count": count, "slot": slot, "canUse": False,
            "consumable": False, "price": 0, "rawDescription": "", "rawDisplayName": f"Item_{item_id}"}


def player(riot_id, cid, name, position, team, *, kills=0, deaths=0, assists=0, cs=0, ward=0.0,
           level=1, items=(), dead=False, respawn=0.0, bot=False):
    game_name = riot_id.split("#")[0]
    return {
        "championName": name, "isBot": bot, "isDead": dead, "items": list(items), "level": level,
        "position": position, "rawChampionName": f"game_character_displayname_{cid}",
        "rawSkinName": "", "respawnTimer": respawn, "riotId": riot_id, "riotIdGameName": game_name,
        "riotIdTagLine": riot_id.split("#")[1], "runes": {}, "scores": {
            "assists": assists, "creepScore": cs, "deaths": deaths, "kills": kills, "wardScore": ward},
        "skinID": 0, "summonerName": riot_id, "summonerSpells": {}, "team": team,
    }


def ev(event_id, name, t, **extra):
    return {"EventID": event_id, "EventName": name, "EventTime": t, **extra}


def base_events():
    """Events up to ~14:00: me (Jinx) dies twice, one of them within 60 s of my own kill."""
    k = "Zorro Azul"
    return [
        ev(0, "GameStart", 0.05),
        ev(1, "MinionsSpawning", 65.0),
        ev(2, "ChampionKill", 150.0, KillerName="Pez Dorado", VictimName="Mono Sabio", Assisters=["Gato Negro"]),
        ev(3, "FirstBlood", 150.0, Recipient="Pez Dorado"),
        # my kill at 5:00, then I die 40 s later to three enemies (killer + 2 assisters)
        ev(4, "ChampionKill", 300.0, KillerName=k, VictimName="Cuervo Alto", Assisters=["Luna Chica"]),
        ev(5, "ChampionKill", 340.0, KillerName="Cuervo Alto", VictimName=k,
           Assisters=["Oso Pardo", "Gato Negro"]),
        ev(6, "TurretKilled", 420.0, TurretKilled="Turret_T2_R_03_A", KillerName="Minion_T100L0S1",
           Assisters=[]),
        ev(7, "FirstBrick", 420.0, KillerName=k),
        ev(8, "DragonKill", 500.0, DragonType="Fire", Stolen="False", KillerName="Lince Gris",
           Assisters=["Rana Veloz"]),
        ev(9, "Multikill", 560.0, KillerName="Pez Dorado", KillStreak=2),
        ev(10, "ChampionKill", 600.0, KillerName="Turret_T2_L_02_A", VictimName=k, Assisters=[]),
        ev(11, "HeraldKill", 700.0, Stolen="False", KillerName="Gato Negro", Assisters=[]),
        ev(12, "InhibKilled", 800.0, InhibKilled="Barracks_T1_C1", KillerName="Toro Rojo", Assisters=[]),
        ev(13, "InhibRespawningSoon", 1000.0, InhibRespawningSoon="Barracks_T1_C1"),
    ]


def make_allgamedata(t=845.0, mode="CLASSIC", events=None, me_gold=1234.5, dead=None):
    """allgamedata at game time `t` (default 14:05) with a realistic mid-game scoreboard."""
    dead = dead or {}
    blue_stats = [(2, 1, 3, 98, 8.0, 7), (1, 2, 5, 70, 14.0, 7), (3, 2, 4, 101, 6.0, 8),
                  (5, 2, 4, 112, 5.0, 8), (0, 3, 9, 14, 29.0, 6)]
    red_stats = [(4, 1, 2, 95, 6.0, 8), (4, 1, 5, 80, 10.0, 8), (5, 1, 2, 110, 5.0, 9),
                 (2, 2, 3, 104, 4.0, 7), (0, 2, 11, 12, 31.0, 6)]
    blue_items = [
        [item(1055, "Hoja de Doran", slot=0), item(2003, "Poción de salud", count=2, slot=1), item(3340, "Tótem", slot=6)],
        [item(1001, "Botas", slot=0), item(3340, "Tótem", slot=6)],
        [item(3020, "Botas de hechicero", slot=0), item(1056, "Anillo de Doran", slot=1)],
        [item(3006, "Grebas de berserker", slot=0), item(1038, "Espada larga B. F.", slot=1),
         item(2055, "Centinela de control", count=1, slot=2), item(3340, "Tótem", slot=6)],
        [item(3850, "Filo del Sol", slot=0), item(2055, "Centinela de control", count=2, slot=1)],
    ]
    red_items = [
        [item(1055, "Hoja de Doran", slot=0)], [item(1001, "Botas", slot=0)], [item(3020, "Botas", slot=0)],
        [item(3006, "Grebas", slot=0), item(1038, "Espada larga B. F.", slot=1)], [item(3850, "Filo", slot=0)],
    ]
    players = []
    for i, (rid, cid, name, pos) in enumerate(BLUE):
        k, d, a, cs, ward, lvl = blue_stats[i]
        players.append(player(rid, cid, name, pos, "ORDER", kills=k, deaths=d, assists=a, cs=cs,
                              ward=ward, level=lvl, items=blue_items[i],
                              dead=rid in dead, respawn=dead.get(rid, 0.0)))
    for i, (rid, cid, name, pos) in enumerate(RED):
        k, d, a, cs, ward, lvl = red_stats[i]
        players.append(player(rid, cid, name, pos, "CHAOS", kills=k, deaths=d, assists=a, cs=cs,
                              ward=ward, level=lvl, items=red_items[i],
                              dead=rid in dead, respawn=dead.get(rid, 0.0)))
    return {
        "activePlayer": {
            "abilities": {}, "championStats": {
                "maxHealth": 1450.0, "attackDamage": 118.4, "abilityPower": 0.0, "armor": 52.0,
                "magicResist": 39.0, "attackSpeed": 1.31, "critChance": 0.0, "abilityHaste": 0.0,
                "moveSpeed": 355.0, "currentHealth": 900.0, "resourceType": "MANA"},
            "currentGold": me_gold, "fullRunes": {"keystone": {"displayName": "Dominio del cazador"}},
            "level": 8, "riotId": ME, "riotIdGameName": "Zorro Azul", "riotIdTagLine": "T001",
            "summonerName": ME, "teamRelativeColors": True},
        "allPlayers": players,
        "events": {"Events": copy.deepcopy(base_events() if events is None else events)},
        "gameData": {"gameMode": mode, "gameTime": t, "mapName": "Map11", "mapNumber": 11,
                     "mapTerrain": "Default"},
    }


def with_events(raw, *extra):
    out = copy.deepcopy(raw)
    out["events"]["Events"].extend(extra)
    return out


class FakeClient:
    """Stands in for live.LiveClient: returns the scripted (kind, data) pairs, then repeats the last."""

    def __init__(self, script):
        self.script = list(script)
        self.problem = None

    def fetch(self):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


SUPPORT_ME = "Luna Chica#T005"      # blue support (Lulu)
JUNGLE_ME = "Lince Gris#T003"       # blue jungler (Sejuani)


def as_viewer(raw, riot_id):
    """The same game seen by another player: `riot_id` becomes the active player."""
    out = copy.deepcopy(raw)
    game_name, tag = riot_id.split("#")
    out["activePlayer"].update({"riotId": riot_id, "riotIdGameName": game_name, "riotIdTagLine": tag,
                                "summonerName": riot_id})
    return out
