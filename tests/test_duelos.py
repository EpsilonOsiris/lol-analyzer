"""Duelos por línea: role assignment, matchup rows, thresholds, personal record, win probability, calibration."""
import random
import threading
import time
from collections import Counter

import pytest
from conftest import make_match
from opgg_fixtures import FakeMcp, FakeResponse
from scout_fixtures import ME, ME_PUUID, spectator_game

from lolan import duelos as dl
from lolan import winprob as wp
from lolan.opgg import McpClient
from lolan.scout import parse_spectator
from lolan.store import Store

NOW = 1_800_000_000


# ------------------------------------------------------------------- helpers
def make_report(length=-20, queue=420, mode="CLASSIC", game_id=7700001, ally_tier="GOLD", enemy_tier="SILVER"):
    raw = spectator_game(game_id=game_id, queue=queue, length=length)
    raw["gameMode"] = mode
    game = parse_spectator(raw, ME_PUUID, ME)
    for side, tier in (("ally", ally_tier), ("enemy", enemy_tier)):
        for p in game["teams"][side]:
            p["rank"] = {"solo": {"tier": tier, "division": "II", "lp": 40}, "flex": None} if tier and not p["hidden"] else None
            p.pop("_puuid", None)
    return {"game": {k: game[k] for k in ("game_id", "mode", "queue_id", "queue_label", "map_id", "started_ms", "length_s")},
            "teams": game["teams"], "bans": game["bans"], "scouted": {"riot_id": ME, "is_main": True}}


class Rig:
    def __init__(self, tmp_path, fake=None, patch="16.21"):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = tmp_path / "lol.db"
        Store(self.db).close()
        self.fake = fake or FakeMcp()
        self.client = McpClient(session=self.fake)
        self.svc = dl.DuelosService(lambda: Store(self.db), tmp_path / "winprob_model.json", client=self.client,
                                    patch_func=lambda: patch, clock=lambda: NOW)

    def build(self, report=None, registered=()):
        return self.svc.build(report or make_report(), "la1", ME, registered)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def lane(out, role):
    return next(l for l in out["lanes"] if l["role"] == role)


# -------------------------------------------------------------------- roles
def test_smite_sends_the_champion_to_the_jungle_and_rates_fill_the_rest(rig):
    out = rig.build()
    ally = {l["role"]: l["ally"]["champion_id"] for l in out["lanes"]}
    enemy = {l["role"]: l["enemy"]["champion_id"] for l in out["lanes"]}
    assert ally == {"TOP": 86, "JUNGLE": 113, "MIDDLE": 103, "BOTTOM": 222, "UTILITY": 117}      # Garen, Sejuani, Ahri, Jinx, Lulu
    assert enemy == {"TOP": 58, "JUNGLE": 64, "MIDDLE": 238, "BOTTOM": 145, "UTILITY": 412}      # Renekton, Lee Sin, Zed, Kai'Sa, Thresh
    assert not any(l["low_confidence"] for l in out["lanes"])


def scores(rates, smite=False, counts=None):
    return dl.role_scores({"spell_ids": [4, 11 if smite else 14]}, rates, counts)


def test_smite_is_decisive_without_the_champion_data():
    plain = [scores(None) for _ in range(4)] + [scores(None, smite=True)]
    roles, conf = dl.assign_roles(plain)
    assert roles[4] == "JUNGLE" and "JUNGLE" not in roles[:4] and conf[4] > 0.95


def test_a_champion_that_plays_two_lanes_goes_where_the_rest_of_the_team_leaves_room():
    flex = {"mid": 0.5, "adc": 0.5}
    team = [scores({"top": 1.0}), scores({"jungle": 1.0}, smite=True), scores({"mid": 1.0}), scores(flex),
            scores({"support": 1.0})]
    roles, conf = dl.assign_roles(team)
    assert roles == ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"] and min(conf) > 0.9


def test_two_interchangeable_players_are_marked_as_low_confidence():
    flex = {"mid": 0.5, "adc": 0.5}
    team = [scores({"top": 1.0}), scores({"jungle": 1.0}, smite=True), scores(flex), scores(flex), scores({"support": 1.0})]
    roles, conf = dl.assign_roles(team)
    assert sorted(roles) == sorted(dl.ROLE_ORDER)
    assert conf[2] < dl.LOW_CONFIDENCE and conf[3] < dl.LOW_CONFIDENCE and conf[0] > 0.9


def test_a_registered_players_own_history_breaks_the_tie():
    flex = {"mid": 0.5, "adc": 0.5}
    mid_main = Counter({"MIDDLE": 18, "BOTTOM": 2})
    team = [scores({"top": 1.0}), scores({"jungle": 1.0}, smite=True), scores(flex, counts=mid_main), scores(flex), scores({"support": 1.0})]
    roles, conf = dl.assign_roles(team)
    assert roles[2] == "MIDDLE" and roles[3] == "BOTTOM" and conf[2] > dl.LOW_CONFIDENCE
    few = Counter({"MIDDLE": 3})
    assert dl.role_scores({"spell_ids": []}, flex, few) == dl.role_scores({"spell_ids": []}, flex, None)     # too few games to count


def test_a_champion_unknown_to_opgg_can_play_any_lane_but_still_counts_smite():
    s = scores(None)
    assert s["TOP"] == s["MIDDLE"] and s["JUNGLE"] < s["TOP"]
    assert scores(None, smite=True)["JUNGLE"] > scores(None, smite=True)["TOP"]


def test_low_confidence_lane_is_flagged_in_the_report(tmp_path):
    fake = FakeMcp()
    rig = Rig(tmp_path, fake)
    report = make_report()
    # Zed (mid .85 / jungle .05) and Kai'Sa (adc .7 / mid .3) are fine; make Ahri flexible by swapping her rates
    import opgg_fixtures
    old = dict(opgg_fixtures.ROLE_RATES["Ahri"])
    opgg_fixtures.ROLE_RATES["Ahri"] = {"mid": 0.5, "adc": 0.5}
    opgg_fixtures.ROLE_RATES["Jinx"] = {"mid": 0.5, "adc": 0.5}
    try:
        out = rig.build(report)
    finally:
        opgg_fixtures.ROLE_RATES["Ahri"] = old
        opgg_fixtures.ROLE_RATES["Jinx"] = {"adc": 1.0}
    assert lane(out, "MIDDLE")["low_confidence"] and lane(out, "BOTTOM")["low_confidence"]
    assert not lane(out, "TOP")["low_confidence"]


# ----------------------------------------------------------------- matchups
@pytest.mark.parametrize("games,wins,tone", [
    (1000, 520, "fav"), (1000, 521, "fav"), (1000, 519, "even"), (1000, 500, "even"), (1000, 481, "even"),
    (1000, 480, "unfav"), (1000, 300, "unfav"), (200, 104, "fav"), (199, 150, "few"), (0, 0, "few")])
def test_thresholds_of_the_chip(games, wins, tone):
    assert dl.tone_of(games, wins) == tone


def test_each_lane_is_ally_against_enemy_from_the_allys_side(rig):
    out = rig.build()
    assert out["status"] == "ok" and out["source"] == "OP.GG" and out["scope"] == "todos los rangos" and out["patch"] == "16.21"
    top = lane(out, "TOP")
    assert top["games"] == 2400 and top["wr"] == pytest.approx(0.54) and top["wr_text"] == "54%" and top["tone"] == "fav"
    assert lane(out, "JUNGLE")["tone"] == "unfav" and lane(out, "JUNGLE")["wr_text"] == "47%"
    assert lane(out, "MIDDLE")["tone"] == "even" and lane(out, "MIDDLE")["wr_text"] == "51%"
    assert lane(out, "BOTTOM")["wr_text"] == "54%" and lane(out, "BOTTOM")["ally"]["is_viewer"]


def test_few_games_hide_the_percentage(rig):
    sup = lane(rig.build(), "UTILITY")
    assert sup["games"] == 150 and sup["wr"] is None and sup["wr_text"] == "" and sup["tone"] == "few"


def test_missing_pairs_make_a_partial_report(tmp_path):
    fake = FakeMcp()
    del fake.matchups[("mid", "Ahri", "Zed")]
    out = Rig(tmp_path, fake).build()
    assert out["status"] == "partial" and lane(out, "MIDDLE")["tone"] == "none" and lane(out, "MIDDLE")["games"] is None
    assert lane(out, "TOP")["tone"] == "fav"


def test_no_pair_at_all_is_nodata_not_an_error(tmp_path):
    fake = FakeMcp()
    fake.matchups.clear()
    out = Rig(tmp_path, fake).build()
    assert out["status"] == "nodata" and out["message"] == "Sin datos de OP.GG" and out["prob"] is None


def test_opgg_down_means_no_data_and_never_raises(tmp_path):
    fake = FakeMcp()
    fake.fail_tools["lol_list_champions"] = lambda a: FakeResponse(503, None)
    out = Rig(tmp_path, fake).build()
    assert out == {"status": "error", "message": "Sin datos de OP.GG"}


def test_every_matchup_failing_is_an_error(tmp_path):
    fake = FakeMcp()
    fake.fail_tools["lol_get_lane_matchup_guide"] = lambda a: FakeResponse(500, None)
    assert Rig(tmp_path, fake).build()["status"] == "error"


def test_synergy_failure_only_drops_the_duo(tmp_path):
    fake = FakeMcp()
    fake.fail_tools["lol_get_champion_synergies"] = lambda a: FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32600, "message": "Invalid position or champion specified"}})
    out = Rig(tmp_path, fake).build()
    assert out["status"] == "ok" and out["duo"]["ally"]["games"] is None and out["prob"] is not None


def test_bot_lane_duo_for_both_teams(rig):
    duo = rig.build()["duo"]
    assert duo["ally"] == {"adc_id": 222, "support_id": 117, "games": 607, "wr": pytest.approx(305 / 607), "wr_text": "50%"}
    assert duo["enemy"]["games"] == 900 and duo["enemy"]["wr_text"] == "49%" and duo["enemy"]["adc_id"] == 145


def test_calls_per_game_are_about_ten_and_a_second_game_is_free(rig):
    out = rig.build()
    assert rig.fake.count("lol_get_lane_matchup_guide") == 5 and rig.fake.count("lol_get_champion_synergies") == 2
    assert rig.fake.count("lol_list_champions") == 1 and rig.fake.count("lol_list_lane_meta_champions") == 1
    assert out["calls"] == 9
    again = rig.build()
    assert again["calls"] == 0 and len(rig.fake.tool_calls) == 9


def test_at_most_two_calls_in_flight(tmp_path):
    fake = FakeMcp()
    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    original = fake.post

    def slow(url, json=None, headers=None, timeout=None):
        if json.get("method") == "tools/call":
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.03)
            try:
                return original(url, json=json, headers=headers, timeout=timeout)
            finally:
                with lock:
                    state["now"] -= 1
        return original(url, json=json, headers=headers, timeout=timeout)

    fake.post = slow
    assert Rig(tmp_path, fake).build()["status"] == "ok"
    assert state["max"] == 2


def test_games_without_lanes_are_skipped_without_calling_opgg(rig):
    for report in (make_report(queue=450, mode="ARAM"), make_report(queue=1700, mode="CHERRY")):
        report["game"]["map_id"] = 12 if report["game"]["mode"] == "ARAM" else 30
        assert rig.build(report) == {"status": "na"}
    short = make_report()
    short["teams"]["enemy"] = short["teams"]["enemy"][:3]
    assert rig.build(short) == {"status": "na"} and rig.fake.tool_calls == []


# ----------------------------------------------------- own record in the lane
def add_games(db, wins, losses, enemy_champion_id=145, my_role="BOTTOM", viewer=("Yo", "LAN")):
    store = Store(db)
    for i in range(wins + losses):
        m = make_match(f"LA1_P{i}_{wins}{losses}", win=i < wins)
        parts = m["info"]["participants"]
        for p in parts:
            p["championId"] = 1
        me = next(p for p in parts[:5] if p["teamPosition"] == my_role)
        me.update({"riotIdGameName": viewer[0], "riotIdTagline": viewer[1], "championId": 222, "win": i < wins})
        opp = next(p for p in parts[5:] if p["teamPosition"] == my_role)
        opp.update({"championId": enemy_champion_id})
        store.save_match(m["metadata"]["matchId"], m)
    store.close()


def test_your_record_against_that_champion_shows_from_three_games(tmp_path):
    rig = Rig(tmp_path)
    add_games(rig.db, wins=3, losses=1)
    own = lane(rig.build(), "BOTTOM")["personal"]
    assert own == {"games": 4, "wins": 3, "text": "3V-1D"}
    assert all(l["personal"] is None for l in rig.build()["lanes"] if l["role"] != "BOTTOM")


def test_your_record_is_hidden_below_three_games_and_for_other_champions(tmp_path):
    rig = Rig(tmp_path)
    add_games(rig.db, wins=1, losses=1)
    assert lane(rig.build(), "BOTTOM")["personal"] is None
    rig2 = Rig(tmp_path / "second")
    add_games(rig2.db, wins=4, losses=0, enemy_champion_id=99)
    assert lane(rig2.build(), "BOTTOM")["personal"] is None


def test_the_roster_players_main_role_comes_from_the_database(tmp_path):
    rig = Rig(tmp_path)
    add_games(rig.db, wins=6, losses=6, my_role="BOTTOM")
    rig.svc.personal.load([ME])
    assert rig.svc.personal.roles(ME)["BOTTOM"] == 12 and rig.svc.personal.roles("Nadie#000") is None
    assert rig.svc.personal.record(ME, "BOTTOM", 145) == {"games": 12, "wins": 6}


# --------------------------------------------------------- the probability
def test_probability_is_shown_stored_and_uses_ranks_and_lanes(rig):
    out = rig.build()
    prob = out["prob"]
    assert 0.25 <= prob["value"] <= 0.75 and prob["text"] == f"{round(100 * prob['value'])}%"
    assert prob["lanes_used"] == 5 and prob["rank_used"] and prob["syn_used"] and prob["syn_sides"] == 2          # the 150-game lane counts, shrunk
    assert prob["label"] == "estimación con datos de OP.GG; aún sin calibrar" and not prob["calibrated"]
    store = Store(rig.db)
    row = store.pending_predictions()[0]
    store.close()
    assert row["match_id"] == "LA1_7700001" and row["scouted"] == ME and row["p"] == pytest.approx(prob["value"])
    assert row["features"]["lanes_used"] == 5 and row["model"] == "v1-por-defecto"


def test_better_ranked_allies_raise_the_estimate(tmp_path):
    ahead, behind = Rig(tmp_path / "a"), Rig(tmp_path / "b")
    hi = ahead.build(make_report(ally_tier="DIAMOND", enemy_tier="SILVER", game_id=1))["prob"]["value"]
    lo = behind.build(make_report(ally_tier="SILVER", enemy_tier="DIAMOND", game_id=1))["prob"]["value"]
    assert hi > lo


def test_estimate_made_late_in_the_game_is_shown_but_not_stored(rig):
    out = rig.build(make_report(length=600))
    assert out["prob"] is not None
    store = Store(rig.db)
    assert store.pending_predictions() == []
    store.close()


def test_too_few_lanes_means_no_probability_and_nothing_stored(tmp_path):
    fake = FakeMcp()
    for key in [("top", "Garen", "Renekton"), ("jungle", "Sejuani", "Lee Sin"), ("mid", "Ahri", "Zed")]:
        del fake.matchups[key]
    rig = Rig(tmp_path, fake)
    out = rig.build()
    assert out["status"] == "partial" and out["prob"] is None
    store = Store(rig.db)
    assert store.pending_predictions() == []
    store.close()


def test_the_game_result_closes_the_prediction_and_calibration_appears_at_20(tmp_path):
    rig = Rig(tmp_path)
    store = Store(rig.db)
    for i in range(20):
        wp.record_prediction(store, "LA1", 1000 + i, ME, {"lane": 0.3, "syn": 0, "rank": 0, "lanes_used": 5}, 0.6, "v1-por-defecto", NOW - 5000 + i)
        m = make_match(f"LA1_{1000 + i}", my_pid=3, win=i % 5 != 0)
        m["info"]["participants"][2].update({"riotIdGameName": "Yo", "riotIdTagline": "LAN"})
        store.save_match(f"LA1_{1000 + i}", m)
    store.close()
    out = rig.build(make_report(game_id=5000))
    calib = out["calibration"]
    assert calib["n"] == 20 and calib["accuracy_text"] == "80%"
    assert "Acierta el 80% (20 partidas, Brier" in out["prob"]["label"] and out["prob"]["calibrated"]


def test_with_100_results_the_weights_are_refitted_only_if_better(tmp_path):
    rig = Rig(tmp_path)
    rng = random.Random(5)
    store = Store(rig.db)
    for i in range(500):
        lane_f = rng.uniform(-1.5, 1.5)
        win = rng.random() < wp.sigmoid(2.5 * lane_f)
        wp.record_prediction(store, "LA1", 2000 + i, ME, {"lane": lane_f, "syn": 0, "rank": 0, "lanes_used": 5},
                             wp.clamp(wp.sigmoid(0.5 * lane_f)), "v1-por-defecto", NOW - 9000 + i)
        m = make_match(f"LA1_{2000 + i}", my_pid=3, win=win)
        m["info"]["participants"][2].update({"riotIdGameName": "Yo", "riotIdTagline": "LAN"})
        store.save_match(f"LA1_{2000 + i}", m)
    store.close()
    rig.build(make_report(game_id=6000))
    weights, name = rig.svc.model.weights()
    assert name.startswith("v1-ajustado") and weights["w_lane"] > 1.0
    again = rig.build(make_report(game_id=6001))
    assert again["prob"]["model"].startswith("v1-ajustado")
    store = Store(rig.db)
    assert any(r["model"].startswith("v1-ajustado") for r in store.pending_predictions())
    store.close()
