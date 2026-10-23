import json
import random
import threading

import pytest
from conftest import ME_PUUID, make_match, make_timeline

from lolan import goldmodel as gm
from lolan.store import Store

@pytest.fixture(autouse=True)
def autofit_on(monkeypatch):
    monkeypatch.setattr(gm, "AUTOFIT", True)


TRUE_COEF = [-100.0, 136.0, 26.0, 380.0, 100.0, 185.0, 53.0]


def synthetic_games(n_games=60, noise=0.0, seed=1):
    rnd = random.Random(seed)
    games = []
    for g in range(n_games):
        rows = []
        for _ in range(40):
            minute = rnd.uniform(2, 35)
            cs = rnd.uniform(0, 9) * minute
            kills, assists = rnd.randint(0, 12), rnd.randint(0, 15)
            support = rnd.random() < 0.2
            x = gm.features(minute, cs, kills, assists, support)
            gold = sum(c * v for c, v in zip(TRUE_COEF, x)) + rnd.gauss(0, noise)
            rows.append((minute, cs, kills, assists, support, gold))
        games.append((f"LA1_{g}", rows))
    return games


def test_fit_recovers_known_coefficients():
    model = gm.fit(synthetic_games())
    for got, want in zip(model["coef"], TRUE_COEF):
        assert got == pytest.approx(want, rel=1e-6, abs=1e-5)
    assert model["games"] == 60 and model["rows"] == 60 * 40
    assert model["mae"] < 1e-3                          # noiseless data: the holdout error is ~0


def test_fit_with_noise_reports_holdout_error_per_phase():
    model = gm.fit(synthetic_games(n_games=120, noise=200.0, seed=3))
    for got, want in zip(model["coef"], TRUE_COEF):
        assert got == pytest.approx(want, rel=0.15, abs=40)
    assert 100 < model["mae"] < 300                     # gaussian noise sd 200 -> mean abs about 160
    assert set(model["errors"]) == {"early", "mid", "late", "end"}
    assert all(100 < e < 300 for e in model["errors"].values())
    assert model["held_out_games"] == sum(gm.is_holdout(f"LA1_{g}") for g in range(120)) > 0


def test_holdout_split_is_by_game_and_about_one_in_five(monkeypatch):
    ids = [f"LA1_{i}" for i in range(1000)]
    held = [gm.is_holdout(i) for i in ids]
    assert 0.15 < sum(held) / len(held) < 0.25
    assert held == [gm.is_holdout(i) for i in ids]      # stable
    games = synthetic_games(30)
    seen = []
    orig = gm.is_holdout
    monkeypatch.setattr(gm, "is_holdout", lambda gid: seen.append(gid) or orig(gid))
    gm.fit(games)
    assert seen == [gid for gid, _ in games]            # decided once per game, never per frame


def test_fit_refuses_too_little_data_and_singular_data():
    with pytest.raises(ValueError):
        gm.fit(synthetic_games(n_games=5))
    constant = [(f"G{i}", [(10.0, 50, 1, 1, False, 3000.0)] * 60) for i in range(30)]
    with pytest.raises(ValueError):
        gm.fit(constant)


def test_solve_linear_uses_pivoting():
    assert gm.solve_linear([[0, 2, 4], [1, 1, 3]]) == pytest.approx([1.0, 2.0])   # first pivot is 0
    with pytest.raises(ValueError):
        gm.solve_linear([[1, 1, 2], [2, 2, 4]])


def test_defaults_when_there_is_no_data(tmp_path):
    store = gm.GoldModelStore(tmp_path / "gold_model.json")
    model = store.model()
    assert model.source == "default" and model.games == 0
    assert model.coef == gm.DEFAULT_COEF and model.error_at(5) == 183 and model.error_at(35) == 734
    assert "valores de referencia" in model.legend(5)


def test_estimate_rounds_to_50_and_clamps_to_starting_gold():
    model = gm.GoldModel()
    est = model.estimate(14.0, 100, 2, 3, False)
    assert est % 50 == 0
    assert abs(est - model.raw(14.0, 100, 2, 3, False)) <= 25
    assert model.estimate(0.5, 0, 0, 0, False) == 500           # never below the 500 starting gold
    assert model.estimate(14.0, 100, 2, 3, True) > est          # supports get more gold per minute


def test_unspent_rounding_and_clamping():
    assert gm.unspent_estimate(5000, 3980) == 1000
    assert gm.unspent_estimate(5000, 3949) == 1100
    assert gm.unspent_estimate(3000, 3500) == 0                 # never negative
    assert gm.unspent_estimate(3000, None) is None


def test_legend_text_with_a_fitted_model():
    model = gm.GoldModel.from_dict({"coef": TRUE_COEF, "games": 321,
                                    "errors": {"early": 150, "mid": 260.4, "late": 450, "end": 700}})
    assert model.legend(14.0) == (
        "Estimado con tu CS, kills, asistencias y el minuto, calibrado con 321 partidas guardadas "
        "(error típico ±260 en esta fase). Riot solo da el oro real de quien está jugando.")


def test_model_file_is_validated(tmp_path):
    path = tmp_path / "gold_model.json"
    path.write_text("{ not json", encoding="utf-8")
    assert gm.GoldModelStore(path).model().source == "default"
    path.write_text(json.dumps({"coef": [1, 2], "errors": {}}), encoding="utf-8")
    assert gm.GoldModelStore(path).model().source == "default"


def test_persistence_roundtrip_and_reload_on_change(tmp_path):
    path = tmp_path / "data" / "gold_model.json"
    store = gm.GoldModelStore(path)
    data = gm.fit(synthetic_games())
    data["stored_matches"] = 60
    store.save(data)
    model = store.model()
    assert model.source == "fit" and model.games == 60 and model.coef[1] == pytest.approx(136.0)
    assert gm.GoldModelStore(path).model().coef == model.coef
    store.save(dict(data, coef=[0.0] * 7))
    assert store.model().coef == [0.0] * 7


def test_refit_trigger(tmp_path):
    path = tmp_path / "gold_model.json"
    counts = [100]
    fits = []

    def fit_func():
        fits.append(1)
        return gm.fit(synthetic_games(30))

    clock = [0.0]
    store = gm.GoldModelStore(path, fit_func=fit_func, count_func=lambda: counts[0], clock=lambda: clock[0])
    assert store.due(100) and not store.due(0)                 # missing file -> due (unless nothing is stored)
    assert store.maybe_refit()
    store.join(10)
    assert len(fits) == 1 and path.exists() and store.model().source == "fit"
    assert json.loads(path.read_text())["stored_matches"] == 100

    clock[0] += gm.CHECK_EVERY_S + 1
    counts[0] = 100 + gm.MIN_NEW_MATCHES - 1
    assert not store.maybe_refit()                             # 19 new matches: not yet
    clock[0] += gm.CHECK_EVERY_S + 1
    counts[0] = 100 + gm.MIN_NEW_MATCHES
    assert store.maybe_refit()                                 # 20 new: refit
    store.join(10)
    assert len(fits) == 2 and json.loads(path.read_text())["stored_matches"] == 120


def test_refit_is_throttled_and_never_blocks(tmp_path):
    clock = [0.0]
    gate = threading.Event()

    def slow_fit():
        gate.wait(5)
        return gm.fit(synthetic_games(30))

    store = gm.GoldModelStore(tmp_path / "g.json", fit_func=slow_fit, count_func=lambda: 50,
                              clock=lambda: clock[0])
    assert store.maybe_refit()                                 # returns at once while the fit runs
    assert store.fitting
    assert store.model().source == "default"                   # readers keep the defaults meanwhile
    clock[0] += 100
    assert not store.maybe_refit()                             # one fit at a time
    gate.set()
    store.join(10)
    assert store.model().source == "fit"


def test_failed_fit_keeps_defaults_and_does_not_retry_every_minute(tmp_path):
    tries = []
    clock = [0.0]

    def bad_fit():
        tries.append(1)
        raise ValueError("muy pocas partidas")

    store = gm.GoldModelStore(tmp_path / "g.json", fit_func=bad_fit, count_func=lambda: 7,
                              clock=lambda: clock[0])
    assert store.maybe_refit()
    store.join(5)
    assert store.model().source == "default" and store.last_error == "muy pocas partidas"
    clock[0] += 1000
    assert not store.maybe_refit() and len(tries) == 1          # same data: do not try again


def test_autofit_can_be_switched_off(tmp_path, monkeypatch):
    store = gm.GoldModelStore(tmp_path / "g.json", fit_func=lambda: {}, count_func=lambda: 50)
    monkeypatch.setattr(gm, "AUTOFIT", False)
    assert not store.maybe_refit()


# ------------------------------------------------- reading stored timelines
def test_rows_from_timeline_counts_kills_assists_and_support():
    match = make_match("LA1_1")
    match["info"]["participants"][4]["teamPosition"] = "UTILITY"        # participant 5
    rows = gm.rows_from_timeline(match, make_timeline(minutes=8))
    assert all(r[0] >= gm.MIN_FIT_MINUTE for r in rows)
    # participant 3 (me): cs 8 per minute -> 64 at minute 8, gold 500 + 400 * 8
    mine = [r for r in rows if r[0] == 8.0 and r[1] == 64]
    assert len(mine) == 1 and mine[0][5] == 500 + 400 * 8 and not mine[0][4]
    # default events: I killed participant 7 at 5:30 -> one kill; the 12:00 assist is not reached yet
    assert mine[0][2] == 1 and mine[0][3] == 0
    others = [r for r in rows if r[0] == 8.0 and r[1] == 48]            # cs 6 per minute
    assert sum(1 for r in others if r[3] == 1) >= 1                     # participant 1 assisted the 5:30 kill
    assert sum(1 for r in rows if r[4]) == 14                           # both supports (pid 5 and 10), minutes 2..8


def test_fit_from_db_uses_only_solo_flex_games_with_timeline(tmp_path):
    db = tmp_path / "lol.db"
    store = Store(db)
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    rnd = random.Random(5)

    def jitter(pfs, m):
        for pf in pfs.values():
            pf["totalGold"] = 500 + rnd.randint(250, 350) * m

    for i in range(40):
        store.save_match(f"LA1_{i}", make_match(f"LA1_{i}", queue=420 if i % 2 else 440),
                         make_timeline(minutes=20, tweak=jitter))
    store.save_match("LA1_ARAM", make_match("LA1_ARAM", queue=450), make_timeline(minutes=20))
    store.save_match("LA1_NOTL", make_match("LA1_NOTL", queue=420), None)
    store.close()
    ids = [gid for gid, _ in gm.games_from_db(db)]
    assert len(ids) == 40 and "LA1_ARAM" not in ids and "LA1_NOTL" not in ids
    model = gm.fit_from_db(db)
    assert model["games"] == 40 and len(model["coef"]) == gm.N_FEATURES



def test_level_model_is_fitted_when_rows_carry_the_level():
    rng = random.Random(3)
    true = [-250.0, 310.0, 30.0, 400.0, 70.0, 250.0, -90.0]
    games = []
    for g in range(40):
        rows = []
        for minute in range(2, 30):
            level, kills, assists = min(18, 1 + minute * 0.6 + rng.random()), rng.randint(0, 5), rng.randint(0, 8)
            sup = g % 5 == 0
            x = gm.features_level(minute, level, kills, assists, sup)
            gold = sum(c * v for c, v in zip(true, x))
            rows.append((float(minute), minute * 7.0 + rng.randint(0, 20), kills, assists, sup, gold, level))
        games.append((f"G{g}", rows))
    data = gm.fit(games)
    assert all(abs(a - b) < 1e-6 for a, b in zip(data["coef_level"], true))
    model = gm.GoldModel.from_dict(data)
    assert model.coef_level == data["coef_level"]
    assert model.estimate_by_level(10, 7, 1, 2, False) % 50 == 0


def test_old_model_file_without_level_coefficients_uses_defaults():
    model = gm.GoldModel.from_dict({"coef": gm.DEFAULT_COEF, "errors": gm.DEFAULT_ERRORS})
    assert model.coef_level == gm.DEFAULT_COEF_LEVEL
    assert model.error_at(5, by_level=True) == round(gm.DEFAULT_ERRORS_LEVEL["early"])
