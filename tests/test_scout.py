"""Scouting core (lolan/scout.py): spectator parsing, rank / mastery text, history, budget and cache."""
import json

import pytest
from conftest import make_match
from scout_fixtures import (ALLIES, ENEMIES, ME, ME_PUUID, NOW, NOW_MS, DAY_MS, World, api_match,
                            league, mastery, riot_error, spectator_game, swap_teams)

from lolan import comps
from lolan import scout as sc
from lolan.riot import InvalidApiKeyError
from lolan.store import Store

CLAN = comps.ClanConfig.build("Epsilon", "", {})


@pytest.fixture
def env(tmp_path):
    class Env:
        db = tmp_path / "data" / "lol.db"
        world = World()
        now = NOW

        def store(self):
            return Store(self.db)

        def api(self, budget=sc.BUDGET):
            return sc.ScoutApi(self.world.client("la1"), self.world.client("americas"), budget=budget)

        def run(self, raw=None, api=None, **kw):
            raw = raw if raw is not None else spectator_game()
            game = sc.parse_spectator(raw, kw.pop("viewer_puuid", ME_PUUID), ME)
            store = self.store()
            try:
                self.last_api = api or self.api()
                run = sc.ScoutRun(self.last_api, store, sc.LocalHistory(self.store), kw.pop("clan", CLAN),
                                  viewer_riot_id=ME, viewer_is_main=True, now=lambda: self.now, **kw)
                self.last_run = run
                return run.run(game)
            finally:
                store.close()

    e = Env()
    e.world.stock()
    return e


def players(report):
    return {p["riot_id"] or p["champion_id"]: p for side in ("ally", "enemy") for p in report["teams"][side]}


# ------------------------------------------------------------------- parsing
def test_spectator_split_ally_enemy_hidden_and_bot():
    game = sc.parse_spectator(spectator_game(), ME_PUUID, ME)
    assert [p["champion_id"] for p in game["teams"]["ally"]] == [222, 86, 113, 117, 103]
    assert [p["champion_id"] for p in game["teams"]["enemy"]] == [58, 64, 238, 145, 412]
    me, _rana, _beta, hidden, _mono = game["teams"]["ally"]
    assert me["is_viewer"] and me["riot_id"] == ME and me["spell_ids"] == [4, 7]
    assert me["keystone_id"] == 8008 and me["style_id"] == 8000 and me["substyle_id"] == 8200
    assert hidden["hidden"] and hidden["riot_id"] is None and hidden["_puuid"] == ""
    assert hidden["keystone_id"] is None and hidden["spell_ids"] == [4, 14]       # champion + spells only
    bot = game["teams"]["enemy"][3]
    assert bot["bot"] and not bot["hidden"] and bot["riot_id"] is None and bot["_puuid"] == ""
    assert game["queue_label"] == "Clasificatoria Solo/Dúo" and game["length_s"] == -20
    assert game["bans"]["ally"] == [{"champion_id": 266, "pick_turn": 1}]            # the -1 "no ban" is dropped
    assert game["bans"]["enemy"] == [{"champion_id": 99, "pick_turn": 2}]


def test_spectator_is_relative_to_the_scouted_players_team():
    here = sc.parse_spectator(spectator_game(), ME_PUUID, ME)
    swapped = sc.parse_spectator(swap_teams(spectator_game()), ME_PUUID, ME)
    for side in ("ally", "enemy"):               # same split whichever side (100 / 200) the viewer is on
        assert [p["champion_id"] for p in swapped["teams"][side]] == [p["champion_id"] for p in here["teams"][side]]
    assert swapped["bans"] == here["bans"]
    other = sc.parse_spectator(spectator_game(), "P-toro", "Toro Rojo#T006")        # scouting an enemy instead
    assert [p["champion_id"] for p in other["teams"]["ally"]] == [58, 64, 238, 145, 412]
    assert other["bans"]["ally"] == [{"champion_id": 99, "pick_turn": 2}]


def test_spectator_garbage_is_not_a_game():
    assert sc.parse_spectator(None, "x") is None
    assert sc.parse_spectator({"participants": []}, "x") is None
    assert sc.parse_spectator("nope", "x") is None


def test_hidden_players_are_never_looked_up_even_with_a_puuid(env):
    report = env.run(spectator_game(extra_hidden_with_puuid=True))
    hidden = report["teams"]["ally"][3]
    assert hidden["hidden"] and hidden["status"] == "hidden" and hidden["rank"] is None
    assert hidden["mastery"] is None and hidden["recent"] is None and hidden["history"] is None
    assert "P-secret" not in json.dumps(env.world.calls) and "P-secret" not in json.dumps(report)
    bot = report["teams"]["enemy"][3]
    assert bot["status"] == "bot" and bot["rank"] is None
    assert all(c[1] != "" for c in env.world.calls)                 # nothing was asked for a blank id


def test_report_never_contains_puuids(env):
    report = env.run()
    blob = json.dumps(report)
    assert "P-me" not in blob and "P-toro" not in blob and "_puuid" not in blob


# ---------------------------------------------------------- rank and mastery
@pytest.mark.parametrize("tier, rank, label", [
    ("IRON", "IV", "Hierro IV"), ("GOLD", "II", "Oro II"), ("EMERALD", "I", "Esmeralda I"),
    ("MASTER", "I", "Maestro"), ("GRANDMASTER", "I", "Gran Maestro"), ("CHALLENGER", "I", "Retador"),
])
def test_rank_labels(tier, rank, label):
    r = sc.rank_entry({"tier": tier, "rank": rank, "leaguePoints": 45, "wins": 60, "losses": 50})
    assert r["label"] == label and r["lp_text"] == "45 PL"
    assert r["winrate_text"] == "55%" and r["record_text"] == "60V-50D" and r["games"] == 110


def test_rank_without_games_has_no_winrate_and_queues_are_split():
    ranks = sc.format_ranks(league(solo=("GOLD", "IV", 0, 0, 0, True), flex=("SILVER", "I", 80, 10, 5, False)))
    assert ranks["solo"]["winrate"] is None and ranks["solo"]["winrate_text"] == "-" and ranks["solo"]["hot_streak"]
    assert ranks["flex"]["label"] == "Plata I"
    assert sc.format_ranks([]) == {"solo": None, "flex": None}


@pytest.mark.parametrize("points, label, tone", [
    (0, sc.LABEL_LOW, "low"), (9_999, sc.LABEL_LOW, "low"), (10_000, None, ""), (100_000, None, ""),
    (100_001, sc.LABEL_MAIN, "main"), (850_000, sc.LABEL_MAIN, "main"),
])
def test_mastery_labels(points, label, tone):
    m = sc.mastery_info(mastery(points), NOW_MS)
    assert m["label"] == label and m["tone"] == tone and m["points"] == points


def test_mastery_never_played_and_days():
    never = sc.mastery_info(None, NOW_MS)
    assert never["never"] and never["label"] == sc.LABEL_LOW and never["last_text"] == "nunca lo ha jugado"
    assert sc.mastery_info(mastery(20_000, days_ago=0), NOW_MS)["last_text"] == "hoy"
    assert sc.mastery_info(mastery(20_000, days_ago=1), NOW_MS)["last_text"] == "ayer"
    m = sc.mastery_info(mastery(12_345, level=7, days_ago=3), NOW_MS)
    assert m["days_since"] == 3 and m["last_text"] == "hace 3 días" and m["points_text"] == "12,3k"
    assert sc.pts_text(950) == "950" and sc.pts_text(1_500_000) == "1,5M" and sc.pts_text(20_000) == "20k"


# ----------------------------------------------------------------- full report
def test_full_report_with_rank_mastery_and_clan(env):
    env.world.leagues["P-toro"] = league(solo=("DIAMOND", "II", 12, 200, 150, True),
                                         flex=("PLATINUM", "I", 50, 20, 20, False))
    env.world.masteries[("P-toro", 58)] = mastery(250_000, level=10)
    env.world.masteries[("P-gato", 64)] = None                  # never played
    report = env.run()
    byid = players(report)
    toro = byid["Toro Rojo#T006"]
    assert toro["rank"]["solo"]["label"] == "Diamante II" and toro["rank"]["flex"]["label"] == "Platino I"
    assert toro["streak"] is True and toro["mastery"]["label"] == sc.LABEL_MAIN
    assert byid["Gato Negro#T007"]["mastery"]["label"] == sc.LABEL_LOW and byid["Gato Negro#T007"]["mastery"]["never"]
    assert byid["Epsilon Beta#LAN"]["clan"] is True and byid["Rana Veloz#T002"]["clan"] is False
    assert byid[ME]["is_viewer"] and byid[ME]["clan"] is False
    assert report["scouted"] == {"riot_id": ME, "is_main": True} and report["viewer_label"] == "ti"
    assert report["requests"]["stopped"] is None and report["complete"] is True


def test_cold_request_count_is_within_the_budget(env):
    env.run()
    # 8 visible players x (rank + mastery) + 4 enemies x (ids + 3 details) + 2 non-clan allies x ids
    assert env.last_api.used == 16 + 16 + 2 == 34 <= sc.BUDGET
    names = env.world.names()
    assert names.count("league") == 8 and names.count("mastery") == 8
    assert names.count("ids") == 6 and names.count("match") == 12


# ---------------------------------------------------------- budget and priority
def test_enemies_are_asked_first_then_allies_then_you(env):
    env.run()
    first = [c for c in env.world.calls if c[0] == "league"]
    assert [c[1] for c in first] == ["P-toro", "P-gato", "P-pez", "P-oso", "P-rana", "P-beta", "P-mono", ME_PUUID]
    ids = [c[1] for c in env.world.calls if c[0] == "ids"]
    assert ids == ["P-toro", "P-gato", "P-pez", "P-oso", "P-rana", "P-mono"]       # enemies, then allies; no clan, no you


def test_budget_runs_out_enemies_first_and_reports_what_was_skipped(env):
    report = env.run(api=env.api(budget=5))
    assert env.last_api.used == 5 and report["requests"]["stopped"] == "presupuesto"
    assert env.world.calls[:5] == [("league", "P-toro"), ("mastery", ("P-toro", 58)), ("league", "P-gato"),
                                   ("mastery", ("P-gato", 64)), ("league", "P-pez")]
    byid = players(report)
    assert byid["Toro Rojo#T006"]["status"] == "ok" and byid["Toro Rojo#T006"]["rank"] is not None
    assert byid["Pez Dorado#T008"]["status"] == "partial" and byid["Pez Dorado#T008"]["mastery"] is None
    assert byid["Mono Sabio#T004"]["rank"] is None
    assert report["complete"] is False
    assert any("Rango o maestría de 6 jugadores" in s and "máximo de 5 peticiones" in s
               for s in report["requests"]["skipped"])
    assert any(s.startswith("Partidas recientes de 2 jugadores") for s in report["requests"]["skipped"])


def test_core_data_comes_before_any_deep_data(env):
    env.run(api=env.api(budget=18))
    assert env.world.names()[:16].count("ids") == 0 and env.world.names()[16:] == ["ids", "match"]


def test_rate_limit_that_would_wait_too_long_stops_politely(env):
    env.world.waits["la1"] = 61.0
    report = env.run()
    assert env.world.calls == [] and report["requests"]["stopped"] == "limite"
    assert all(p["status"] in ("partial", "hidden", "bot") for s in ("ally", "enemy") for p in report["teams"][s])
    assert any("más de 60 s" in s for s in report["requests"]["skipped"])


def test_regional_limit_only_costs_the_recent_games(env):
    env.world.waits["americas"] = 90.0
    report = env.run()
    assert env.world.count("league") == 8 and env.world.count("ids") == 0
    assert report["requests"]["stopped"] == "limite"
    assert all(p["rank"] is not None for p in players(report).values() if p["status"] == "ok")


def test_one_failure_is_skipped_but_many_in_a_row_stop(env):
    env.world.fail["league"] = lambda puuid: riot_error() if puuid == "P-pez" else None
    report = env.run()
    byid = players(report)
    assert byid["Pez Dorado#T008"]["status"] == "partial" and byid["Pez Dorado#T008"]["rank"] is None
    assert byid["Pez Dorado#T008"]["mastery"] is not None and report["requests"]["stopped"] is None
    assert any(s.startswith("Rango o maestría de 1 jugador") for s in report["requests"]["skipped"])
    env.world.calls.clear()
    env.world.fail = {"league": riot_error(), "mastery": riot_error()}
    env.world.leagues.clear()
    store = env.store()
    store.conn.execute("DELETE FROM scout_cache")
    store.conn.commit()
    store.close()
    report = env.run()
    assert report["requests"]["stopped"] == "error" and len(env.world.calls) == 3
    assert any("no respondió bien" in s for s in report["requests"]["skipped"])


def test_invalid_key_aborts_the_scout(env):
    env.world.fail["league"] = InvalidApiKeyError("expiró")
    with pytest.raises(InvalidApiKeyError):
        env.run()


def test_recent_games_enemies_get_details_allies_only_ids(env):
    report = env.run()
    byid = players(report)
    toro = byid["Toro Rojo#T006"]["recent"]
    assert toro["ids"] == 10 and toro["known"] == 3 and toro["on_champion"] == []       # played Aatrox, not Renekton
    assert byid["Rana Veloz#T002"]["recent"]["known"] == 0
    assert [c for c in env.world.calls if c[0] == "match"][:3] == [
        ("match", "LA1_P-toro_0"), ("match", "LA1_P-toro_1"), ("match", "LA1_P-toro_2")]


def test_details_stop_once_three_games_on_the_champion_are_known(env):
    for i in range(10):
        env.world.matches[f"LA1_P-toro_{i}"] = api_match(f"LA1_P-toro_{i}", "P-toro", 58, "Renekton", win=bool(i % 2))
    report = env.run()
    recent = players(report)["Toro Rojo#T006"]["recent"]
    assert env.world.count("match") == 3 + 9 and recent["known"] == 3
    assert len(recent["on_champion"]) == 3 and recent["on_champion"][0]["kda"] == "5/2/7"
    assert recent["record_text"] == "1V-2D" and recent["on_champion"][0]["when_text"] == "hace 2 días"


def test_stored_match_details_are_not_downloaded_again(env):
    # LA1_P-toro_0 is stored here: found by Riot ID, whatever puuid it holds.
    match = make_match("LA1_P-toro_0", queue=420)
    for p in match["info"]["participants"]:
        p["riotIdGameName"], p["riotIdTagline"] = f"Otro{p['participantId']}", "X"
    match["info"]["participants"][5].update({"riotIdGameName": "toro rojo", "riotIdTagline": "t006",
                                             "puuid": "old-app-puuid", "championName": "Renekton",
                                             "championId": 58, "win": True})
    store = env.store()
    store.save_match("LA1_P-toro_0", match)
    store.close()
    report = env.run()
    assert ("match", "LA1_P-toro_0") not in env.world.calls
    toro = players(report)["Toro Rojo#T006"]["recent"]
    assert toro["known"] == 4 and len(toro["on_champion"]) == 1        # the stored one + 3 downloaded
    toro_fetches = [c for c in env.world.calls if c[0] == "match" and "P-toro" in c[1]]
    assert len(toro_fetches) == 3 and ("match", "LA1_P-toro_3") in toro_fetches


# ---------------------------------------------------------------------- cache
def test_second_scout_of_the_same_lobby_costs_nothing(env):
    first = env.run()
    cold = env.last_api.used
    env.world.calls.clear()
    second = env.run()
    assert env.world.calls == [] and env.last_api.used == 0 and cold == 34
    assert second["requests"]["from_cache"] == 8 and second["requests"]["used"] == 0
    assert second["teams"] == first["teams"]


def test_cache_expires_after_six_hours(env):
    env.run()
    env.world.calls.clear()
    env.now = NOW + 6 * 3600 - 60
    env.run()
    assert env.world.calls == []
    env.now = NOW + 6 * 3600 + 60
    env.run()
    assert env.world.count("league") == 8 and env.world.count("mastery") == 8


def test_cache_is_per_player_and_champion(env):
    env.run()
    env.world.calls.clear()
    raw = spectator_game()
    raw["participants"][5]["championId"] = 99                       # Toro Rojo now plays Lux
    env.world.masteries[("P-toro", 99)] = mastery(70_000)
    report = env.run(raw)
    assert ("league", "P-toro") in env.world.calls and ("mastery", ("P-toro", 99)) in env.world.calls
    assert all(c[1] != "P-gato" and c[1] != ("P-gato", 64) for c in env.world.calls)
    assert players(report)["Toro Rojo#T006"]["mastery"]["points"] == 70_000


def test_a_partial_scout_is_completed_by_the_next_one(env):
    env.run(api=env.api(budget=5))
    env.world.calls.clear()
    report = env.run()
    names = env.world.names()
    assert ("league", "P-toro") not in env.world.calls and ("mastery", ("P-toro", 58)) not in env.world.calls
    assert names.count("league") == 5 and names.count("mastery") == 6 and report["requests"]["stopped"] is None


def test_old_cache_rows_are_pruned(env):
    env.run()
    store = env.store()
    assert store.prune_scout_cache(sc.CACHE_PRUNE_S, NOW + 3600) == 0
    assert store.prune_scout_cache(sc.CACHE_PRUNE_S, NOW + 25 * 3600) == 8
    store.close()


# --------------------------------------------------------------- local history
def test_local_history_by_riot_id_not_by_puuid(env):
    def stored(match_id, mine_team, rana_team, toro_team, mine_win, champion="Ahri", created=1_700_000_000_000):
        m = make_match(match_id, win=True)
        info = m["info"]
        info["gameCreation"] = created
        for p in info["participants"]:
            p["puuid"] = f"old-{p['participantId']}"                 # none of these is a current puuid
            p["riotIdGameName"], p["riotIdTagline"] = f"Nadie{p['participantId']}", "000"
        for pid, (name, tag), team, ch in ((1, ("YO", "lan"), mine_team, "Jinx"),
                                           (2, ("rana veloz", "T002"), rana_team, champion),
                                           (6, ("Toro Rojo", "T006"), toro_team, "Renekton")):
            p = info["participants"][pid - 1]
            p.update({"riotIdGameName": name, "riotIdTagline": tag, "teamId": team, "championName": ch,
                      "win": mine_win if team == mine_team else not mine_win})
        return m
    store = env.store()
    store.save_match("LA1_1", stored("LA1_1", 100, 100, 200, True))                         # with Rana, against Toro: win
    store.save_match("LA1_2", stored("LA1_2", 100, 100, 200, False, champion="Ahri", created=1_700_100_000_000))
    store.save_match("LA1_3", stored("LA1_3", 200, 100, 200, True, created=1_700_200_000_000))   # against Rana, with Toro: win
    store.close()
    report = env.run()
    byid = players(report)
    rana = byid["Rana Veloz#T002"]["history"]
    assert rana["together"] == {"wins": 1, "losses": 1} and rana["against"] == {"wins": 1, "losses": 0}
    assert rana["last"]["champion"] == "Ahri" and rana["last"]["relation"] == "against"
    toro = byid["Toro Rojo#T006"]["history"]
    assert toro["against"] == {"wins": 1, "losses": 1} and toro["together"] == {"wins": 1, "losses": 0}
    assert byid["Mono Sabio#T004"]["history"] is None and byid[ME]["history"] is None
    text = sc.history_text(toro, "ti", lambda c: c.upper())
    assert text == "Antes: 1V-1D contra ti · 1V-0D con ti (la última vez jugó RENEKTON)"
    assert sc.history_text(None, "ti") == ""


def test_history_label_names_the_scouted_player_when_not_main():
    h = {"together": {"wins": 0, "losses": 0}, "against": {"wins": 2, "losses": 1}, "last": None}
    assert sc.history_text(h, "Amigo") == "Antes: 2V-1D contra Amigo"


# ------------------------------------------------------------------ resolver
def test_puuid_resolver_caches_per_riot_id_and_key_fingerprint():
    calls = []
    resolver = sc.PuuidResolver()

    def fetch(name, tag):
        calls.append((name, tag))
        return f"puuid-{len(calls)}"

    assert resolver.resolve("Yo#LAN", "KEY-A", fetch) == "puuid-1"
    assert resolver.resolve("yo#lan", "KEY-A", fetch) == "puuid-1" and len(calls) == 1
    assert resolver.resolve("Yo#LAN", "KEY-B", fetch) == "puuid-2"            # key changed: asked again
    assert resolver.known("Yo#LAN", "KEY-A") and not resolver.known("Otro#LAN", "KEY-A")
    assert "KEY-A" not in repr(resolver.__dict__)
    assert len(sc.key_fingerprint("KEY-A")) == 12 and sc.key_fingerprint("KEY-A") != sc.key_fingerprint("KEY-B")
    with pytest.raises(ValueError):
        resolver.resolve("sin tag", "KEY-A", fetch)


# -------------------------------------------------------------- enrich + text
def test_text_report_shows_the_players_and_hides_nothing_it_should_not(env):
    env.world.leagues["P-oso"] = league(solo=("GOLD", "I", 99, 30, 20, False))
    report = env.run()
    text = sc.render_text(report, sc.Assets(None))
    assert "Partida de Yo#LAN: Clasificatoria Solo/Dúo · en la pantalla de carga" in text
    assert "Jugador oculto" in text and "Bot" in text and "Oso Pardo#T010" in text
    assert "Solo Oro I 99 PL 60% (50)" in text and "[de tu clan]" in text and "[tú]" in text
    assert "Peticiones a Riot usadas: 34 de 45" in text
    assert "P-oso" not in text
    hidden_line = next(l for l in text.splitlines() if "Jugador oculto" in l)
    assert "rango" not in hidden_line


def test_enrich_hidden_and_bot_labels(env):
    rep = sc.enrich(env.run(), sc.Assets(None))
    assert rep["teams"]["ally"][3]["display_name"] == sc.HIDDEN_LABEL
    assert rep["teams"]["enemy"][3]["display_name"] == sc.BOT_LABEL
    assert rep["teams"]["ally"][1]["display_name"] == "Rana Veloz#T002"
    assert rep["teams"]["ally"][0]["champion_name"] == "Campeón 222"     # no Data Dragon data cached
