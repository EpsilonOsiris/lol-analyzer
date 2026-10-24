import json

import pytest
from lane_fixtures import (BLUE_MID, RED_MID, bought, make_lane_match, make_lane_timeline, mid_edge,
                           row)

from lolan import samples
from lolan.comps import ClanConfig
from lolan.samples import LineStore, PersonLabeler, add_matches, text_summary
from lolan.store import Store


@pytest.fixture
def ls(tmp_path):
    return LineStore(tmp_path / "lineas")


def add(ls, *rows):
    ls.append_rows(list(rows))


def lines_of(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ files
def test_rows_are_appended_per_patch_and_source_and_deduplicated(ls, tmp_path):
    add(ls, row(match_id="A"), row(match_id="B", patch="16.20"), row(match_id="C", source="diamante"))
    base = tmp_path / "lineas" / "filas"
    assert (base / "16.21" / "propia.jsonl").is_file() and (base / "16.20" / "propia.jsonl").is_file()
    assert (base / "16.21" / "diamante.jsonl").is_file()
    add(ls, row(match_id="A", diff14=999))                      # same (match, role, side) again
    assert len(lines_of(base / "16.21" / "propia.jsonl")) == 2
    got = {r["match_id"]: r for r in ls.read_rows()}
    assert set(got) == {"A", "B", "C"} and got["A"]["diff14"] == 999
    assert ls.patches() == ["16.21", "16.20"] and ls.sources() == ["diamante", "propia"]


def test_truncated_last_line_is_skipped_and_appending_recovers(ls, tmp_path):
    add(ls, row(match_id="A"))
    path = tmp_path / "lineas" / "filas" / "16.21" / "propia.jsonl"
    with open(path, "ab") as fh:
        fh.write(b'{"match_id":"X","role":"MIDDLE","cham')      # an interrupted run
    assert [r["match_id"] for r in ls.read_rows()] == ["A"]
    add(ls, row(match_id="B"))                                   # must not glue onto the broken line
    assert sorted(r["match_id"] for r in ls.read_rows()) == ["A", "B"]
    for line in lines_of(path)[:1] + lines_of(path)[-1:]:
        json.loads(line)


def test_jsonl_rows_are_compact_and_aggregates_pretty(ls, tmp_path):
    add(ls, row())
    assert "\n" not in lines_of(tmp_path / "lineas" / "filas" / "16.21" / "propia.jsonl")[0].strip()
    ls.rebuild()
    text = (tmp_path / "lineas" / "duelos" / "MIDDLE" / "1.json").read_text(encoding="utf-8")
    assert text.startswith("{\n ") and '"overall"' in text
    assert not list((tmp_path / "lineas").rglob("*.tmp"))


# ------------------------------------------------------------------ aggregation
def mid_rows():
    rows = [row(match_id=f"w{i}", result="win", diff14=600, gold14=400, xp14=200) for i in range(2)]
    rows.append(row(match_id="l1", result="loss", diff14=-500, gold14=-300, xp14=-200))
    rows.append(row(match_id="e1", result="even", diff14=100, gold14=60, xp14=40))
    rows += [row(match_id=f"o{i}", opp=3, result="loss", diff14=-800, gold14=-500, xp14=-300) for i in range(4)]
    return rows


def test_matchup_counts_lane_wr_and_averages(ls):
    add(ls, *mid_rows())
    m = ls.matchup("MIDDLE", 1, 2)
    assert (m["games"], m["wins"], m["losses"], m["even"]) == (4, 2, 1, 1)
    assert m["lane_wr"] == 0.625                                # (2 + 0.5 * 1) / 4
    assert m["avg_diff14"] == 200.0 and m["avg_gold14"] == 140.0 and m["avg_xp14"] == 60.0
    assert m["low_sample"] is True and m["patches"] == ["16.21"]
    assert ls.matchup("MIDDLE", 1, 99)["games"] == 0
    assert ls.matchup("MIDDLE", 1, 99)["lane_wr"] is None


def test_shrinkage_pulls_toward_the_champion_overall(ls):
    add(ls, *mid_rows())
    overall = ls.champion_overall("MIDDLE", 1)
    assert overall["games"] == 8 and overall["lane_wr"] == pytest.approx(2.5 / 8, abs=1e-3)
    m = ls.matchup("MIDDLE", 1, 2)
    prior = (2.5 + samples.PRIOR_GAMES * 0.5) / (8 + samples.PRIOR_GAMES)       # overall, pulled to 50%
    expected = (2.5 + samples.PRIOR_GAMES * prior) / (4 + samples.PRIOR_GAMES)
    assert m["shrunk_lane_wr"] == pytest.approx(expected, abs=1e-3)
    assert overall["lane_wr"] < m["shrunk_lane_wr"] < m["lane_wr"]
    # two games that both won do not say 100%
    add(ls, row(match_id="x1", champ=7, opp=8), row(match_id="x2", champ=7, opp=8))
    thin = ls.matchup("MIDDLE", 7, 8)
    assert thin["lane_wr"] == 1.0 and thin["shrunk_lane_wr"] < 0.7


def test_brackets_are_gain_per_minute_summed_over_games(ls):
    add(ls, row(match_id="a", brackets={"1-2": {"gain": 100, "minutes": 2}, "3-5": {"gain": -300, "minutes": 3}}),
        row(match_id="b", brackets={"1-2": {"gain": 200, "minutes": 2}}))
    got = {b["bracket"]: b for b in ls.matchup("MIDDLE", 1, 2)["brackets"]}
    assert got["1-2"] == {"bracket": "1-2", "gain_per_min": 75.0, "minutes": 4}
    assert got["3-5"]["gain_per_min"] == -100.0 and "6-8" not in got


def test_first_rates_ignore_unknown(ls):
    add(ls, row(match_id="a", first6=1, first2=1), row(match_id="b", first6=0, first2=None),
        row(match_id="c", first6=None, first2=1), row(match_id="d", first6=1, first2=0))
    m = ls.matchup("MIDDLE", 1, 2)
    assert m["first6_rate"] == pytest.approx(2 / 3, abs=1e-3) and m["first2_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert ls.matchup("MIDDLE", 1, 99)["first6_rate"] is None


def test_default_window_is_the_three_newest_patches_of_the_role(ls):
    add(ls, *[row(match_id=f"m{p}", patch=p) for p in ("16.9", "16.10", "16.11", "16.12")])
    add(ls, row(match_id="top", patch="16.13", role="TOP"))      # a newer patch, but another role
    m = ls.matchup("MIDDLE", 1, 2)
    assert m["patches"] == ["16.12", "16.11", "16.10"] and m["games"] == 3      # 16.10 > 16.9 numerically
    assert ls.matchup("MIDDLE", 1, 2, recent=1)["patches"] == ["16.12"]
    assert ls.matchup("MIDDLE", 1, 2, patches=["16.9"])["games"] == 1
    assert ls.matchup("MIDDLE", 1, 2, patches=samples.ALL_PATCHES)["games"] == 4
    assert ls.matchup("TOP", 1, 2)["patches"] == ["16.13"]


def test_source_and_person_filters(ls):
    add(ls, row(match_id="a", source="propia", person="Epsilon Osiris"),
        row(match_id="b", source="diamante", tier="DIAMOND"), row(match_id="c", source="propia"))
    assert ls.matchup("MIDDLE", 1, 2)["games"] == 3
    assert ls.matchup("MIDDLE", 1, 2, sources=["diamante"])["games"] == 1
    assert ls.matchup("MIDDLE", 1, 2, person="epsilon osiris")["games"] == 1
    assert ls.people() == {"Epsilon Osiris": 1}
    assert ls.find_person("Osiris", "Epsilon") == "Epsilon Osiris"
    assert ls.find_person("nadie", "Epsilon") is None


def test_best_into_ranks_by_shrunk_rate_with_games(ls):
    add(ls, *[row(match_id=f"g{i}", champ=1, opp=9, result="win") for i in range(3)],
        *[row(match_id=f"b{i}", champ=5, opp=9, result="loss") for i in range(3)],
        *[row(match_id=f"h{i}", champ=1, opp=9, result="win") for i in range(0)])
    ranked = ls.best_into("MIDDLE", 9, [5, 6, 1, 1])
    assert [m["champ"] for m in ranked] == [1, 6, 5]
    assert [m["games"] for m in ranked] == [3, 0, 3]
    assert ranked[0]["shrunk_lane_wr"] > 0.5 > ranked[2]["shrunk_lane_wr"]
    assert ranked[1]["shrunk_lane_wr"] == 0.5 and ranked[1]["lane_wr"] is None


def test_matchups_of_lists_opponents_with_enough_games(ls):
    add(ls, *mid_rows())
    got = ls.matchups_of("MIDDLE", 1)
    assert [m["opp"] for m in got] == [2, 3]                    # 2 first: it is better
    assert ls.matchups_of("MIDDLE", 1, min_games=5) == []


# ------------------------------------------------------------------ spikes
def spike_series(levels, fast_level=6, fast=300, slow=50):
    diff, out = 0, []
    for m, lv in enumerate(levels):
        if m:
            diff += fast if levels[m - 1] == fast_level else slow
        out.append([m, lv, 1, diff])
    return out


LEVELS = [1, 1, 2, 3, 3, 4, 5, 6, 6, 7, 8, 9, 9, 10, 10, 11, 11, 11, 11, 11, 11, 11]


def test_level_spikes_gain_per_own_level(ls):
    series = spike_series(LEVELS)
    add(ls, row(match_id="a", series=series), row(match_id="b", series=series))
    spikes = {s["level"]: s for s in ls.matchup("MIDDLE", 1, 2)["level_spikes"]}
    assert spikes[6] == {"level": 6, "gain_per_min": 300.0, "minutes": 4}        # minutes 8 and 9, two games
    assert spikes[2]["gain_per_min"] == 50.0 and spikes[1]["minutes"] == 4
    assert 11 not in spikes                                                      # only minutes before 15


def linear_item_series(change_at=10, before=50, after=150, minutes=25):
    return [[m, 5, 5, before * m if m <= change_at else before * change_at + after * (m - change_at)]
            for m in range(minutes + 1)]


def test_item_spikes_compare_three_minutes_after_and_before(ls):
    add(ls, row(match_id="a", items=[10.0, None, None], series=linear_item_series()),
        row(match_id="b", items=[12.0, 20.0, None], series=linear_item_series()))
    first = ls.matchup("MIDDLE", 1, 2)["item_spikes"][0]
    assert first["item"] == 1 and first["games"] == 2 and first["median_minute"] == 11.0
    # item at 10: 50 before / 150 after; item at 12: (800 - 450) / 3 = 116.7 before, 150 after
    assert first["gain_before"] == pytest.approx(83.3, abs=0.1) and first["gain_after"] == 150.0
    second = ls.matchup("MIDDLE", 1, 2)["item_spikes"][1]
    assert second["item"] == 2 and second["games"] == 1 and second["gain_after"] == 150.0


def test_item_spike_interpolates_and_needs_both_windows(ls):
    add(ls, row(match_id="a", items=[10.5, None, None], series=linear_item_series()),
        row(match_id="b", items=[2.0, None, None], series=linear_item_series()))      # no 3 min before it
    first = ls.matchup("MIDDLE", 1, 2)["item_spikes"][0]
    assert first["games"] == 2 and first["windows"] == 1
    assert first["gain_before"] == pytest.approx(66.7, abs=0.1) and first["gain_after"] == 150.0


def test_rows_without_items_give_no_item_spikes(ls):
    add(ls, row(match_id="a", items=None), row(match_id="b", items=[None, None, None]))
    assert ls.matchup("MIDDLE", 1, 2)["item_spikes"] == []


def test_game_length_win_rate_by_bucket(ls):
    add(ls, *[row(match_id=f"s{i}", minutes=22.0, win=1 if i < 3 else 0) for i in range(4)],
        *[row(match_id=f"l{i}", minutes=38.0, win=1 if i < 1 else 0) for i in range(4)],
        row(match_id="m", minutes=27.0, win=1))
    gl = ls.matchup("MIDDLE", 1, 2)["game_length"]
    assert gl["overall"] == {"games": 9, "wins": 5, "win_rate": 0.556}
    got = {b["bucket"]: b for b in gl["buckets"]}
    assert got["<25"]["win_rate"] == 0.75 and got["35+"]["win_rate"] == 0.25 and got["25-30"]["games"] == 1
    assert "30-35" not in got
    assert ls.matchup("MIDDLE", 1, 99)["game_length"] == {"overall": None, "buckets": []}


# ------------------------------------------------------------------ text
def test_text_summary_lines():
    base = {"games": 34, "lane_wr": 0.58, "avg_diff14": 420.0, "low_sample": False,
            "brackets": [{"bracket": "1-2", "gain_per_min": 35.0, "minutes": 60},
                         {"bracket": "3-5", "gain_per_min": 2.0, "minutes": 90},
                         {"bracket": "6-8", "gain_per_min": -60.0, "minutes": 80}],
            "first6_rate": 0.7, "first2_rate": 0.4}
    lines = text_summary(base)
    assert lines[0] == "Ganas la línea 58% (34 partidas, +420 oro+exp al 14)"
    assert lines[1] == "Fuerte en niveles 1-2 (+35/min) · débil en 6-8 (−60/min)"
    assert lines[2] == "Llegas antes a nivel 6 el 70% (a nivel 2, el 40%)"
    assert len(lines) == 3
    losing = text_summary({**base, "lane_wr": 0.3, "avg_diff14": -250.0})
    assert losing[0].startswith("Pierdes la línea: solo la ganas 30%") and "−250" in losing[0]
    assert text_summary({**base, "lane_wr": 0.5})[0].startswith("La línea va pareja")
    assert text_summary({"games": 0}) == ["Sin partidas de este cruce en las muestras."]


def test_text_summary_flags_thin_samples_and_ignores_thin_brackets():
    res = {"games": 2, "lane_wr": 1.0, "shrunk_lane_wr": 0.58, "avg_diff14": 300.0, "low_sample": True,
           "brackets": [{"bracket": "1-2", "gain_per_min": 90.0, "minutes": 2}],
           "first6_rate": None, "first2_rate": None}
    lines = text_summary(res)
    assert lines[0] == "Ganas la línea 100% (2 partidas, +300 oro+exp al 14)"
    assert lines[-1].startswith("Pocas partidas") and "58%" in lines[-1]
    assert not any("niveles" in line for line in lines)         # 2 minutes is not a pattern


def test_text_summary_spike_and_length_lines(ls):
    series = spike_series(LEVELS)       # +300 a minute while level 6 (minutes 8-9), +50 otherwise
    add(ls, *[row(match_id=f"a{i}", series=series, items=[7.0, None, None], minutes=22.0, win=1)
              for i in range(3)])
    add(ls, *[row(match_id=f"b{i}", series=series, items=[7.0, None, None], minutes=38.0, win=0)
              for i in range(3)])
    res = ls.matchup("MIDDLE", 1, 2)
    lines = text_summary(res)
    assert "Picos (pocas partidas): nivel 6; 1er item (min 7)" in lines
    assert "Mejor en partidas cortas (<25 min: 100%, pocas partidas)" in lines
    flat = spike_series(LEVELS, fast=50)                         # same pace everywhere: no peaks
    add(ls, *[row(match_id=f"f{i}", champ=9, series=flat) for i in range(3)])
    assert not any(line.startswith("Picos") for line in text_summary(ls.matchup("MIDDLE", 9, 2)))


def test_text_summary_without_spikes_has_no_extra_lines(ls):
    add(ls, row(match_id="a"))
    assert not any(line.startswith(("Picos", "Mejor en")) for line in text_summary(ls.matchup("MIDDLE", 1, 2)))


# ------------------------------------------------------------------ rebuild
def test_rebuild_writes_pretty_aggregates_and_index(ls, tmp_path):
    add(ls, *mid_rows(), row(match_id="d1", source="diamante", tier="DIAMOND", champ=1, opp=2),
        row(match_id="t1", role="TOP", champ=4, opp=5, patch="16.20"))
    ls.remember_names({1: "Diana", 2: "Akali", 3: "Zed", 4: "Garen", 5: "Darius"})
    index = ls.rebuild(now=0)
    path = tmp_path / "lineas" / "duelos" / "MIDDLE" / "1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["champion"] == 1 and data["champion_name"] == "Diana" and data["role"] == "MIDDLE"
    assert data["patches"] == ["16.21"] and data["sources"] == ["diamante", "propia"]
    assert data["overall"]["games"] == 9 and set(data["vs"]) == {"2", "3"}
    vs = data["vs"]["2"]
    assert vs["opp_name"] == "Akali" and vs["games"] == 5 and "shrunk_lane_wr" in vs
    assert {"brackets", "first6_rate", "first2_rate", "level_spikes", "item_spikes", "game_length"} <= set(vs)
    assert (tmp_path / "lineas" / "duelos" / "TOP" / "4.json").is_file()
    assert index["rows"] == {"diamante": 1, "propia": 9} and index["files"] == 2
    assert index["patches"] == ["16.21", "16.20"] and index["recent_patches"]["TOP"] == ["16.20"]
    assert json.loads((tmp_path / "lineas" / "indice.json").read_text(encoding="utf-8")) == index
    assert ls.read_duelo("MIDDLE", 1) == data and ls.read_duelo("MIDDLE", 77) is None


def test_rebuild_omits_spike_blocks_for_thin_matchups_and_drops_stale_files(ls, tmp_path):
    add(ls, *[row(match_id=f"a{i}") for i in range(samples.SPIKE_MIN_GAMES)], row(match_id="z", opp=3))
    ls.rebuild()
    vs = ls.read_duelo("MIDDLE", 1)["vs"]
    assert "level_spikes" in vs["2"] and "level_spikes" not in vs["3"] and vs["3"]["games"] == 1
    assert "level_spikes" in ls.read_duelo("MIDDLE", 1)["overall"]
    (tmp_path / "lineas" / "filas" / "16.21" / "propia.jsonl").unlink()
    ls.rebuild()
    assert not list((tmp_path / "lineas" / "duelos").glob("*/*.json"))


def test_rebuild_window_is_three_newest_patches_per_role(ls):
    add(ls, *[row(match_id=f"m{p}", patch=p) for p in ("16.9", "16.10", "16.11", "16.12")])
    ls.rebuild()
    data = ls.read_duelo("MIDDLE", 1)
    assert data["patches"] == ["16.12", "16.11", "16.10"] and data["overall"]["games"] == 3


def test_parse_role_and_names(ls):
    assert [samples.parse_role(t) for t in ("mid", "Jungla", "ADC", "soporte", "top", "support", "TOP", "x")] == [
        "MIDDLE", "JUNGLE", "BOTTOM", "UTILITY", "TOP", "UTILITY", "TOP", None]
    ls.remember_names({4: "TwistedFate", 7: "MonkeyKing"})
    assert ls.champion_id("twisted fate") == 4 and ls.champion_id("Wukong") == 7
    assert ls.champion_id("7") == 7 and ls.champion_id("nadie") is None
    assert ls.champion_name(4) == "Twisted Fate" and ls.champion_name(99) == "#99"


# ------------------------------------------------------------------ from matches
def lane_matches(n=2, **kwargs):
    ev = [bought(BLUE_MID, 3031, 600_000)]
    gold, xp = mid_edge(100, 30)
    return [(f"LA1_{i}", make_lane_match(f"LA1_{i}", created=1_700_000_000_000 + i, **kwargs),
             make_lane_timeline(gold=gold, xp=xp, events=ev)) for i in range(n)]


def test_add_matches_is_idempotent_and_records_skips(ls):
    short = ("LA1_short", make_lane_match("LA1_short", duration=800), make_lane_timeline())
    bare = ("LA1_bare", make_lane_match("LA1_bare"), None)
    items = lane_matches(2) + [short, bare]
    first = add_matches(ls, items, legendary_for=lambda version: {3031})
    assert first["matches"] == 3 and first["rows"] == 20
    assert dict(first["skipped"]) == {"corta": 1, "sin_timeline": 1}
    done = ls.processed()
    assert done["LA1_0"] == ["propia", "ok"] and done["LA1_short"] == ["propia", "corta"]
    assert "LA1_bare" not in done                               # may get its timeline later
    again = add_matches(ls, items, legendary_for=lambda version: {3031})
    assert again["matches"] == 0 and again["rows"] == 0
    assert len(ls.read_rows()) == 20
    mid = next(r for r in ls.read_rows() if r["match_id"] == "LA1_0" and r["role"] == "MIDDLE" and r["side"] == 100)
    assert mid["items"] == [10.0, None, None] and mid["source"] == "propia" and mid["platform"] == "la1"
    assert mid["tier"] is None and mid["patch"] == "16.21" and mid["win"] == 1 and mid["minutes"] == 20.0
    assert ls.names()[103] == "Champ3"


def test_wipe_source_forgets_only_that_source(ls):
    add_matches(ls, lane_matches(1))
    add_matches(ls, [("LA1_d", make_lane_match("LA1_d"), make_lane_timeline())], source="diamante", tier="DIAMOND")
    ls.wipe_source("propia")
    assert set(ls.processed()) == {"LA1_d"}
    assert {r["source"] for r in ls.read_rows()} == {"diamante"}
    assert add_matches(ls, lane_matches(1))["rows"] == 10


def test_no_row_stores_puuids_or_outsider_names(ls):
    names = {1: "Epsilon Seth", 3: "Epsilon Osiris", 5: "Amigo", 8: "Rival Secreto", 9: "Epsilon Thot"}
    puuids = {1: "p-seth", 3: "p-osiris", 5: "p-tracked", 8: "p-rival", 9: "p-thot"}
    clan = ClanConfig.build("Epsilon", "Epsilon Seth=Epsilon Osiris", {"p-tracked": "Amigo#LAN"})
    labeler = PersonLabeler({"p-tracked": "Amigo#LAN"}, clan)
    items = [(f"LA1_{i}", make_lane_match(f"LA1_{i}", names=names, puuids=puuids, created=1_700_000_000_000 + i),
              make_lane_timeline()) for i in range(2)]
    add_matches(ls, items, labeler=labeler)
    ls.rebuild()
    people = {r["person"] for r in ls.read_rows()}
    assert people == {"Epsilon Osiris", "Amigo", "Epsilon Thot", None}      # Seth is an alias of Osiris
    by_champ = {r["champ"]: r["person"] for r in ls.read_rows()}
    assert by_champ[101] == by_champ[103] == "Epsilon Osiris"
    assert by_champ[108] is None and by_champ[102] is None
    blob = "".join(p.read_text(encoding="utf-8") for p in ls.base.rglob("*") if p.is_file())
    for secret in ("puuid", "p-seth", "p-osiris", "p-tracked", "p-rival", "p-thot",
                   "Rival Secreto", "Epsilon Seth", "Jugador"):
        assert secret not in blob
    assert "Epsilon Osiris" in blob and "Epsilon Thot" in blob and "Amigo" in blob


# ------------------------------------------------------------------ from the stored matches
def make_lol_store(tmp_path, with_items=True):
    data = tmp_path / "data"
    store = Store(data / "lol.db")
    store.add_player("puuid-3", "Epsilon Osiris#LAN", is_main=True)
    for item in lane_matches(3, names={3: "Epsilon Osiris"}, puuids={3: "puuid-3"}):
        store.save_match(item[0], item[1], item[2])
    store.save_match("LA1_old", make_lane_match("LA1_old", version="16.19.1.1"), make_lane_timeline())
    store.save_match("LA1_bare", make_lane_match("LA1_bare"), None)
    if with_items:
        folder = data / "ddragon" / "16.21.1" / "data" / "es_MX"
        folder.mkdir(parents=True)
        (folder / "item.json").write_text(json.dumps(
            {"data": {"3031": {"gold": {"purchasable": True, "total": 3500}}}}), encoding="utf-8")
        (data / "ddragon" / "versions.json").write_text('["16.21.1"]', encoding="utf-8")
    return store, data


def test_update_from_lol_incremental_and_full(tmp_path):
    store, data = make_lol_store(tmp_path)
    done = samples.update_from_lol(store, data, tmp_path)
    assert done["matches"] == 4 and done["rows"] == 40 and done["sin_timeline"] == 1
    ls = LineStore(data / "lineas")
    rows = ls.read_rows()
    own = [r for r in rows if r["match_id"] == "LA1_0" and r["role"] == "MIDDLE" and r["side"] == 100][0]
    assert own["person"] == "Epsilon Osiris" and own["items"] == [10.0, None, None]
    # a patch that has no Data Dragon item list gets null items, never a guess from another patch
    old = [r for r in rows if r["match_id"] == "LA1_old"][0]
    assert old["items"] is None and old["patch"] == "16.19"
    assert (data / "lineas" / "indice.json").is_file()
    store.save_match("LA1_new", make_lane_match("LA1_new"), make_lane_timeline())
    assert samples.update_from_lol(store, data, tmp_path)["rows"] == 10             # only the new one
    full = samples.update_from_lol(store, data, tmp_path, full=True)
    assert full["rows"] == 50 and len(ls.read_rows()) == 50
    assert samples.update_from_lol(store, data, tmp_path)["rows"] == 0
    store.close()


def test_update_lanes_in_sync_is_fail_soft(tmp_path, monkeypatch):
    from lolan.sync import update_lanes

    store, data = make_lol_store(tmp_path)
    out = []
    update_lanes(store, out.append)
    assert out == ["Líneas: 40 filas nuevas de 4 partidas."]
    update_lanes(store, out.append)
    assert len(out) == 1                                         # nothing new, nothing said

    def boom(*a, **k):
        raise RuntimeError("roto")

    monkeypatch.setattr(samples, "update_from_lol", boom)
    update_lanes(store, out.append)                              # must not raise
    update_lanes(Store(":memory:"), out.append)
    assert len(out) == 1
    store.close()


def test_sync_players_updates_lanes_at_the_end(tmp_path):
    from conftest import AppRiot, world_matches

    from lolan.sync import sync_players

    store = Store(tmp_path / "data" / "lol.db")
    out = []
    sync_players(AppRiot("A", world_matches(2)), store, ["Yo#LAN"], main_riot_id="Yo#LAN", out=out.append)
    store.close()
    assert (tmp_path / "data" / "lol.db").is_file()
    assert not any("Líneas" in line for line in out)             # synthetic games carry no championIds


# ------------------------------------------------------------------ rebuild windowing
def test_rebuild_parses_only_the_window_folders_and_counts_the_rest_by_lines(ls, monkeypatch):
    for patch in ("16.18", "16.19", "16.20", "16.21"):
        add(ls, row(match_id=f"a{patch}", patch=patch), row(match_id=f"b{patch}", patch=patch, role="TOP"),
            row(match_id=f"c{patch}", patch=patch, role="JUNGLE"), row(match_id=f"d{patch}", patch=patch, role="BOTTOM"),
            row(match_id=f"e{patch}", patch=patch, role="UTILITY"))
    opened = []
    real = LineStore._read_file
    monkeypatch.setattr(LineStore, "_read_file", lambda self, path: opened.append(path.parent.name) or real(self, path))
    index = ls.rebuild()
    assert sorted(set(opened)) == ["16.19", "16.20", "16.21"]
    assert index["rows"] == {"propia": 20} and index["patches"][0] == "16.21"
    assert index["recent_patches"]["MIDDLE"] == ["16.21", "16.20", "16.19"]


def test_rebuild_window_is_per_role_even_when_a_role_lacks_the_newest_patches(ls):
    add(ls, *[row(match_id=f"m{p}", patch=p) for p in ("16.18", "16.19", "16.20", "16.21")],
        row(match_id="t17", patch="16.17", role="TOP", champ=4, opp=5), row(match_id="t21", patch="16.21", role="TOP", champ=4, opp=5))
    index = ls.rebuild()
    assert index["recent_patches"]["MIDDLE"] == ["16.21", "16.20", "16.19"]
    assert index["recent_patches"]["TOP"] == ["16.21", "16.17"]
    assert ls.read_duelo("TOP", 4)["overall"]["games"] == 2


def test_add_matches_reports_rows_per_champion(ls):
    done = add_matches(ls, lane_matches(2), legendary_for=lambda version: {3031})
    assert done["champions"][103] == 2 and done["champions"][108] == 2 and sum(done["champions"].values()) == 20


# ------------------------------------------------------------------ style fields
def test_rows_carry_the_style_fields_and_the_champion_aggregate_averages_them(ls):
    from test_lanephase import _style_extra
    add_matches(ls, [("LA1_1", make_lane_match("LA1_1", extra=_style_extra()), make_lane_timeline())])
    blue = next(r for r in ls.read_rows() if r["role"] == "MIDDLE" and r["side"] == 100)
    assert blue["bld_pm"] == 60.0 and blue["turret_plates"] == 3 and blue["cc_pm"] == 2.0
    assert set(samples.STYLE_KEYS) <= set(blue)
    ls.rebuild()
    overall = ls.read_duelo("MIDDLE", blue["champ"])["overall"]
    assert overall["bld_pm"] == 60.0 and overall["kp"] == blue["kp"] and overall["turret_plates"] == 3.0
    # per matchup entries do not repeat them
    assert "bld_pm" not in next(iter(ls.read_duelo("MIDDLE", blue["champ"])["vs"].values()))


def test_old_rows_without_style_fields_still_aggregate(ls):
    add(ls, row(), row(match_id="B", bld_pm=30.0, kp=0.5))      # one old row, one with two fields
    ls.rebuild()
    overall = ls.read_duelo("MIDDLE", 1)["overall"]
    assert overall["bld_pm"] == 30.0 and overall["kp"] == 0.5
    assert overall["tank_share"] is None and overall["dmg_share"] is None
    assert ls.champion_overall("MIDDLE", 1)["cc_pm"] is None            # reads without KeyError
