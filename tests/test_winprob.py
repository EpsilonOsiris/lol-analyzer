"""Win probability: formula, clamping, features, predictions storage, outcome join, calibration, refit."""
import math
import random

import pytest
from conftest import make_match

from lolan import winprob as wp
from lolan.store import Store


def lane(games, wins):
    return {"games": games, "wins": wins}


def rank(tier, division="II", lp=0, queue="solo"):
    return {queue: {"tier": tier, "division": division, "lp": lp}, **({"flex": None} if queue == "solo" else {"solo": None})}


# ----------------------------------------------------------------- the math
def test_logit_and_sigmoid_are_inverses():
    for p in (0.1, 0.5, 0.73):
        assert wp.sigmoid(wp.logit(p)) == pytest.approx(p)
    assert wp.sigmoid(-800) == pytest.approx(0.0) and wp.sigmoid(800) == pytest.approx(1.0)


def test_rates_are_shrunk_toward_half():
    assert wp.shrunk(0, 0) == 0.5
    assert wp.shrunk(60, 100) == pytest.approx(160 / 300)
    assert wp.shrunk(5400, 10000) == pytest.approx(0.5392, abs=1e-3)       # a big sample barely moves


def test_no_data_means_even_and_no_estimate():
    f = wp.features([None] * 5, None, None, None, None)
    assert f["lanes_used"] == 0 and f["lane"] == 0 and wp.predict(f) is None
    f = wp.features([lane(1000, 520), lane(1000, 520)] + [None] * 3, None, None, None, None)
    assert wp.predict(f) is None                                           # two lanes are not enough


def test_probability_follows_the_documented_formula():
    lanes = [lane(2000, 1100), lane(2000, 1100), lane(2000, 900), lane(2000, 1000), lane(2000, 1000)]
    syn_a, syn_e = lane(1000, 540), lane(1000, 480)
    f = wp.features(lanes, syn_a, syn_e, 1300.0, 1100.0)
    lane_sum = sum(wp.logit(wp.shrunk(l["wins"], l["games"])) for l in lanes)
    syn = wp.logit(wp.shrunk(540, 1000)) - wp.logit(wp.shrunk(480, 1000))
    assert f["lane"] == pytest.approx(lane_sum, abs=1e-4) and f["syn"] == pytest.approx(syn, abs=1e-4)
    assert f["rank"] == pytest.approx(0.5) and f["lanes_used"] == 5 and f["syn_used"] and f["rank_used"]
    assert f["syn_sides"] == 2
    w = wp.DEFAULT_WEIGHTS
    expected = wp.sigmoid(w["w0"] + w["w_lane"] * lane_sum + w["w_syn"] * syn + w["w_rank"] * 0.5)
    assert wp.predict(f) == pytest.approx(expected, abs=1e-4)


def test_defaults_are_conservative():
    assert wp.DEFAULT_WEIGHTS["w_lane"] == 0.5
    f = wp.features([lane(5000, 2700)] * 5, None, None, None, None)           # five 54% lanes
    assert 0.55 < wp.predict(f) < 0.65                                         # not the 80%+ of an independent product


def test_probability_is_clamped_to_25_75():
    hot = wp.features([lane(5000, 4500)] * 5, lane(5000, 4500), lane(5000, 500), None, None)
    cold = wp.features([lane(5000, 500)] * 5, None, None, None, None)
    assert wp.predict(hot) == 0.75 and wp.predict(cold) == 0.25


def test_pairs_with_very_few_games_do_not_count():
    f = wp.features([lane(10, 9)] * 5, lane(10, 9), lane(10, 1), None, None)
    assert f["lanes_used"] == 0 and not f["syn_used"] and f["syn"] == 0 and f["syn_sides"] == 0


def test_one_sided_duo_is_set_against_a_neutral_50():
    lanes = [lane(2000, 1000)] * 5
    ally = wp.features(lanes, lane(1000, 540), None, None, None)
    assert ally["syn_used"] and ally["syn_sides"] == 1
    assert ally["syn"] == pytest.approx(wp.logit(wp.shrunk(540, 1000)), abs=1e-4) and ally["syn"] > 0
    enemy = wp.features(lanes, None, lane(1000, 540), None, None)
    assert enemy["syn_used"] and enemy["syn_sides"] == 1
    assert enemy["syn"] == pytest.approx(-ally["syn"], abs=1e-4)
    thin = wp.features(lanes, lane(1000, 540), lane(10, 1), None, None)        # the enemy duo is too thin to count
    assert thin["syn_sides"] == 1 and thin["syn"] == pytest.approx(ally["syn"], abs=1e-4)
    both = wp.features(lanes, lane(1000, 540), lane(1000, 540), None, None)
    assert both["syn_sides"] == 2 and both["syn"] == pytest.approx(0.0, abs=1e-4)
    assert wp.predict(ally) > wp.predict(wp.features(lanes, None, None, None, None))


def test_stored_features_without_syn_sides_still_work():
    old = {"lane": 0.4, "syn": 0.2, "rank": 0.1, "lanes_used": 5, "syn_used": True, "rank_used": True}
    assert wp.predict(old) is not None and wp._vector(old) == [1.0, 0.4, 0.2, 0.1]
    rows = [{"features": old, "outcome": i % 2} for i in range(40)]
    assert set(wp.fit_weights(rows)) == set(wp.WEIGHT_KEYS)


def test_rank_points_and_team_average():
    assert wp.rank_points(rank("IRON", "IV", 0)) == 0
    assert wp.rank_points(rank("GOLD", "I", 50)) == 3 * 400 + 300 + 50
    assert wp.rank_points(rank("GOLD", "II", 10, queue="flex")) == 3 * 400 + 200 + 10
    assert wp.rank_points(rank("MASTER", "I", 100)) > wp.rank_points(rank("DIAMOND", "I", 99))
    assert wp.rank_points(None) is None and wp.rank_points({"solo": None, "flex": None}) is None
    players = [{"rank": rank("GOLD", "IV")}, {"rank": rank("PLATINUM", "IV")}, {"rank": None}, {}]
    assert wp.team_rank(players) == (1200 + 1600) / 2 and wp.team_rank([{}]) is None
    f = wp.features([None] * 5, None, None, 3000.0, 0.0)
    assert f["rank"] == wp.RANK_LIMIT                                           # capped


# --------------------------------------------------- predictions and outcomes
def put(store, game_id, p=0.6, scouted="Yo#LAN", at=1000, platform="LA1", **f):
    features = {"lane": 0.4, "syn": 0.0, "rank": 0.0, "lanes_used": 5, **f}
    return wp.record_prediction(store, platform, game_id, scouted, features, p, "v1-por-defecto", at)


def played(match_id, win, name="Yo", tag="LAN", duration=1500):
    m = make_match(match_id, my_pid=3, win=win, duration=duration)
    me = m["info"]["participants"][2]
    me["riotIdGameName"], me["riotIdTagline"] = name, tag
    return m


def test_prediction_is_stored_with_features_and_replaced_for_the_same_game(tmp_path):
    store = Store(tmp_path / "lol.db")
    assert put(store, 700, p=0.55) == "LA1_700"
    put(store, 700, p=0.6, at=2000)
    rows = store.pending_predictions()
    assert len(rows) == 1 and rows[0]["p"] == 0.6 and rows[0]["created_at"] == 2000
    assert rows[0]["features"]["lanes_used"] == 5 and rows[0]["model"] == "v1-por-defecto" and rows[0]["game_id"] == 700
    store.close()


def test_outcome_is_recorded_when_the_game_is_stored(tmp_path):
    store = Store(tmp_path / "lol.db")
    put(store, 1)
    put(store, 2)
    put(store, 3)
    put(store, 4, scouted="Otro#ZZZ")
    put(store, 5)
    store.save_match("LA1_1", played("LA1_1", True))
    store.save_match("LA1_2", played("LA1_2", False))
    store.save_match("LA1_4", played("LA1_4", True))                         # the scouted player is not in it
    store.save_match("LA1_5", played("LA1_5", True, duration=200))          # a remake
    assert wp.resolve_outcomes(store, now=5000) == 2
    done = {r["match_id"]: r["outcome"] for r in store.resolved_predictions()}
    assert done == {"LA1_1": 1, "LA1_2": 0}
    assert {r["match_id"] for r in store.pending_predictions()} == {"LA1_3", "LA1_4"}
    assert wp.resolve_outcomes(store, now=5000) == 0
    store.close()


def test_games_shorter_than_15_minutes_are_dropped_not_counted(tmp_path):
    store = Store(tmp_path / "lol.db")
    put(store, 1)
    put(store, 2)
    put(store, 3)
    store.save_match("LA1_1", played("LA1_1", False, duration=465))         # surrender at 7:45 with an AFK
    store.save_match("LA1_2", played("LA1_2", False, duration=16 * 60))     # a normal (short) game
    store.save_match("LA1_3", played("LA1_3", True, duration=wp.SHORT_GAME_S - 1))
    assert wp.resolve_outcomes(store, now=5000) == 1
    assert {r["match_id"]: r["outcome"] for r in store.resolved_predictions()} == {"LA1_2": 0}
    assert store.pending_predictions() == []                                # the short ones were deleted
    store.close()


def test_games_that_never_get_stored_are_dropped_after_30_days(tmp_path):
    store = Store(tmp_path / "lol.db")
    put(store, 9, at=1000)
    wp.resolve_outcomes(store, now=1000 + wp.PENDING_KEEP_S - 5)
    assert len(store.pending_predictions()) == 1
    wp.resolve_outcomes(store, now=1000 + wp.PENDING_KEEP_S + 5)
    assert store.pending_predictions() == []
    store.close()


# ------------------------------------------------------------- calibration
def resolved(n, p=0.7, hit_rate=0.5, seed=1):
    rng = random.Random(seed)
    return [{"p": p, "outcome": 1 if rng.random() < hit_rate else 0} for _ in range(n)]


def test_calibration_needs_20_results():
    assert wp.calibration(resolved(19)) is None
    c = wp.calibration([{"p": 0.7, "outcome": 1}] * 12 + [{"p": 0.7, "outcome": 0}] * 8)
    assert c["n"] == 20 and c["accuracy"] == 0.6 and c["accuracy_text"] == "60%"
    assert c["brier"] == pytest.approx((12 * 0.09 + 8 * 0.49) / 20) and c["brier_text"] == "0,25"
    assert wp.calibration([{"p": 0.4, "outcome": 0}] * 20)["accuracy"] == 1.0          # p < 0.5 predicts a loss


# ------------------------------------------------------------------ refit
def synthetic(n, true_weights, seed=7, start=0):
    """Rows whose outcomes follow `true_weights`."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        f = {"lane": rng.uniform(-1.5, 1.5), "syn": rng.uniform(-0.4, 0.4), "rank": rng.uniform(-2, 2), "lanes_used": 5}
        p = wp.sigmoid(true_weights["w0"] + true_weights["w_lane"] * f["lane"] + true_weights["w_syn"] * f["syn"]
                       + true_weights["w_rank"] * f["rank"])
        rows.append({"match_id": f"LA1_{start + i}", "created_at": start + i, "features": f,
                     "outcome": 1 if rng.random() < p else 0, "p": wp.clamp(p)})
    return rows


def test_fit_recovers_stronger_weights_on_enough_data():
    truth = {"w0": 0.1, "w_lane": 1.4, "w_syn": 0.5, "w_rank": 0.3}
    w = wp.fit_weights(synthetic(4000, truth))
    assert w["w_lane"] == pytest.approx(1.4, abs=0.2) and w["w0"] == pytest.approx(0.1, abs=0.1)


def test_fit_is_regularised_toward_the_defaults():
    rows = synthetic(30, {"w0": 0, "w_lane": 3.0, "w_syn": 0, "w_rank": 0})
    w = wp.fit_weights(rows)
    assert wp.DEFAULT_WEIGHTS["w_lane"] < w["w_lane"] < 3.0


def test_no_refit_below_100_results(tmp_path):
    model = wp.ModelFile(tmp_path / "winprob_model.json")
    assert wp.maybe_refit(synthetic(99, {"w0": 0, "w_lane": 2, "w_syn": 0, "w_rank": 0}), model) is None
    assert not model.path.exists() and model.weights() == (wp.DEFAULT_WEIGHTS, "v1-por-defecto")


def test_refit_switches_only_when_it_beats_the_defaults_on_the_holdout(tmp_path):
    good = wp.ModelFile(tmp_path / "good.json")
    out = wp.maybe_refit(synthetic(600, {"w0": 0, "w_lane": 1.6, "w_syn": 0.5, "w_rank": 0.1}), good, now=50)
    assert out["active"] and out["holdout"]["log_loss_fit"] < out["holdout"]["log_loss_default"] and out["n"] == 600
    weights, name = good.weights()
    assert weights["w_lane"] > 1.0 and name == "v1-ajustado-600"
    assert wp.maybe_refit(synthetic(610, {"w0": 0, "w_lane": 1.6, "w_syn": 0.5, "w_rank": 0.1}), good) is None   # too soon

    same = wp.ModelFile(tmp_path / "same.json")
    out = wp.maybe_refit(synthetic(200, wp.DEFAULT_WEIGHTS, seed=0), same, now=60)
    assert out is not None and not out["active"] and out["tried_n"] == 200
    assert same.weights() == (wp.DEFAULT_WEIGHTS, "v1-por-defecto")                      # defaults stay


def test_refit_holds_out_the_most_recent_games(tmp_path):
    # The first 75% follow stronger weights, the last 25% follow the defaults: the fit loses the hold-out.
    old = synthetic(450, {"w0": 0, "w_lane": 2.0, "w_syn": 0, "w_rank": 0}, seed=1)
    new = synthetic(150, wp.DEFAULT_WEIGHTS, seed=2, start=450)
    model = wp.ModelFile(tmp_path / "m.json")
    out = wp.maybe_refit(old + new, model)
    assert out["holdout"]["n"] == 150 and not out["active"]


def test_corrupt_model_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{oops", encoding="utf-8")
    assert wp.ModelFile(path).weights()[0] == wp.DEFAULT_WEIGHTS
    path.write_text('{"active": true, "weights": {"w0": "x"}}', encoding="utf-8")
    assert wp.ModelFile(path).weights()[0] == wp.DEFAULT_WEIGHTS


def test_log_loss_is_finite_for_extreme_values():
    assert math.isfinite(wp.log_loss([(0.0, 1), (1.0, 0)])) and wp.log_loss([]) == float("inf")
