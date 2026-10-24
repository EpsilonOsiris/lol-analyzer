"""The classification tables come from lolan/data/champion_classes.json, and the `mago` type."""
import pytest
from comps_fixtures import ALFA, BETA, GAMMA, script_matches
from composiciones_golden import ME_RIOT_ID, add_script_games, page_body
from conftest import ME_PUUID

from lolan import comps, web


def entry(name, proposal, aliases=None, **extra):
    out = {"name": name, "riot_class": "Mage", "riot_subclasses": ["Burst"], "proposal": proposal, **extra}
    if aliases:
        out["aliases"] = aliases
    return out


def prop(kind, basis="regla"):
    return {"type": kind, "basis": basis, "current": None, "current_source": None, "note": ""}


# ------------------------------------------------------------------ build_tables
def test_tables_are_built_from_the_json_content_with_a_source_per_basis():
    classes = {
        "Alpha": entry("Alpha", {"TOP": prop("mago", "regla"), "MIDDLE": prop("control", "manual"),
                                 "JUNGLE": prop("farm", "actual"), "BOTTOM": prop("mago", "datos")}),
        "Beta": entry("Beta", {"TOP": prop("otro", "regla"), "UTILITY": prop("poke", "regla"),
                               "NOWHERE": prop("poke", "regla")}),
    }
    tables = comps.build_tables(classes, video_adc={}, video_support={}, pending={})
    assert tables["TOP"] == {"Alpha": {"type": "mago", "source": "oficial"}}         # "otro" is left out
    assert tables["MIDDLE"]["Alpha"] == {"type": "control", "source": "criterio"}
    assert tables["JUNGLE"]["Alpha"] == {"type": "farm", "source": "criterio"}
    assert tables["BOTTOM"]["Alpha"] == {"type": "mago", "source": "datos"}
    assert tables["UTILITY"]["Beta"] == {"type": "poke", "source": "oficial"}
    assert set(tables) == set(comps.TABLE_LANES)


def test_video_entries_win_over_the_json():
    classes = {"Alpha": entry("Alpha", {"BOTTOM": prop("mago"), "UTILITY": prop("poke")}),
               "Gamma": entry("Gamma", {"BOTTOM": prop("mago")})}
    tables = comps.build_tables(classes, video_adc={"kiting": ["Alpha"]}, video_support={"peel": ["Alpha"]},
                                pending={})
    assert tables["BOTTOM"]["Alpha"] == {"type": "kiting", "source": "video"}
    assert tables["UTILITY"]["Alpha"] == {"type": "peel", "source": "video"}
    assert tables["BOTTOM"]["Gamma"] == {"type": "mago", "source": "oficial"}


def test_pending_entries_keep_the_proposal_but_a_data_decision_replaces_the_mark():
    classes = {"Alpha": entry("Alpha", {"TOP": prop("bruiser", "regla")}),
               "Beta": entry("Beta", {"TOP": prop("split", "datos")}),
               "Gamma": entry("Gamma", {"TOP": prop("tanque", "manual")})}
    tables = comps.build_tables(classes, video_adc={}, video_support={},
                                pending={"TOP": frozenset({"Alpha", "Beta"}), "MIDDLE": frozenset({"Gamma"})})
    assert tables["TOP"]["Alpha"] == {"type": "bruiser", "source": "pendiente"}
    assert tables["TOP"]["Beta"] == {"type": "split", "source": "datos"}
    assert tables["TOP"]["Gamma"]["source"] == "criterio"            # pending is per lane


def test_an_alias_is_the_table_key():
    classes = {"Fiddlesticks": entry("Fiddlesticks", {"JUNGLE": prop("farm")}, aliases=["FiddleSticks"])}
    assert list(comps.build_tables(classes, video_adc={}, video_support={}, pending={})["JUNGLE"]) == ["FiddleSticks"]


# --------------------------------------------------------------- the real tables
def test_the_real_json_feeds_every_public_table():
    classes = comps.load_classes()
    assert len(classes) == 173 and comps.CLASSES.keys() == classes.keys()
    tables = comps.build_tables(classes)
    assert comps.ADC_CLASS == tables["BOTTOM"] and comps.SUPPORT_CLASS == tables["UTILITY"]
    for lane in ("TOP", "JUNGLE", "MIDDLE"):
        assert comps.ROLE_CLASS[lane] == tables[lane]
    # the sources used in the tables are the documented ones
    sources = {e["source"] for t in (comps.ADC_CLASS, comps.SUPPORT_CLASS, *comps.ROLE_CLASS.values())
               for e in t.values()}
    assert sources <= set(comps.SOURCE_LABELS)


def test_every_video_entry_is_kept_exactly_as_it_was():
    for kind, champions in comps.VIDEO_ADC.items():
        for champion in champions:
            assert comps.ADC_CLASS[champion] == {"type": kind, "source": "video"}, champion
    for kind, champions in comps.VIDEO_SUPPORT.items():
        for champion in champions:
            assert comps.SUPPORT_CLASS[champion] == {"type": kind, "source": "video"}, champion
    assert {c for cs in comps.VIDEO_ADC.values() for c in cs} == {
        "Kaisa", "Twitch", "Tristana", "Samira", "Lucian", "Draven", "Vayne", "Zeri", "Ezreal", "Sivir",
        "Ashe", "Jinx", "Aphelios", "Senna", "Varus", "Caitlyn", "KogMaw"}
    assert {c for cs in comps.VIDEO_SUPPORT.values() for c in cs} == {"Taric", "TahmKench", "Braum"}
    # a JSON value that disagrees with the video does not matter: Varus is flexible as an ADC
    assert comps.adc_archetype("Varus") == "flexible" and comps.adc_source("Varus") == "video"


def test_fiddlesticks_uses_the_spelling_of_the_match_data():
    assert "FiddleSticks" in comps.ROLE_CLASS["JUNGLE"] and "Fiddlesticks" not in comps.ROLE_CLASS["JUNGLE"]
    assert comps.canonical_champion("Fiddlesticks") == "FiddleSticks" == comps.canonical_champion("fiddle sticks")
    assert comps.role_type("JUNGLE", "Fiddlesticks") == "farm"
    assert comps.is_hard_engage("Fiddlesticks") is False


def test_pending_entries_exist_in_the_tables_and_are_judgment_calls_of_the_documented_lanes():
    assert set(comps.PENDING_DATA) == {"TOP", "JUNGLE", "MIDDLE"}
    for lane, champions in comps.PENDING_DATA.items():
        for champion in champions:
            assert comps.ROLE_CLASS[lane][champion]["source"] == "pendiente", (lane, champion)
    # the ones the user named
    assert {"Camille", "Irelia", "Singed", "Volibear", "Zaahen"} <= comps.PENDING_DATA["TOP"]
    assert {"Diana", "Ekko", "Hecarim"} <= comps.PENDING_DATA["JUNGLE"] and "Locke" in comps.PENDING_DATA["MIDDLE"]
    assert comps.role_source("JUNGLE", "Diana") == "pendiente" and comps.role_type("JUNGLE", "Diana") == "early"


def test_old_entries_missing_from_the_proposal_are_kept():
    assert comps.role_type("TOP", "Rammus") == "tanque" and comps.role_type("TOP", "Skarner") == "bruiser"
    assert comps.role_type("JUNGLE", "Rell") == "tanque" and comps.role_type("MIDDLE", "Corki") == "control"


def test_source_tags_in_plain_spanish():
    assert comps.SOURCE_LABELS == {"video": "del video", "oficial": "clase oficial de Riot",
                                   "criterio": "criterio", "datos": "por datos",
                                   "pendiente": "pendiente de datos"}
    assert comps.source_tag("oficial") == ", clase oficial de Riot" and comps.source_tag(None) == ""


# -------------------------------------------------------------------------- mago
def test_mago_exists_in_top_and_bottom_with_labels_and_order():
    assert comps.role_type("TOP", "Vladimir") == "mago" and comps.role_type("BOTTOM", "Ziggs") == "mago"
    assert comps.role_type("TOP", "Jayce") == "mago" and comps.role_type("MIDDLE", "Vladimir") == "control"
    assert comps.type_label("TOP", "mago") == "Mago" and comps.type_label("BOTTOM", "mago") == "Mago"
    assert "mago" in comps.ROLE_TYPE_ORDER["TOP"] and "mago" in comps.ROLE_TYPE_ORDER["BOTTOM"]
    assert comps.ROLE_TYPE_ORDER["BOTTOM"][-1] == "otro" and comps.ROLE_TYPE_ORDER["TOP"][-1] == "otro"
    for role in ("JUNGLE", "MIDDLE", "UTILITY"):
        assert "mago" not in comps.ROLE_TYPE_ORDER[role]
        assert "mago" not in {e["type"] for e in (comps.ROLE_CLASS.get(role) or comps.SUPPORT_CLASS).values()}


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if len(ordered) % 2 else sum(ordered[len(ordered) // 2 - 1:][:2]) / 2


def test_mago_fit_is_neutral_never_the_best_anywhere():
    assert set(comps.FIT["mago"]) == set(comps.SUPPORT_ORDER) and set(comps.FIT_UNKNOWN_SUPPORT) == set(comps.ADC_ORDER)
    video = ("followup", "kiting", "flexible")
    for support in ("engage", "peel", "mixto", "poke"):
        values = [comps.FIT[t][support] for t in video]
        assert comps.FIT["mago"][support] <= median(values), support
        assert comps.FIT["mago"][support] < max(values) and comps.FIT["mago"][support] < comps.FIT["flexible"][support]
    assert comps.FIT_UNKNOWN_SUPPORT["mago"] <= median([comps.FIT_UNKNOWN_SUPPORT[t] for t in video])
    assert comps.FIT_UNKNOWN_SUPPORT["mago"] < comps.FIT_UNKNOWN_SUPPORT["flexible"]
    assert set(comps.ENGAGE_FIT["mago"]) == set(comps.ENGAGE_BUCKETS)
    assert set(comps.ENGAGE_FIT["mago"].values()) == {0.0}


def test_a_top_mage_is_not_split_and_an_artillery_mage_is_poke():
    sig = comps.team_signals([{"role": "TOP", "champion": "Vladimir"}])
    assert sig["split"] == [] and sig["poke"] == []
    assert comps.team_plan(team("Vladimir", "Viego", "Orianna", "Kaisa", "Alistar"))[0] == "mixto"
    assert comps.team_plan(team("Fiora", "Viego", "Orianna", "Kaisa", "Alistar"))[0] == "split"      # control
    # an artillery mage in the ADC slot counts as poke, like Xerath or Lux do at mid
    assert comps.team_signals(team("Garen", "Viego", "Xerath", "Ziggs", "Alistar"))["poke"] == ["Xerath", "Ziggs"]
    assert comps.team_plan(team("Garen", "Viego", "Xerath", "Ziggs", "Alistar"))[0] == "poke"
    assert comps.team_signals(team("Garen", "Viego", "Orianna", "Jinx", "Lux"))["poke"] == ["Lux"]       # Lux support
    # a non-artillery mage (Viktor, Syndra) stays out of the poke count
    assert comps.team_signals(team("Garen", "Viego", "Orianna", "Viktor", "Alistar"))["poke"] == []
    # and a mage ADC is not a hypercarry or a follow-up
    sig = comps.team_signals(team("Garen", "Viego", "Orianna", "Syndra", "Lulu"))
    assert sig["hyper"] == [] and sig["followup"] == []


def team(top, jg, mid, adc, sup):
    return [{"role": r, "champion": c} for r, c in
            zip(("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"), (top, jg, mid, adc, sup))]


def test_type_representatives_for_mago_carry_no_plan_signal():
    assert comps.TYPE_REPRESENTATIVE["TOP"]["mago"] == "Vladimir"
    assert comps.TYPE_REPRESENTATIVE["BOTTOM"]["mago"] == "Syndra"
    for role, champion in (("TOP", "Vladimir"), ("BOTTOM", "Syndra")):
        sig = comps.team_signals([{"role": role, "champion": champion}])
        assert not any(sig.values()), (role, champion)


def test_the_draft_suggests_the_top_mago_type_without_a_boost():
    top = comps.recommend_draft({}, [], max_picks=4)["roles"]["TOP"]["picks"]
    mago = next(p for p in top if p["type"] == "mago")
    assert mago["type_label"] == "Mago" and mago["kind"] == "tipo" and mago["plan"] != "split"
    assert mago["reasons"] == ["Sin una regla clara ni datos suficientes: pick neutro."]
    assert len(comps.recommend_draft({}, [])["roles"]["TOP"]["picks"]) == comps.MAX_PICKS


EXTRA_TEAMS = ({}, {"TOP": (None, "Malphite"), "JUNGLE": (None, "Sejuani")},
               {"TOP": (None, "Malphite"), "JUNGLE": (None, "Sejuani"), "MIDDLE": (None, "Galio")},
               {"MIDDLE": (None, "Orianna"), "JUNGLE": (None, "Zac")})


@pytest.mark.parametrize("support", [None, "Leona", "Lulu", "Taric", "Thresh", "Lux", "Garen"])
@pytest.mark.parametrize("extra", range(len(EXTRA_TEAMS)))
def test_a_mago_never_ranks_first_on_rule_fit_alone_in_the_adc_ranking(support, extra):
    lineup = {**EXTRA_TEAMS[extra], **({"UTILITY": (None, support)} if support else {})}
    ranking = comps.recommend(lineup, [], include_all=True)               # nobody has games: rules only
    assert ranking[0]["adc_type"] != "mago"
    mages = [x["raw_score"] for x in ranking if x["adc_type"] == "mago"]
    flexible = [x["raw_score"] for x in ranking if x["adc_type"] == "flexible" and x["adc_source"] == "video"]
    assert mages and flexible and max(mages) <= min(flexible)           # equal evidence: video flexibles win


@pytest.mark.parametrize("extra", range(len(EXTRA_TEAMS)))
@pytest.mark.parametrize("support", [None, "Lulu", "Leona"])
@pytest.mark.parametrize("main_role", ["BOTTOM", "TOP"])
def test_a_mago_never_ranks_first_in_a_type_only_top_suggestion(extra, support, main_role):
    lineup = {**{k: v for k, v in EXTRA_TEAMS[extra].items() if k != "TOP"},
              **({"UTILITY": (None, support)} if support else {})}
    picks = comps.recommend_draft(lineup, [], max_picks=4, main_role=main_role)["roles"]["TOP"]["picks"]
    assert {p["kind"] for p in picks} == {"tipo"} and "mago" in {p["type"] for p in picks}
    assert picks[0]["type"] != "mago"
    by_type = {p["type"]: p["raw_score"] for p in picks}
    assert by_type["mago"] <= by_type["bruiser"]


def test_a_mage_adc_is_recommended_with_a_neutral_reason_and_a_label():
    ranking = {x["champion"]: x for x in comps.recommend({"UTILITY": (None, "Lulu")}, [], include_all=True)}
    ziggs, jinx = ranking["Ziggs"], ranking["Jinx"]
    assert ziggs["adc_type"] == "mago" and ziggs["adc_source"] == "oficial" and ziggs["in_pool"] is False
    assert any("mago" in r for r in ziggs["reasons"])
    assert jinx["score"] > ziggs["score"]                      # flexible still beats a mage by the rules
    unknown = {x["champion"]: x for x in comps.recommend({}, [], include_all=True)}["Ziggs"]
    assert 40 <= unknown["score"] <= 60                         # neutral without a known support
    assert any("no hay una regla clara" in r for r in unknown["reasons"])


def test_a_mage_adc_shows_in_the_matrix_and_the_per_line_table():
    script = (("BOTTOM", "Ziggs", 3, 4, {"UTILITY": (ALFA, "Lulu"), "JUNGLE": (BETA, "Viego"),
                                          "MIDDLE": (GAMMA, "Orianna"), "TOP": (None, "Vladimir")}),
              ("BOTTOM", "Jinx", 1, 3, {"UTILITY": (ALFA, "Leona"), "JUNGLE": (BETA, "Zac"),
                                        "MIDDLE": (GAMMA, "Orianna"), "TOP": (None, "Garen")}))
    records = comps.build_records(script_matches(script), ME_PUUID)
    a = comps.analyze_all(records)
    rows = {r["adc"]: r for r in a["matrix"]["rows"]}
    assert "mago" in rows and rows["mago"]["games"] == 4
    lines = {x["role"]: {t["key"]: t for t in x["by_type"]} for x in a["lines"]}
    assert lines["TOP"]["mago"]["games"] == 4
    md = comps.render_markdown(a, [], {}, "todas las colas", [])
    assert "Mago" in md


@pytest.fixture
def mage_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv("LOLAN_OPGG", "off")
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME_RIOT_ID}\n", encoding="utf-8")
    script = (("BOTTOM", "Ziggs", 3, 4, {"UTILITY": (ALFA, "Lulu"), "JUNGLE": (BETA, "Viego"),
                                          "MIDDLE": (GAMMA, "Orianna"), "TOP": (None, "Vladimir")}),)
    add_script_games(tmp_path, script)
    return web.create_app(tmp_path).test_client()


def test_the_pages_render_the_mago_type(mage_client):
    html = page_body(mage_client.get("/composiciones?cola=flex").get_data(as_text=True))
    assert "Mago" in html[html.index('id="por-linea"'):]                 # the per-line table of the top
    draft = page_body(mage_client.get("/composiciones?cola=flex&armar=1").get_data(as_text=True))
    assert "Mago · clase oficial de Riot" in draft            # Ziggs in the ADC ranking: label and source
