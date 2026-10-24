"""The En vivo page by role: laner (unchanged), jungle ("jungla rival", objectives) and support (vision, KP, duo)."""
import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_fixtures import JUNGLE_ME, SUPPORT_ME, as_viewer, ev, make_allgamedata, with_events

from lolan import goldmodel, live, livectx

TESTS = Path(__file__).resolve().parent
STATIC = TESTS.parent / "lolan" / "static"
GOLDEN = TESTS / "fixtures" / "envivo_laner_golden.json"
PRICES = {1055: 450, 2003: 50, 3340: 0, 1001: 300, 3020: 1100, 1056: 400, 3006: 1100, 1038: 1300, 2055: 75, 3850: 400}
node = shutil.which("node")


def game(champion="Jinx", role="BOTTOM", cs7=50, cs14=100, cs21=150, gold14=200, cpm=7.0, kp=0.6, vpm=0.8):
    return {"champion": champion, "role": role, "cs_7": cs7, "cs_14": cs14, "cs_21": cs21, "gold_diff_14": gold14,
            "cs_per_min": cpm, "kp": kp, "vision_per_min": vpm, "match_id": f"M{id(object())}"}


def laner_games():
    return [game(cs14=100 + i) for i in range(4)] + [game("Ezreal", cs14=90 + i) for i in range(3)]


def support_games():
    return ([game("Lulu", "UTILITY", cs7=8, cs14=20, cs21=30, gold14=-50, cpm=1.0, kp=0.7 + 0.01 * i, vpm=2.0 + 0.2 * i)
             for i in range(4)] + [game("Thresh", "UTILITY", cpm=1.0, kp=0.5, vpm=3.0) for i in range(3)])


def jungle_games():
    return ([game("Sejuani", "JUNGLE", cs7=45, cs14=95, cs21=140, gold14=150, cpm=5.5, kp=0.6 + 0.02 * i) for i in range(4)]
            + [game("LeeSin", "JUNGLE", cs14=90) for i in range(3)])


def payload(raw, games, mode_state=None):
    st = live.normalize(raw)
    st["me"]["checkpoints"] = {"7": {"cs": 55, "clock": "7:01"}}
    out = live.enrich(st, item_cost=PRICES.get, prices_available=True, gold_model=goldmodel.GoldModel(),
                      champion_icon=lambda c: f"/i/{c}.png", item_icon=lambda i: f"/it/{i}.png")
    ctx = livectx.LiveContext(lambda: SimpleNamespace(games=games, total=10, analyses={}), clock=lambda: 0.0)
    return {"status": "playing", "version": 1, "updated": 0, "problem": None, "game": out,
            "context": ctx.build(out), "scout": None}


def render(pl):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "payload.json"
        path.write_text(json.dumps(pl), encoding="utf-8")
        out = subprocess.run([node, str(TESTS / "envivo_harness.js"), str(STATIC / "envivo.js"), str(path)],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(node is None, reason="Node no está instalado")


# ------------------------------------------------------------ laner: regression against the golden
ADDED_TOTALS = {"grubs"}
ADDED_ME = {"kp", "team_kills"}
ADDED_PLAYER = {"diff_scope", "duo_gold_diff", "duo_value_diff", "duo_opponents"}
ADDED_CONTEXT = {"role", "role_source"}


def without_additions(pl):
    out = copy.deepcopy(pl)
    for role in ("ally", "enemy"):
        for k in ADDED_TOTALS:
            out["game"]["teams"][role]["totals"].pop(k, None)
        for p in out["game"]["teams"][role]["players"]:
            for k in ADDED_PLAYER:
                p.pop(k, None)
    for k in ADDED_ME:
        out["game"]["me"].pop(k, None)
    for k in ADDED_CONTEXT:
        out["context"].pop(k, None)
    return out


def golden_games():
    pool = [game(cs7=50 + i, cs14=100 + i, cs21=150 + i, gold14=100 + 10 * i) for i in range(4)]
    return pool + [game("Ezreal", "BOTTOM", cs14=90 + i) for i in range(3)] + [game("Ahri", "MIDDLE", cs14=80 + i) for i in range(3)]


def test_a_laner_payload_only_gains_keys():
    """Everything the page read for a laner is still there with the same values (plus new keys)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["payload"]
    now = payload(make_allgamedata(), golden_games())
    assert without_additions(now) == golden
    assert now["context"]["role"] == "BOTTOM" and now["context"]["role_source"] == "juego"
    assert "kp" not in now["context"] and "vision" not in now["context"]       # only jungle and support get those


@needs_node
def test_a_laner_page_is_byte_for_byte_the_golden():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert render(payload(make_allgamedata(), golden_games())) == golden["html"]


# --------------------------------------------------------------------------- the game side
def test_normalize_counts_grubs_and_kill_participation():
    raw = with_events(make_allgamedata(), ev(20, "HordeKill", 400.0, KillerName="Lince Gris", Assisters=[]),
                      ev(21, "HordeKill", 410.0, KillerName="Gato Negro", Assisters=[]),
                      ev(22, "HordeKill", 420.0, KillerName="Lince Gris", Assisters=[]))
    st = live.normalize(raw)
    assert st["teams"]["ally"]["totals"]["grubs"] == 2 and st["teams"]["enemy"]["totals"]["grubs"] == 1
    assert not any("larva" in e["text"].lower() or "horde" in e["text"].lower() for e in st["feed"])
    # blue team kills: 2 + 1 + 3 + 5 + 0 = 11; Jinx has 5 kills and 4 assists
    me = st["me"]
    assert me["team_kills"] == 11 and me["kp"] == round(9 / 11, 3)
    assert live.normalize(as_viewer(make_allgamedata(), SUPPORT_ME))["me"]["kp"] == round(9 / 11, 3)   # 0 + 9


def test_enrich_gives_the_duo_difference_to_the_bot_lane_only():
    out = payload(make_allgamedata(), [])["game"]
    ally = {p["position"]: p for p in out["teams"]["ally"]["players"]}
    enemy = {p["position"]: p for p in out["teams"]["enemy"]["players"]}
    duo_gold = ally["BOTTOM"]["gold_est"] + ally["UTILITY"]["gold_est"] - enemy["BOTTOM"]["gold_est"] - enemy["UTILITY"]["gold_est"]
    duo_value = ally["BOTTOM"]["value"] + ally["UTILITY"]["value"] - enemy["BOTTOM"]["value"] - enemy["UTILITY"]["value"]
    for pos in ("BOTTOM", "UTILITY"):
        assert ally[pos]["duo_gold_diff"] == duo_gold and ally[pos]["duo_value_diff"] == duo_value
        assert enemy[pos]["duo_gold_diff"] == -duo_gold and enemy[pos]["duo_value_diff"] == -duo_value
        assert sorted(ally[pos]["duo_opponents"]) == ["Kai'Sa", "Thresh"]
    assert ally["TOP"]["duo_gold_diff"] is None and ally["JUNGLE"]["duo_opponents"] is None
    assert ally["UTILITY"]["diff_scope"] == "duo" and ally["JUNGLE"]["diff_scope"] == "jungle"
    assert ally["TOP"]["diff_scope"] == "lane" and ally["BOTTOM"]["diff_scope"] == "lane"
    assert ally["JUNGLE"]["lane_opponent"] == "Lee Sin"           # the existing field is the enemy jungler


def test_no_duo_difference_when_the_positions_are_unknown():
    raw = make_allgamedata()
    for p in raw["allPlayers"]:
        p["position"] = ""
    out = payload(raw, [])["game"]
    for p in out["teams"]["ally"]["players"]:
        assert p["duo_gold_diff"] is None and p["diff_scope"] is None


# -------------------------------------------------------------------------- the personal side
def test_history_role_prefers_the_champion_then_the_most_played():
    pool = support_games() + jungle_games() + [game("Garen", "TOP")] * 2
    assert livectx.history_role(pool, "Lulu") == "UTILITY"
    assert livectx.history_role(pool, "Sejuani") == "JUNGLE"
    assert livectx.history_role(pool, "Teemo") in ("UTILITY", "JUNGLE")         # the most played overall
    assert livectx.history_role([game(), game()], "Jinx") is None               # too little history


def test_strict_baselines_never_mix_in_other_roles():
    pool = laner_games() + support_games()
    base = livectx.baselines(pool, "Lulu", "UTILITY", "Lulu", "Soporte", strict=True)
    assert base["scope"] == "champion" and base["games"] == 4 and base["cs"][14] == 20
    assert base["vision_per_min"] == pytest.approx(2.3) and base["kp"] == pytest.approx(0.715)
    base = livectx.baselines(pool, "Karma", "UTILITY", "Karma", "Soporte", strict=True)    # never played her
    assert base["scope"] == "role" and base["games"] == 7
    assert livectx.baselines(laner_games(), "Lulu", "UTILITY", strict=True) is None        # no support games
    assert livectx.baselines(laner_games(), "Lulu", "UTILITY") is not None                 # the lax mode would use all


def test_a_support_gets_vision_and_kill_participation_not_a_cs_reference():
    pl = payload(as_viewer(make_allgamedata(), SUPPORT_ME), support_games())
    ctx = pl["context"]
    assert ctx["role"] == "UTILITY" and ctx["role_source"] == "juego"
    v = ctx["vision"]
    assert v["now"] == 29.0 and v["rival"] == {"champion_name": "Thresh", "ward_score": 31.0}
    assert v["approx"] is True and v["has_base"] and v["scope_label"] == "con Lulu"
    minutes = pl["game"]["time"] / 60
    assert v["reference"] == round(2.3 * minutes, 1) and v["avg_per_min"] == 2.3
    assert ctx["kp"]["now"] == round(9 / 11, 3) and ctx["kp"]["avg"] == pytest.approx(0.715)


def test_a_jungler_gets_kill_participation_and_jungle_only_averages():
    pl = payload(as_viewer(make_allgamedata(), JUNGLE_ME), laner_games() + jungle_games())
    ctx = pl["context"]
    assert ctx["role"] == "JUNGLE" and "vision" not in ctx
    assert ctx["cs"]["scope_label"] == "con Sejuani" and ctx["cs"]["games"] == 4
    assert ctx["kp"]["now"] == round(6 / 11, 3) and ctx["kp"]["avg"] == pytest.approx(0.63)
    # without jungle games of his own there is no jungle reference (the laner games must not stand in)
    none = payload(as_viewer(make_allgamedata(), JUNGLE_ME), laner_games())["context"]
    assert none["cs"]["has_base"] is False and not none["kp"]["has_base"]


def test_the_role_comes_from_the_history_when_the_game_gives_none():
    raw = as_viewer(make_allgamedata(), SUPPORT_ME)
    for p in raw["allPlayers"]:
        p["position"] = ""
    ctx = payload(raw, support_games())["context"]
    assert ctx["role"] == "UTILITY" and ctx["role_source"] == "historial" and "vision" in ctx
    nothing = payload(raw, [])["context"]
    assert nothing["role"] is None and nothing["role_source"] is None
    raw["gameData"]["gameMode"] = "ARAM"                       # no roles in ARAM: do not guess
    assert payload(raw, support_games())["context"]["role"] is None


# --------------------------------------------------------------------------------- the page
@needs_node
def test_a_support_sees_vision_kp_and_the_duo_not_cs():
    pl = payload(as_viewer(make_allgamedata(), SUPPORT_ME), support_games())
    html = render(pl)
    me, ctx = html["live-me"], html["live-ctx"]
    assert "<span>Visión</span><b>29 <small title=\"Thresh\">rival 31</small>" in me
    assert "Participación en kills" in me and "Dúo de línea frente a" in me
    assert "<span>CS</span>" not in me and "Visión por minuto" in me
    assert "visión 29" in ctx and "soporte rival (Thresh)" in ctx and "Aproximado" in ctx
    assert "Participación en kills ahora: <b>82%</b>" in ctx and "tu media con Lulu" in ctx
    assert "CS" not in ctx and "Dif. de oro" not in ctx
    assert "rival de línea" not in me + ctx
    # table: the supports' difference is the duo's, labelled so; the ADC row keeps the lane difference
    assert html["live-ally"].count("Dúo de línea (ADC + soporte) frente al dúo rival (Kai&#39;Sa + Thresh)") == 2
    assert html["live-enemy"].count("Dúo de línea (ADC + soporte) frente al dúo rival") == 2
    assert "Frente a su rival de línea (Kai&#39;Sa)" in html["live-ally"]


@needs_node
def test_a_support_without_history_still_sees_the_live_numbers():
    html = render(payload(as_viewer(make_allgamedata(), SUPPORT_ME), laner_games()))
    assert "visión 29" in html["live-ctx"] and "de soporte" in html["live-ctx"]
    assert "tu visión media" not in html["live-ctx"]


@needs_node
def test_a_jungler_sees_the_enemy_jungler_objectives_and_kp():
    raw = with_events(as_viewer(make_allgamedata(), JUNGLE_ME),
                      ev(20, "HordeKill", 400.0, KillerName="Lince Gris", Assisters=[]))
    html = render(payload(raw, laner_games() + jungle_games()))
    me, ctx = html["live-me"], html["live-ctx"]
    assert "Jungla rival · Lee Sin" in me and "Objetivos de tu equipo" in me and "Larvas <b>1</b>" in me
    assert "Dragones <b>1</b> (Infernal)" in me and "Participación en kills" in me
    assert "Larvas" not in me.split("Objetivos del equipo rival")[1]            # the rival took none
    assert "frente a la jungla rival" in ctx and "Participación en kills ahora" in ctx
    assert "rival de línea" not in me + ctx
    assert "Frente a la jungla rival (Lee Sin)" in html["live-ally"] and "Frente a la jungla rival (Sejuani)" in html["live-enemy"]


@needs_node
def test_the_history_role_is_announced_on_the_page():
    raw = as_viewer(make_allgamedata(), JUNGLE_ME)
    for p in raw["allPlayers"]:
        p["position"] = ""
    html = render(payload(raw, laner_games() + jungle_games()))
    assert "El juego no dijo tu posición" in html["live-ctx"] and "Jungla" in html["live-ctx"]
