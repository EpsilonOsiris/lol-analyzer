"""Team compositions: classification, clan detection, aggregations and the ADC recommender."""
import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_comp_match, make_match, make_timeline, set_final_stats

from lolan import comps

HOUR = 3600_000
ME = (ME_PUUID, "Epsilon Me")
ALFA = ("p-alfa", "Epsilon Alfa")
BETA = ("p-beta", "Epsilon Beta")
ALTERNA = ("p-alterna", "Epsilon Alterna")      # second account of Beta
RANDOM_SUP = ("p-random", "Pepito")


def member(who, champion):
    return (who[0], who[1], champion)


class Builder:
    """Collects (match_id, match, timeline) tuples with increasing creation times."""

    def __init__(self):
        self.items = []

    def add(self, adc, support, sup_champion, win, jungle=None, timeline=None, **kw):
        n = len(self.items)
        blue = {"BOTTOM": (ME_PUUID, "Epsilon Me", adc), "UTILITY": member(support, sup_champion)}
        if jungle:
            blue["JUNGLE"] = member(*jungle)
        match = make_comp_match(f"LA1_{n}", blue=blue, win=win, created=BASE_TIME_MS + n * HOUR, **kw)
        self.items.append((f"LA1_{n}", match, timeline))
        return match

    def many(self, count, wins, *args, **kw):
        for i in range(count):
            self.add(*args, win=i < wins, **kw)


def clan(**kw):
    return comps.ClanConfig.build(kw.pop("prefix", "Epsilon"), kw.pop("aliases", ""), kw.pop("tracked", None))


def records_of(builder, clan_cfg=None, queue=None):
    recs = comps.build_records(builder.items, ME_PUUID, queue=queue)
    return comps.resolve_people(recs, clan_cfg or clan())


@pytest.fixture
def duo_records():
    b = Builder()
    b.many(4, 3, "Jinx", ALFA, "Taric")          # flexible + peel: 3-1
    b.many(3, 3, "Vayne", ALFA, "Taric")         # kiting + peel: 3-0
    b.many(3, 1, "Kaisa", ALFA, "Leona")         # follow-up + engage: 1-2
    b.many(3, 0, "Kaisa", RANDOM_SUP, "Janna")     # follow-up + peel, random support: 0-3
    return records_of(b)


# ---------------------------------------------------------------- classification
def test_adc_archetypes_and_sources():
    assert comps.adc_archetype("Kaisa") == "followup"
    assert comps.adc_archetype("Vayne") == "kiting"
    assert comps.adc_archetype("Jinx") == "flexible"
    assert comps.adc_archetype("Hwei") == "otro"
    assert comps.adc_source("Kaisa") == "video"
    assert comps.adc_source("Jhin") == "propia"
    assert comps.adc_source("Hwei") is None
    assert comps.adc_archetype("Xayah") == "kiting" and comps.adc_source("Xayah") == "propia"


def test_lookups_accept_typed_names():
    assert comps.adc_archetype("kai'sa") == "followup"
    assert comps.adc_archetype("Kog'Maw") == "flexible"
    assert comps.canonical_champion("Wukong") == "MonkeyKing"
    assert comps.canonical_champion("jarvan iv") == "JarvanIV"
    assert comps.canonical_champion("Zzz Desconocido") == "Zzz Desconocido"
    assert comps.canonical_champion("  ") == ""


def test_support_archetypes():
    assert comps.support_archetype("Leona") == "engage"
    assert comps.support_archetype("Taric") == "peel" and comps.support_source("Taric") == "video"
    assert comps.support_archetype("TahmKench") == "peel"
    assert comps.support_archetype("Lulu") == "peel" and comps.support_source("Lulu") == "propia"
    assert comps.support_archetype("Thresh") == "mixto"
    assert comps.support_archetype("Brand") == "poke"
    assert comps.support_archetype("Senna") == "poke"       # as a support; as ADC it is flexible
    assert comps.support_archetype("Garen") == "otro"


def test_engage_counting():
    assert comps.is_hard_engage("Malphite") and comps.is_hard_engage("Wukong")
    assert not comps.is_hard_engage("Jinx")
    # Pyke is an engage support but not a hard-engage champion elsewhere.
    assert comps.is_hard_engage("Pyke", "UTILITY") and not comps.is_hard_engage("Pyke", "MIDDLE")
    team = [{"champion": "Malphite", "role": "TOP"}, {"champion": "Pyke", "role": "UTILITY"},
            {"champion": "Ahri", "role": "MIDDLE"}]
    assert comps.engage_count(team) == 2
    assert [comps.engage_bucket(n) for n in (0, 1, 2, 3, 5)] == ["0", "1", "2", "3+", "3+"]


# ------------------------------------------------------------------ statistics
def test_shrunk_wr():
    assert comps.shrunk_wr(0, 0) == 0.5
    assert comps.shrunk_wr(3, 3) == pytest.approx(8 / 13)
    assert comps.shrunk_wr(20, 28) == pytest.approx(25 / 38)
    # A perfect small sample ranks below a very good large one.
    assert comps.shrunk_wr(2, 2) < comps.shrunk_wr(20, 28)


# --------------------------------------------------------------------- clan config
def test_parse_aliases():
    assert comps.parse_aliases("Epsilon Alterna=Epsilon Beta; Otro = Principal") == {
        "epsilon alterna": "Epsilon Beta", "otro": "Principal"}
    assert comps.parse_aliases("") == {}
    assert comps.parse_aliases("mal;A=B") == {"a": "B"}            # lenient by default
    with pytest.raises(ValueError):
        comps.parse_aliases("mal;A=B", strict=True)
    with pytest.raises(ValueError):
        comps.parse_aliases("A=", strict=True)


def test_clan_config_defaults_and_alias_chain():
    cfg = comps.ClanConfig.build("", "")
    assert cfg.prefix == "Epsilon"
    assert cfg.matches_prefix("epsilon Zeta") and not cfg.matches_prefix("Otro Epsilon")
    cfg = comps.ClanConfig.build("Club", "Club B=Club A;Club A=Club Z")
    assert cfg.person("Club B") == "Club Z"
    loop = comps.ClanConfig.build("Club", "A=B;B=A")
    assert loop.person("A") in ("A", "B")        # a loop terminates


def test_clan_detection_prefix_aliases_and_tracked():
    b = Builder()
    b.many(2, 1, "Jinx", ALFA, "Taric", jungle=(ALTERNA, "Sejuani"))
    b.many(2, 1, "Jinx", RANDOM_SUP, "Janna", jungle=(BETA, "Sejuani"))
    recs = records_of(b, clan(aliases="Epsilon Alterna=Epsilon Beta"))
    people = {m["puuid"]: m["person"] for r in recs for m in r["team"]}
    assert people[ME_PUUID] == "Epsilon Me"
    assert people[ALFA[0]] == "Epsilon Alfa"
    assert people[ALTERNA[0]] == "Epsilon Beta" and people[BETA[0]] == "Epsilon Beta"
    assert people[RANDOM_SUP[0]] is None
    assert people["puuid-1"] is None                       # nameless teammate

    # Without the alias they are two people; prefix is case-insensitive; a tracked puuid counts
    # even if its name does not match the prefix.
    recs = records_of(b, clan(prefix="EPSILON"))
    names = {m["person"] for r in recs for m in r["team"]}
    assert {"Epsilon Alterna", "Epsilon Beta"} <= names
    recs = records_of(b, clan(tracked={RANDOM_SUP[0]: "Pepito#LAN"}))
    assert {m["puuid"]: m["person"] for r in recs for m in r["team"]}[RANDOM_SUP[0]] == "Pepito"


def test_person_follows_latest_name_of_a_puuid():
    b = Builder()
    old = ("p-old", "Epsilon Viejo")
    new = ("p-old", "Epsilon Nuevo")
    b.add("Jinx", old, "Taric", True)
    b.add("Jinx", new, "Taric", True)
    recs = records_of(b)
    assert {m["person"] for r in recs for m in r["team"] if m["puuid"] == "p-old"} == {"Epsilon Nuevo"}


def test_build_records_skips_remakes_and_filters_queue():
    b = Builder()
    b.add("Jinx", ALFA, "Taric", True, queue=440)
    b.add("Jinx", ALFA, "Taric", True, queue=420)
    b.add("Jinx", ALFA, "Taric", True, queue=440, duration=200)       # remake
    b.add("Jinx", ALFA, "Taric", True, queue=1700)                    # not Summoner's Rift
    assert len(records_of(b)) == 2
    assert [r["queue_id"] for r in records_of(b, queue=420)] == [420]
    r = records_of(b)[0]
    assert r["main_champion"] == "Jinx" and r["main_role"] == "BOTTOM" and len(r["team"]) == 5
    assert sum(m["is_main"] for m in r["team"]) == 1
    assert comps.main_person([r]) == "Epsilon Me"


# ------------------------------------------------------------------- aggregations
def test_duo_bot_aggregation(duo_records):
    duo = comps.duo_bot(duo_records)
    assert duo["games"] == 13 and duo["no_support"] == 0
    by_person = {r["key"]: r for r in duo["by_person"]}
    assert by_person["Epsilon Alfa"]["games"] == 10 and by_person["Epsilon Alfa"]["wins"] == 7
    assert by_person[comps.RANDOM]["games"] == 3 and by_person[comps.RANDOM]["wins"] == 0
    assert by_person["Epsilon Alfa"]["shrunk"] == pytest.approx(12 / 20)
    assert [r["key"] for r in duo["by_person"]][0] == "Epsilon Alfa"       # ranked by shrunk
    by_champ = {r["key"]: r for r in duo["by_champion"]}
    assert by_champ["Taric"]["games"] == 7 and by_champ["Taric"]["archetype"] == "peel"
    assert by_champ["Taric"]["source"] == "video" and by_champ["Leona"]["source"] == "propia"
    by_arch = {r["key"]: r for r in duo["by_archetype"]}
    assert by_arch["peel"]["games"] == 10 and by_arch["peel"]["wins"] == 6
    assert by_arch["engage"]["games"] == 3
    # The main's own metrics come from the metrics pipeline (make_match: deaths=2, no lane gold diff).
    assert by_arch["peel"]["deaths"] == 2


def test_small_samples_flagged_and_ranked_last():
    b = Builder()
    b.many(2, 2, "Jinx", BETA, "Braum")           # 2-0: small
    b.many(10, 6, "Jinx", ALFA, "Taric")
    rows = comps.duo_bot(records_of(b), min_games=3)["by_person"]
    assert rows[0]["key"] == "Epsilon Alfa" and not rows[0]["small"]
    assert rows[-1]["key"] == "Epsilon Beta" and rows[-1]["small"]


def test_duo_bot_ignores_games_not_as_adc():
    b = Builder()
    b.add("Jinx", ALFA, "Taric", True)
    b.add("Ahri", ALFA, "Taric", True, main_role="MIDDLE")
    assert comps.duo_bot(records_of(b))["games"] == 1


def test_matrix_aggregation(duo_records):
    m = comps.adc_support_matrix(duo_records)
    rows = {r["adc"]: dict(zip(m["support_types"], r["cells"])) for r in m["rows"]}
    assert rows["flexible"]["peel"]["games"] == 4 and rows["flexible"]["peel"]["wins"] == 3
    assert rows["kiting"]["peel"]["games"] == 3
    assert rows["followup"]["engage"]["wins"] == 1 and rows["followup"]["engage"]["games"] == 3
    assert rows["followup"]["peel"]["games"] == 3 and rows["followup"]["peel"]["wins"] == 0
    assert rows["flexible"]["engage"] is None
    assert m["games"] == 13


def test_adc_engage_table():
    b = Builder()
    b.many(2, 2, "Kaisa", ALFA, "Leona")                           # Leona: 1 engage
    b.many(2, 0, "Kaisa", ALFA, "Leona", jungle=(BETA, "Sejuani"))  # Leona + Sejuani: 2
    b.many(3, 3, "Vayne", ALFA, "Taric", jungle=(BETA, "Sejuani"))  # Sejuani: 1
    b.many(1, 0, "Vayne", ALFA, "Taric")                           # 0 engage
    t = comps.adc_engage_table(records_of(b))
    cells = {r["adc"]: dict(zip(t["buckets"], r["cells"])) for r in t["rows"]}
    assert cells["followup"]["1"]["games"] == 2 and cells["followup"]["1"]["wins"] == 2
    assert cells["followup"]["2"]["games"] == 2 and cells["followup"]["2"]["wins"] == 0
    assert cells["kiting"]["1"]["games"] == 3 and cells["kiting"]["0"]["games"] == 1
    assert cells["kiting"]["3+"] is None


def test_clan_pools():
    b = Builder()
    b.many(4, 3, "Jinx", ALFA, "Taric")
    b.many(2, 0, "Jinx", ALFA, "Lulu")
    pools = {p["person"]: p for p in comps.clan_pools(records_of(b))}
    alfa = pools["Epsilon Alfa"]
    assert alfa["games"] == 6 and alfa["wins"] == 3 and not alfa["is_main"]
    assert len(alfa["roles"]) == 1 and alfa["roles"][0]["role"] == "UTILITY"
    champs = alfa["roles"][0]["champions"]
    assert [(c["champion"], c["games"], c["wins"]) for c in champs] == [("Taric", 4, 3), ("Lulu", 2, 0)]
    assert pools["Epsilon Me"]["is_main"] and pools["Epsilon Me"]["roles"][0]["role"] == "BOTTOM"


def test_lineups_and_pairs():
    b = Builder()
    b.many(5, 4, "Jinx", ALFA, "Taric", jungle=(BETA, "Sejuani"))
    b.many(3, 0, "Jinx", ALFA, "Lulu", jungle=(BETA, "Sejuani"))
    b.many(2, 2, "Jinx", RANDOM_SUP, "Janna", jungle=(BETA, "Sejuani"))     # only 2 clan members
    recs = records_of(b)
    lineups = comps.clan_lineups(recs)
    assert len(lineups) == 1
    top = lineups[0]
    assert top["games"] == 8 and top["wins"] == 4
    assert top["lineup"] == [("Epsilon Beta", "JUNGLE"), ("Epsilon Me", "BOTTOM"), ("Epsilon Alfa", "UTILITY")]
    assert comps.most_common_lineup(recs) == {"JUNGLE": "Epsilon Beta", "BOTTOM": "Epsilon Me",
                                              "UTILITY": "Epsilon Alfa"}
    pairs = comps.clan_pairs(recs, top=2)
    keyed = {(x["a"], x["b"]): x for x in pairs["best"] + pairs["worst"]}
    taric_pair = keyed[(("Epsilon Alfa", "Taric"), ("Epsilon Me", "Jinx"))]
    assert taric_pair["games"] == 5 and taric_pair["wins"] == 4
    # Best first, worst last; pairs below the minimum are absent (Janna's side has no clan member).
    assert pairs["best"][0]["shrunk"] >= pairs["best"][-1]["shrunk"]
    assert all(x["games"] >= 3 for x in pairs["best"] + pairs["worst"])
    assert pairs["worst"] and pairs["worst"][0]["shrunk"] <= pairs["best"][0]["shrunk"]
    assert not {id(x) for x in pairs["best"]} & {id(x) for x in pairs["worst"]}


def test_most_common_lineup_empty_without_clan():
    b = Builder()
    b.many(3, 1, "Jinx", RANDOM_SUP, "Janna")
    assert comps.most_common_lineup(records_of(b)) == {}


def test_armor_pen_2x2():
    b = Builder()

    def game(tanky, pen, win, duration=1800, with_timeline=True):
        tl = make_timeline(minutes=25)
        set_final_stats(tl, 6, armor=250 if tanky else 120)
        for pid in (7, 8, 9, 10):
            set_final_stats(tl, pid, armor=90)
        set_final_stats(tl, 4, armorPenPercent=30 if pen else 0, bonusArmorPenPercent=0, armorPen=5)
        b.add("Jinx", ALFA, "Taric", win, timeline=tl if with_timeline else None, duration=duration)

    game(True, True, True)
    game(True, True, True)
    game(True, False, False)
    game(True, False, False)
    game(False, True, True)
    game(False, False, False)
    game(True, True, False, duration=1100)            # shorter than 20 minutes: excluded
    game(True, True, False, with_timeline=False)      # no timeline: excluded
    t = comps.penetration_table(records_of(b))
    assert t["eligible"] == 6 and t["excluded"] == 2 and t["tanky_games"] == 4
    tanky, normal = t["rows"]
    assert tanky["tanky"] and not normal["tanky"]
    assert (tanky["with_pen"]["games"], tanky["with_pen"]["wins"]) == (2, 2)
    assert (tanky["without_pen"]["games"], tanky["without_pen"]["wins"]) == (2, 0)
    assert (normal["with_pen"]["games"], normal["with_pen"]["wins"]) == (1, 1)
    assert (normal["without_pen"]["games"], normal["without_pen"]["wins"]) == (1, 0)
    assert t["tanky_only_lethality"] == 2           # armorPen=5 but no % pen against a tank


def test_armor_pen_counts_bonus_percent_too():
    b = Builder()
    tl = make_timeline(minutes=25)
    set_final_stats(tl, 6, armor=300)
    set_final_stats(tl, 4, armorPenPercent=0, bonusArmorPenPercent=20, armorPen=0)
    b.add("Jinx", ALFA, "Taric", True, timeline=tl)
    t = comps.penetration_table(records_of(b))
    assert t["rows"][0]["with_pen"]["games"] == 1


def test_cleanse_terciles():
    b = Builder()
    # 9 games with enemy CC 10..90; Cleanse (summoner id 1) taken in some of them.
    for i, cc in enumerate((10, 20, 30, 40, 50, 60, 70, 80, 90)):
        cleanse = i in (0, 3, 6, 7)
        b.add("Jinx", ALFA, "Taric", win=cleanse, enemy_cc=cc + 0,
              spells=(1, 4) if cleanse else (4, 14))
    cl = comps.cleanse_terciles(records_of(b))
    assert cl["games"] == 9 and cl["cleanse_games"] == 4
    assert [bk["name"] for bk in cl["buckets"]] == ["CC bajo", "CC medio", "CC alto"]
    assert [bk["games"] for bk in cl["buckets"]] == [3, 3, 3]
    low, mid, high = cl["buckets"]
    # enemy_cc is TOP's timeCCingOthers plus 4 * 10 from the others.
    assert low["min"] == 50 and low["max"] == 70 and high["max"] == 130
    assert (low["with_cleanse"]["games"], low["with_cleanse"]["wins"]) == (1, 1)
    assert (low["without_cleanse"]["games"], low["without_cleanse"]["wins"]) == (2, 0)
    assert (high["with_cleanse"]["games"], high["without_cleanse"]["games"]) == (2, 1)
    assert comps.cleanse_terciles(records_of(Builder()))["buckets"] == []


def test_analyze_all_smoke(duo_records):
    a = comps.analyze_all(duo_records)
    assert a["games"] == 13 and a["bottom_games"] == 13 and a["clan_games"] == 10
    for key in ("pools", "duo", "matrix", "engage", "lineups", "pairs", "penetration", "cleanse"):
        assert key in a
    text = comps.render_markdown(a, [], {}, "todas las colas", [])
    assert "Dúo bot" in text and "Itemización" in text
    assert "Dúo bot" in comps.render_summary(a, [], {}, [])


# ------------------------------------------------------------------ recommender
def names(ranking):
    return [x["champion"] for x in ranking]


def test_recommend_uses_support_history_of_the_person(duo_records):
    lineup = {"UTILITY": ("Epsilon Alfa", None)}
    ranking = comps.recommend(lineup, duo_records)
    # Alfa mostly plays Taric (peel): kiting > flexible > follow-up.
    assert names(ranking) == ["Vayne", "Jinx", "Kaisa"]
    assert all(1 <= len(x["reasons"]) <= 3 for x in ranking)
    vayne = ranking[0]
    assert any("Epsilon Alfa suele jugar Taric" in r and "peel" in r for r in vayne["reasons"])
    assert any("Con Vayne y Epsilon Alfa de soporte: 3-0 (100%)" in r for r in vayne["reasons"])
    assert 0 <= ranking[-1]["score"] <= vayne["score"] <= 100


def test_recommend_with_known_support_champion(duo_records):
    lineup = {"UTILITY": ("Epsilon Alfa", "Leona")}
    ranking = comps.recommend(lineup, duo_records)
    assert names(ranking)[0] == "Jinx" and names(ranking)[-1] == "Vayne"
    text = " ".join(" ".join(x["reasons"]) for x in ranking)
    assert "Leona (engage)" in text
    # A hand-typed name is normalized.
    again = comps.recommend({"UTILITY": ("Epsilon Alfa", "leona")}, duo_records)
    assert names(again) == names(ranking)


def test_recommend_unknown_support_favors_flexible_and_warns_on_followups(duo_records):
    ranking = comps.recommend({}, duo_records)
    assert names(ranking)[0] == "Jinx" and names(ranking)[-1] == "Kaisa"
    kaisa = ranking[-1]
    assert any("mal first pick" in r for r in kaisa["reasons"])
    notes = comps.draft_notes({}, duo_records)
    assert any("Soporte desconocido" in n for n in notes)


def test_recommend_random_support_is_unknown(duo_records):
    assert names(comps.recommend({"UTILITY": (comps.RANDOM, None)}, duo_records)) == \
        names(comps.recommend({}, duo_records))


def test_recommend_team_engage_helps_followups_and_hurts_kiting(duo_records):
    base = {"UTILITY": (None, "Taric")}
    heavy = {**base, "JUNGLE": (None, "Sejuani"), "TOP": (None, "Malphite"), "MIDDLE": (None, "Vi")}
    by = {x["champion"]: x["raw_score"] for x in comps.recommend(base, duo_records)}
    heavy_by = {x["champion"]: x["raw_score"] for x in comps.recommend(heavy, duo_records)}
    assert heavy_by["Kaisa"] > by["Kaisa"]
    assert heavy_by["Vayne"] < by["Vayne"]
    assert comps.draft_notes(heavy, duo_records)[-1].startswith("Engage duro en tu equipo")
    reasons = " ".join(" ".join(x["reasons"]) for x in comps.recommend(heavy, duo_records))
    assert "engage duro" in reasons


def test_recommend_pool_and_include_all(duo_records):
    assert set(names(comps.recommend({}, duo_records))) == {"Jinx", "Vayne", "Kaisa"}
    ranking = comps.recommend({}, duo_records, include_all=True)
    assert {"Jinx", "Vayne", "Kaisa", "Ashe", "Zeri", "Draven"} <= set(names(ranking))
    ashe = next(x for x in ranking if x["champion"] == "Ashe")
    assert not ashe["in_pool"] and ashe["games"] == 0
    assert any("No lo has jugado" in r for r in ashe["reasons"])
    # Ashe (flexible, no data) beats the follow-up Kaisa who loses games when the support is unknown.
    assert names(ranking).index("Ashe") < names(ranking).index("Kaisa")


def test_recommend_rules_dominate_only_when_data_is_thin():
    lineup = {"UTILITY": (None, "Taric")}
    thin = Builder()
    thin.many(3, 0, "Vayne", ALFA, "Taric")        # kiting + peel, but 0-3
    thin.many(3, 3, "Jinx", ALFA, "Taric")
    # With 3 games the framework (kiting loves peel) still puts Vayne first...
    assert names(comps.recommend(lineup, records_of(thin))) == ["Vayne", "Jinx"]

    deep = Builder()
    deep.many(30, 3, "Vayne", ALFA, "Taric")       # ...but 3-27 is plenty of evidence
    deep.many(3, 3, "Jinx", ALFA, "Taric")
    assert names(comps.recommend(lineup, records_of(deep))) == ["Jinx", "Vayne"]


def test_recommend_without_enough_games_returns_nothing_unless_include_all():
    b = Builder()
    b.many(2, 2, "Jinx", ALFA, "Taric")
    recs = records_of(b)
    assert comps.recommend({}, recs) == []
    assert comps.recommend({}, recs, include_all=True)


def test_normalize_lineup_accepts_dicts_and_blanks():
    lu = comps.normalize_lineup({"UTILITY": {"person": "Epsilon Alfa", "champion": "taric"},
                                 "TOP": ("", None), "JUNGLE": (comps.RANDOM, "")})
    assert lu["UTILITY"] == {"person": "Epsilon Alfa", "champion": "Taric"}
    assert lu["TOP"] == {"person": None, "champion": None}
    assert lu["JUNGLE"]["person"] is None and lu["MIDDLE"]["champion"] is None


def test_make_match_builders_still_work_without_clan_fields():
    """Plain make_match games (no riotIdGameName) produce records with no clan members."""
    recs = comps.resolve_people(
        comps.build_records([("LA1_1", make_match("LA1_1", queue=440), make_timeline())], ME_PUUID),
        clan())
    assert len(recs) == 1 and not any(m["person"] for m in recs[0]["team"][:3])
