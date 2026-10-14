"""/composiciones page, the clan settings in Configuración and the `composiciones` command."""
import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_comp_match, make_timeline, set_final_stats

from lolan import web
from lolan.__main__ import main
from lolan.store import Store

HOUR = 3600_000
HEADERS = {"X-Lolan": "1"}
ALFA = ("p-alfa", "Epsilon Alfa")
BETA = ("p-beta", "Epsilon Beta")


def add_comp_games(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Epsilon Me#LAN", is_main=True)
    plan = [("Jinx", True)] * 4 + [("Jinx", False)] * 2 + [("Vayne", True)] * 3 + [("Kaisa", False)] * 3
    for i, (adc, win) in enumerate(plan):
        blue = {"BOTTOM": (ME_PUUID, "Epsilon Me", adc),
                "UTILITY": (*ALFA, "Taric" if i % 3 else "Leona"),
                "JUNGLE": (*BETA, "Sejuani")}
        match = make_comp_match(f"LA1_{i}", blue=blue, win=win, created=BASE_TIME_MS + i * HOUR,
                                enemy_cc=40 + 10 * i, spells=(1, 4) if i % 2 else (4, 14))
        tl = make_timeline(minutes=25)
        set_final_stats(tl, 6, armor=250 if i % 2 else 100)
        set_final_stats(tl, 4, armorPenPercent=30 if i % 3 == 0 else 0)
        store.save_match(f"LA1_{i}", match, tl)
    store.close()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Epsilon Me#LAN\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(root):
    add_comp_games(root)
    return web.create_app(root).test_client()


@pytest.fixture
def empty_client(root):
    return web.create_app(root).test_client()


def text(resp):
    return resp.get_data(as_text=True)


# ------------------------------------------------------------------------- page
def test_page_renders_with_data(client):
    r = client.get("/composiciones?cola=flex")
    assert r.status_code == 200
    html = text(r)
    for needle in ("Armar draft", "Pools del clan", "Dúo bot", "Matriz", "Alineaciones del clan",
                   "penetración de armadura", "Purificar", "Epsilon Alfa", "no son del video"):
        assert needle in html
    assert 'aria-current="page">Composiciones' in html
    # The form is pre-filled with the most common lineup of the clan.
    assert '<option value="Epsilon Alfa" selected>' in html
    assert '<option value="Epsilon Beta" selected>' in html


def test_page_shows_lines_pairs_and_plans(client):
    html = text(client.get("/composiciones?cola=flex"))
    for needle in ('id="por-linea"', "tus aliados, sin contarte", 'id="duplas"', "Jungla × Mid",
                   "Jungla × Top", "Soporte × ADC", 'id="plan-de-pelea"', "Tu plan × plan rival",
                   "Tanque/engage", "son propias", "EarlyGame", "no son datos oficiales de Riot"):
        assert needle in html, needle
    # Sejuani is the only classified ally in the jungle: 12 games, and Beta appears as a person there.
    assert "Epsilon Beta" in html[html.index('id="por-linea"'):html.index('id="duplas"')]
    # Plan distribution: the table of the team plans lists at least one plan label with its games.
    assert "Proteger al carry" in html[html.index('id="plan-de-pelea"'):]


def test_page_draft_recommends_every_line(client):
    r = client.get("/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa&p_JUNGLE=Epsilon+Beta&c_TOP=ornn")
    html = text(r)
    assert r.status_code == 200
    for needle in ("Otras líneas", "Ornn ya está escogido", "Plan de pelea resultante", "ADC (tú)"):
        assert needle in html, needle
    # Beta's jungle pool (Sejuani) is suggested as a champion; the random mid as types only.
    assert "<strong>Sejuani</strong>" in html and "Tipo: Control/artillería" in html
    assert "Sin persona del clan ni campeón" in html


def test_draft_defaults_prefill_and_recommend_types_for_random_lines(client):
    html = text(client.get("/composiciones?cola=flex&armar=1"))
    assert "Tipo: " in html and "Plan de pelea resultante" in html
    assert text(client.get("/composiciones?cola=flex")).count("Otras líneas") == 0


def test_match_page_shows_both_team_plans(client):
    r = client.get("/partida/LA1_0?cola=flex")
    html = text(r)
    assert r.status_code == 200
    assert 'id="plan-pelea"' in html and "Plan de pelea: tu equipo" in html
    assert "Clasificación propia" in html


def test_nav_link_after_clan(client):
    html = text(client.get("/composiciones?cola=todas"))
    assert html.index(">Clan</a>") < html.index(">Composiciones</a>") < html.index(">Jugadores</a>")


def test_page_without_data_is_200(empty_client):
    r = empty_client.get("/composiciones")
    assert r.status_code == 200
    assert "Sincronizar" in text(r)


def test_page_with_data_in_another_queue_is_200(client):
    r = client.get("/composiciones")          # default queue (solo) has no games
    assert r.status_code == 200
    assert "No hay partidas en esta cola" in text(r)


def test_page_with_draft_params(client):
    r = client.get("/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa&c_UTILITY=taric"
                   "&c_JUNGLE=Sejuani&p_JUNGLE=&enemigos=Zed,+Leona&todos=1")
    html = text(r)
    assert r.status_code == 200
    assert "Recomendación" in html and "rec-list" in html
    assert "<strong>Vayne</strong>" in html and "<strong>Ashe</strong>" in html          # todos=1 adds the framework's ADCs
    assert "Taric" in html and 'value="Zed, Leona"' in html


def test_draft_default_pool_only(client):
    html = text(client.get("/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa"))
    assert "rec-list" in html and "<strong>Ashe</strong>" not in html and "<strong>Jinx</strong>" in html


def test_draft_with_garbage_params_does_not_break(client):
    r = client.get("/composiciones?cola=flex&armar=1&p_UTILITY=nadie&c_TOP=" + "x" * 500 + "&enemigos=,,;;")
    assert r.status_code == 200


def test_draft_not_submitted_shows_no_recommendation(client):
    assert "rec-list" not in text(client.get("/composiciones?cola=flex"))


def test_queue_switch_is_kept_in_links(client):
    html = text(client.get("/composiciones?cola=todas"))
    assert 'name="cola" value="todas"' in html


# -------------------------------------------------------------------- settings
def test_config_page_shows_clan_fields(client):
    html = text(client.get("/configuracion"))
    assert 'name="clan_prefijo"' in html and 'name="clan_alias"' in html
    assert 'value="Epsilon"' in html


def test_config_saves_clan_settings_and_page_uses_them(client, root):
    body = {"riot_id": "Epsilon Me#LAN", "clan_prefijo": "Epsilon", "clan_alias": "Epsilon Beta=Epsilon Alfa"}
    r = client.post("/api/configuracion", json=body, headers=HEADERS)
    assert r.status_code == 200
    env = (root / ".env").read_text(encoding="utf-8")
    assert "CLAN_ALIASES='Epsilon Beta=Epsilon Alfa'" in env or "CLAN_ALIASES=Epsilon Beta=Epsilon Alfa" in env
    html = text(client.get("/composiciones?cola=flex"))
    # Beta is now an alias of Alfa: one person fewer in the draft form.
    assert '<option value="Epsilon Beta"' not in html and '<option value="Epsilon Alfa"' in html


def test_config_rejects_bad_alias_and_keeps_env(client, root):
    before = (root / ".env").read_text(encoding="utf-8")
    r = client.post("/api/configuracion",
                    json={"riot_id": "Epsilon Me#LAN", "clan_alias": "sin igual"}, headers=HEADERS)
    assert r.status_code == 400 and "Alias" in r.get_json()["error"]
    assert (root / ".env").read_text(encoding="utf-8") == before


def test_config_blank_prefix_falls_back_to_default(client, root):
    r = client.post("/api/configuracion", json={"riot_id": "Epsilon Me#LAN", "clan_prefijo": ""},
                    headers=HEADERS)
    assert r.status_code == 200
    assert web.read_settings(root).clan_prefix == "Epsilon"


# -------------------------------------------------------------------------- CLI
def test_cli_composiciones_prints_summary_and_writes_report(root, capsys):
    add_comp_games(root)
    assert main(["composiciones"]) == 0
    out = capsys.readouterr().out
    assert "Dúo bot" in out and "Matriz ADC x soporte" in out and "Recomendación de ADC" in out
    report = root / "reports" / "composiciones.md"
    assert report.exists()
    md = report.read_text(encoding="utf-8")
    assert "## Pools del clan" in md and "## Recomendación de ADC" in md
    for header in ("## Por línea", "## Duplas de tipos", "## Plan de pelea", "## Recomendación por línea",
                   "## Sobre las clasificaciones"):
        assert header in md
    for needle in ("Por línea (WR", "Duplas Jungla × Mid", "Plan de pelea de tu equipo",
                   "Recomendación por línea (alineación más común)"):
        assert needle in out


def test_cli_composiciones_by_queue_goes_to_its_folder(root, capsys):
    add_comp_games(root)
    assert main(["composiciones", "--cola", "flex"]) == 0
    assert (root / "reports" / "flex" / "composiciones.md").exists()
    assert main(["composiciones", "--cola", "solo"]) == 0
    assert "No hay partidas analizables" in capsys.readouterr().out


def test_cli_composiciones_empty_db(root, capsys):
    assert main(["composiciones"]) == 0
    assert "No hay partidas" in capsys.readouterr().out


def test_cli_composiciones_all_adc_flag_still_works(root, capsys):
    add_comp_games(root)
    assert main(["composiciones", "--todos-los-adc"]) == 0
    out = capsys.readouterr().out
    assert "Recomendación de ADC" in out and "  4. " in out and "Recomendación por línea" in out
