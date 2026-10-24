"""The draft recommender with the main player in any line (main_role), and the ADC default untouched."""
import json
from pathlib import Path

import pytest
from comps_fixtures import ENEMY, LINEUPS, build_records

from lolan import comps

GOLDEN = Path(__file__).parent / "fixtures" / "draft_golden.json"


@pytest.fixture(scope="module")
def records():
    return build_records()


def lineup_of(raw):
    return {role: tuple(v) for role, v in raw.items()}


@pytest.mark.parametrize("name", sorted(LINEUPS))
@pytest.mark.parametrize("include_all", [False, True])
def test_default_main_role_is_identical_to_the_original_output(records, name, include_all):
    """GOLDEN was produced by the code before main_role existed (main player = ADC)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[f"{name}|{include_all}"]
    lineup = lineup_of(LINEUPS[name])
    for explicit in ({}, {"main_role": "BOTTOM"}):
        got = {
            "draft": comps.recommend_draft(lineup, records, ENEMY, include_all=include_all, **explicit),
            "notes": comps.draft_notes(lineup, records, ENEMY, **explicit),
            "ranking": comps.recommend(lineup, records, ENEMY, include_all=include_all),
        }
        assert json.loads(json.dumps(got)) == golden


def test_main_mid_gets_his_own_mid_pool_and_bottom_becomes_a_normal_line(records):
    lineup = {"UTILITY": ("Epsilon Alfa", None), "BOTTOM": ("Epsilon Gamma", None)}
    draft = comps.recommend_draft(lineup, records, ENEMY, main_role="MIDDLE")
    assert list(draft["roles"]) == ["TOP", "JUNGLE", "MIDDLE", "UTILITY", "BOTTOM"]
    mid = draft["roles"]["MIDDLE"]
    assert mid["is_main"] and mid["mode"] == "campeones" and mid["person"] == "Epsilon Me"
    assert {p["champion"] for p in mid["picks"]} == {"Zed", "Ahri", "Orianna"}      # his own mid games
    bottom = draft["roles"]["BOTTOM"]
    assert bottom["mode"] == "campeones" and "is_main" not in bottom
    assert {p["champion"] for p in bottom["picks"]} <= {"Jinx", "Kaisa", "Jhin"}      # Gamma's ADC pool
    assert all(p["type"] in ("followup", "kiting", "flexible", "otro") for p in bottom["picks"])
    assert [r for r in draft["roles"] if "is_main" in draft["roles"][r]] == ["MIDDLE"]


def test_main_top_with_unknown_teammates_suggests_types_elsewhere(records):
    draft = comps.recommend_draft({}, records, ENEMY, main_role="TOP")
    top = draft["roles"]["TOP"]
    assert top["is_main"] and [p["champion"] for p in top["picks"]] == ["Garen"]
    for role in ("JUNGLE", "MIDDLE", "UTILITY", "BOTTOM"):
        assert draft["roles"][role]["mode"] == "tipos"
    assert {p["type"] for p in draft["roles"]["BOTTOM"]["picks"]} == {"followup", "kiting", "flexible"}


def test_main_without_enough_games_in_the_line_gets_types_and_a_note(records):
    draft = comps.recommend_draft({}, records, main_role="JUNGLE")
    jungle = draft["roles"]["JUNGLE"]
    assert jungle["mode"] == "tipos" and "Tienes menos de 3 partidas en Jungla" in jungle["note"]


def test_a_champion_locked_by_the_main_is_fixed_and_counts_for_the_plan(records):
    draft = comps.recommend_draft({"MIDDLE": (None, "Zed")}, records, main_role="MIDDLE")
    mid = draft["roles"]["MIDDLE"]
    assert mid["mode"] == "fijo" and mid["fixed"] == "Zed" and mid["picks"] == []
    assert {"role": "MIDDLE", "label": "Mid", "text": "Zed"} in draft["plan"]["members"]


def test_a_typed_adc_is_part_of_the_team_when_the_main_is_not_the_adc(records):
    with_adc = comps.recommend_draft({"BOTTOM": (None, "Jinx")}, records, main_role="MIDDLE")
    assert with_adc["roles"]["BOTTOM"]["mode"] == "fijo"
    assert {"role": "BOTTOM", "label": "ADC", "text": "Jinx"} in with_adc["plan"]["members"]


def test_notes_name_the_line_instead_of_the_adc_fit_when_the_main_is_not_the_adc(records):
    notes = comps.draft_notes({"TOP": (None, "Malphite")}, records, ENEMY, main_role="MIDDLE")
    assert notes[0].startswith("Juegas Mid: tus picks salen de tu historial en Mid")
    assert not any(n.startswith("Soporte") for n in notes)
    assert any(n.startswith("Engage duro en tu equipo (sin contarte): 1") for n in notes)
    # with the main in the ADC slot a typed ADC would be ignored; as mid it counts
    adc_notes = comps.draft_notes({"BOTTOM": (None, "Leona")}, records, main_role="MIDDLE")
    assert any("Engage duro" in n and ": 1" in n for n in adc_notes)


def test_unknown_main_role_is_rejected(records):
    with pytest.raises(ValueError):
        comps.recommend_draft({}, records, main_role="ADC")
    with pytest.raises(ValueError):
        comps.draft_notes({}, records, main_role="")
