"""Death / fight / objective analysis (lolan.deaths) on synthetic matches and timelines."""
import pytest
from conftest import ME_PID, ME_PUUID, make_match, make_timeline

from lolan import deaths as dx

# Participants of make_match: 1-5 blue (TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY), 6-10 red; me = 3 (blue mid),
# enemy jungler = 7, enemy bot duo = 9 + 10, lane opponent = 8.
MIN = 60_000


def kill(ts, killer, victim, assists=(), x=6000, y=1500, received=None, dealt=None):
    ev = {"type": "CHAMPION_KILL", "timestamp": ts, "killerId": killer, "victimId": victim,
          "assistingParticipantIds": list(assists), "position": {"x": x, "y": y}}
    if received is not None:
        ev["victimDamageReceived"] = received
    if dealt is not None:
        ev["victimDamageDealt"] = dealt
    return ev


def dmg(pid, name, spell, slot, phys=0, mag=0, true=0, basic=False, typ="OTHER"):
    return {"participantId": pid, "name": name, "spellName": spell, "spellSlot": slot, "basic": basic,
            "physicalDamage": phys, "magicDamage": mag, "trueDamage": true, "type": typ}


def analyze(events, minutes=16, detail=True, names=None, tweak=None, win=True):
    match = make_match(win=win)
    timeline = make_timeline(minutes=minutes, events=events, tweak=tweak)
    return dx.analyze_game(match, timeline, ME_PUUID, detail=detail, names=names)


class StubNames:
    """Data Dragon stand-in: Ezreal's spells and passive, one item."""

    spells = {"ezrealq": ("Q", "Disparo místico"), "ezreale": ("E", "Translocación arcana")}

    def spell(self, champion, spell_name, slot):
        if champion != "Champ4":
            return None
        if spell_name in self.spells:
            return self.spells[spell_name]
        if isinstance(slot, int) and 0 <= slot <= 3:
            return "QWER"[slot], ["Disparo místico", "Flujo de esencia", "Translocación arcana", "Descarga"][slot]
        return None

    def passive(self, champion):
        return "Ritmo de batalla" if champion == "Champ4" else None

    def item(self, item_id):
        return {"3152": "Cinturón cohete hextech"}.get(str(item_id))


# ------------------------------------------------------------------ geometry
@pytest.mark.parametrize("x, y, zone, corner", [
    (1000, 1000, "base", "azul"), (13800, 13800, "base", "rojo"),
    (1200, 8000, "top", None), (8000, 13500, "top", None),
    (8000, 1200, "bot", None), (13500, 7000, "bot", None),
    (7400, 7400, "mid", None), (5200, 5600, "mid", None),
    (4000, 10000, "rio", None), (10500, 4800, "rio", None),
    (3500, 6500, "jungla", None), (11000, 8000, "jungla", None),
])
def test_zone_of(x, y, zone, corner):
    assert dx.zone_of(x, y) == (zone, corner)


def test_zone_of_missing_position():
    assert dx.zone_of(None, 5) == (None, None)
    assert dx.place(None, None, 100)["label"] == "Sin posición"


def test_side_is_relative_to_the_team_and_mirrors_for_red():
    # Bot lane near the blue base: own side for blue, enemy side for red.
    assert dx.place(8000, 1200, 100)["side"] == "propio"
    assert dx.place(8000, 1200, 200)["side"] == "enemigo"
    # Same point of the red half seen from both teams.
    assert dx.place(11000, 8000, 100)["side"] == "enemigo"
    assert dx.place(11000, 8000, 200)["side"] == "propio"
    # Bases.
    assert dx.place(1000, 1000, 100)["side"] == "propio"
    assert dx.place(1000, 1000, 200)["side"] == "enemigo"
    assert dx.place(13800, 13800, 200)["side"] == "propio"
    # The middle of the river diagonal is "centro" for both teams.
    assert dx.place(7400, 7500, 100)["side"] == "centro"
    assert dx.place(7400, 7500, 200)["side"] == "centro"


def test_place_texts():
    assert dx.place_phrase("rio", "propio") == "en tu lado del río"
    assert dx.place_phrase("bot", "enemigo") == "en el lado enemigo de la línea de bot"
    assert dx.place_phrase("base", "enemigo") == "en la base enemiga"
    assert dx.place_label("bot", "propio") == "Línea de bot · tu lado"
    assert dx.place_label("base", "propio") == "Tu base"


def test_death_timer_grows_with_level_and_time():
    assert dx.death_timer_s(1, 2) < dx.death_timer_s(10, 2) < dx.death_timer_s(18, 2)
    assert dx.death_timer_s(18, 40) > dx.death_timer_s(18, 10)
    assert dx.death_timer_s(99, 100) == pytest.approx(52.5 * 1.5)       # clamped level, capped factor


# ------------------------------------------------------------------- damage
def test_ability_label_cases():
    names = StubNames()
    label = lambda entry, champ="Champ4": dx.ability_label(entry, champ, names)  # noqa: E731
    assert label(dmg(4, "x", "ezrealq", 0)) == "Q · Disparo místico"
    assert label(dmg(4, "x", "ezreale", 2)) == "E · Translocación arcana"
    assert label(dmg(4, "x", "weirdname", 3)) == "R · Descarga"            # falls back to the slot
    assert label(dmg(4, "x", "ezrealbasicattack", 64, basic=True)) == "Ataque básico"
    assert label(dmg(4, "x", "jinxcritattack", 73, basic=True)) == "Ataque básico"
    assert label(dmg(4, "x", "ezrealpassive", 63)) == "Pasiva · Ritmo de batalla"
    assert label(dmg(4, "x", "", -1)) == "Runas y otros efectos"
    assert label(dmg(4, "x", "burning", 0)) == "Quemadura"
    assert label(dmg(4, "x", "summonerdot", 4)) == "Ignición"
    assert label(dmg(4, "x", "summonersmiteavatarutility", 5)) == "Castigo"
    assert label(dmg(4, "x", "3152active", 6)) == "Objeto: Cinturón cohete hextech"
    assert label(dmg(4, "x", "itemknightsvow", 8)) == "Objeto: Voto del caballero"
    assert label(dmg(4, "x", "foo", 70)) == "Otra habilidad"


def test_ability_label_without_data_dragon_uses_letters():
    assert dx.ability_label(dmg(4, "x", "ezrealq", 0), "Ezreal") == "Habilidad Q"
    assert dx.ability_label(dmg(4, "x", "ezrealpassive", 63), "Ezreal") == "Pasiva"


def test_damage_breakdown_groups_sources_and_sums_types():
    received = [
        dmg(4, "Ezreal", "ezrealq", 0, phys=100), dmg(4, "Ezreal", "ezrealq", 0, phys=50),
        dmg(4, "Ezreal", "ezreale", 2, mag=70),
        dmg(4, "Ezreal", "ezrealbasicattack", 64, phys=30, basic=True),
        dmg(0, "m", "sru_orderminionmeleebasicattack", 64, phys=15, basic=True, typ="MINION"),
        dmg(0, "m2", "sru_orderminionrangedbasicattack", 64, phys=5, basic=True, typ="MINION"),
        dmg(0, "t", "turretbasicattack", 46, phys=200, basic=True, typ="TOWER"),
        dmg(9, "Champ9", "", -1, true=10),
        dmg(0, "x", "zero", 0, phys=0),                                    # no damage: ignored
    ]
    game = dx._Game(make_match(), make_timeline(), ME_PUUID)
    sources = dx.damage_breakdown(game, received, StubNames())
    assert [s["key"] for s in sources] == ["p4", "tower", "minion", "p9"]
    ez = sources[0]
    assert ez["champion"] == "Champ4" and ez["total"] == 250 and ez["team"] == 100
    assert [(a["label"], a["total"]) for a in ez["abilities"]] == [
        ("Q · Disparo místico", 150), ("E · Translocación arcana", 70), ("Ataque básico", 30)]
    assert ez["abilities"][0]["type"] == "fisico" and ez["abilities"][1]["type"] == "magico"
    assert sources[2]["label"] == "Minions" and sources[2]["total"] == 20
    assert sources[1]["label"] == "Torres"
    totals = dx.damage_totals(received)
    assert totals["total"] == 480 and totals["phys"] == 400 and totals["mag"] == 70 and totals["true"] == 10
    assert totals["champions"] == 260


def test_damage_dealt_is_grouped_by_target():
    game = dx._Game(make_match(), make_timeline(), ME_PUUID)
    dealt = dx.damage_dealt_breakdown(game, [
        dmg(4, "Champ3", "ahriq", 0, mag=80), dmg(4, "Champ3", "ahribasicattack", 64, phys=20, basic=True),
        dmg(0, "Champ3", "x", 0, phys=40, typ="MINION")])
    assert dealt["total"] == 140 and dealt["champions"] == 100
    assert dealt["targets"][0]["champion"] == "Champ4" and dealt["targets"][0]["total"] == 100


# ------------------------------------------------------------- death records
def tweak_state(pfs, minute):
    """Me: level 5, 1240 gold from minute 4 on; killer 8 (opp mid): level 6, 3000 total gold."""
    if minute >= 4:
        pfs["3"].update({"level": 5, "currentGold": 1240, "totalGold": 2500, "position": {"x": 6000, "y": 1500}})
        pfs["8"].update({"level": 6, "totalGold": 3000, "position": {"x": 6500, "y": 1800}})
        pfs["7"]["position"] = {"x": 6800, "y": 2000}


def test_death_record_facts():
    events = [
        kill(4 * MIN + 25_000, 3, 9, x=6000, y=1500),                     # my own kill
        kill(5 * MIN + 0, 8, 3, assists=[7], x=6000, y=1500,
             received=[dmg(8, "Zed", "zedq", 0, phys=300), dmg(7, "Champ7", "", -1, phys=100)],
             dealt=[dmg(8, "Champ3", "ahriq", 0, mag=90)]),
        {"type": "WARD_PLACED", "timestamp": 5 * MIN - 60_000, "creatorId": 1},
        {"type": "WARD_PLACED", "timestamp": 5 * MIN - 30_000, "creatorId": 5},
        {"type": "WARD_PLACED", "timestamp": 5 * MIN - 30_000, "creatorId": 8},          # enemy ward
        {"type": "WARD_PLACED", "timestamp": 5 * MIN - 200_000, "creatorId": 5},        # too old
        {"type": "ITEM_PURCHASED", "timestamp": 5 * MIN - 120_000, "participantId": 3, "itemId": 2055},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 5 * MIN + 20_000, "killerId": 9, "killerTeamId": 200,
         "monsterType": "DRAGON", "monsterSubType": "FIRE_DRAGON"},
        {"type": "BUILDING_KILL", "timestamp": 5 * MIN - 10_000, "killerId": 1, "teamId": 200,
         "buildingType": "TOWER_BUILDING"},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 5 * MIN + 90_000, "killerId": 1, "killerTeamId": 100,
         "monsterType": "BARON_NASHOR"},                                                 # too far in time
    ]
    a = analyze(events, tweak=tweak_state)
    assert a["context"] == {"lane_opponent": "Zed", "enemy_jungler": "Champ7", "enemy_jungler_pid": 7,
                            "enemy_bot": ["Champ9", "Champ10"], "enemy_bot_pids": [9, 10],
                            "enemy_adc": "Champ9", "enemy_adc_pid": 9,
                            "enemy_support": "Champ10", "enemy_support_pid": 10}
    assert len(a["deaths"]) == 1
    d = a["deaths"][0]
    assert d["t"] == 5 * MIN and d["clock"] == "5:00" and d["early"]
    assert d["pos"] == (6000, 1500) and (d["zone"], d["side"]) == ("bot", "propio")
    assert d["kind"] == "campeon" and d["killer"]["champion"] == "Zed" and d["killer"]["role"] == "MIDDLE"
    assert [x["pid"] for x in d["assisters"]] == [7] and d["enemies"] == 2
    assert d["enemy_jungler_involved"] is True
    assert (d["level"], d["killer_level"], d["level_diff"]) == (5, 6, -1)
    assert d["current_gold"] == 1240 and d["unspent_1000"] is True and d["gold_diff"] == -500
    assert d["frame_ts"] == 5 * MIN
    assert d["after_own_kill"] and d["since_own_kill_s"] == 35
    assert d["wards_allied_90s"] == 2 and d["control_ward_3min"] is True
    near = {(o["kind"], o["by"], o["dt_s"]) for o in d["objectives_near"]}
    assert near == {("dragon", "enemigo", 20), ("tower", "aliado", -10)}
    assert d["damage_received"]["total"] == 400 and d["damage_received"]["champions"] == 400
    assert d["damage_sources"][0]["champion"] == "Zed"
    assert d["damage_dealt"]["total"] == 90
    assert d["positions"][8] == (6500, 1800)
    facts = " ".join(dx.describe_death(d, a))
    assert "Te mató Zed (Mid) con ayuda de Champ7 (Jungla); su jungla (Champ7) participó." in facts
    assert "Tenías 1 nivel menos que Zed y 1240 de oro sin gastar." in facts
    assert "Habías matado a alguien 35 s antes." in facts
    assert "Moriste en tu lado de la línea de bot." in facts
    assert "El equipo enemigo tomó un dragón 20 s después de tu muerte." in facts
    assert "Tu equipo destruyó una torre 10 s antes de tu muerte." in facts
    assert "2 wards" in facts and "sí habías comprado un ward de control" in facts
    assert "aproximado: Riot no da la posición de los wards" in facts


def test_jungler_not_involved_solo_death_and_no_flags():
    events = [kill(8 * MIN, 8, 3, x=6000, y=1500), kill(12 * MIN, 9, 3, assists=[10], x=7400, y=7500)]
    a = analyze(events, tweak=tweak_state)
    first, second = a["deaths"]
    assert first["enemy_jungler_involved"] is False and first["enemies"] == 1
    assert not first["after_own_kill"] and first["wards_allied_90s"] == 0 and not first["control_ward_3min"]
    assert second["enemy_jungler_involved"] is False and second["enemies"] == 2
    assert second["side"] == "centro" and second["zone"] == "mid"
    text = " ".join(dx.describe_death(first, a))
    assert "en solitario" in text and "su jungla (Champ7) no participó" in text


def test_death_by_tower_has_no_killer():
    events = [kill(6 * MIN, 0, 3, received=[dmg(0, "t", "turretbasicattack", 46, phys=300, basic=True, typ="TOWER")])]
    d = analyze(events)["deaths"][0]
    assert d["kind"] == "torre" and d["killer"] is None and d["enemies"] == 0
    assert d["enemy_jungler_involved"] is None and d["level_diff"] is None
    assert dx.describe_death(d, analyze(events))[0] == "Te mató una torre."


def test_no_timeline_or_player_gives_none():
    match = make_match()
    assert dx.analyze_game(match, None, ME_PUUID) is None
    assert dx.analyze_game(match, make_timeline(events=[]), "someone-else") is None


def test_lite_analysis_skips_detail():
    events = [kill(5 * MIN, 8, 3, received=[dmg(8, "Zed", "zedq", 0, phys=300)])]
    a = analyze(events, detail=False)
    assert "damage_sources" not in a["deaths"][0] and "fights" not in a
    assert a["deaths"][0]["damage_received"]["total"] == 300


# -------------------------------------------------------------------- fights
def k(ts, killer, victim, assists=(), x=6000, y=6000):
    return kill(ts, killer, victim, assists, x, y)


def fake_kills(*specs):
    return [{"timestamp": t, "killerId": 1, "victimId": 6, "position": {"x": x, "y": y}} for t, x, y in specs]


def test_cluster_splits_on_time_gap():
    groups = dx.cluster_kills(fake_kills((0, 5000, 5000), (10_000, 5100, 5000), (24_000, 5200, 5000),
                                         (40_000, 5000, 5000)))
    assert [len(g) for g in groups] == [3, 1]                 # 14 s gap joins, 16 s gap splits


def test_cluster_splits_on_distance_and_keeps_simultaneous_fights_apart():
    kills = fake_kills((0, 2000, 2000), (3000, 9000, 9000), (6000, 2200, 2100), (9000, 9100, 9200))
    groups = dx.cluster_kills(kills)
    assert sorted(len(g) for g in groups) == [2, 2]
    assert {k["position"]["x"] < 5000 for k in groups[0]} in ({True}, {False})


def test_fight_rules_and_result():
    events = [
        # Fight A: 3 kills, I am not involved -> counts (>= 3 kills).
        k(2 * MIN, 6, 1, x=1500, y=9000), k(2 * MIN + 5000, 1, 7, x=1600, y=9100), k(2 * MIN + 9000, 6, 5, x=1500, y=9200),
        # Pair of kills without me far away -> not a fight.
        k(5 * MIN, 8, 1, x=12000, y=3000), k(5 * MIN + 4000, 9, 2, x=12100, y=3000),
        # Pair of kills where I die first -> a fight, lost 1-2.
        k(9 * MIN, 8, 3, assists=[7], x=7000, y=7000), k(9 * MIN + 6000, 4, 9, x=7100, y=7100),
        k(9 * MIN + 12_000, 8, 5, x=7200, y=7200),
    ]
    a = analyze(events)
    fights = a["fights"]
    assert [f["clock"] for f in fights] == ["2:00", "9:00"]
    first, second = fights
    assert first["result"] == "perdida" and (first["my_team_kills"], first["enemy_team_kills"]) == (1, 2)
    assert first["first_death"] == 1 and first["first_death_mine"] and not first["player_involved"]
    assert first["place"].startswith("Línea de top")
    assert second["player_died"] and second["first_death"] == 3
    assert (second["my_team_kills"], second["enemy_team_kills"]) == (1, 2) and second["result"] == "perdida"
    assert second["player_first"]["what"] == "murió" and second["player_first"]["pos"] == (7000, 7000)
    assert second["duration_s"] == 12 and len(second["kills"]) == 3
    assert second["positions"][3] == (7000, 7000) and second["positions_frame_ts"] == 9 * MIN
    # The player's death knows which fight it belongs to.
    assert a["deaths"][0]["fight"] == 2


def test_fight_aftermath_and_player_kills():
    events = [
        k(6 * MIN, 3, 6, assists=[], x=5000, y=5000), k(6 * MIN + 5000, 4, 7, assists=[3], x=5100, y=5000),
        {"type": "ELITE_MONSTER_KILL", "timestamp": 6 * MIN + 40_000, "killerId": 1, "killerTeamId": 100,
         "monsterType": "DRAGON"},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 6 * MIN + 120_000, "killerId": 1, "killerTeamId": 100,
         "monsterType": "BARON_NASHOR"},
    ]
    f = analyze(events)["fights"][0]
    assert f["result"] == "ganada" and f["player_kills"] == 1 and f["player_assists"] == 1
    assert [(o["kind"], o["by"]) for o in f["aftermath"]] == [("dragon", "aliado")]
    assert "Ganó tu equipo 2-0" in dx.describe_fight(f, {"participants": {1: {"champion": "A"}, 6: {"champion": "B"}}})


# ---------------------------------------------------------- lost objectives
def tweak_far(pfs, minute):
    pfs["3"]["position"] = {"x": 9800, "y": 4400} if minute < 8 else {"x": 1000, "y": 13000}
    pfs["3"]["level"] = 5


def test_lost_objectives_dead_far_and_near():
    events = [
        {"type": "ELITE_MONSTER_KILL", "timestamp": 4 * MIN, "killerId": 9, "killerTeamId": 200, "monsterType": "DRAGON"},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 6 * MIN, "killerId": 9, "killerTeamId": 200, "monsterType": "DRAGON"},
        kill(6 * MIN - 8000, 8, 3, x=9800, y=4400),                                   # died 8 s before the 2nd dragon
        {"type": "ELITE_MONSTER_KILL", "timestamp": 10 * MIN, "killerId": 8, "killerTeamId": 200, "monsterType": "RIFTHERALD"},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 11 * MIN, "killerId": 1, "killerTeamId": 100, "monsterType": "DRAGON"},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 12 * MIN, "killerId": 8, "killerTeamId": 200, "monsterType": "HORDE"},
        kill(10 * MIN - 5000, 8, 1), kill(10 * MIN - 4000, 8, 2),
    ]
    lost = analyze(events, tweak=tweak_far)["objectives_lost"]
    assert [(o["kind"], o["status"]) for o in lost] == [("dragon", "cerca"), ("dragon", "muerto"), ("herald", "lejos")]
    near, dead, far = lost
    assert near["distance"] < 100 and not near["dead"] and not near["far"]
    assert dead["dead"] and dead["dead_for_s"] == pytest.approx(8, abs=0.5) and not dead["far"]
    assert far["far"] and far["distance"] > dx.FAR_UNITS and far["allies_dead"] == 2     # pids 1 and 2 had just died
    assert near["died_recently"] is False and dead["died_recently"] is True


# ---------------------------------------------------------------- aggregates
def game(match_id, win, deaths_specs, enemy_jungler="Wukong", bot=("Jinx", "Thresh"), objs=()):
    """A minimal per-game analysis dict with the fields aggregate() reads."""
    deaths = []
    for spec in deaths_specs:
        d = {"early": False, "enemy_jungler_involved": False, "enemies": 2, "kind": "campeon",
             "level_diff": 0, "unspent_1000": False, "after_own_kill": False, "zone": "bot", "side": "propio",
             "pos": (6000, 1500)}
        d.update(spec)
        deaths.append(d)
    return {"match_id": match_id, "win": win, "created": int(match_id[1:]) if match_id[1:].isdigit() else 0,
            "champion": "Jinx", "deaths": deaths, "objectives_lost": list(objs),
            "context": {"enemy_jungler": enemy_jungler, "enemy_jungler_pid": 7, "enemy_bot": list(bot),
                        "enemy_adc": bot[0], "enemy_adc_pid": 9, "enemy_support": bot[1], "enemy_support_pid": 10}}


def test_aggregate_splits_wins_and_losses():
    win = game("g1", True, [{"early": True, "enemy_jungler_involved": True}, {"enemies": 1, "unspent_1000": True}])
    loss = game("g2", False, [{"early": True, "enemies": 3, "level_diff": -1, "after_own_kill": True, "side": "enemigo"},
                              {"early": True, "enemy_jungler_involved": True, "enemies": 3, "level_diff": -2}])
    ag = dx.aggregate([win, loss])
    assert (ag["games"], ag["wins"], ag["losses"], ag["deaths"]) == (2, 1, 1, 4)
    assert ag["deaths_pg"] == {"all": 2.0, "win": 2.0, "loss": 2.0}
    early = ag["jungler_early"]
    assert (early["all"]["n"], early["all"]["of"]) == (2, 3)
    assert (early["win"]["n"], early["win"]["of"]) == (1, 1) and (early["loss"]["n"], early["loss"]["of"]) == (1, 2)
    assert ag["jungler_late"]["all"]["of"] == 1 and ag["jungler_late"]["all"]["n"] == 0
    assert [ag[k]["all"]["n"] for k in ("enemies_1", "enemies_2", "enemies_3p")] == [1, 1, 2]
    assert ag["enemies_3p"]["loss"]["pct"] == 1.0 and ag["enemies_3p"]["win"]["pct"] == 0.0
    assert (ag["lower_level"]["all"]["n"], ag["lower_level"]["all"]["of"]) == (2, 4)
    assert ag["unspent_1000"]["win"]["n"] == 1 and ag["unspent_1000"]["loss"]["n"] == 0
    assert ag["after_kill"]["loss"]["n"] == 1 and ag["enemy_side"]["loss"]["n"] == 1
    zones = {z["label"]: z["all"]["n"] for z in ag["zones"]}
    assert zones == {"Línea de bot · tu lado": 3, "Línea de bot · lado enemigo": 1}


def test_rival_rows_count_deaths_that_rival_took_part_in():
    by_jg = {"killer": {"pid": 7}, "assisters": []}
    by_adc = {"killer": {"pid": 9}, "assisters": [{"pid": 7}]}
    other = {"killer": {"pid": 3}, "assisters": []}
    games = [game(f"g{i}", True, [by_jg, by_adc, other]) for i in range(3)]
    ag = dx.aggregate(games)
    jg, adc, sup = ag["junglers"][0], ag["adcs"][0], ag["supports"][0]
    assert (jg["by_them"], jg["by_them_pg"], jg["deaths_pg"]) == (6, 2.0, 3.0)  # kill or assist both count
    assert (adc["by_them"], sup["by_them"]) == (3, 0)


def test_aggregate_objectives_and_empty():
    objs = [{"kind": "dragon", "dead": True, "far": False, "died_recently": True, "distance": 100},
            {"kind": "baron", "dead": False, "far": True, "died_recently": False, "distance": 6000},
            {"kind": "dragon", "dead": False, "far": False, "died_recently": False, "distance": 500},
            {"kind": "herald", "dead": True, "far": False, "died_recently": True, "distance": 100}]
    ag = dx.aggregate([game("g1", False, [{}], objs=objs)])
    ob = ag["objectives"]
    assert ob["total"] == 3 and ob["dead"]["all"]["n"] == 1 and ob["far"]["all"]["n"] == 1
    assert ob["dead_or_far"]["loss"]["n"] == 2 and ob["dead_or_far"]["win"]["of"] == 0
    assert dx.aggregate([])["deaths"] == 0


def test_matchup_tables_need_three_games():
    games = [game(f"g{i}", i % 2 == 0, [{}, {}], enemy_jungler="MonkeyKing" if i < 3 else "Kindred") for i in range(4)]
    games.append(game("g9", True, [{}], enemy_jungler="Kindred", bot=("Ezreal", "Brand")))
    ag = dx.aggregate(games)
    assert [r["key"] for r in ag["junglers"]] == ["Wukong"]               # Kindred has only 2 games
    row = ag["junglers"][0]
    assert row["games"] == 3 and row["deaths"] == 6 and row["deaths_pg"] == 2.0
    assert row["winrate"] == pytest.approx(2 / 3)
    # Rivals are counted one by one, not as a duo: Thresh has 4 games; Brand only 1.
    assert [r["key"] for r in ag["adcs"]] == ["Jinx"] and ag["adcs"][0]["games"] == 4
    assert [r["key"] for r in ag["supports"]] == ["Thresh"]
    assert dx.matchup_table(games, lambda a: "x", min_games=6) == []


def test_heat_points_filters():
    win = game("g1", True, [{"early": True, "pos": (1, 1)}, {"early": False, "pos": (2, 2)}])
    loss = game("g2", False, [{"early": True, "pos": (3, 3)}, {"early": False, "pos": None}])
    assert dx.heat_points([win, loss]) == [(1, 1), (2, 2), (3, 3)]
    assert dx.heat_points([win, loss], "antes14") == [(1, 1), (3, 3)]
    assert dx.heat_points([win, loss], "despues14") == [(2, 2)]
    assert dx.heat_points([win, loss], "todas", "derrotas") == [(3, 3)]
    assert dx.heat_points([win, loss], "antes14", "victorias") == [(1, 1)]


def test_heat_points_mirror_red_side_so_own_base_is_bottom_left():
    blue = game("g1", True, [{"early": True, "pos": (1000, 2000)}])
    red = game("g2", True, [{"early": True, "pos": (1000, 2000)}])
    blue["team"], red["team"] = 100, 200
    assert dx.heat_points([blue, red]) == [(1000, 2000), (dx.MAP_SIZE - 1000, dx.MAP_SIZE - 2000)]


# --------------------------------------------------------------------- plan
def plan_games(n_each=6):
    """Losses have 3 deaths after an own kill, wins 0; everything else is identical in both."""
    games = []
    for i in range(n_each * 2):
        win = i % 2 == 0
        extra = (i // 2) % 2                                   # 0/1 so the groups have some spread
        after = [{"after_own_kill": True}] * extra if win else [{"after_own_kill": True}] * (3 + extra)
        games.append(game(f"g{i + 10}", win, [{}] + after))
    return games


def test_plan_picks_the_metric_that_separates_losses_from_wins():
    games = plan_games()
    plan = dx.improvement_plan(games)
    assert plan["enough"]
    assert [p["key"] for p in plan["priorities"]] == ["after_kill"]
    p = plan["priorities"][0]
    assert p["goal_text"] == "Máximo 1 muerte en el minuto siguiente a una kill tuya"
    assert p["met_rate"]["n"] == 6 and p["met_rate"]["of"] == 12 and p["miss"] == 0.5     # met in every win
    assert p["winrate_met"] == 1.0 and p["winrate_not_met"] == 0.0 and p["effect"] > 1
    assert len(p["last"]) == 10
    assert [c["ok"] for c in p["last"]] == [c["win"] for c in p["last"]]       # met exactly in the wins
    assert p["last"][-1]["match_id"] == games[-1]["match_id"]


def test_plan_orders_by_score_and_keeps_top_three():
    games = []
    for i in range(16):
        win = i % 2 == 0
        j = (i // 2) % 2                                          # spread inside each group
        specs = [{}]
        if win:
            specs += [{"after_own_kill": True}] * j
        else:
            specs += [{"after_own_kill": True}] * (3 + j)                     # strong separator, always missed
            specs += [{"unspent_1000": True}] * (2 + j) if i % 4 == 1 else []  # weaker: only half of the losses
            specs += [{"early": True}] * (2 + j)
        games.append(game(f"g{i + 10}", win, specs))
    plan = dx.improvement_plan(games)
    keys = [p["key"] for p in plan["priorities"]]
    assert len(keys) == 3 and keys[0] in ("after_kill", "early_deaths")
    assert "unspent" in keys
    scores = [p["score"] for p in plan["priorities"]]
    assert scores == sorted(scores, reverse=True)


def test_plan_needs_enough_games_and_a_real_difference():
    assert not dx.improvement_plan(plan_games(3))["enough"]                   # 6 games < 8
    all_wins = [game(f"g{i + 10}", True, [{}]) for i in range(10)]
    assert not dx.improvement_plan(all_wins)["enough"]
    same = [game(f"g{i + 10}", i % 2 == 0, [{"after_own_kill": i % 3 == 0}]) for i in range(12)]
    plan = dx.improvement_plan(same)
    assert plan["enough"] and plan["priorities"] == []
