"""`lolan arquetipos`: deciding the ambiguous champion types from the Diamond+ rows."""
import json
import re
import shutil

import pytest
from lane_fixtures import row

from lolan import archetypes_data as ad
from lolan import comps
from lolan.__main__ import main
from lolan.samples import LineStore

STYLE_BASE = {"bld_pm": 200.0, "tank_share": 0.20, "dmg_share": 0.20, "cc_pm": 1.0, "kp": 0.5,
              "turret_plates": 1.0, "ka14": 4, "kp14": 0.4}
IDS = iter(range(1, 10_000))


@pytest.fixture
def ls(tmp_path):
    return LineStore(tmp_path / "lineas")


def add_games(ls, cid, name, lane, n=40, gain=0.0, diff14=0.0, early_wins=0.5, patch="16.21", **style):
    """n diamond rows of a champion; `gain` is gold+xp per minute at levels 1-5, early_wins the
    win rate of its short games (its long games win 1 - early_wins)."""
    fields = {**STYLE_BASE, **style}
    rows = []
    for i in range(n):
        short = i % 2 == 0
        wins = early_wins if short else 1 - early_wins
        win = 1 if (i // 2) % 100 < wins * 100 else 0
        rows.append(row(role=lane, champ=cid, source="diamante", patch=patch, side=100 + 100 * (i % 2),
                        match_id=f"{lane}-{cid}-{next(IDS)}", diff14=diff14 + (i % 3), win=win,
                        minutes=20.0 if short else 40.0,
                        brackets={"1-2": {"gain": gain * 2, "minutes": 2}, "3-5": {"gain": gain * 3, "minutes": 3}},
                        **fields))
    ls.append_rows(rows)
    ls.remember_names({cid: name})


def run(ls, lanes=ad.LANE_ORDER, classes=None):
    return ad.analyze(ls, classes or comps.load_classes(), lanes)


def cand(report, lane, champion):
    return next(c for c in report["candidates"] if c["lane"] == lane and c["champion"] == champion)


# ------------------------------------------------------------------ window and reading
def test_window_is_the_three_newest_patches_with_diamond_rows(ls):
    for patch in ("16.18", "16.19", "16.20", "16.21"):
        add_games(ls, 1, "Ekko", "JUNGLE", n=2, patch=patch)
    ls.append_rows([row(source="propia", patch="16.22", match_id="own")])           # not diamond
    assert ad.window_patches(ls) == ["16.21", "16.20", "16.19"]
    accs, patches = ad.collect(ls)
    assert patches == ["16.21", "16.20", "16.19"] and accs["JUNGLE"][1].games == 6      # 16.18 left out
    assert ad.window_patches(LineStore(ls.base / "nada")) == []


def test_rows_are_streamed_deduplicated_and_damaged_lines_skipped(ls):
    add_games(ls, 1, "Ekko", "JUNGLE", n=3)
    path = ls.rows_dir / "16.21" / "diamante.jsonl"
    with open(path, "ab") as fh:
        fh.write(path.read_bytes().splitlines()[0] + b"\n")                  # the same row again
        fh.write(b'{"match_id":"x","role":"JUNGLE","cham')                    # an interrupted run
    assert len(list(ad.iter_rows(ls, ["16.21"]))) == 3


def test_old_rows_without_style_fields_count_for_the_early_measures_only(ls):
    rows = [row(role="TOP", champ=1, source="diamante", match_id=f"old{i}", side=100,
                brackets={"1-2": {"gain": 100, "minutes": 2}}) for i in range(40)]
    ls.append_rows(rows)
    measures = ad.collect(ls)[0]["TOP"][1].measures()
    assert measures["early_gain"] == 50 and measures["tank_share"] is None and measures["bld_pm"] is None
    assert measures["wr_gap"] is None                                       # no short or long games


# ---------------------------------------------------------------------- the rules
def jungle_lane(ls):
    add_games(ls, 1, "Ekko", "JUNGLE", kp14=0.7, ka14=7, early_wins=0.9)
    add_games(ls, 2, "Diana", "JUNGLE", kp14=0.1, ka14=1, early_wins=0.1)
    for cid, name in ((3, "Viego"), (4, "LeeSin"), (5, "Zac"), (6, "Sejuani")):
        add_games(ls, cid, name, "JUNGLE")


def test_jungle_early_versus_farm_by_early_ness(ls):
    jungle_lane(ls)
    report = run(ls, ("JUNGLE",))
    ekko, diana = cand(report, "JUNGLE", "Ekko"), cand(report, "JUNGLE", "Diana")
    assert ekko["decision"] == "early" and ekko["ev"]["early"] > 1 and ekko["games"] == 40
    assert ekko["ev"]["early_parts"] == ["kills", "slope"] and "faltan" not in ekko["reason"]
    assert diana["decision"] == "farm" and diana["ev"]["early"] < -1
    assert ekko["ev"]["tank"] is None                                        # no style fields in these rows


def test_the_ambiguous_middle_of_the_jungle_stays_pending(ls):
    jungle_lane(ls)
    add_games(ls, 7, "Hecarim", "JUNGLE", kp14=0.41, early_wins=0.5)               # a typical jungler
    report = run(ls, ("JUNGLE",))
    hecarim = cand(report, "JUNGLE", "Hecarim")
    assert hecarim["decision"] is None and "ambigua" in hecarim["reason"]


def test_a_champion_needs_30_games_in_the_lane(ls):
    jungle_lane(ls)
    add_games(ls, 7, "Nidalee", "JUNGLE", n=29, kp14=0.7, early_wins=0.9)
    add_games(ls, 8, "Graves", "JUNGLE", n=30, kp14=0.7, early_wins=0.9)
    report = run(ls, ("JUNGLE",))
    thin = cand(report, "JUNGLE", "Nidalee")
    assert thin["decision"] is None and "29 partidas" in thin["reason"]
    assert cand(report, "JUNGLE", "Graves")["decision"] == "early"
    absent = cand(report, "JUNGLE", "Trundle")                               # no rows at all
    assert absent["games"] == 0 and absent["decision"] is None


def test_gold_and_xp_against_the_enemy_jungler_do_not_make_a_jungler_early(ls):
    jungle_lane(ls)
    # a fast clearer: huge gold+xp lead and diff14, but typical kills and a typical game-length slope
    add_games(ls, 7, "Karthus", "JUNGLE", gain=150, diff14=2500, kp14=0.4, early_wins=0.5)
    accs = ad.collect(ls)[0]["JUNGLE"]
    stats = ad.lane_stats({cid: (a.measures(), a.games) for cid, a in accs.items()})
    ev = ad.evidence(accs[7].measures(), stats)
    assert ev["lane"] > 1                                   # the gold-based score does see it ...
    assert abs(ev["early"]) < ad.DECISION_MARGIN            # ... but early-ness does not
    assert ad.decide("JUNGLE", ev)[0] is None


def test_without_early_kills_only_the_slope_counts_and_the_report_says_so(ls):
    for cid, name, wins in ((1, "Ekko", 0.9), (2, "Diana", 0.1), (3, "Viego", 0.5), (4, "LeeSin", 0.5),
                            (5, "Zac", 0.5), (6, "Sejuani", 0.5)):
        add_games(ls, cid, name, "JUNGLE", early_wins=wins, ka14=None, kp14=None)
    ekko = cand(run(ls, ("JUNGLE",)), "JUNGLE", "Ekko")
    assert ekko["ev"]["early_parts"] == ["slope"] and ekko["decision"] == "early"
    assert "solo la pendiente de victorias" in ekko["reason"] and "ka14" in ekko["reason"]


def test_the_slope_alone_needs_a_clearer_signal_than_both_parts(ls):
    ev = {"early": 0.5, "early_parts": ["slope"]}
    assert ad.decide("JUNGLE", ev)[0] is None and "solo la pendiente" in ad.decide("JUNGLE", ev)[1]
    assert ad.decide("JUNGLE", {"early": 0.8, "early_parts": ["slope"]})[0] == "early"
    assert ad.decide("JUNGLE", {"early": -0.8, "early_parts": ["slope"]})[0] == "farm"
    assert ad.decide("JUNGLE", {"early": 0.5, "early_parts": ["kills", "slope"]})[0] == "early"


def test_ka14_stands_in_when_kp14_is_missing(ls):
    for cid, name, ka in ((1, "Ekko", 8), (2, "Diana", 0), (3, "Viego", 4), (4, "LeeSin", 4), (5, "Zac", 4),
                          (6, "Sejuani", 4)):
        add_games(ls, cid, name, "JUNGLE", ka14=ka, kp14=None)
    report = run(ls, ("JUNGLE",))
    assert cand(report, "JUNGLE", "Ekko")["decision"] == "early" and cand(report, "JUNGLE", "Diana")["decision"] == "farm"


def test_lane_stats_need_enough_champions(ls):
    for cid, name in ((1, "Ekko"), (2, "Diana"), (3, "Viego")):
        add_games(ls, cid, name, "JUNGLE", gain=cid * 10)
    ekko = cand(run(ls, ("JUNGLE",)), "JUNGLE", "Ekko")
    assert ekko["ev"]["early"] is None and ekko["decision"] is None          # 3 champions < MIN_CHAMPIONS


def top_lane(ls):
    add_games(ls, 1, "Chogath", "TOP", tank_share=0.30, cc_pm=3.0, bld_pm=100.0, kp=0.5)
    add_games(ls, 2, "Camille", "TOP", tank_share=0.14, bld_pm=600.0, turret_plates=3.0, kp=0.40)
    add_games(ls, 3, "Volibear", "TOP")
    for cid, name in ((4, "Garen"), (5, "Darius"), (6, "Fiora")):
        add_games(ls, cid, name, "TOP")


def test_top_tanque_split_or_bruiser(ls):
    top_lane(ls)
    report = run(ls, ("TOP",))
    chogath, camille, volibear = (cand(report, "TOP", n) for n in ("Chogath", "Camille", "Volibear"))
    assert chogath["decision"] == "tanque" and chogath["ev"]["tank"] > 1 and chogath["ev"]["split"] < 0
    assert camille["decision"] == "split" and camille["ev"]["split"] > 1 and camille["ev"]["tank"] < 0
    assert volibear["decision"] == "bruiser"
    assert volibear["ev"]["tank"] < ad.DECISION_MARGIN and volibear["ev"]["split"] < ad.DECISION_MARGIN


def test_top_without_style_fields_stays_pending(ls):
    for cid, name in ((1, "Chogath"), (2, "Camille"), (3, "Volibear"), (4, "Garen"), (5, "Darius"), (6, "Fiora")):
        add_games(ls, cid, name, "TOP", tank_share=None, bld_pm=None, cc_pm=None, kp=None, turret_plates=None,
                  dmg_share=None)
    camille = cand(run(ls, ("TOP",)), "TOP", "Camille")
    assert camille["decision"] is None and "faltan los campos de estilo" in camille["reason"]


def test_a_missing_optional_part_does_not_block_a_decision(ls):
    top_lane(ls)
    # turret plates are optional (not every match carries them): drop them everywhere
    path = ls.rows_dir / "16.21" / "diamante.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    path.write_text("\n".join(json.dumps({**r, "turret_plates": None}) for r in rows) + "\n", encoding="utf-8")
    camille = cand(run(ls, ("TOP",)), "TOP", "Camille")
    assert camille["decision"] == "split"


def mid_lane(ls):
    add_games(ls, 1, "Zoe", "MIDDLE", tank_share=0.14, gain=40, diff14=500, early_wins=0.7)
    add_games(ls, 2, "Sylas", "MIDDLE", tank_share=0.27, gain=-20, diff14=-200, early_wins=0.4)
    add_games(ls, 3, "Locke", "MIDDLE", tank_share=0.20, gain=2, diff14=10)
    for cid, name in ((4, "Orianna"), (5, "Syndra"), (6, "Veigar")):
        add_games(ls, cid, name, "MIDDLE")


def test_mid_asesino_versus_melee_by_tankiness_and_early_burst(ls):
    mid_lane(ls)
    report = run(ls, ("MIDDLE",))
    zoe, sylas, locke = (cand(report, "MIDDLE", n) for n in ("Zoe", "Sylas", "Locke"))
    assert zoe["decision"] == "asesino" and sylas["decision"] == "melee"
    assert locke["decision"] is None and "ambigua" in locke["reason"]


def test_other_lanes_have_no_data_rule(ls):
    assert ad.decide("BOTTOM", {"early": 2.0, "tank": 2.0, "split": 2.0})[0] is None
    assert ad.decide("UTILITY", {})[0] is None
    assert ad.DECIDABLE == {"JUNGLE": ("early", "farm"), "TOP": ("tanque", "bruiser", "split"),
                            "MIDDLE": ("asesino", "melee")}


# --------------------------------------------------------------------- sanity report
def test_sanity_report_flags_strong_contradictions_only(ls):
    jungle_lane(ls)
    add_games(ls, 9, "Viego2", "JUNGLE")
    # Leona is an engage support with far less tankiness than her lane; Nautilus fits
    add_games(ls, 20, "Leona", "UTILITY", tank_share=0.05)
    add_games(ls, 21, "Nautilus", "UTILITY", tank_share=0.26)
    for cid, name in ((22, "Alistar"), (23, "Lulu"), (24, "Janna"), (25, "Soraka")):
        add_games(ls, cid, name, "UTILITY", tank_share=0.10 if name != "Alistar" else 0.26)
    report = run(ls)
    found = {(c["lane"], c["champion"]): c for c in report["contradictions"]}
    assert ("UTILITY", "Leona") in found and "tank" in found[("UTILITY", "Leona")]["why"]
    assert ("UTILITY", "Nautilus") not in found and ("UTILITY", "Lulu") not in found
    # jungle: Zac / Sejuani sit at the average, nothing to flag; Ekko is `early` and measures early
    assert not any(c["lane"] == "JUNGLE" and c["champion"] in ("Ekko", "Zac", "Sejuani") for c in found.values())
    assert found[("UTILITY", "Leona")]["source"] == "oficial"
    assert all(c["games"] >= ad.MIN_GAMES for c in report["contradictions"])


def test_report_text_lists_candidates_and_contradictions(ls):
    jungle_lane(ls)
    lines = ad.report_lines(run(ls, ("JUNGLE",)))
    text = "\n".join(lines)
    assert "parches 16.21" in text and "mínimo 30 partidas" in text
    assert re.search(r"Jungla\s+Ekko\s+early\s+40", text) and "early (igual)" in text
    assert "pendiente: solo 0 partidas" in text and "usa --decidir" in text
    assert "Contradicciones fuertes" in text
    assert "No hay partidas Diamante+" in "\n".join(ad.report_lines(run(LineStore(ls.base / "vacio"))))


# ------------------------------------------------------------------------- writing
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    copy = tmp_path / "classes.json"
    shutil.copyfile(comps.CLASSES_PATH, copy)
    monkeypatch.setattr(comps, "CLASSES_PATH", copy)
    return tmp_path


def test_arquetipos_prints_without_writing(home, capsys):
    jungle_lane(LineStore(home / "data" / "lineas"))
    before = comps.CLASSES_PATH.read_bytes()
    assert main(["arquetipos", "--linea", "jungla"]) == 0
    out = capsys.readouterr().out
    assert "Ekko" in out and "early (igual)" in out and "usa --decidir" in out and "Top " not in out
    assert comps.CLASSES_PATH.read_bytes() == before


def test_decidir_writes_the_decisions_into_the_json(home, capsys):
    jungle_lane(LineStore(home / "data" / "lineas"))
    original = json.loads(comps.CLASSES_PATH.read_text(encoding="utf-8"))
    assert main(["arquetipos", "--decidir"]) == 0
    out = capsys.readouterr().out
    assert "se escribieron en lolan/data/champion_classes.json" in out and "Reinicia el panel" in out
    saved = json.loads(comps.CLASSES_PATH.read_text(encoding="utf-8"))
    ekko = saved["Ekko"]["proposal"]["JUNGLE"]
    assert ekko["type"] == "early" and ekko["basis"] == "datos" and ekko["n"] == 40
    assert ekko["patches"] == ["16.21"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ekko["date"])
    assert ekko["previous"] == "early" and ekko["evidence"]["early"] > 1 and "Diamante+" in ekko["note"]
    assert ekko["evidence"]["measures"]["kp14"] == 0.7 and ekko["evidence"]["measures"]["wr_gap"] >= 0.5
    diana = saved["Diana"]["proposal"]["JUNGLE"]
    assert diana["type"] == "farm" and diana["previous"] == "early"           # the rule changed it
    # nothing else changed: same champions, same other lanes, same other jungle entries
    changed = {(n, lane) for n, e in saved.items() for lane, p in e["proposal"].items()
               if p != original[n]["proposal"][lane]}
    assert changed == {("Ekko", "JUNGLE"), ("Diana", "JUNGLE")}
    assert {k: v for k, v in saved["Ekko"].items() if k != "proposal"} == {
        k: v for k, v in original["Ekko"].items() if k != "proposal"}
    # the tables built from the file now say "por datos"
    tables = comps.build_tables(saved)
    assert tables["JUNGLE"]["Diana"] == {"type": "farm", "source": "datos"}
    assert tables["JUNGLE"]["Hecarim"]["source"] == "pendiente"             # not decided: stays pending
    assert saved["Ekko"]["proposal"]["TOP"] == original["Ekko"]["proposal"]["TOP"]


def test_decidir_again_keeps_a_decision_the_data_no_longer_supports(home, capsys):
    jungle_lane(LineStore(home / "data" / "lineas"))
    main(["arquetipos", "--decidir"])
    capsys.readouterr()
    shutil.rmtree(home / "data" / "lineas" / "filas")                        # the sample is gone
    assert main(["arquetipos", "--decidir"]) == 0
    capsys.readouterr()
    saved = json.loads(comps.CLASSES_PATH.read_text(encoding="utf-8"))
    assert saved["Diana"]["proposal"]["JUNGLE"]["basis"] == "datos"           # kept, not erased
    assert comps.build_tables(saved)["JUNGLE"]["Diana"]["source"] == "datos"


def test_arquetipos_with_no_sample_is_friendly(home, capsys):
    assert main(["arquetipos"]) == 0
    assert "No hay partidas Diamante+" in capsys.readouterr().out
    assert main(["arquetipos", "--decidir"]) == 0
    assert "Reinicia el panel" not in capsys.readouterr().out
    assert main(["arquetipos", "--linea", "soporte"]) == 0


def test_arquetipos_rejects_an_unknown_line(home, capsys):
    with pytest.raises(SystemExit):
        main(["arquetipos", "--linea", "banca"])


def test_apply_decisions_only_touches_decided_candidates():
    classes = {"A": {"proposal": {"TOP": {"type": "bruiser", "basis": "regla"}}},
               "B": {"proposal": {"TOP": {"type": "split", "basis": "manual"}}}}
    report = {"patches": ["16.21"], "candidates": [
        {"lane": "TOP", "champion": "A", "games": 50, "decision": "tanque", "reason": "x", "type": "bruiser",
         "ev": {"early": None, "tank": 1.234, "split": -0.5, "carry": 0.1}, "measures": {"tank_share": 0.3}},
        {"lane": "TOP", "champion": "B", "games": 10, "decision": None, "reason": "y", "type": "split",
         "ev": {}, "measures": {}}]}
    written = ad.apply_decisions(classes, report, today="2026-10-24")
    assert [w["champion"] for w in written] == ["A"]
    assert classes["A"]["proposal"]["TOP"]["type"] == "tanque" and classes["A"]["proposal"]["TOP"]["date"] == "2026-10-24"
    assert classes["A"]["proposal"]["TOP"]["evidence"]["tank"] == 1.23
    assert classes["B"]["proposal"]["TOP"] == {"type": "split", "basis": "manual"}
