"""Champion select as a compact anonymous state: real Practice Tool and ARAM captures (sanitized) and a synthetic flex draft."""
import copy
import json
import threading
from pathlib import Path

import pytest
from comps_fixtures import build_records

from lolan import comps, draft_live as dl
from lolan.lcu import ChampSelectRecorder, LcuFollower

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    data = json.loads((FIXTURES / f"champ_select_{name}.json").read_text(encoding="utf-8"))
    return data["gameflow"], data["snapshots"]


# ------------------------------------------------------------ the fixtures themselves
@pytest.mark.parametrize("name", ["practice", "aram"])
def test_fixtures_carry_no_secrets(name):
    text = (FIXTURES / f"champ_select_{name}.json").read_text(encoding="utf-8")
    for needle in ("multiUserChat", "mucJwt", "Password", "token", "Osiris", "matrix108"):
        assert needle not in text
    _gf, snaps = load(name)
    for s in snaps:
        assert s["chatDetails"] == {}
        for m in s["myTeam"] + s["theirTeam"]:
            assert m["puuid"] == "" or m["puuid"].startswith("00000000-0000-4000-8000-")


# ----------------------------------------------------- Practice Tool (real: hover -> lock)
def test_practice_tool_hover_then_lock():
    gf, snaps = load("practice")
    states = [dl.normalize(s, gf) for s in snaps]
    first, aatrox, ashe, locked = states
    assert first["queue_id"] == 3140 and first["map_id"] == 11 and first["is_custom"] is True
    assert first["lanes"] is True and first["me"] == 0
    assert first["timer"]["phase"] == "BAN_PICK" and first["timer"]["seconds_left"] == 90
    me = first["allies"][0]
    assert me["is_me"] and me["visible"] and me["riot_id"] == "Epsilon Principal#TEST"
    assert me["role"] is None                          # blind pick: assignedPosition is ""
    assert (me["champion_id"], me["hover_id"], me["locked"]) == (0, 0, False)
    assert (aatrox["allies"][0]["hover_id"], aatrox["allies"][0]["locked"]) == (266, False)
    assert (ashe["allies"][0]["hover_id"], ashe["allies"][0]["champion_id"]) == (22, 0)
    me = locked["allies"][0]
    assert (me["champion_id"], me["hover_id"], me["locked"]) == (22, 0, True)
    assert locked["timer"]["phase"] == "FINALIZATION" and locked["timer"]["seconds_left"] == 10
    assert locked["enemies"] == [{"champion_id": 0}]
    assert locked["bans"] == {"ally": [], "enemy": []}
    assert locked["bench"] == {"enabled": False, "champions": [], "rerolls": 0, "can_reroll": False}


def test_the_hidden_enemy_has_no_identity_in_the_state():
    gf, snaps = load("practice")
    state = dl.normalize(snaps[-1], gf)
    assert state["enemies"] == [{"champion_id": 0}]       # champion data and nothing else
    text = json.dumps(state)
    for needle in ("puuid", "summoner", "gameName", "tagLine", "obfuscated", "9000", "00000000-0000"):
        assert needle not in text


def test_hidden_players_keep_nothing_that_identifies_them_even_if_the_client_fills_it_in():
    gf, snaps = load("practice")
    raw = copy.deepcopy(snaps[-1])
    ally = {**raw["myTeam"][0], "cellId": 1, "nameVisibilityType": "HIDDEN", "gameName": "Secreto",
            "tagLine": "XYZ", "puuid": "abc-secret-puuid", "summonerId": 777, "obfuscatedPuuid": "obf-puuid",
            "obfuscatedSummonerId": 888}
    raw["myTeam"].append(ally)
    raw["theirTeam"][0].update(gameName="Enemigo", tagLine="ENE", puuid="enemy-puuid", summonerId=999,
                               obfuscatedPuuid="enemy-obf")
    state = dl.normalize(raw, gf)
    hidden = state["allies"][1]
    assert hidden["visible"] is False and hidden["riot_id"] is None
    text = json.dumps(state)
    for needle in ("Secreto", "XYZ", "abc-secret", "777", "obf-puuid", "888", "Enemigo", "ENE", "enemy-puuid",
                   "999", "enemy-obf"):
        assert needle not in text
    # visible allies keep only the Riot ID: never puuid or summoner id
    visible = state["allies"][0]
    assert set(visible) == {"cell_id", "role", "champion_id", "hover_id", "locked", "is_me", "visible", "riot_id"}


# ------------------------------------------------------------- ARAM (real capture)
def test_aram_has_a_bench_no_lanes_and_hidden_enemies():
    gf, snaps = load("aram")
    states = [dl.normalize(s, gf) for s in snaps]
    first, mid, last = states[0], states[3], states[-1]
    assert first["queue_id"] == 2400 and first["map_id"] == 12 and first["game_mode"] == "KIWI"
    assert first["queue_name"] == "ARAM: Caos" and first["is_custom"] is False
    assert all(not s["lanes"] for s in states)                      # no Composiciones outside the Rift
    assert first["bench"] == {"enabled": True, "champions": [], "rerolls": 0, "can_reroll": True}
    assert mid["bench"]["champions"] == [122, 50, 99, 54]           # field shape: [{championId, isPriority}]
    assert [a["cell_id"] for a in mid["allies"]] == [5, 6, 7, 8, 9] and first["me"] == 7
    assert [a["is_me"] for a in mid["allies"]] == [False, False, True, False, False]
    assert [a["champion_id"] for a in mid["allies"]] == [63, 61, 421, 0, 53]
    assert all(a["role"] is None and a["visible"] for a in mid["allies"])
    assert [a["locked"] for a in mid["allies"]] == [True, True, True, False, True]    # assigned, never hovered
    assert all(a["hover_id"] == 0 for a in mid["allies"])
    assert mid["enemies"] == [{"champion_id": 0}] * 5
    assert mid["bans"] == {"ally": [], "enemy": []}
    assert last["timer"]["phase"] == "GAME_STARTING" and last["timer"]["seconds_left"] == 0
    assert last["bench"]["champions"] == [53, 421, 76, 54, 102]


def test_aram_state_has_no_identity_of_the_enemy_and_nothing_but_riot_ids_for_allies():
    gf, snaps = load("aram")
    text = json.dumps(dl.normalize(snaps[5], gf))
    for needle in ("puuid", "summonerId", "9005", "00000000-0000"):
        assert needle not in text


# ------------------------------------------------------------ synthetic flex draft
def player(cell, position, champion=0, intent=0, name=None, visible=True, team=1):
    return {"assignedPosition": position, "cellId": cell, "championId": champion, "championPickIntent": intent,
            "gameName": name or "", "tagLine": "LAN" if name else "",
            "nameVisibilityType": "VISIBLE" if visible else "HIDDEN", "puuid": f"secret-{cell}",
            "summonerId": 5000 + cell, "team": team}


def flex_session():
    ally = [player(0, "top", 54, 54, "Epsilon Alfa"), player(1, "jungle", 0, 64, "Epsilon Beta"),
            player(2, "middle", 0, 0, "Pepito"), player(3, "bottom", 0, 0, "Epsilon Main"),
            player(4, "utility", 89, 89, None, visible=False)]
    enemy = [player(5 + i, pos, ch, team=2, visible=False) for i, (pos, ch) in enumerate(
        [("top", 122), ("jungle", 0), ("middle", 103), ("bottom", 0), ("utility", 0)])]
    actions = [
        [{"actorCellId": 6, "championId": 0, "completed": True, "isAllyAction": False, "type": "ban"},
         {"actorCellId": 0, "championId": 238, "completed": True, "isAllyAction": True, "type": "ban"},
         {"actorCellId": 1, "championId": 0, "completed": True, "isAllyAction": True, "type": "ban"}],
        [{"actorCellId": 0, "championId": 54, "completed": True, "isAllyAction": True, "type": "pick"},
         {"actorCellId": 1, "championId": 64, "completed": False, "isAllyAction": True, "isInProgress": True,
          "type": "pick"},
         {"actorCellId": 4, "championId": 89, "completed": True, "isAllyAction": True, "type": "pick"},
         {"actorCellId": 5, "championId": 122, "completed": True, "isAllyAction": False, "type": "pick"},
         {"actorCellId": 7, "championId": 103, "completed": True, "isAllyAction": False, "type": "pick"}],
    ]
    return {"myTeam": ally, "theirTeam": enemy, "actions": actions, "localPlayerCellId": 3, "queueId": 440,
            "isCustomGame": False, "benchEnabled": False, "benchChampions": [], "rerollsRemaining": 0,
            "bans": {"myTeamBans": [238, 0], "theirTeamBans": [157, 238], "numBans": 10},
            "timer": {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 21400, "isInfinite": False}}


FLEX_GAMEFLOW = {"map": {"id": 11}, "gameData": {"queue": {"id": 440, "mapId": 11, "gameMode": "CLASSIC",
                                                          "description": "Flexible 5v5"}}}


def test_flex_draft_roles_locks_hovers_enemies_and_bans():
    state = dl.normalize(flex_session(), FLEX_GAMEFLOW)
    assert state["lanes"] and state["queue_id"] == 440 and state["queue_name"] == "Flexible 5v5"
    allies = {a["role"]: a for a in state["allies"]}
    assert set(allies) == {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}      # assignedPosition mapped
    assert allies["TOP"]["locked"] and allies["TOP"]["champion_id"] == 54 and allies["TOP"]["hover_id"] == 0
    # intent 64 with a still-open pick action: hovering, not locked
    assert not allies["JUNGLE"]["locked"] and allies["JUNGLE"]["champion_id"] == 0 and allies["JUNGLE"]["hover_id"] == 64
    assert allies["MIDDLE"]["hover_id"] == 0 and not allies["MIDDLE"]["locked"]
    assert allies["BOTTOM"]["is_me"] and allies["BOTTOM"]["riot_id"] == "Epsilon Main#LAN"
    assert allies["UTILITY"]["visible"] is False and allies["UTILITY"]["riot_id"] is None
    assert allies["UTILITY"]["locked"] and allies["UTILITY"]["champion_id"] == 89
    assert [e["champion_id"] for e in state["enemies"]] == [122, 0, 103, 0, 0]
    assert state["bans"] == {"ally": [238], "enemy": [157, 238]}
    assert state["timer"] == {"phase": "BAN_PICK", "seconds_left": 21, "infinite": False}
    assert "secret" not in json.dumps(state) and "5003" not in json.dumps(state)


def test_enemy_pick_is_read_from_a_completed_action_when_the_team_list_still_says_zero():
    raw = flex_session()
    assert raw["theirTeam"][1]["championId"] == 0
    raw["actions"][1].append({"actorCellId": 6, "championId": 11, "completed": True, "isAllyAction": False,
                              "type": "pick"})
    assert dl.normalize(raw, FLEX_GAMEFLOW)["enemies"][1] == {"champion_id": 11}


def test_unknown_map_counts_as_rift_unless_there_is_a_bench():
    raw = flex_session()
    assert dl.normalize(raw, None)["lanes"] is True
    raw["benchEnabled"] = True
    assert dl.normalize(raw, None)["lanes"] is False
    assert dl.normalize(flex_session(), {"map": {"id": 12}})["lanes"] is False
    assert dl.normalize(flex_session(), {"map": {"id": 22}})["lanes"] is False


@pytest.mark.parametrize("junk", [None, {}, [], "x", {"myTeam": "no"}])
def test_not_a_session_is_none(junk):
    assert dl.normalize(junk, None) is None


def test_odd_values_do_not_raise():
    raw = {"myTeam": [None, {"cellId": "a", "championId": "x", "assignedPosition": 7}, {}],
           "theirTeam": "nope", "actions": [None, [None, {"type": "pick"}]], "bans": [], "timer": 3,
           "benchChampions": [None, {"championId": "z"}, 5], "localPlayerCellId": "q"}
    state = dl.normalize(raw, {"map": 5, "gameData": []})
    assert state["enemies"] == [] and state["me"] is None


def test_roles_are_mapped_from_the_clients_spelling():
    assert [dl.role_of(x) for x in ("top", "JUNGLE", "Middle", "bottom", "utility", "support", "", None, "fill")] == \
        ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "UTILITY", None, None, None]


def test_signature_ignores_only_the_timer():
    state = dl.normalize(flex_session(), FLEX_GAMEFLOW)
    other = {**state, "timer": {"phase": "BAN_PICK", "seconds_left": 3, "infinite": False}}
    assert dl.signature(state) == dl.signature(other) and state != other
    assert dl.signature({**other, "me": 2}) != dl.signature(state)


# -------------------------------------------------------------- people and lineup
def clan():
    return comps.ClanConfig.build("Epsilon", "Epsilon Vieja=Epsilon Alfa", {"puuid-x": "Compa#MX"})


def test_only_registered_clan_members_become_persons():
    state = dl.normalize(flex_session(), FLEX_GAMEFLOW)
    allies = dl.attach_people(state, clan(), "Epsilon Main")
    by_role = {a["role"]: a["person"] for a in allies}
    assert by_role == {"TOP": "Epsilon Alfa", "JUNGLE": "Epsilon Beta", "MIDDLE": None,
                       "BOTTOM": "Epsilon Main", "UTILITY": None}
    assert dl.person_of("Compa#MX", clan()) == "Compa" and dl.person_of("Compa#OTRO", clan()) is None
    assert dl.person_of("Epsilon Vieja#LAN", clan()) == "Epsilon Alfa"        # aliases are followed
    assert dl.person_of(None, clan()) is None


def with_keys(allies, keys):
    return [{**a, "key": keys.get(a["cell_id"])} for a in allies]


def test_lineup_uses_client_positions_and_marks_hovers():
    state = dl.normalize(flex_session(), FLEX_GAMEFLOW)
    records = build_records()
    allies = with_keys(dl.attach_people(state, clan(), "Epsilon Me"), {0: "Fiora", 1: "Viego", 4: "Lulu"})
    built = dl.build_lineup(allies, records, ["Darius", None, "Syndra"])
    assert built["main_role"] == "BOTTOM" and built["role_source"] == "cliente"
    assert built["lineup"]["TOP"] == ("Epsilon Alfa", "Fiora")
    assert built["lineup"]["JUNGLE"] == ("Epsilon Beta", "Viego") and built["hover_roles"] == ["JUNGLE"]
    assert built["lineup"]["BOTTOM"] == ("Epsilon Me", None)              # my slot is not fixed by a hover
    assert built["lineup"]["MIDDLE"] == (None, None)
    assert built["enemies"] == ["Darius", "Syndra"]


def test_lineup_my_choice_wins_and_blind_games_fall_back_to_history():
    gf, snaps = load("practice")
    state = dl.normalize(snaps[-1], gf)
    records = build_records()
    allies = with_keys(dl.attach_people(state, clan(), "Epsilon Me"), {0: "Ashe"})
    built = dl.build_lineup(allies, records, [])
    assert built["main_role"] == "BOTTOM" and built["role_source"] == "historial"      # his most played line
    assert built["lineup"] == {"BOTTOM": ("Epsilon Me", "Ashe")}                        # locked: fixed
    chosen = dl.build_lineup(allies, records, [], my_role="MIDDLE")
    assert chosen["main_role"] == "MIDDLE" and chosen["role_source"] == "elegido"
    assert dl.build_lineup(allies, [], [], my_role="nope")["role_source"] == "supuesto"


def test_clan_teammates_without_a_position_take_their_usual_line_once():
    records = build_records()
    base = {"cell_id": 0, "role": None, "champion_id": 0, "hover_id": 0, "locked": False, "is_me": False,
            "visible": True, "riot_id": None, "key": None}
    allies = [{**base, "cell_id": 0, "is_me": True, "person": "Epsilon Me"},
              {**base, "cell_id": 1, "person": "Epsilon Alfa"},          # always support in the records
              {**base, "cell_id": 2, "person": "Epsilon Gamma"},         # mid or ADC: whichever is left
              {**base, "cell_id": 3, "person": None}]
    built = dl.build_lineup(allies, records, [], my_role="BOTTOM")
    assert built["roles"][1] == "UTILITY" and built["roles"][2] == "MIDDLE" and 3 not in built["roles"]
    again = dl.build_lineup(allies, records, [], my_role="MIDDLE")
    assert again["roles"][2] == "BOTTOM"                                # mid taken: Gamma's other line


# --------------------------------------------------------------------- the feed
def test_feed_publishes_only_real_changes_and_wakes_waiters():
    feed = dl.DraftFeed()
    assert feed.snapshot() == {"version": 0, "phase": None, "state": None}
    feed.publish("Lobby", None)
    feed.publish("Lobby", None)                       # nothing new: same version
    assert feed.snapshot()["version"] == 1
    got = []
    t = threading.Thread(target=lambda: got.append(feed.wait_for_change(1, 5)))
    t.start()
    feed.publish("ChampSelect", {"x": 1})
    t.join(5)
    assert got and got[0]["version"] == 2 and got[0]["phase"] == "ChampSelect"
    assert feed.wait_for_change(2, 0.01)["version"] == 2          # times out quietly
    assert not feed.is_stopped()
    feed.stop()
    assert feed.is_stopped() and feed.wait_for_change(2, 5)["version"] == 2


class ScriptedClient:
    problem = None

    def __init__(self, gameflow, sessions):
        self.phase_now, self.sessions, self.gameflow, self.gf_calls = None, sessions, gameflow, 0

    def phase(self):
        return self.phase_now

    def champ_select(self):
        return self.sessions[0] if self.sessions else None

    def gameflow_session(self):
        self.gf_calls += 1
        return self.gameflow

    def lobby(self):
        return None

    def close(self):
        pass


def test_the_recorders_loop_feeds_the_page_without_a_second_poller(tmp_path):
    gf, snaps = load("practice")
    client = ScriptedClient(gf, snaps)
    feed = dl.DraftFeed()
    rec = ChampSelectRecorder(client, out_dir=tmp_path / "capturas", say=lambda t: None, warn=lambda t: None,
                              observer=feed)
    client.phase_now = None
    rec.step()
    assert feed.snapshot()["phase"] is None
    client.phase_now = "Lobby"
    rec.step()
    assert feed.snapshot()["phase"] == "Lobby" and feed.snapshot()["state"] is None
    client.phase_now = "ChampSelect"
    rec.step()
    snap = feed.snapshot()
    assert snap["phase"] == "ChampSelect" and snap["state"]["lanes"] and snap["state"]["queue_id"] == 3140
    client.sessions = snaps[1:]
    rec.step()
    rec.step()                                        # same session: nothing new, gameflow read once
    assert feed.snapshot()["state"]["allies"][0]["hover_id"] == 266 and client.gf_calls == 2   # recorder + feed
    assert list((tmp_path / "capturas").glob("*.jsonl"))                      # and it still records
    client.phase_now = "InProgress"
    rec.step()
    assert feed.snapshot()["phase"] == "InProgress" and feed.snapshot()["state"] is None


def test_an_observer_that_crashes_does_not_stop_the_recording(tmp_path):
    gf, snaps = load("practice")
    client = ScriptedClient(gf, snaps)
    warned = []

    def boom(*_):
        raise RuntimeError("x")

    rec = ChampSelectRecorder(client, out_dir=tmp_path / "capturas", say=lambda t: None, warn=warned.append,
                              observer=boom)
    client.phase_now = "ChampSelect"
    rec.step()
    rec.step()
    assert len(warned) == 1 and list((tmp_path / "capturas").glob("*.jsonl"))


def test_the_follower_polls_without_recording():
    gf, snaps = load("practice")
    client = ScriptedClient(gf, snaps)
    feed = dl.DraftFeed()
    follower = LcuFollower(feed, client=client, fast_s=0.5, idle_s=7)
    assert follower.step() == 7 and feed.snapshot()["phase"] is None
    client.phase_now = "ChampSelect"
    assert follower.step() == 0.5
    assert feed.snapshot()["state"]["queue_id"] == 3140
    client.phase_now = "Lobby"
    follower.step()
    assert feed.snapshot()["state"] is None
