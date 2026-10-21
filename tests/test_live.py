import copy
import threading

import pytest
from live_fixtures import ME, ev, make_allgamedata, with_events

from lolan import goldmodel, live


def state(**kw):
    return live.normalize(make_allgamedata(**kw))


# ------------------------------------------------------------- normalization
def test_teams_totals_and_order():
    st = state()
    assert st["clock"] == "14:05" and st["mode_label"] == "Clásico" and st["map_label"] == "Grieta del Invocador"
    ally, enemy = st["teams"]["ally"], st["teams"]["enemy"]
    assert [p["position"] for p in ally["players"]] == ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    assert [p["champion"] for p in enemy["players"]] == ["Renekton", "LeeSin", "Zed", "Kaisa", "Thresh"]
    assert ally["totals"]["kills"] == 2 + 1 + 3 + 5 + 0
    assert enemy["totals"]["kills"] == 4 + 4 + 5 + 2 + 0
    # towers/inhibitors/dragons/heralds come from the events, credited to the killing side
    assert ally["totals"]["towers"] == 1 and enemy["totals"]["towers"] == 0
    assert ally["totals"]["dragons"] == [{"type": "Fire", "label": "Infernal"}]
    assert enemy["totals"]["inhibitors"] == 1 and enemy["totals"]["heralds"] == 1
    assert ally["totals"]["barons"] == 0


def test_me_is_found_and_has_real_gold_and_stats():
    st = state()
    me = st["me"]
    assert me["champion"] == "Jinx" and me["position"] == "BOTTOM" and me["role_label"] == "ADC"
    assert me["gold"] == 1234 and me["cs"] == 112 and me["cs_per_min"] == pytest.approx(8.0)
    assert me["keystone"] == "Dominio del cazador"
    assert {s["key"]: s["value"] for s in me["stats"]}["attackSpeed"] == "1.31"
    assert [p["name"] for p in st["teams"]["ally"]["players"] if p["is_me"]] == ["Zorro Azul"]


def test_enemy_side_when_the_active_player_is_chaos():
    raw = make_allgamedata()
    for p in raw["allPlayers"]:
        p["team"] = "CHAOS" if p["team"] == "ORDER" else "ORDER"
    st = live.normalize(raw)
    assert st["teams"]["ally"]["side"] == "CHAOS"
    assert st["teams"]["ally"]["players"][3]["is_me"]


def test_feed_is_newest_first_in_spanish_with_champion_names():
    feed = state()["feed"]
    assert [e["t"] for e in feed] == sorted((e["t"] for e in feed), reverse=True)
    texts = [e["text"] for e in feed]
    assert "Jinx eliminó a Kai'Sa (con Lulu)" in texts
    assert "Kai'Sa eliminó a Jinx (con Thresh, Lee Sin)" in texts
    assert "Jinx murió a manos de una torre" in texts
    assert "Doble kill de Zed" in texts
    assert "Sejuani mató un dragón (Infernal)" in texts
    assert "Lee Sin mató al Heraldo de la Grieta" in texts
    assert "Primera sangre: Zed" in texts
    mine = {e["text"] for e in feed if e["mine"]}
    assert "Jinx murió a manos de una torre" in mine and "Doble kill de Zed" not in mine
    kill = next(e for e in feed if e["text"].startswith("Jinx eliminó"))
    assert kill["team"] == "ally"
    assert next(e for e in feed if e["kind"] == "dragon")["team"] == "ally"


def test_turret_killed_by_a_minion_is_credited_by_owner():
    # Turret_T2 belongs to CHAOS, so the kill is ORDER's (my team) even with a minion as the killer
    st = state()
    assert st["teams"]["ally"]["totals"]["towers"] == 1
    assert any(e["text"] == "El equipo aliado destruyó una torre" for e in st["feed"])


def test_champion_names_come_from_the_namer():
    st = live.normalize(make_allgamedata(), namer=lambda cid, api: {"Kaisa": "Kai'Sa (es)"}.get(cid, api))
    assert any("Kai'Sa (es)" in e["text"] for e in st["feed"])
    assert st["teams"]["enemy"]["players"][3]["champion_name"] == "Kai'Sa (es)"


def test_dead_players_and_respawn_timer():
    st = state(dead={"Gato Negro#T007": 18.4, ME: 7.0})
    jungle = st["teams"]["enemy"]["players"][1]
    assert jungle["dead"] and jungle["respawn"] == 18.4
    assert st["teams"]["ally"]["players"][3]["dead"]
    assert not st["teams"]["ally"]["players"][0]["dead"] and st["teams"]["ally"]["players"][0]["respawn"] == 0.0


def test_game_end_marks_finished_with_result():
    st = live.normalize(with_events(make_allgamedata(), ev(99, "GameEnd", 840.0, Result="Win")))
    assert st["ended"] and st["result"] == "win"
    assert st["feed"][0]["text"] == "Fin de la partida: victoria"
    lost = live.normalize(with_events(make_allgamedata(), ev(99, "GameEnd", 840.0, Result="Lose")))
    assert lost["result"] == "loss"


def test_baron_ace_stolen_and_elder():
    raw = with_events(
        make_allgamedata(),
        ev(20, "BaronKill", 900.0, Stolen="True", KillerName="Pez Dorado", Assisters=[]),
        ev(21, "DragonKill", 910.0, DragonType="Elder", Stolen="False", KillerName="Mono Sabio", Assisters=[]),
        ev(22, "Ace", 920.0, Acer="Mono Sabio", AcingTeam="ORDER"),
        ev(23, "Multikill", 930.0, KillerName="Mono Sabio", KillStreak=5))
    st = live.normalize(raw)
    assert st["teams"]["enemy"]["totals"]["barons"] == 1
    assert st["teams"]["ally"]["totals"]["dragons"][-1]["label"] == "Anciano"
    texts = [e["text"] for e in st["feed"]]
    assert "Pez Dorado" not in " ".join(texts)              # champion names, not player names
    assert "Zed mató al Barón Nashor (robado)" in texts
    assert "Penta kill de Ahri" in texts
    assert any(e["kind"] == "ace" and e["team"] == "ally" for e in st["feed"])


def test_my_death_facts_for_the_plan():
    deaths = state()["me_events"]["deaths"]
    first, second = deaths
    assert first["t"] == 340.0 and first["involved"] == 3 and first["after_own_kill"]
    assert first["jungler_involved"]                         # Lee Sin assisted
    assert second["involved"] == 0 and not second["after_own_kill"] and not second["by_champion"]
    assert state()["me_events"]["kills"] == [300.0]


def test_loading_screen_and_garbage_are_not_a_game():
    assert live.normalize({}) is None
    assert live.normalize({"allPlayers": [], "gameData": {"gameTime": 3}}) is None
    raw = make_allgamedata()
    del raw["gameData"]["gameTime"]
    assert live.normalize(raw) is None
    assert live.normalize("nope") is None
    raw = make_allgamedata()
    raw["events"] = {"Events": [{"EventName": "ChampionKill"}, "x", None, {"EventName": "Wat", "EventTime": 3}]}
    assert live.normalize(raw)["feed"][0]["kind"] == "kill"      # tolerant: odd events do not raise


def test_policy_nothing_with_competitive_edge_is_exposed():
    st = state()
    blob = repr(st).lower()
    for forbidden in ("summonerspell", "cooldown", "ultimate", "respawning", "inhibrespawn"):
        assert forbidden not in blob
    for p in st["teams"]["enemy"]["players"]:
        assert set(p) >= {"champion", "kills", "items"} and "spells" not in p and "runes" not in p
    # objective respawn events are dropped from the feed
    assert all("reaparece" not in e["text"].lower() for e in st["feed"])


# --------------------------------------------------------- item value and enrich
PRICES = {1055: 450, 2003: 50, 3340: 0, 1001: 300, 3020: 1100, 1056: 400, 3006: 1100, 1038: 1300,
          2055: 75, 3850: 400}


def enriched(prices=PRICES, **kw):
    st = state(**kw.pop("raw_kw", {}))
    available = prices is not None
    return live.enrich(st, item_cost=(prices.get if prices else None), prices_available=available, **kw)


def test_item_value_counts_stacks_and_ignores_free_trinkets():
    value, complete = live.item_value(
        [{"id": 2003, "count": 2}, {"id": 3340, "count": 1}, {"id": 2055, "count": 3}], PRICES.get)
    assert value == 2 * 50 + 0 + 3 * 75 and complete
    assert live.item_value([], PRICES.get) == (0, True)


def test_item_value_unknown_item_is_incomplete_and_no_data_is_none():
    value, complete = live.item_value([{"id": 1055, "count": 1}, {"id": 9999, "count": 1}], PRICES.get)
    assert value == 450 and not complete
    assert live.item_value([{"id": 1055, "count": 1}], PRICES.get, available=False) == (None, False)


def test_enrich_values_lane_diff_and_team_totals():
    out = enriched()
    ally, enemy = out["teams"]["ally"]["players"], out["teams"]["enemy"]["players"]
    adc, their_adc = ally[3], enemy[3]
    assert adc["value"] == 1100 + 1300 + 75 + 0 == 2475
    assert their_adc["value"] == 1100 + 1300
    assert adc["value_diff"] == 75 and their_adc["value_diff"] == -75
    assert adc["lane_opponent"] == "Kai'Sa"
    support = ally[4]
    assert support["value"] == 400 + 2 * 75                       # two control wards
    assert ally[0]["value"] == 450 + 2 * 50 + 0
    assert out["teams"]["ally"]["totals"]["value"] == sum(p["value"] for p in ally)
    assert out["teams"]["enemy"]["totals"]["value"] == sum(p["value"] for p in enemy)
    assert out["value_diff"] == (out["teams"]["ally"]["totals"]["value"]
                                 - out["teams"]["enemy"]["totals"]["value"])
    assert not out["teams"]["ally"]["totals"]["value_approx"]


def test_enrich_partial_prices_are_flagged_approximate():
    out = enriched(prices={k: v for k, v in PRICES.items() if k != 1038})
    assert out["teams"]["ally"]["players"][3]["value_approx"]
    assert out["teams"]["ally"]["totals"]["value_approx"]


def test_enrich_without_item_data_shows_nothing_not_zero():
    out = enriched(prices=None)
    assert all(p["value"] is None and p["value_diff"] is None
               for t in ("ally", "enemy") for p in out["teams"][t]["players"])
    assert out["teams"]["ally"]["totals"]["value"] is None and out["value_diff"] is None
    assert out["prices_available"] is False


def test_lane_diff_needs_a_single_opponent_in_the_same_position():
    raw = make_allgamedata()
    for p in raw["allPlayers"]:
        p["position"] = ""                                         # practice tool / custom game
    out = live.enrich(live.normalize(raw), item_cost=PRICES.get, prices_available=True)
    assert all(p["value_diff"] is None for p in out["teams"]["ally"]["players"])
    assert out["value_diff"] is not None                           # team totals still work


def test_enrich_icons_and_names_and_original_is_untouched():
    st = state()
    before = copy.deepcopy(st)
    out = live.enrich(st, champion_icon=lambda c: f"/ddragon/x/{c}.png",
                      item_icon=lambda i: f"/ddragon/i/{i}.png" if i != 1001 else None,
                      item_name=lambda i: "Nombre" if i == 3006 else None,
                      item_cost=PRICES.get, prices_available=True)
    assert st == before
    adc = out["teams"]["ally"]["players"][3]
    assert adc["icon"] == "/ddragon/x/Jinx.png" and adc["items"][0]["name"] == "Nombre"
    assert out["teams"]["ally"]["players"][1]["items"][0]["icon"] is None
    assert out["me"]["icon"] == "/ddragon/x/Jinx.png" and out["me"]["value"] == adc["value"]


def test_enrich_gold_estimate_unspent_and_team_diffs():
    model = goldmodel.GoldModel()
    out = live.enrich(state(), item_cost=PRICES.get, prices_available=True, gold_model=model)
    ally, enemy = out["teams"]["ally"]["players"], out["teams"]["enemy"]["players"]
    adc = ally[3]
    assert adc["gold_est"] == model.estimate(845 / 60, 112, 5, 4, False)
    assert adc["gold_est"] % 50 == 0
    assert adc["unspent_est"] is None                              # the real current gold is shown instead
    other = enemy[3]
    assert other["unspent_est"] == goldmodel.unspent_estimate(other["gold_est"], other["value"])
    assert other["unspent_est"] % 100 == 0
    support = ally[4]
    assert support["gold_est"] == model.estimate(845 / 60, 14, 0, 9, True)
    assert adc["gold_diff"] == adc["gold_est"] - other["gold_est"]
    assert out["teams"]["ally"]["totals"]["gold_est"] == sum(p["gold_est"] for p in ally)
    assert out["gold_diff"] == (out["teams"]["ally"]["totals"]["gold_est"]
                                - out["teams"]["enemy"]["totals"]["gold_est"])
    assert "calibrado" not in out["gold_model"]["legend"] and "valores de referencia" in out["gold_model"]["legend"]
    assert "Riot solo da el oro real de quien está jugando" in out["gold_model"]["legend"]


def test_enrich_without_gold_model_has_no_estimate():
    out = enriched()
    assert out["gold_model"] is None and out["gold_diff"] is None
    assert out["teams"]["ally"]["players"][0]["gold_est"] is None


# ----------------------------------------------------------------- poller
class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.problem = None
        self.calls = 0

    def fetch(self):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return item


DOWN, LOADING = ("down", None), ("loading", None)


def ok(**kw):
    return ("ok", make_allgamedata(**kw))


def poller(script, **kw):
    return live.LivePoller(FakeClient(script), fast_s=1.0, idle_s=5.0, loading_s=2.0, down_after=3, **kw)


def test_waiting_when_the_game_is_closed():
    p = poller([DOWN])
    assert p.poll_once() == 5.0
    assert p.status == "waiting" and p.snapshot()["game"] is None and not p.in_game


def test_loading_screen_then_playing_then_game_end_keeps_final_snapshot():
    p = poller([DOWN, LOADING, ok(t=30.0), ok(t=60.0),
                ("ok", with_events(make_allgamedata(t=1800.0), ev(99, "GameEnd", 1799.0, Result="Win"))),
                DOWN])
    p.poll_once()
    assert p.status == "waiting"
    assert p.poll_once() == 2.0 and p.status == "loading"       # 404 on the loading screen
    p.poll_once()
    assert p.status == "playing" and p.in_game and p.snapshot()["game"]["clock"] == "0:30"
    p.poll_once()
    assert p.snapshot()["game"]["clock"] == "1:00"
    p.poll_once()
    snap = p.snapshot()
    assert snap["status"] == "finished" and snap["game"]["result"] == "win" and not p.in_game
    version = snap["version"]
    p.poll_once()                                                 # the client closed: still finished
    assert p.status == "finished" and p.snapshot()["game"]["clock"] == "30:00"
    assert p.snapshot()["version"] == version


def test_a_new_game_replaces_the_finished_one():
    end = ("ok", with_events(make_allgamedata(t=1800.0), ev(99, "GameEnd", 1799.0, Result="Lose")))
    p = poller([end, LOADING, ok(t=20.0)])
    p.poll_once()
    assert p.status == "finished"
    p.poll_once()
    assert p.status == "finished"                                 # loading screen of the next game: keep the summary
    p.poll_once()
    assert p.status == "playing" and p.snapshot()["game"]["clock"] == "0:20"


def test_short_connection_hiccups_do_not_end_a_running_game():
    p = poller([ok(), DOWN, DOWN, ok(t=900.0), DOWN, DOWN, DOWN])
    p.poll_once()
    assert p.poll_once() == 1.0 and p.status == "playing"         # retry fast while in a game
    p.poll_once()
    assert p.status == "playing"
    p.poll_once()
    assert p.status == "playing" and p.snapshot()["game"]["clock"] == "15:00"
    for _ in range(3):
        p.poll_once()
    assert p.status == "finished"                                 # three misses in a row: the client closed
    assert p.snapshot()["game"]["clock"] == "15:00"               # the last snapshot stays


def test_loading_responses_in_a_running_game_count_as_misses():
    p = poller([ok(), LOADING, LOADING, LOADING])
    p.poll_once()
    for _ in range(3):
        p.poll_once()
    assert p.status == "finished"


def test_fast_polling_only_with_listeners():
    p = poller([ok()])
    assert p.poll_once() == 5.0                                   # nobody listens: slow probe
    p.add_listener()
    assert p.listeners == 1 and p.poll_once() == 1.0
    p.remove_listener()
    p.remove_listener()
    assert p.listeners == 0 and p.poll_once() == 5.0


def test_tls_problem_is_reported_while_waiting():
    client = FakeClient([DOWN])
    client.problem = "tls"
    p = live.LivePoller(client)
    p.poll_once()
    assert p.snapshot()["problem"] == "tls"


def test_checkpoints_record_cs_when_the_clock_crosses_the_minute():
    p = poller([ok(t=419.0), ok(t=421.0), ok(t=425.0), ok(t=900.0), ok(t=2000.0)])
    for _ in range(4):
        p.poll_once()
    cps = p.snapshot()["game"]["me"]["checkpoints"]
    assert set(cps) == {"7"} and cps["7"]["cs"] == 112            # first sample inside the 7:00 window
    p.poll_once()                                                 # joined late: 14:00 was missed
    assert "14" not in p.snapshot()["game"]["me"]["checkpoints"]


def test_new_game_resets_checkpoints_when_the_clock_goes_back():
    p = poller([ok(t=421.0), ok(t=30.0)])
    p.poll_once()
    assert "7" in p.snapshot()["game"]["me"]["checkpoints"]
    p.poll_once()
    assert p.snapshot()["game"]["me"]["checkpoints"] == {}


def test_wait_for_change_wakes_on_publish_and_times_out():
    p = poller([ok()])
    version = p.snapshot()["version"]
    assert p.wait_for_change(version, 0.05)["version"] == version
    threading.Timer(0.05, p.poll_once).start()
    assert p.wait_for_change(version, 2.0)["version"] > version


def test_thread_runs_and_stops_and_survives_errors():
    class Boom(FakeClient):
        def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("raro")
            return ("ok", make_allgamedata())

    client = Boom([])
    p = live.LivePoller(client, fast_s=0.01, idle_s=0.01)
    p.start()
    p.start()                                                     # idempotent
    snap = p.wait_for_change(0, 3.0)
    assert snap["status"] == "playing"
    p.stop()
    assert p.is_stopped()


def test_a_poller_never_raises_from_snapshot_or_listener_calls():
    p = poller([DOWN])
    p.remove_listener()
    assert p.snapshot()["status"] == "waiting"


def test_checkpoints_survive_after_the_game_ends():
    end = ("ok", with_events(make_allgamedata(t=1800.0), ev(99, "GameEnd", 1799.0, Result="Win")))
    p = poller([ok(t=421.0), end, end])
    p.poll_once()
    p.poll_once()
    p.poll_once()                                                 # still finished: the 7:00 value stays
    snap = p.snapshot()
    assert snap["status"] == "finished" and "7" in snap["game"]["me"]["checkpoints"]
