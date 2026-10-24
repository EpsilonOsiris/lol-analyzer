"""Aggregate pages by line (Resumen, Campeón, Partidas, Clan) for a support main who also plays jungle."""
import re

import pytest
from conftest import ME_PUUID
from line_fixtures import JUNGLE_CHALLENGES, SUPPORT_CHALLENGES, add_role_games

from lolan import web
from lolan.store import Store

FRIEND = "puuid-friend"


def text(resp):
    return resp.get_data(as_text=True)


def main_block(html):
    return html[html.index("<main"):html.index("</main>")]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv("LOLAN_OPGG", "off")
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Yo#LAN\n", encoding="utf-8")
    store = Store(tmp_path / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player(FRIEND, "Amigo Fiel#LAN")
    add_role_games(store, "UTILITY", 10, "S", champion="Soraka", challenges=SUPPORT_CHALLENGES,
                   vary=lambda i: {"fields": {"visionScore": 40 + 5 * i}})
    add_role_games(store, "JUNGLE", 4, "J", champion="Vi", challenges=JUNGLE_CHALLENGES, start=30)
    add_role_games(store, "MIDDLE", 2, "M", champion="Ahri", start=40, challenges=None)
    # the friend is a laner (mid) in their own games
    add_role_games(store, "MIDDLE", 6, "F", champion="Ahri", puuid=FRIEND, start=50, challenges=None)
    store.close()
    return tmp_path


@pytest.fixture
def client(root):
    return web.create_app(root).test_client()


def test_support_main_summary_is_seen_as_a_support(client):
    html = main_block(text(client.get("/")))
    assert "Línea: <strong>Soporte</strong> · 10 de 16 partidas" in html
    assert "Visión/min" in html and "Curación y escudos/min" in html and "Particip. en kills" in html
    assert "CS/min" not in html and "Dif. oro @14" not in html
    assert "fueron en la jungla enemiga" not in html and "participó el jungla enemigo" in html
    # the selector offers the line the page resolved and "Todas"
    head = text(client.get("/"))
    assert "Automática: Soporte" in head and 'linea=todas' in head


def test_jungle_line_shows_the_jungle_stats_and_invasion_deaths(client):
    html = main_block(text(client.get("/?linea=JUNGLE")))
    assert "Línea: <strong>Jungla</strong> · 4 de 16 partidas" in html
    assert "CS de jungla @10" in html and "Objetivos épicos" in html and "CS/min" not in html
    assert "participó el jungla enemigo" not in html
    # links keep the chosen line
    assert "linea=JUNGLE" in html


def test_all_lines_hide_line_specific_numbers_and_say_so(client):
    html = main_block(text(client.get("/?linea=todas")))
    assert "16 de 16 partidas" in html and "solo cuentan las partidas de carril" in html
    assert "participó el jungla enemigo" not in html and "fueron en la jungla enemiga" not in html
    by_role = html[html.index("Por rol"):html.index("Después de una victoria")]
    rows = re.findall(r"<tr>\s*<td>(\w+)</td>(.*?)</tr>", by_role, re.S)
    cells = {name: re.findall(r'data-v="([^"]*)"', body) for name, body in rows}
    header = re.findall(r"<th[^>]*>([^<]+)</th>", by_role)
    cs_col = header.index("CS/min") - 1                     # data-v cells start at Partidas
    assert cells["Soporte"][cs_col] == "None" and cells["Mid"][cs_col] != "None"
    assert cells["Jungla"][cs_col] == "None"


def test_unknown_or_empty_line_choice_falls_back_to_auto(client):
    for value in ("banana", ""):
        html = main_block(text(client.get(f"/?linea={value}")))
        assert "Línea: <strong>Soporte</strong>" in html and "banana" not in html


def test_line_without_games_has_its_own_message(client):
    resp = client.get("/?linea=TOP")
    html = text(resp)
    assert resp.status_code == 200 and "No hay partidas de esta línea" in html and "linea=todas" in html
    assert "No hay partidas de esta línea" in text(client.get("/partidas?linea=TOP"))


def test_games_list_filters_and_adapts_columns(client):
    html = main_block(text(client.get("/partidas")))
    assert "Mostrando 1–10 de 10" in html and ">Soporte<" in html and ">Jungla<" not in html
    assert "Visión/min" in html and "Curación y escudos/min" in html and "CS/min" not in html
    html = main_block(text(client.get("/partidas?linea=todas")))
    assert "Mostrando 1–16 de 16" in html and "CS/min" in html
    cells = re.findall(r"<td>Soporte</td>.*?</tr>", html, re.S)[0]
    assert ">-<" in cells                          # a support's row has no CS/min


def test_champion_page_by_line(client):
    html = main_block(text(client.get("/campeon/Soraka")))
    assert "Wards puestos por minuto" in html and "Curación y escudos por minuto" in html
    assert ">CS por minuto<" not in html and "Dif. de CS @14" not in html
    # Vi is jungle: the support view says where the games are instead of showing nothing
    html = main_block(text(client.get("/campeon/Vi")))
    assert "como Soporte" in html and "otras líneas" in html and "linea=todas" in html
    html = main_block(text(client.get("/campeon/Vi?linea=JUNGLE")))
    assert "CS de jungla antes del min 10" in html and '>CS por minuto<' not in html
    assert text(client.get("/campeon/Nadie")).count("No hay partidas con el campeón") == 1


def test_match_page_uses_the_role_played_whatever_the_line_choice(client):
    for suffix in ("", "?linea=todas", "?linea=TOP"):
        html = main_block(text(client.get("/partida/S_3" + suffix)))
        assert "Soporte rival" in html and "Wards puestos por minuto" in html and "CS por minuto" not in html
        assert "Solo informativo" in html            # the damage rows
    html = main_block(text(client.get("/partida/J_1")))
    assert "Jungla rival" in html and "CS de jungla antes del min 10" in html
    assert "no se compara con otras líneas" in html


def test_clan_table_follows_each_players_line(client):
    html = main_block(text(client.get("/clan")))
    assert "Métrica de su línea" in html and "Curación y escudos/min:" in html
    assert "su línea más jugada" in html
    rows = dict(re.findall(r"<tr>\s*<td><a[^>]*>([^<]+)</a>.*?</tr>", html, re.S) and
                [(m[0], m[1]) for m in re.findall(r"<tr>\s*<td><a[^>]*>([^<]+)</a>(.*?)</tr>", html, re.S)])
    mine = re.findall(r'data-v="([^"]*)"', rows["Yo#LAN"])
    friend = re.findall(r'data-v="([^"]*)"', rows["Amigo Fiel#LAN"])
    header = re.findall(r"<th[^>]*>([^<]+)</th>", html[:html.index("</thead>")])
    cs = header.index("CS/min") - 1
    assert mine[cs] == "None" and friend[cs] != "None"
    # an explicit line applies to everyone
    html = main_block(text(client.get("/clan?linea=MIDDLE")))
    assert "la línea Mid" in html


def test_selector_has_todas_only_on_aggregate_pages(client):
    assert 'linea=todas' in text(client.get("/partidas"))
    comps = text(client.get("/composiciones?linea=todas"))
    assert 'linea=todas' not in comps and 'name="linea"' not in comps
    assert 'aria-label="Línea"' in comps


def test_line_choice_travels_in_links(client):
    html = main_block(text(client.get("/partidas?linea=todas")))
    assert re.search(r'href="/partida/S_\d+\?[^"]*linea=todas', html)
    sel = text(client.get("/?linea=JUNGLE&cola=todas"))
    assert 'aria-current="true">Jungla</a>' in sel


def test_laner_friend_page_is_a_plain_laner_page(client):
    html = main_block(text(client.get("/?jugador=amigo-fiel")))
    assert "CS/min" in html and "Línea:" not in html
