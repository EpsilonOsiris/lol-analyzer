from types import SimpleNamespace

import pytest
from live_fixtures import make_allgamedata

from lolan import live, livectx


def game(champion="Jinx", role="BOTTOM", cs7=50, cs14=100, cs21=150, gold14=200, cpm=7.0):
    return {"champion": champion, "role": role, "cs_7": cs7, "cs_14": cs14, "cs_21": cs21,
            "gold_diff_14": gold14, "cs_per_min": cpm, "match_id": f"M{id(object())}"}


def games_pool():
    jinx = [game(cs7=50 + i, cs14=100 + i, cs21=150 + i, gold14=100 + 10 * i) for i in range(4)]   # avg cs14 101.5
    other_adc = [game("Ezreal", "BOTTOM", cs14=90 + i) for i in range(3)]
    mid = [game("Ahri", "MIDDLE", cs14=80 + i) for i in range(3)]
    return jinx + other_adc + mid


def test_baseline_prefers_the_champion():
    base = livectx.baselines(games_pool(), "Jinx", "BOTTOM", "Jinx", "ADC")
    assert base["scope"] == "champion" and base["games"] == 4
    assert base["cs"][14] == pytest.approx(101.5) and base["cs"][7] == pytest.approx(51.5)
    assert base["gold_diff_14"] == pytest.approx(115.0)
    assert base["scope_label"] == "con Jinx"


def test_baseline_falls_back_to_role_then_all():
    pool = games_pool()
    base = livectx.baselines(pool, "Caitlyn", "BOTTOM", "Caitlyn", "ADC")   # never played her
    assert base["scope"] == "role" and base["games"] == 7 and "ADC" in base["scope_label"]
    base = livectx.baselines(pool, "Caitlyn", "TOP", "Caitlyn", "Top")      # never played top
    assert base["scope"] == "all" and base["games"] == len(pool)
    two = [game(), game()]
    assert livectx.baselines(two, "Jinx", "BOTTOM") is None                  # too little to compare


def test_baseline_champion_match_ignores_case_and_punctuation():
    pool = [game("KaiSa", cs14=100 + i) for i in range(3)]
    assert livectx.baselines(pool, "Kaisa", "BOTTOM")["scope"] == "champion"


def test_reference_cs_interpolates_and_stops_after_21():
    cs = {7: 50, 14: 100, 21: 150}
    assert livectx.reference_cs(cs, 0) == 0
    assert livectx.reference_cs(cs, 3.5 * 60) == pytest.approx(25)
    assert livectx.reference_cs(cs, 14 * 60) == pytest.approx(100)
    assert livectx.reference_cs(cs, 845) == pytest.approx(100 + 50 * (845 - 840) / 420)
    assert livectx.reference_cs(cs, 22 * 60) is None
    assert livectx.reference_cs({7: 50, 14: None, 21: None}, 600) is None


def state_at(t=845.0):
    st = live.normalize(make_allgamedata(t=t))
    st["me"]["checkpoints"] = {"7": {"cs": 55, "clock": "7:01"}}
    return st


def test_cs_context_numbers():
    base = livectx.baselines(games_pool(), "Jinx", "BOTTOM", "Jinx", "ADC")
    ctx = livectx.cs_context(state_at(), base)
    assert ctx["now"] == 112 and ctx["clock"] == "14:05" and ctx["scope_label"] == "con Jinx"
    assert ctx["reference"] == round(base["cs"][14] + (base["cs"][21] - base["cs"][14]) * 5 / 420)
    p7, p14, p21 = ctx["points"]
    assert p7["live"] == 55 and p7["reached"] and p14["live"] is None and p14["reached"]
    assert not p21["reached"]
    assert ctx["gold_diff_14"] == pytest.approx(115.0) and ctx["games"] == 4


def test_cs_context_without_history_is_marked():
    ctx = livectx.cs_context(state_at(), None)
    assert ctx["has_base"] is False and ctx["now"] == 112 and "reference" not in ctx


PLAN = {"enough": True, "games": 12, "min_games": 8, "priorities": [
    {"key": "early_deaths", "label": "Muertes antes del minuto 14", "goal": 1},
    {"key": "after_kill", "label": "Muertes en el minuto siguiente a una kill tuya", "goal": 0},
    {"key": "unspent", "label": "Muertes con 1000 de oro o más sin gastar", "goal": 0},
]}


def test_plan_progress_counts_live_events_neutrally():
    prog = livectx.plan_progress(PLAN, state_at())
    early, after, unspent = prog["goals"]
    assert early["value"] == 2 and early["within"] is False and early["text"] == "2 · meta: máximo 1"
    assert after["value"] == 1 and after["text"] == "1 · meta: ninguna"
    assert unspent["live"] is False and unspent["value"] is None and "en vivo" in unspent["text"]


def test_plan_progress_all_counters():
    st = state_at()
    st["me_events"]["deaths"] = [
        {"t": 100.0, "involved": 3, "jungler_involved": True, "after_own_kill": False},
        {"t": 900.0, "involved": 1, "jungler_involved": False, "after_own_kill": True},
        {"t": 839.9, "involved": 4, "jungler_involved": False, "after_own_kill": True},
        {"t": 840.0, "involved": 0, "jungler_involved": False, "after_own_kill": False},
    ]
    plan = {"enough": True, "games": 9, "min_games": 8, "priorities": [
        {"key": k, "label": k, "goal": 1} for k in ("early_deaths", "after_kill", "outnumbered", "jungler_early")]}
    values = {g["key"]: g["value"] for g in livectx.plan_progress(plan, st)["goals"]}
    assert values == {"early_deaths": 2, "after_kill": 2, "outnumbered": 2, "jungler_early": 1}


def test_plan_progress_without_enough_games():
    prog = livectx.plan_progress({"enough": False, "games": 3, "min_games": 8, "priorities": []}, state_at())
    assert prog == {"enough": False, "games": 3, "min_games": 8, "goals": []}
    assert livectx.plan_progress(None, state_at())["enough"] is False


def test_no_advice_in_the_wording():
    prog = livectx.plan_progress(PLAN, state_at())
    blob = " ".join(g["text"] + g["label"] for g in prog["goals"]).lower()
    for word in ("debes", "evita", "vuelve", "ahora", "deberías", "cuidado"):
        assert word not in blob


class FakeData(SimpleNamespace):
    pass


def test_live_context_builds_and_caches_the_plan():
    calls = []
    clock = [0.0]

    def get_data():
        calls.append(1)
        return FakeData(games=games_pool(), total=10, analyses={})

    ctx = livectx.LiveContext(get_data, clock=lambda: clock[0])
    out = ctx.build(state_at())
    assert out["cs"]["scope_label"] == "con Jinx" and out["has_games"]
    assert out["plan"]["enough"] is False                        # no analyzed timelines
    ctx.build(state_at())
    assert len(calls) == 1                                       # refreshed at most every REFRESH_S
    clock[0] = livectx.REFRESH_S + 1
    ctx.build(state_at())
    assert len(calls) == 2


def test_live_context_without_stored_games():
    ctx = livectx.LiveContext(lambda: FakeData(games=[], total=0, analyses={}))
    out = ctx.build(state_at())
    assert out["has_games"] is False and out["cs"]["has_base"] is False
    assert livectx.LiveContext(lambda: None).build(state_at())["has_games"] is False
