"""Composiciones from the player's own line: the Línea selector, a support main and a jungle main."""
import pytest
from comps_fixtures import ALFA, BASE_TIME_MS, ENEMY, HOUR, JUNGLE_SCRIPT, SUPPORT_SCRIPT, build_records
from composiciones_golden import ME_RIOT_ID, add_script_games
from conftest import ME_PUUID, make_comp_match

from lolan import comps, web
from lolan.__main__ import main
from lolan.store import Store

ADC_ONLY_TEXTS = ("ADC (tú)", "Tu ADC × engage duro", "Matriz: tu ADC × soporte", "tú de ADC",
                  "No lo has jugado de ADC", "Incluir todos los ADC clasificados", "El marco de los ADC",
                  "Recomendación de ADC")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv("LOLAN_OPGG", "off")
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME_RIOT_ID}\n", encoding="utf-8")
    return tmp_path


def client_for(root, script):
    add_script_games(root, script)
    return web.create_app(root).test_client()


def text(resp):
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def add_queue_games(root, queue, role, count, first=0):
    """`count` games of `role` in `queue` next to a clan ADC / support (stored in the same DB)."""
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, ME_RIOT_ID, is_main=True)
    for i in range(count):
        blue = {role: (ME_PUUID, "Epsilon Me", "Lulu" if role == "UTILITY" else "Jinx"),
                "BOTTOM" if role == "UTILITY" else "UTILITY": (*ALFA, "Jinx" if role == "UTILITY" else "Lulu")}
        match_id = f"LA1_q{queue}_{role}_{first + i}"
        match = make_comp_match(match_id, blue=blue, win=i % 2 == 0, queue=queue, main_role=role,
                                created=BASE_TIME_MS + (first + i) * HOUR)
        store.save_match(match_id, match, None)
    store.close()


# ------------------------------------------------------------------- selector
def test_auto_line_is_the_most_played_one_of_each_queue(root):
    add_queue_games(root, 420, "BOTTOM", 5)
    add_queue_games(root, 440, "UTILITY", 6)
    add_queue_games(root, 440, "BOTTOM", 2, first=100)
    client = web.create_app(root).test_client()
    solo, flex, todas = (text(client.get(f"/composiciones?cola={c}")) for c in ("solo", "flex", "todas"))
    assert "Automática: ADC" in solo and "Tú en" not in solo and "tú de ADC" in solo
    assert "Automática: Soporte" in flex and "tú de soporte" in flex and "tú de ADC" not in flex
    assert "Automática: ADC" in todas        # 7 ADC games against 6 as support


def test_a_chosen_line_is_kept_in_links_and_marks_the_button(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex&linea=JUNGLE"))
    assert '<a href="/composiciones?cola=flex&amp;linea=JUNGLE" aria-current="true">Jungla</a>' in html
    assert 'aria-current="true">Automática: Soporte</a>' not in html
    # the choice travels with the queue switch, the navigation and the form, like the queue does
    assert 'href="/composiciones?cola=solo&amp;linea=JUNGLE"' in html
    assert 'href="/?cola=flex&amp;linea=JUNGLE"' in html
    assert 'name="linea" value="JUNGLE"' in html
    assert "Tú en jungla" in html and "tú de soporte" not in html


def test_auto_keeps_the_links_clean(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex"))
    assert 'href="/?cola=flex"' in html and 'name="linea"' not in html
    assert 'aria-current="true">Automática: Soporte</a>' in html


@pytest.mark.parametrize("value, expected", [("soporte", "UTILITY"), ("Jungla", "JUNGLE"), ("mid", "MIDDLE"),
                                             ("adc", "BOTTOM"), ("UTILITY", "UTILITY")])
def test_the_line_accepts_the_spanish_names_too(root, value, expected):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get(f"/composiciones?cola=flex&linea={value}"))
    assert f'href="/?cola=flex&amp;linea={expected}"' in html


@pytest.mark.parametrize("value", ["banana", "", "TOP;", "auto"])
def test_an_invalid_line_falls_back_to_auto(root, value):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get(f"/composiciones?cola=flex&linea={value}"))
    assert 'aria-current="true">Automática: Soporte</a>' in html and "tú de soporte" in html
    assert 'name="linea"' not in html and "banana" not in html


def test_the_selector_only_shows_on_pages_that_use_it(root):
    client = client_for(root, SUPPORT_SCRIPT)
    assert 'aria-label="Línea"' in text(client.get("/composiciones?cola=flex"))
    for path in ("/?cola=flex", "/clan?cola=flex", "/partidas?cola=flex", "/jugadores"):
        assert 'aria-label="Línea"' not in text(client.get(path)), path


def test_line_without_games_still_renders(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex&linea=TOP&armar=1"))
    assert "Tú en top" in html and "0 partidas" in html and "Top (tú)" in html


# ------------------------------------------------------------------ support main
def test_support_main_page_is_seen_from_the_support_seat(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex"))
    body = html[html.index("<main"):]
    for needle in ("21 como Soporte", "Dúo bot", "tú de soporte", "Por ADC (persona)", "Por campeón de ADC",
                   "Por arquetipo de ADC", "Matriz: tu soporte × ADC", "Qué soporte llevar según el ADC",
                   "Soporte (tú)", "Epsilon Alfa"):
        assert needle in body, needle
    for needle in ADC_ONLY_TEXTS + ("Itemización: penetración de armadura", "Tú en soporte"):
        assert needle not in body, needle
    # The duo table is the support's own results with each ADC (Alfa: 8-5 with Jinx/Kaisa).
    assert "8-5" in body[body.index("Por ADC (persona)"):body.index("Por campeón de ADC")]


def test_support_main_default_lineup_never_puts_someone_in_his_line(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex"))
    assert 'name="p_UTILITY"' not in html and 'name="c_UTILITY"' not in html
    for role, person in (("BOTTOM", "Epsilon Alfa"), ("JUNGLE", "Epsilon Beta"), ("MIDDLE", "Epsilon Gamma")):
        block = html[html.index(f'name="p_{role}"'):]
        assert f'<option value="{person}" selected>' in block[:block.index("</select>")]
    assert 'value="Epsilon Me"' not in html         # the main is not offered as a teammate


def test_support_main_draft_shows_his_own_picks_first(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex&armar=1&p_BOTTOM=Epsilon+Alfa"))
    assert "Tus picks y las otras líneas" in html and "<h3>Otras líneas</h3>" not in html
    assert "<h4>ADC (tú)</h4>" not in html and "Ningún ADC tiene aún" not in html
    mine = html.index('<h4>Soporte <span class="badge main">tú</span>')
    assert mine < html.index("<h4>Top") < html.index("<h4>ADC")
    own = html[mine:html.index("<h4>Top")]
    assert "<strong>Lulu</strong>" in own and "Lulu en Soporte contigo: 5-2" in own      # his own pool, not types
    # the ADC is a teammate: the clan ADC's pool, not a ranking for him
    adc = html[html.index("<h4>ADC"):]
    assert "<strong>Jinx</strong>" in adc and "Epsilon Alfa" in adc
    assert "todos" not in html[html.index('class="draft-form"'):html.index("Armar draft</button>")]


def test_support_main_draft_with_fixed_picks(root):
    client = client_for(root, SUPPORT_SCRIPT)
    html = text(client.get("/composiciones?cola=flex&armar=1&p_BOTTOM=Epsilon+Alfa&c_BOTTOM=Kaisa&c_TOP=ornn"))
    assert "Kaisa ya está escogido" in html and "Ornn ya está escogido" in html
    assert "Plan de pelea resultante" in html


# ------------------------------------------------------------------- jungle main
def test_jungle_main_page_shows_his_pairs_and_no_adc_sections(root):
    client = client_for(root, JUNGLE_SCRIPT)
    html = text(client.get("/composiciones?cola=flex"))
    body = html[html.index("<main"):]
    assert "Automática: Jungla" in html
    for needle in ("15 como Jungla", "Tú en jungla × mid", "Tú en jungla × top", "Jungla (tú)",
                   "Cómo se clasifican los campeones", "Itemización: penetración de armadura"):
        assert needle in body, needle
    for needle in ADC_ONLY_TEXTS + ("Dúo bot", "Matriz: tu soporte", "Por ADC (persona)"):
        assert needle not in body, needle
    pairs = body[body.index('id="tu-asiento"'):body.index('id="por-linea"')]
    assert "Carry/farm" in pairs and "Asesino/roamer" in pairs and "4-2" in pairs      # Viego + Ahri


def test_jungle_main_default_lineup_and_draft(root):
    client = client_for(root, JUNGLE_SCRIPT)
    html = text(client.get("/composiciones?cola=flex"))
    assert 'name="p_JUNGLE"' not in html
    for role, person in (("MIDDLE", "Epsilon Gamma"), ("BOTTOM", "Epsilon Alfa"), ("UTILITY", "Epsilon Beta")):
        block = html[html.index(f'name="p_{role}"'):]
        assert f'<option value="{person}" selected>' in block[:block.index("</select>")]
    draft = text(client.get("/composiciones?cola=flex&armar=1"))
    mine = draft.index('<h4>Jungla <span class="badge main">tú</span>')
    assert mine < draft.index("<h4>Top")
    assert "<strong>Viego</strong>" in draft[mine:draft.index("<h4>Top")]


def test_top_and_mid_get_their_pair_with_the_jungle(root):
    client = client_for(root, JUNGLE_SCRIPT)
    for line, title in (("TOP", "Tú en top × jungla"), ("MIDDLE", "Tú en mid × jungla")):
        html = text(client.get(f"/composiciones?cola=flex&linea={line}"))
        assert title in html and "Dúo bot" not in html and "Tú en top × mid" not in html


# ------------------------------------------------------------ engine and command
def test_default_lineup_comes_from_the_main_lines_games_only():
    records = build_records(SUPPORT_SCRIPT)
    lineup = comps.analyze_all(records, main_role="UTILITY")["common_lineup"]
    assert lineup == {"JUNGLE": "Epsilon Beta", "MIDDLE": "Epsilon Gamma", "BOTTOM": "Epsilon Alfa",
                      "UTILITY": "Epsilon Me"}
    # a line he never played has no lineup: another game's lineup would put people in his line
    assert comps.analyze_all(records, main_role="TOP")["common_lineup"] == {}
    only_jungle = [r for r in records if r["main_role"] == "JUNGLE"]
    assert comps.most_common_lineup(only_jungle, "UTILITY") == {}


def test_recommend_is_only_for_an_adc_main():
    records = build_records(SUPPORT_SCRIPT)
    assert comps.recommend({}, records, ENEMY, main_role="UTILITY") == []
    assert comps.recommend({}, records, ENEMY, main_role="BOTTOM") == comps.recommend({}, records, ENEMY)
    with pytest.raises(ValueError):
        comps.recommend({}, records, main_role="SUPPORT")


def test_analyze_all_computes_only_what_the_line_needs():
    records = build_records(SUPPORT_SCRIPT)
    adc = comps.analyze_all(records)
    assert adc["main_role"] == "BOTTOM" and adc["own_pairs"] == [] and adc["engage"] is not None
    sup = comps.analyze_all(records, main_role="UTILITY")
    assert sup["duo"]["games"] == 21 and sup["engage"] is None and sup["own_pairs"] == []
    assert [x["key"] for x in sup["duo"]["by_person"]][:2] == ["Aleatorio", "Epsilon Alfa"]
    assert sup["matrix"]["games"] == 21 and sup["own_games"] == 21
    jg = comps.analyze_all(records, main_role="JUNGLE")
    assert jg["duo"] is None and jg["matrix"] is None
    assert [p["key"] for p in jg["own_pairs"]] == ["jungle_middle", "jungle_top"] and jg["own_games"] == 2
    assert [p["key"] for p in comps.analyze_all(records, main_role="TOP")["own_pairs"]] == ["top_jungle"]


def test_line_parsing_and_resolution():
    assert comps.parse_line(None) == comps.parse_line("") == comps.parse_line("AUTO") == "auto"
    assert [comps.parse_line(x) for x in ("top", "jungla", "mid", "adc", "soporte")] == \
        ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    assert comps.parse_line("banana") is None
    records = build_records(SUPPORT_SCRIPT)
    assert comps.main_line(records) == "UTILITY" and comps.main_line([]) == "BOTTOM"
    tie = [r for r in records if r["main_role"] == "JUNGLE"][:1] + [r for r in records if r["main_role"] == "UTILITY"][:1]
    assert comps.main_line(tie) == "JUNGLE"         # a tie goes to the earlier line


def test_cli_support_main_prints_the_support_sections(root, capsys):
    add_script_games(root, SUPPORT_SCRIPT)
    assert main(["composiciones"]) == 0           # auto: his most played line
    out = capsys.readouterr().out
    for needle in ("21 como Soporte", "Dúo bot, por ADC", "Mejores campeones de ADC", "Matriz soporte x ADC",
                   "Soporte (tú) (Epsilon Me)", "Thresh"):
        assert needle in out, needle
    for needle in ("Recomendación de ADC", "Matriz ADC x soporte", "Dúo bot, por soporte", "Ningún ADC"):
        assert needle not in out, needle
    assert out.index("Soporte (tú)") < out.index("  Top:") < out.index("  ADC (Epsilon Alfa)")
    md = (root / "reports" / "composiciones.md").read_text(encoding="utf-8")
    assert "## Dúo bot (tú de soporte)" in md and "## Matriz soporte x ADC" in md and "como Soporte: 21" in md
    assert "## Recomendación de ADC" not in md and "Itemización" not in md and "- **Soporte (tú)**" in md


def test_cli_chosen_line_overrides_the_automatic_one(root, capsys):
    add_script_games(root, JUNGLE_SCRIPT)
    assert main(["composiciones", "--linea", "jungla"]) == 0
    out = capsys.readouterr().out
    assert "Tú en jungla × mid" in out and "Tú en jungla × top" in out and "Jungla (tú)" in out
    assert main(["composiciones", "--linea", "soporte"]) == 0
    assert "como Soporte" in capsys.readouterr().out
    md = (root / "reports" / "composiciones.md").read_text(encoding="utf-8")
    assert "## Dúo bot (tú de soporte)" in md and "(tú)" in md


def test_cli_rejects_an_unknown_line(root, capsys):
    add_script_games(root, SUPPORT_SCRIPT)
    with pytest.raises(SystemExit):
        main(["composiciones", "--linea", "banana"])
    assert "auto, top, jungla, mid, adc o soporte" in capsys.readouterr().err


def test_cli_help_is_not_adc_only(capsys):
    with pytest.raises(SystemExit):
        main(["composiciones", "--help"])
    out = capsys.readouterr().out
    assert "--linea" in out and "soporte" in out
