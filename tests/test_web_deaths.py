"""Panel pages with the deaths / fights / patterns sections."""
import re

import pytest
from test_ddragon import FakeCdn
from test_web import HOUR, add_games, root, text  # noqa: F401  (root is a fixture)

from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan import web
from lolan.ddragon import DDragon
from lolan.store import Store


@pytest.fixture
def client(root):  # noqa: F811
    add_games(root)
    return web.create_app(root).test_client()


def test_match_page_shows_deaths_fights_and_lost_objectives(client):
    html = text(client.get("/partida/LA1_3?cola=solo"))
    assert 'id="muertes"' in html and 'id="peleas"' in html and 'id="objetivos"' in html
    assert html.count('class="death"') == 2                         # the default timeline has 2 deaths of mine
    assert "Te mató Zed (Mid)" in html                              # opp champion of the synthetic match
    assert "aproximado: Riot no da la posición de los wards" in html
    assert 'id="muerte-1"' in html and "partida.js" in html
    assert "Ver esta pelea" not in html                              # single kills are not fights there
    assert "No se detectaron peleas" in html


def test_match_page_works_without_ddragon(client):
    html = text(client.get("/partida/LA1_3"))
    assert "mapa esquemático" in html or "Sin conexión" in html
    assert "<image" not in html                                      # no pictures when nothing is cached
    assert 'class="sm-bg"' in html                                   # schematic minimap instead


def test_match_page_without_timeline(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.save_match("LA1_9", make_match("LA1_9", created=BASE_TIME_MS), None)
    store.close()
    c = web.create_app(root).test_client()
    html = text(c.get("/partida/LA1_9"))
    assert "no tiene timeline" in html and 'id="muertes"' not in html
    assert c.get("/").status_code == 200
    assert "Patrones de tus muertes" not in text(c.get("/"))


def test_pages_with_a_ddragon_cache_show_icons_and_serve_them(root, tmp_path):
    add_games(root)
    dd = DDragon(root / "data" / "ddragon", fetcher=FakeCdn())
    dd.prepare(("14.5.1",), [f"Champ{i}" for i in range(1, 11)] + ["Ahri", "Zed"], wait=5)
    c = web.create_app(root, ddragon=dd).test_client()
    html = text(c.get("/partida/LA1_3"))
    assert '<image class="map-img" href="/ddragon/16.20.1/img/map/map11.png"' in html
    assert 'class="sm-bg"' not in html
    resp = c.get("/ddragon/16.20.1/img/map/map11.png")
    assert resp.status_code == 200 and resp.data.startswith(b"\x89PNG")
    assert c.get("/ddragon/16.20.1/img/champion/Nope.png").status_code == 404
    assert c.get("/ddragon/16.20.1/data/es_MX/item.json").status_code == 404
    assert c.get("/ddragon/..%2Fversions.json").status_code == 404
    assert "/ddragon/16.20.1/img/map/map11.png" in text(c.get("/"))


def test_summary_patterns_and_plan(client):
    html = text(client.get("/?cola=solo"))
    assert "Patrones de tus muertes" in html and "Mapa de calor de tus muertes" in html
    assert "Contra qué junglas" in html and "Contra qué dúos de bot" in html
    assert 'id="plan"' in html and "Se necesitan al menos 8 partidas" in html     # only 6 solo games
    assert "En victorias" in html and "En derrotas" in html
    assert '<svg class="minimap heat-map"' in html
    # 6 games x 2 deaths
    assert "12 muertes" in html


def test_heatmap_filters_and_links_keep_the_queue(client):
    base = text(client.get("/?cola=solo"))
    early = text(client.get("/?cola=solo&fase=antes14&res=derrotas"))
    n_all = int(re.search(r"(\d+) muertes\. Más rojo", base).group(1))
    n_filtered = int(re.search(r"(\d+) muertes\. Más rojo", early).group(1))
    assert 0 < n_filtered < n_all
    assert "fase=despues14" in early and "cola=solo" in early
    assert 'aria-current="true">Antes del minuto 14' in early and 'aria-current="true">Solo derrotas' in early
    junk = client.get("/?fase=zzz&res=%3Cb%3E")
    assert junk.status_code == 200 and "<b>" not in text(junk).split("Mapa de calor")[1]


def test_plan_renders_with_enough_games(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    for i in range(12):
        win = i % 2 == 0
        events = [{"type": "CHAMPION_KILL", "timestamp": 600_000 + n * 20_000, "killerId": 8, "victimId": 3,
                   "assistingParticipantIds": [], "position": {"x": 6000, "y": 6000}}
                  for n in range(0 if win else 3 + (i // 2) % 2)]
        store.save_match(f"LA1_{i}", make_match(f"LA1_{i}", win=win, created=BASE_TIME_MS + i * 3 * HOUR),
                         make_timeline(events=events))
    store.close()
    html = text(web.create_app(root).test_client().get("/"))
    assert "Tus 1 prioridades" in html or "Tus 2 prioridades" in html or "Tus 3 prioridades" in html
    assert 'class="chk ok"' in html and 'class="chk no"' in html
    assert "/partida/LA1_11?cola=solo#muertes" in html


def test_other_player_pages_use_their_own_data(root):
    add_games(root)
    store = Store(root / "data" / "lol.db")
    store.add_player("puuid-4", "Amigo#LAN")
    store.close()
    c = web.create_app(root).test_client()
    html = text(c.get("/?jugador=amigo"))
    assert "Patrones de tus muertes" in html           # puuid-4 is on the same synthetic matches
    assert c.get("/partida/LA1_3?jugador=amigo").status_code == 200
