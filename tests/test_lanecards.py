"""Lane cards: Diamond+-first samples with a labelled fallback, spike texts, best-into lists, the cards of a draft."""
import pytest
from lanecards_fixtures import lane_rows, write_lines

from lolan import lanecards as lc
from lolan.samples import LineStore

AHRI, ZED, GAREN, RENEKTON, JINX, KAISA, LEE = 103, 238, 86, 58, 222, 145, 64
NAMES = {AHRI: "Ahri", ZED: "Zed", GAREN: "Garen", RENEKTON: "Renekton", JINX: "Jinx", KAISA: "Kai'Sa", LEE: "Lee Sin"}


def name_of(cid):
    return NAMES.get(cid, f"Campeón {cid}")


@pytest.fixture
def base(tmp_path):
    return tmp_path / "lineas"


def book_of(base, *groups, **kw):
    write_lines(base, *groups, **kw)
    return lc.LaneBook(base, recheck_s=0)


# --------------------------------------------------------------------- samples
def test_without_a_diamond_sample_it_says_so_and_falls_back_to_every_source(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 7, person="Epsilon Seth"))
    assert not book.has_diamond() and book.any_data()
    view = book.matchup("MIDDLE", AHRI, ZED)
    assert view["kind"] == "fallback" and not view["diamond"] and view["label"] == "muestra propia (clan)"
    block = lc.sample_block(view)
    assert block["note"] == lc.NO_DIAMOND_NOTE and block["games"] == 10 and block["label"] == "muestra propia (clan)"
    assert 0.5 < block["wr"] < 0.7 and block["wr_text"].endswith("%") and block["raw_text"] == "70%"
    assert block["diff14_text"] == "+240 oro+exp al 14" and block["tone"] == "fav" and not block["thin"]


def test_the_diamond_sample_wins_over_our_own_when_it_has_games_of_the_pair(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 9, person="Epsilon Seth"),             # ours: 90%
                   lane_rows(AHRI, ZED, 20, 6, source="diamante"))                      # Diamond+: 30%
    view = book.matchup("MIDDLE", AHRI, ZED)
    assert view["kind"] == "diamante" and view["label"] == "Diamante+" and view["stats"]["games"] == 20
    block = lc.sample_block(view)
    assert block["note"] is None and block["raw_text"] == "30%" and block["tone"] == "unfav" and block["games"] == 20


def test_a_pair_missing_from_the_diamond_sample_says_so_and_uses_every_source(base):
    book = book_of(base, lane_rows(GAREN, RENEKTON, 6, 3, role="TOP"), lane_rows(AHRI, ZED, 20, 6, source="diamante"))
    view = book.matchup("TOP", GAREN, RENEKTON)
    assert view["kind"] == "fallback" and view["diamond"]
    assert lc.sample_block(view)["note"] == lc.NO_DIAMOND_PAIR_NOTE


def test_a_pair_nobody_played_has_no_numbers_but_still_a_note(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 7))
    block = lc.sample_block(book.matchup("MIDDLE", AHRI, LEE))
    assert block["games"] == 0 and block["wr"] is None and "sin partidas de este cruce" in block["note"]


def test_answers_are_remembered_until_the_files_change(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 7))
    first = book.matchup("MIDDLE", AHRI, ZED)
    assert book.matchup("MIDDLE", AHRI, ZED) is first                      # same object: remembered
    LineStore(base).append_rows(lane_rows(AHRI, ZED, 5, 5, source="diamante", prefix="N"))
    assert book.has_diamond() and book.matchup("MIDDLE", AHRI, ZED)["kind"] == "diamante"


def test_the_files_are_looked_at_only_once_in_a_while(base):
    now = [100.0]
    write_lines(base, lane_rows(AHRI, ZED, 10, 7))
    book = lc.LaneBook(base, recheck_s=5, clock=lambda: now[0])
    assert not book.has_diamond()
    LineStore(base).append_rows(lane_rows(AHRI, ZED, 5, 5, source="diamante", prefix="N"))
    assert not book.has_diamond()                                          # not looked at yet
    now[0] += 6
    assert book.has_diamond()


def test_an_empty_folder_is_not_an_error(tmp_path):
    book = lc.LaneBook(tmp_path / "no-hay")
    assert not book.has_diamond() and not book.any_data()
    assert lc.sample_block(book.matchup("MIDDLE", AHRI, ZED))["games"] == 0
    assert book.best_into("MIDDLE", ZED, [AHRI])["items"][0]["games"] == 0


def test_a_persons_own_record_in_the_matchup(base):
    book = book_of(base, lane_rows(AHRI, ZED, 3, 2, person="Epsilon Seth"), lane_rows(AHRI, ZED, 4, 1, person="Epsilon Ra", prefix="R"),
                   lane_rows(AHRI, ZED, 20, 6, source="diamante"))
    rec = book.person_record("MIDDLE", AHRI, ZED, "epsilon seth")
    assert rec == {"games": 3, "wins": 2}
    assert lc.person_block(rec, "Tú")["text"] == "2 de 3 líneas ganadas"
    assert lc.person_block({"games": 1, "wins": 1}, "Tú")["text"] == "1 de 1 línea ganada"
    assert book.person_record("MIDDLE", AHRI, ZED, "Nadie") is None and book.person_record("MIDDLE", AHRI, ZED, None) is None


# ---------------------------------------------------------------------- spikes
def test_a_champions_spike_profile_names_levels_first_item_and_game_length(base):
    book = book_of(base, lane_rows(AHRI, ZED, 12, 8, role="MIDDLE"))
    stats = book.overall("MIDDLE", AHRI)["stats"]
    assert lc.strong_levels(stats) == [2, 6]
    assert lc.first_item(stats)["minute"] == 13
    prof = lc.spike_profile(stats)
    assert prof["text"].startswith("fuerte en nivel 2 y 6 · 1er item min 13")
    assert prof["length"]["bucket"] == "<25" and prof["text"].endswith("mejor en <25 min")


def test_no_clear_spikes_or_no_data_say_so(base):
    flat = book_of(base, lane_rows(AHRI, ZED, 12, 6, spikes=False, minutes=(30.0, 30.0)))
    assert lc.spike_profile(flat.overall("MIDDLE", AHRI)["stats"])["text"] == "sin picos claros"
    assert lc.spike_profile(flat.overall("MIDDLE", LEE)["stats"])["text"] == "sin datos suficientes"


def test_late_game_scaling_is_worded_as_scaling(base):
    rows = lane_rows(AHRI, ZED, 12, 8, minutes=(40.0, 22.0))
    prof = lc.spike_profile(book_of(base, rows).overall("MIDDLE", AHRI)["stats"])
    assert prof["length"]["text"] == "escala (más de 35 min)"


def test_the_duels_own_brackets_are_described_only_with_enough_games(base):
    book = book_of(base, lane_rows(AHRI, ZED, 12, 6), lane_rows(GAREN, RENEKTON, 4, 2, role="TOP"))
    text = lc.duel_text(book.matchup("MIDDLE", AHRI, ZED)["stats"])
    assert text == "En este duelo ganas en niveles 1-2 y pierdes en 6-8"
    assert lc.duel_text(book.matchup("TOP", GAREN, RENEKTON)["stats"]) is None          # 4 games: too few
    assert lc.duel_text({"games": 50, "brackets": [{"bracket": "6-8", "gain_per_min": -30, "minutes": 9}]}) == \
        "En este duelo pierdes en niveles 6-8"


def test_champions_with_a_diamond_aggregate_use_it_otherwise_every_source(base):
    book = book_of(base, lane_rows(AHRI, ZED, 12, 8, person="Epsilon Seth"),
                   lane_rows(AHRI, LEE, 20, 10, source="diamante"), lane_rows(ZED, AHRI, 4, 2, source="diamante"))
    assert book.overall("MIDDLE", AHRI)["kind"] == "diamante"        # 20 Diamond+ games >= OVERALL_MIN_GAMES
    assert book.overall("MIDDLE", ZED)["kind"] == "fallback"         # only 4: back to every source


def test_opgg_game_length_text():
    lengths = [{"from": 0, "rate": 0.5}, {"from": 25, "rate": 0.52}, {"from": 30, "rate": 0.514},
               {"from": 35, "rate": 0.5}, {"from": 40, "rate": 0.487}]
    assert lc.opgg_length_text(lengths) == "mejor en 25-30 min (52%) · peor en más de 40 min (49%)"
    assert lc.opgg_length_text([{"from": 0, "rate": 0.5}, {"from": 25, "rate": 0.505}]) == "parejo en todas las duraciones"
    assert lc.opgg_length_text(None) is None and lc.opgg_length_text([{"from": 0, "rate": 0.5}]) is None


# ------------------------------------------------------------------- best into
def test_best_into_ranks_the_pool_and_leaves_out_picked_and_banned_champions(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 8, person="Epsilon Seth"), lane_rows(LEE, ZED, 10, 3),
                   lane_rows(JINX, ZED, 2, 0))
    pool = [(AHRI, 6), (LEE, 3), (JINX, 2), (999, 5)]
    block = lc.best_block(book, "MIDDLE", ZED, pool, taken={JINX}, name_of=name_of)
    assert [i["champion_id"] for i in block["items"]] == [AHRI, 999, LEE]            # 999 never seen: 50% outranks Lee Sin
    top = block["items"][0]
    assert top["name"] == "Ahri" and top["games"] == 10 and top["own_games"] == 6 and top["wr_text"] == "72%"
    assert block["note"] == lc.NO_DIAMOND_NOTE and block["label"] == "muestra propia (clan)"
    assert block["items"][1]["games"] == 0 and block["items"][1]["tone"] == "few"
    assert lc.best_block(book, "MIDDLE", ZED, [(AHRI, 6)], taken={AHRI}, name_of=name_of) is None
    assert lc.best_block(book, "MIDDLE", ZED, [], taken=set(), name_of=name_of) is None


def test_best_into_prefers_the_diamond_sample(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 2, person="Epsilon Seth"), lane_rows(LEE, ZED, 10, 8),
                   lane_rows(AHRI, ZED, 30, 24, source="diamante"), lane_rows(LEE, ZED, 30, 6, source="diamante"))
    block = lc.best_block(book, "MIDDLE", ZED, [(AHRI, 4), (LEE, 4)], set(), name_of)
    assert block["kind"] == "diamante" and block["note"] is None
    assert [i["champion_id"] for i in block["items"]] == [AHRI, LEE]


# ---------------------------------------------------------------------- OP.GG
def test_opgg_block_speaks_in_spanish_about_the_ally_and_the_rival():
    found = {"status": "ok", "games": 3000, "wins": 1530, "lane_winner": "theirs", "solo_winner": "mine",
             "style": "aggressive", "tip": "Do not use Charm until Zed closes in.",
             "lengths": [{"from": 0, "rate": 0.5}, {"from": 25, "rate": 0.52}]}
    block = lc.opgg_block(found, "Ahri", "Zed")
    assert block["status"] == "ok" and block["wr_text"] == "51%" and block["tone"] == "even"
    assert block["lane"] == "Ventaja en la línea: Zed" and block["solo"] == "Ventaja de solo kill: Ahri"
    assert block["tip"].startswith("Do not use Charm") and block["style"] == "agresivo"
    even = lc.opgg_block({**found, "lane_winner": "even", "solo_winner": None, "games": 150, "wins": 90}, "Ahri", "Zed")
    assert even["lane"] == "La línea va pareja" and even["solo"] is None and even["wr_text"] is None and even["tone"] == "few"
    assert lc.opgg_block({"status": "off"}, "A", "B") is None
    assert lc.opgg_block({"status": "pending"}, "A", "B") == {"status": "pending"}
    assert lc.opgg_block({"status": "none"}, "A", "B") == {"status": "none"}


# ----------------------------------------------------------------------- cards
def ally(cell, role, champ=0, hover=0, person=None, me=False, name=None):
    return {"cell_id": cell, "role": role, "champion_id": champ, "hover_id": hover, "person": person, "is_me": me,
            "name": name or person or "Aleatorio"}


def enemy(champ, role, conf=0.95, low=False):
    return {"champion_id": champ, "role": role, "role_conf": conf, "role_low": low}


def lookup_of(table, calls=None):
    def lookup(role, mine, theirs, fetch):
        if calls is not None:
            calls.append((role, mine, theirs, fetch))
        return table.get((role, mine, theirs), {"status": "idle"})
    return lookup


OPGG_AHRI = {"status": "ok", "games": 3000, "wins": 1530, "lane_winner": "theirs", "solo_winner": "theirs",
             "style": "even", "tip": "tip", "lengths": []}


def test_my_lane_comes_first_and_the_others_are_compact_pairs(base):
    book = book_of(base, lane_rows(AHRI, ZED, 12, 6, person="Epsilon Seth"), lane_rows(GAREN, RENEKTON, 6, 4, role="TOP"),
                   lane_rows(AHRI, ZED, 30, 12, source="diamante"))
    allies = [ally(0, "TOP", GAREN, person="Epsilon Alfa"), ally(1, "MIDDLE", AHRI, person="Epsilon Seth", me=True),
              ally(2, "BOTTOM", 0, JINX)]
    enemies = [enemy(RENEKTON, "TOP"), enemy(ZED, "MIDDLE"), enemy(KAISA, "BOTTOM", 0.6, True)]
    calls = []
    out = lc.build_lanes(book, allies, enemies, "MIDDLE", name_of, lookup_of({("MIDDLE", AHRI, ZED): OPGG_AHRI}, calls),
                         taken=(GAREN, RENEKTON, AHRI, ZED, KAISA))
    assert [c["role"] for c in out["cards"]] == ["MIDDLE", "TOP", "BOTTOM"] and out["diamond"] and out["data"]
    mine = out["cards"][0]
    assert mine["mine"] and mine["state"] == "pair" and mine["ally"]["who"] == "Tú" and not mine["ally"]["hover"]
    assert mine["sample"]["kind"] == "diamante" and mine["sample"]["games"] == 30
    assert mine["spikes"]["ally"]["label"] == "Tú" and mine["spikes"]["enemy"]["label"] == "Rival"
    assert "nivel 2 y 6" in mine["spikes"]["ally"]["text"] and mine["spikes"]["duel"].startswith("En este duelo")
    assert mine["person"]["text"] == "6 de 12 líneas ganadas"
    assert mine["opgg"]["lane"] == "Ventaja en la línea: Zed" and "OP.GG: Ventaja en la línea: Zed" in mine["summary"]
    top = out["cards"][1]
    assert top["spikes"]["ally"]["label"] == "Garen" and top["spikes"]["enemy"]["label"] == "Renekton"
    assert top["sample"]["kind"] == "fallback" and top["sample"]["note"] == lc.NO_DIAMOND_PAIR_NOTE
    assert top["opgg"] == {"status": "idle"}
    bottom = out["cards"][2]
    assert bottom["ally"]["hover"] and bottom["enemy"]["role_low"]                 # a hovered pick, a doubtful enemy line
    # OP.GG is only asked for locked picks: the hovered ADC passes fetch=False
    assert ("BOTTOM", JINX, KAISA, False) in calls and ("MIDDLE", AHRI, ZED, True) in calls


def test_before_i_pick_my_lane_lists_the_best_of_my_pool_against_the_revealed_rival(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 8), lane_rows(LEE, ZED, 10, 3))
    allies = [ally(0, "MIDDLE", 0, 0, person="Epsilon Seth", me=True), ally(1, "TOP", 0, 0, person="Epsilon Alfa"),
              ally(2, "JUNGLE", 0, 0)]
    pools = {("Epsilon Seth", "MIDDLE"): [(AHRI, 6), (LEE, 2)], ("Epsilon Alfa", "TOP"): [(GAREN, 5)]}
    out = lc.build_lanes(book, allies, [enemy(ZED, "MIDDLE"), enemy(RENEKTON, "TOP"), enemy(LEE, "JUNGLE")], "MIDDLE",
                         name_of, pool_of=lambda p, r: pools.get((p, r), []))
    cards = {c["role"]: c for c in out["cards"]}
    assert [c["role"] for c in out["cards"]] == ["MIDDLE", "TOP"]                    # the random jungler has no pool: no card
    mid = cards["MIDDLE"]
    assert mid["state"] == "picking" and mid["summary"] == "Mejores contra Zed"
    assert [i["name"] for i in mid["best"]["items"]] == ["Ahri", "Lee Sin"]
    assert cards["TOP"]["best"]["items"][0]["name"] == "Garen"                       # a clan ally who has not picked


def test_hovering_a_champion_keeps_the_pair_card_and_the_best_list(base):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 8), lane_rows(LEE, ZED, 10, 3))
    allies = [ally(0, "MIDDLE", 0, AHRI, person="Epsilon Seth", me=True)]
    out = lc.build_lanes(book, allies, [enemy(ZED, "MIDDLE")], "MIDDLE", name_of,
                         pool_of=lambda p, r: [(AHRI, 6), (LEE, 2)])
    card = out["cards"][0]
    assert card["state"] == "pair" and card["ally"]["hover"] and card["best"]["items"][0]["name"] == "Ahri"
    locked = lc.build_lanes(book, [ally(0, "MIDDLE", AHRI, 0, person="Epsilon Seth", me=True)], [enemy(ZED, "MIDDLE")],
                            "MIDDLE", name_of, pool_of=lambda p, r: [(AHRI, 6)])
    assert locked["cards"][0]["best"] is None                                         # locked: nothing left to choose


@pytest.mark.parametrize("state,enemies,needle", [
    ("pending", [], "Deduciendo las líneas del rival con OP.GG"),
    ("off", [], "Sin OP.GG no se pueden deducir"),
    ("ok", [], "Aún no se ha revelado el rival de tu línea"),
    ("ok", [enemy(ZED, "MIDDLE")], "Elige un campeón"),
])
def test_my_lane_always_says_why_there_is_no_card(base, state, enemies, needle):
    book = book_of(base, lane_rows(AHRI, ZED, 10, 8))
    out = lc.build_lanes(book, [ally(0, "MIDDLE", 0, 0, me=True)], enemies, "MIDDLE", name_of, enemy_roles_state=state)
    assert [c["state"] for c in out["cards"]] == ["waiting"] and needle in out["cards"][0]["message"]


def test_no_samples_at_all_still_gives_cards_with_opgg(base):
    out = lc.build_lanes(None, [ally(0, "MIDDLE", AHRI, me=True)], [enemy(ZED, "MIDDLE")], "MIDDLE", name_of,
                         lookup_of({("MIDDLE", AHRI, ZED): OPGG_AHRI}))
    card = out["cards"][0]
    assert card["sample"] is None and card["spikes"] is None and card["opgg"]["status"] == "ok"
    assert out["diamond"] is False and out["data"] is False


# ------------------------------------------------------------------ the jungle is not a lane
SEJUANI = 113


def jungle_book(base):
    return book_of(base, lane_rows(LEE, SEJUANI, 12, 9, role="JUNGLE", person="Epsilon Seth"),
                   lane_rows(AHRI, ZED, 12, 6, person="Epsilon Seth"))


def test_a_jungle_card_is_a_duel_between_junglers_not_a_lane(base):
    found = {**OPGG_AHRI, "lane_winner": "mine", "solo_winner": "even"}
    out = lc.build_lanes(jungle_book(base), [ally(0, "JUNGLE", LEE, person="Epsilon Seth", me=True),
                                             ally(1, "MIDDLE", AHRI, person="Epsilon Seth")],
                         [enemy(SEJUANI, "JUNGLE"), enemy(ZED, "MIDDLE")], "JUNGLE", name_of,
                         lookup_of({("JUNGLE", LEE, SEJUANI): found}))
    assert [c["role"] for c in out["cards"]] == ["JUNGLE", "MIDDLE"]                  # my role first, as always
    jg, mid = out["cards"]
    assert jg["role_label"] == "Duelo de junglas" and jg["row_label"] == "Duelo"
    assert jg["sample"]["unit"] == "de duelos de junglas ganados"
    assert jg["sample"]["diff14_text"].endswith("oro+exp al 14 frente a la jungla rival")
    assert jg["person"]["text"] == "9 de 12 duelos de junglas ganados"
    assert jg["opgg"]["lane"] == "Ventaja en el duelo de junglas: Lee Sin"
    assert "en el duelo de junglas" in jg["summary"] and "en la línea" not in jg["summary"]
    blob = " ".join(str(v) for v in (jg["role_label"], jg["person"]["text"], jg["opgg"]["lane"], jg["summary"],
                                     jg["sample"]["diff14_text"]))
    assert "línea" not in blob.lower() and "ventaja en la" not in blob.lower()
    # the other cards keep the lane wording and carry none of the jungle fields
    assert mid["role_label"] == "Mid" and "unit" not in mid["sample"] and "row_label" not in mid
    assert mid["person"]["text"] == "6 de 12 líneas ganadas" and "en la línea" in mid["summary"]


def test_the_jungle_duel_goes_even_and_waits_in_its_own_words(base):
    even = lc.opgg_block({**OPGG_AHRI, "lane_winner": "even"}, "Lee Sin", "Sejuani", role="JUNGLE")
    assert even["lane"] == "El duelo de junglas va parejo"
    one = lc.person_block({"games": 1, "wins": 1}, "Tú", "JUNGLE")
    assert one["text"] == "1 de 1 duelo de junglas ganado"
    out = lc.build_lanes(jungle_book(base), [ally(0, "JUNGLE", 0, 0, me=True)], [], "JUNGLE", name_of)
    card = out["cards"][0]
    assert card["state"] == "waiting" and card["message"] == "Aún no se ha revelado la jungla rival."
    assert card["role_label"] == "Duelo de junglas"
