import math

import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan import analyze as an
from lolan import report
from lolan.metrics import extract_game_metrics
from lolan.store import Store

MIN = 60_000


def test_effect_size_known_values():
    # means 5 vs 3, both variances 2.5 -> pooled std sqrt(2.5)
    d = an.effect_size([3, 4, 5, 6, 7], [1, 2, 3, 4, 5])
    assert d == pytest.approx(2 / math.sqrt(2.5))
    assert an.effect_size([5, 4, 3], [1, 2, 3]) > 0
    assert an.effect_size([1, 2, 3], [5, 4, 3]) < 0


def test_effect_size_edge_cases():
    assert an.effect_size([1], [1, 2, 3]) is None
    assert an.effect_size([2, 2, 2], [2, 2, 2]) is None  # no spread
    assert an.effect_size([None, 1, 2], [None, None]) is None


def test_percentile_rank():
    pop = [1, 2, 3, 4, 5]
    assert an.percentile_rank(5, pop) == pytest.approx(90.0)
    assert an.percentile_rank(1, pop) == pytest.approx(10.0)
    assert an.percentile_rank(3, pop) == pytest.approx(50.0)
    assert an.percentile_rank(10, pop) == 100.0
    assert an.percentile_rank(3, [3, 3, 3, 3]) == 50.0
    assert an.percentile_rank(None, pop) is None and an.percentile_rank(3, []) is None


def game(i, champion="Ahri", win=1, **overrides):
    g = extract_game_metrics(
        make_match(match_id=f"LA1_{i}", champion=champion, win=bool(win),
                   created=BASE_TIME_MS + i * 3 * 3600_000),
        make_timeline(), ME_PUUID)
    g.update(overrides)
    return g


def games_list(n=8, champion="Ahri"):
    gs = [game(i, champion=champion, win=i % 2, cs_per_min=5 + i * 0.1, gold_diff_14=100 * i,
               cs_diff_14=i, early_deaths=1) for i in range(n)]
    an.add_session_info(gs)
    return gs


def test_report_card_flags_hard_rules():
    gs = games_list()
    target = dict(gs[3], gold_diff_14=-800, early_deaths=3, solo_deaths=2, cs_diff_14=-20,
                  deaths_enemy_half=3, match_id="LA1_target")
    card = an.game_report_card(target, gs + [target])
    weak = {w["key"] for w in card["weaknesses"]}
    assert {"gold_diff_14", "early_deaths", "solo_deaths", "cs_diff_14", "deaths_enemy_half"} <= weak
    assert "Debilidades" in card["text"] and "Fortalezas" in card["text"]


def test_report_card_strengths_and_percentiles():
    gs = games_list()
    target = dict(gs[3], gold_diff_14=900, early_deaths=0, cs_per_min=9.0, match_id="LA1_target")
    card = an.game_report_card(target, gs + [target])
    strong = {s["key"] for s in card["strengths"]}
    assert "gold_diff_14" in strong and "early_deaths" in strong
    row = next(r for r in card["rows"] if r["key"] == "cs_per_min")
    assert row["scope"] == "campeón" and row["percentile"] > 75 and row["verdict"] == "fortaleza"
    assert row["opp_value"] is not None


def test_report_card_falls_back_to_role_then_all():
    # Only 3 games on this champion, plenty on the same role with other champions.
    others = [game(i, champion=f"Other{i}") for i in range(10)]
    mine = [game(100 + i, champion="Rare") for i in range(3)]
    gs = others + mine
    card = an.game_report_card(mine[0], gs)
    scopes = {r["scope"] for r in card["rows"] if r["scope"]}
    assert scopes == {"rol"}
    # Different role for the target: should fall back to all games.
    odd = dict(mine[0], role="TOP", match_id="odd")
    card = an.game_report_card(odd, gs + [odd])
    assert {r["scope"] for r in card["rows"] if r["scope"]} == {"todas las partidas"}


def test_report_card_without_timeline_or_opponent():
    m = make_match(drop_opp_position=True)
    g = extract_game_metrics(m, None, ME_PUUID)
    card = an.game_report_card(g, [g])
    assert card["has_timeline"] is False and "no tiene timeline" in card["text"]


def test_champion_insights_direction_and_threshold():
    gs = []
    for i in range(10):
        win = i % 2
        gs.append(game(i, win=win, kda=6.0 + i * 0.01 if win else 1.5 + i * 0.01,
                       deaths=(1 + i % 3) if win else (8 + i % 3), deaths_per_10=1.0 if win else 5.0 + i * 0.01))
    ins = an.champion_insights(gs, "ahri")
    assert ins["enough"] and ins["games"] == 10
    wins_keys = {e["key"] for e in ins["higher_in_wins"]}
    loss_keys = {e["key"] for e in ins["higher_in_losses"]}
    assert "kda" in wins_keys and "deaths" in loss_keys
    assert len(ins["higher_in_wins"]) <= 5 and len(ins["higher_in_losses"]) <= 5

    few = an.champion_insights(gs[:4], "Ahri")
    assert few["enough"] is False and few["higher_in_wins"] == []


def test_sessions_and_trends():
    base = BASE_TIME_MS
    specs = [  # (offset minutes from base, win)
        (0, 1), (30, 0), (60, 0),      # session 1: gaps between end and next start < 45 min
        (60 * 24, 1), (60 * 24 + 30, 1),  # session 2
    ]
    gs = []
    for i, (off, win) in enumerate(specs):
        g = extract_game_metrics(make_match(match_id=f"LA1_{i}", win=bool(win), duration=1000,
                                            created=base + off * MIN), make_timeline(), ME_PUUID)
        gs.append(g)
    an.add_session_info(gs)
    assert [g["session_id"] for g in gs] == [1, 1, 1, 2, 2]
    assert [g["session_game"] for g in gs] == [1, 2, 3, 1, 2]

    t = an.trend_by_session_game(gs)
    assert t["sessions"] == 2
    first = next(r for r in t["by_position"] if r["key"] == 1)
    assert first["games"] == 2 and first["winrate"] == 1.0
    assert t["after_loss"]["games"] == 1 and t["after_win"]["games"] == 2

    months = an.trend_by_month(gs)
    assert sum(r["games"] for r in months) == 5
    assert sum(r["games"] for r in an.trend_by_weekday(gs)) == 5


def test_summarize_groups():
    gs = games_list(6) + games_list(4, champion="Zed")
    s = an.summarize(gs)
    assert s["overall"]["games"] == 10
    champs = {r["key"]: r for r in s["by_champion"]}
    assert champs["Ahri"]["games"] == 6 and champs["Zed"]["games"] == 4
    assert s["by_queue"][0]["label"] == "Clasificatoria Solo/Dúo"
    assert s["by_role"][0]["label"] == "Mid"


def test_load_games_excludes_remakes_and_other_queues(tmp_path):
    store = Store(tmp_path / "lol.db")
    store.add_player(ME_PUUID, "A#B", is_main=True)
    store.save_match("LA1_1", make_match("LA1_1", created=BASE_TIME_MS), make_timeline())
    store.save_match("LA1_2", make_match("LA1_2", duration=180, created=BASE_TIME_MS + 1), None)
    store.save_match("LA1_3", make_match("LA1_3", queue=450, created=BASE_TIME_MS + 2), None)
    assert [g["match_id"] for g in an.load_games(store, ME_PUUID)] == ["LA1_1"]
    assert {g["match_id"] for g in an.load_games(store, ME_PUUID, include_all_queues=True)} == {"LA1_1", "LA1_3"}
    store.close()


def test_load_games_single_queue(tmp_path):
    store = Store(tmp_path / "lol.db")
    store.add_player(ME_PUUID, "A#B", is_main=True)
    store.save_match("LA1_1", make_match("LA1_1", queue=420, created=BASE_TIME_MS), None)
    store.save_match("LA1_2", make_match("LA1_2", queue=440, created=BASE_TIME_MS + 1), None)
    assert [g["match_id"] for g in an.load_games(store, ME_PUUID, queue=420)] == ["LA1_1"]
    assert [g["match_id"] for g in an.load_games(store, ME_PUUID, queue=440)] == ["LA1_2"]
    store.close()


def test_write_reports_removes_stale_reports(tmp_path):
    games = [an.extract_game_metrics(make_match("LA1_1", champion="Ahri"), make_timeline(), ME_PUUID)]
    stale = tmp_path / "reports" / "campeones" / "Zed.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("viejo", encoding="utf-8")
    report.write_reports(games, tmp_path / "reports", tmp_path / "data")
    assert not stale.exists()
    assert (tmp_path / "reports" / "campeones" / "Ahri.md").exists()


def test_write_reports_creates_files(tmp_path):
    gs = games_list(8)
    info = report.write_reports(gs, tmp_path / "reports", tmp_path / "data")
    assert (tmp_path / "reports" / "resumen.md").exists()
    assert (tmp_path / "reports" / "campeones" / "Ahri.md").exists()
    assert len(list((tmp_path / "reports" / "partidas").glob("*.md"))) == 8
    csv_text = (tmp_path / "data" / "games.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].startswith("match_id,") and len(csv_text.splitlines()) == 9
    assert info["games"] == 8
    assert "Resumen general" in (tmp_path / "reports" / "resumen.md").read_text(encoding="utf-8")


def test_pct_text_reports_the_side_it_names():
    good = {"label": "KDA", "fmt": "5.00", "scope": "campeón", "goodness": 86.0}
    bad = {"label": "KDA", "fmt": "0.78", "scope": "campeón", "goodness": 0.0}
    assert "mejor que el 86% de tus partidas con este campeón" in an._pct_text(good)
    assert "peor que el 100% de tus partidas con este campeón" in an._pct_text(bad)


def test_load_games_needs_a_player_and_reads_only_their_matches(tmp_path):
    store = Store(tmp_path / "lol.db")
    store.save_match("LA1_1", make_match("LA1_1", created=BASE_TIME_MS), None)
    other = make_match("LA1_2", created=BASE_TIME_MS + 1)
    other["metadata"]["participants"] = ["a", "b"]           # a match the player is not in
    store.save_match("LA1_2", other, None)
    assert an.load_games(store, "") == []
    assert [g["match_id"] for g in an.load_games(store, ME_PUUID)] == ["LA1_1"]
    # The teammate sees the very same stored match from their own side.
    mate = an.load_games(store, "puuid-4")
    assert [g["match_id"] for g in mate] == ["LA1_1"] and mate[0]["champion"] == "Champ4"
    assert mate[0]["team_id"] == an.load_games(store, ME_PUUID)[0]["team_id"] == 100
    store.close()


def row(match_id, team, win, **extra):
    return {"match_id": match_id, "team_id": team, "win": win, "kills": 1, "deaths": 2,
            "assists": 3, **extra}


def test_pair_stats_counts_only_same_team_games_with_minimum():
    a = [row(f"M{i}", 100, 1) for i in range(1, 5)] + [row("M5", 100, 0), row("M6", 100, 0)]
    # b: same team in M1-M3 (+1 more win), opposite team in M4-M6, one extra solo game.
    b = [row("M1", 100, 1), row("M2", 100, 1), row("M3", 100, 1),
         row("M4", 200, 0), row("M5", 200, 1), row("M6", 200, 1), row("M7", 100, 0)]
    pairs = an.pair_stats({"a": a, "b": b})
    assert len(pairs) == 1
    p = pairs[0]
    assert (p["a"], p["b"], p["games"], p["wins"]) == ("a", "b", 3, 3) and p["winrate"] == 1.0
    assert p["winrate_a"] == 4 / 6 and p["winrate_b"] == 5 / 7
    # Games on opposite teams don't count, and 2 together is below the minimum.
    assert an.pair_stats({"a": a, "b": b[:2] + b[3:]}) == []
    assert an.pair_stats({"a": a, "b": b[:2] + b[3:]}, min_games=2)[0]["games"] == 2
    assert an.pair_stats({"a": a}) == []


def test_pair_stats_three_players_sorted_by_games():
    mk = lambda ids, team=100: [row(i, team, 1) for i in ids]          # noqa: E731
    games = {"a": mk(["1", "2", "3", "4"]), "b": mk(["1", "2", "3", "4"]), "c": mk(["2", "3", "4"])}
    pairs = an.pair_stats(games)
    assert [(p["a"], p["b"], p["games"]) for p in pairs] == [("a", "b", 4), ("a", "c", 3), ("b", "c", 3)]


def test_player_overview_picks_most_played_role_and_champion():
    gs = [
        {"win": 1, "kills": 4, "deaths": 2, "assists": 2, "role": "MIDDLE", "champion": "Ahri",
         "solo_deaths": 1, "early_deaths": 0, "deaths_per_10": 1.0},
        {"win": 0, "kills": 0, "deaths": 4, "assists": 2, "role": "MIDDLE", "champion": "Zed",
         "solo_deaths": 3, "early_deaths": 2, "deaths_per_10": 3.0},
        {"win": 1, "kills": 2, "deaths": 0, "assists": 0, "role": "JUNGLE", "champion": "Ahri",
         "solo_deaths": None, "early_deaths": None, "deaths_per_10": 2.0},
    ]
    o = an.player_overview(gs)
    assert o["games"] == 3 and abs(o["winrate"] - 2 / 3) < 1e-9
    assert o["solo_deaths_pg"] == 2.0 and o["early_deaths_pg"] == 1.0     # missing values ignored
    assert (o["top_role"], o["top_role_games"]) == ("Mid", 2)
    assert (o["top_champion"], o["top_champion_games"]) == ("Ahri", 2)
    empty = an.player_overview([])
    assert empty["games"] == 0 and empty["winrate"] is None and empty["top_champion"] is None
