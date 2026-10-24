"""Analysis by line: support and jungle report cards, stats, insights, reports, deaths plan and CLI."""
import pytest
from conftest import BASE_TIME_MS, ME_PUUID, ev_kill
from line_fixtures import (HOUR, JUNGLE_CHALLENGES, SUPPORT_CHALLENGES, add_role_games, make_role_game,
                           role_pid, role_timeline)

from lolan import analyze as an
from lolan import deaths as dx
from lolan import report
from lolan.__main__ import main
from lolan.metrics import extract_game_metrics
from lolan.store import Store


def row(role, i=0, challenges=None, timeline=None, fields=None, **kw):
    kw.setdefault("created", BASE_TIME_MS + i * 3 * HOUR)
    kw.setdefault("duration", 1800)
    m = make_role_game(role, f"LA1_{role[:2]}{i}", challenges=challenges, fields=fields, **kw)
    return extract_game_metrics(m, timeline, ME_PUUID)


def games(role, n=8, vary=None, **kw):
    gs = [row(role, i, win=bool(i % 2), **{**kw, **(vary(i) if vary else {})}) for i in range(n)]
    an.add_session_info(gs)
    return gs


def texts(card, side):
    return " ".join(x["text"] for x in card[side])


# ------------------------------------------------------------------ support
def support_games(**kw):
    return games("UTILITY", challenges=SUPPORT_CHALLENGES, fields={"timeCCingOthers": 40}, champion="Soraka", **kw)


def test_support_card_has_no_cs_and_damage_is_only_information():
    gs = support_games()
    card = an.game_report_card(gs[3], gs)
    keys = {r["key"] for r in card["rows"]}
    assert {"vision_per_min", "wards_per_min", "ward_takedowns_per_min", "control_wards", "heal_shield_per_min",
            "cc_time_per_min", "immobilizations_per_10", "saves", "support_quest", "kp"} <= keys
    assert not keys & {"cs_per_min", "cs_diff_14", "gold_diff_14", "xp_diff_14"}
    damage = [r for r in card["rows"] if r["key"] in ("damage_per_min", "damage_share")]
    assert len(damage) == 2 and all(r["neutral"] and r["verdict"] is None and r["goodness"] is None for r in damage)
    assert card["rival_label"] == "Soporte rival" and card["text"].splitlines()[1].startswith("Soporte rival: ")
    flagged = {x["key"] for x in card["strengths"] + card["weaknesses"]}
    assert not flagged & {"damage_per_min", "damage_share", "cs_per_min", "cs_diff_14"}
    assert "CS" not in card["text"]


def test_support_flags_vision_kp_wards_saves_and_quest():
    good = row("UTILITY", challenges=SUPPORT_CHALLENGES, fields={"visionScore": 90},
               opp_fields={"visionScore": 40}, kills=0, assists=20)
    s, w = an._rule_flags(good)
    assert s["vision_per_min"] == "Visión de 3.0/min, +1.7 sobre el soporte rival."
    assert "kp" in s and s["saves"] == "Salvaste a un aliado de morir 2 veces."
    bad = row("UTILITY", challenges={**SUPPORT_CHALLENGES, "completeSupportQuestInTime": 0, "saveAllyFromDeath": 0},
              fields={"visionScore": 30, "detectorWardsPlaced": 0}, opp_fields={"visionScore": 80},
              kills=0, assists=2)
    s, w = an._rule_flags(bad)
    assert w["vision_per_min"] == "Visión de 1.0/min, -1.7 frente al soporte rival."
    assert w["control_wards"] == "No pusiste ningún ward de control."
    assert w["support_quest"] and "kp" in w


def test_support_card_without_challenges_still_works():
    gs = games("UTILITY", challenges=None)
    card = an.game_report_card(gs[0], gs)
    keys = {r["key"] for r in card["rows"]}
    assert "vision_per_min" in keys and "saves" not in keys and "support_quest" not in keys
    assert card["text"]


def test_support_percentiles_only_use_games_of_the_same_role():
    laners = games("MIDDLE", n=10)
    sups = support_games(vary=lambda i: {"fields": {"visionScore": 20 + 5 * i}})
    for r in laners:
        r["match_id"] = "L" + r["match_id"]
    card = an.game_report_card(sups[7], laners + sups)
    scopes = {r["scope"] for r in card["rows"] if r["scope"]}
    assert scopes <= {"campeón", "rol"}
    # a support with no other support games gets no verdict from laners
    lonely = an.game_report_card(sups[0], laners + sups[:1])
    assert all(r["verdict"] is None for r in lonely["rows"])


def test_laner_card_keeps_the_all_games_fallback():
    gs = games("MIDDLE", n=8)
    odd = dict(gs[0], champion="Other", role="", match_id="odd")
    card = an.game_report_card(odd, gs + [odd])
    assert any(r["scope"] == "todas las partidas" for r in card["rows"])


def test_support_insights_leave_out_cs_and_damage():
    gs = games("UTILITY", n=12, challenges=SUPPORT_CHALLENGES,
               vary=lambda i: {"fields": {"visionScore": 20 + 4 * i, "wardsPlaced": 10 + i}})
    for r in gs:
        r["champion"] = "Soraka"
    ins = an.champion_insights(gs, "Soraka")
    assert ins["enough"]
    keys = {e["key"] for e in ins["higher_in_wins"] + ins["higher_in_losses"]}
    assert keys and not keys & {"cs_per_min", "cs_first10", "max_cs_adv", "cs_diff_14", "gold_diff_14", "damage_per_min"}


# ------------------------------------------------------------------- jungle
def jungle_timeline(extra=()):
    pid = role_pid("JUNGLE")
    return role_timeline("JUNGLE", [ev_kill(120_000, pid, 8, [1]), ev_kill(300_000, pid, 9), *extra], minutes=18)


def test_jungle_card_names_the_enemy_jungler_and_skips_cs():
    m = make_role_game("JUNGLE", challenges=JUNGLE_CHALLENGES, opp_challenges={"enemyJungleMonsterKills": 22})
    r = extract_game_metrics(m, jungle_timeline(), ME_PUUID)
    gs = [dict(r, match_id=f"x{i}") for i in range(8)]
    card = an.game_report_card(r, gs)
    keys = {x["key"] for x in card["rows"]}
    assert {"jungle_cs_10", "enemy_jungle_cs", "scuttle_crabs", "epic_takedowns", "epic_steals", "ka_15",
            "early_ka"} <= keys
    assert not keys & {"cs_per_min", "cs_diff_14", "cs_diff_7"}
    assert card["rival_label"] == "Jungla rival" and "Jungla rival: RivalJungle" in card["text"]
    assert "rival de línea" not in card["text"].lower()
    s, w = an._rule_flags(r)
    assert s["enemy_jungle_cs"] == "Robaste 14 monstruos de la jungla rival."
    assert w["enemy_jungle_cs"] == "El jungla rival te robó 22 monstruos de tu jungla."
    assert s["epic_steals"] == "Robaste 1 objetivo épico."
    assert "early_ka" not in s and "early_ka" not in w          # 2 early takedowns: neither


def test_jungle_gold_and_xp_rules_name_the_jungler():
    r = row("JUNGLE", challenges=JUNGLE_CHALLENGES)
    r.update(gold_diff_14=900, xp_diff_14=-700, cs_diff_14=40)
    s, w = an._rule_flags(r)
    assert s["gold_diff_14"].endswith("frente al jungla rival.")
    assert w["xp_diff_14"].endswith("frente al jungla rival.")
    assert "cs_diff_14" not in s


def test_jungle_early_takedowns_rule_and_fallback_to_the_challenge():
    r = row("JUNGLE", challenges={**JUNGLE_CHALLENGES, "takedownsFirstXMinutes": 9})
    s, _ = an._rule_flags(r)                                    # no timeline: the challenge (minute 15)
    assert s["early_ka"] == "Participaste en 9 kills antes del minuto 15."
    r = row("JUNGLE", challenges={**JUNGLE_CHALLENGES, "takedownsFirstXMinutes": 0})
    _, w = an._rule_flags(r)
    assert w["early_ka"] == "No participaste en ninguna kill antes del minuto 15."
    pid = role_pid("JUNGLE")
    tl = role_timeline("JUNGLE", [ev_kill(60_000 * k, pid, 6 + k % 4) for k in range(1, 9)], minutes=18)
    r = row("JUNGLE", challenges=JUNGLE_CHALLENGES, timeline=tl)
    s, _ = an._rule_flags(r)
    assert s["early_ka"] == "Participaste en 8 kills antes del minuto 14."


def test_jungle_with_missing_keys_does_not_break():
    r = row("JUNGLE", challenges=None)
    card = an.game_report_card(r, [r])
    assert card["rows"] and "jungle_cs_10" not in {x["key"] for x in card["rows"]}


# ------------------------------------------------------ aggregates and profiles
def test_profiles_by_role_and_games():
    assert an.profile_for_role("UTILITY").key == "support" and an.profile_for_role("JUNGLE").key == "jungle"
    assert {an.profile_for_role(r).key for r in ("TOP", "MIDDLE", "BOTTOM", "", None)} == {"lane"}
    sup, jg, mid = support_games(), games("JUNGLE", challenges=JUNGLE_CHALLENGES), games("MIDDLE")
    assert an.profile_for_games(sup).key == "support" and an.profile_for_games(mid + games("TOP")).key == "lane"
    assert an.profile_for_games(sup + jg).key == "mixed" and an.profile_for_games(sup + mid).key == "mixed"
    assert an.profile_for_games([]).key == "lane"


def test_main_line_resolve_and_filter():
    sup, mid = support_games(), games("MIDDLE", n=3)
    allg = sup + mid
    assert an.main_line_of(allg) == "UTILITY" and an.main_line_of([]) is None
    assert an.main_line_of(games("TOP", n=2) + games("JUNGLE", n=2)) == "TOP"      # tie: the earlier role
    assert an.resolve_line("auto", allg) == "UTILITY" and an.resolve_line(None, allg) == "UTILITY"
    assert an.resolve_line("todas", allg) is None and an.resolve_line("MIDDLE", allg) == "MIDDLE"
    assert len(an.games_in_line(allg, "MIDDLE")) == 3 and len(an.games_in_line(allg, None)) == 11


def test_group_stats_never_average_cs_across_lines():
    sup, mid = support_games(), games("MIDDLE", n=4)
    st = an.group_stats(sup)
    assert st["cs_per_min"] is None and st["gold_diff_14"] is None and st["heal_shield_per_min"] is not None
    assert st["jungle_cs_10"] is None
    both = an.group_stats(sup + mid)
    assert both["cs_per_min"] == pytest.approx(an.group_stats(mid)["cs_per_min"])       # lane games only
    assert both["heal_shield_per_min"] == pytest.approx(st["heal_shield_per_min"])
    jg = an.group_stats(games("JUNGLE", challenges=JUNGLE_CHALLENGES))
    assert jg["jungle_cs_10"] == 60 and jg["epic_takedowns"] == 4 and jg["cs_per_min"] is None


def test_row_stat_hides_what_does_not_apply():
    sup = support_games()[0]
    assert an.row_stat(sup, "cs_per_min") is None and an.row_stat(sup, "kp") == sup["kp"]
    assert an.row_stat(games("MIDDLE", n=1)[0], "cs_per_min") is not None


def test_session_trend_with_a_line_keeps_the_real_sequence():
    gs = [dict(match_id=i, win=w, session_id=1, session_game=i + 1, role=r, kills=0, deaths=0, assists=0)
          for i, (w, r) in enumerate([(1, "A"), (0, "B"), (1, "A"), (1, "A")])]
    out = an.trend_by_session_game(gs, only={0, 2, 3})
    assert out["after_win"]["games"] == 1 and out["after_loss"]["games"] == 1      # game 2 follows the uncounted loss
    assert sum(r["games"] for r in out["by_position"]) == 3


def test_player_overview_exposes_the_line_stat():
    assert an.player_overview(support_games())["line_stat"]["key"] == "heal_shield_per_min"
    assert an.player_overview(games("JUNGLE", challenges=JUNGLE_CHALLENGES))["line_stat"]["key"] == "jungle_cs_10"
    assert an.player_overview(games("BOTTOM"))["line_stat"] is None
    assert an.player_overview(support_games() + games("JUNGLE", challenges=JUNGLE_CHALLENGES))["profile"] == "mixed"


# ------------------------------------------------------------------ reports
def test_markdown_reports_use_the_lines_metric_set():
    gs = support_games(n=8)
    summary = report.render_summary(gs)
    assert "Línea: **Soporte**" in summary and "Visión/min" in summary and "Curación y escudos/min" in summary
    assert "CS/min" not in summary
    champ = report.render_champion(gs, "Soraka")
    assert "Wards puestos por minuto" in champ and "CS por minuto" not in champ
    md = report.render_card_markdown(an.game_report_card(gs[1], gs))
    assert "Tú | Soporte rival |" in md
    assert "informativo" in md


# -------------------------------------------------------------------- deaths
def test_plan_for_a_jungler_uses_invasions_not_the_enemy_jungler():
    from test_deaths import game as dgame

    def spec(i, win):
        return [{}] + [{"early": True, "zone": "jungla", "side": "enemigo", "enemy_jungler_involved": True}] * (
            (3 if not win else 0) + (i // 2) % 2)

    gs = [dgame(f"g{i + 10}", i % 2 == 0, spec(i, i % 2 == 0)) for i in range(16)]
    jungle = dx.improvement_plan(gs, line="jungle")
    lane = dx.improvement_plan(gs)
    mixed = dx.improvement_plan(gs, line="mixed")
    keys = lambda p: {x["key"] for x in p["priorities"]}                       # noqa: E731
    assert "invade_early" in keys(jungle) and "jungler_early" not in keys(jungle)
    assert "jungler_early" in keys(lane) and "invade_early" not in keys(lane)
    assert not keys(mixed) & {"jungler_early", "invade_early"}
    label = [x for x in jungle["priorities"] if x["key"] == "invade_early"][0]["goal_text"]
    assert "jungla enemiga" in label


def test_aggregate_counts_deaths_in_the_enemy_jungle():
    from test_deaths import game as dgame
    ag = dx.aggregate([dgame("g1", True, [{"early": True, "zone": "jungla", "side": "enemigo"}, {}]),
                       dgame("g2", False, [{"early": False, "zone": "jungla", "side": "enemigo"}])])
    assert (ag["enemy_jungle_all"]["all"]["n"], ag["enemy_jungle_all"]["all"]["of"]) == (2, 3)
    assert (ag["enemy_jungle_early"]["all"]["n"], ag["enemy_jungle_early"]["all"]["of"]) == (1, 1)


def test_jungler_death_text_does_not_talk_about_the_enemy_jungler_as_a_gank():
    from test_deaths import game as dgame
    g = dgame("g1", False, [{"early": True}])
    d = g["deaths"][0]
    d.update(killer={"champion": "Viego", "role": "JUNGLE", "pid": 7}, assisters=[], place_phrase="en el río",
             level_diff=None, current_gold=0, after_own_kill=False, objectives_near=[], wards_allied_90s=0,
             control_ward_3min=False, damage_received={"total": 0})
    g["context"]["enemy_jungler"] = "Viego"
    d["enemy_jungler_involved"] = True
    g["role"] = "JUNGLE"
    assert not any("su jungla" in f for f in dx.describe_death(d, g))
    g["role"] = "BOTTOM"
    assert any("su jungla (Viego) participó" in f for f in dx.describe_death(d, g))


# ----------------------------------------------------------------------- CLI
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Yo#LAN\n", encoding="utf-8")
    store = Store(tmp_path / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    add_role_games(store, "UTILITY", 8, "S", champion="Soraka", challenges=SUPPORT_CHALLENGES)
    add_role_games(store, "JUNGLE", 3, "J", champion="Vi", challenges=JUNGLE_CHALLENGES, start=20)
    store.close()
    return tmp_path


def test_analyze_defaults_to_the_most_played_line(home, capsys):
    assert main(["analyze"]) == 0
    out = capsys.readouterr().out
    assert "Partidas analizadas: 8" in out and "Línea: Soporte (8 de 11 partidas)" in out
    summary = (home / "reports" / "resumen.md").read_text(encoding="utf-8")
    assert "Línea: **Soporte**" in summary and "CS/min" not in summary
    csv_text = (home / "data" / "games.csv").read_text(encoding="utf-8")
    assert csv_text.count("\n") == 12                                      # the CSV keeps every game
    assert len(list((home / "reports" / "partidas").glob("*.md"))) == 8


def test_analyze_line_options(home, capsys):
    assert main(["analyze", "--linea", "jungla"]) == 0
    assert "Partidas analizadas: 3" in capsys.readouterr().out
    assert "Objetivos épicos" in (home / "reports" / "resumen.md").read_text(encoding="utf-8")
    assert main(["analyze", "--linea", "todas"]) == 0
    out = capsys.readouterr().out
    assert "Partidas analizadas: 11" in out and "Línea: todas" in out
    summary = (home / "reports" / "resumen.md").read_text(encoding="utf-8")
    assert "Línea: **varias" in summary and "solo cuentan las partidas de carril" in summary
    with pytest.raises(SystemExit):
        main(["analyze", "--linea", "banca"])


def test_campeon_respects_the_line(home, capsys):
    assert main(["campeon", "Soraka"]) == 0
    assert "Wards puestos por minuto" in capsys.readouterr().out
    assert main(["campeon", "Vi"]) == 1
    assert "otras líneas: usa --linea todas" in capsys.readouterr().out
    assert main(["campeon", "Vi", "--linea", "jungla"]) == 0
    assert "CS de jungla antes del min 10" in capsys.readouterr().out


def test_game_command_judges_with_the_role_played(home, capsys):
    assert main(["game", "J_1"]) == 0
    out = capsys.readouterr().out
    assert "Jungla rival: RivalJungle" in out
    assert main(["game", "S_1"]) == 0
    assert "Soporte rival: RivalUtility" in capsys.readouterr().out
