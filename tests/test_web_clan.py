"""Web panel with several players: picker, /clan, /jugadores, sync targets."""
import re

import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan import web
from lolan.riot import InvalidApiKeyError, NotFoundError
from lolan.store import Store

HOUR = 3600_000
HEADERS = {"X-Lolan": "1"}
FRIEND = "puuid-4"          # blue teammate of the main player in the A games
THIRD = "puuid-5"           # blue; only in the first two A games
RIVAL = "puuid-8"           # red; plays against the main player in the A games


def swap_puuid(match, old, new):
    for p in match["info"]["participants"]:
        if p["puuid"] == old:
            p["puuid"] = new
    match["metadata"]["participants"] = [p["puuid"] for p in match["info"]["participants"]]
    return match


def add_clan_games(root):
    """Main player Yo#LAN, friend Amigo Fiel#LAN, rival, and a third who played 2 games with them.

    A0-A3: main and friend on the SAME team (blue), 3 wins out of 4; THIRD only in A0 and A1.
    B0-B3: main on red (loses), friend on blue: opposite teams, must not count as "together".
    """
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player(FRIEND, "Amigo Fiel#LAN")
    store.add_player(RIVAL, "Rival#LAN")
    store.add_player(THIRD, "Tercero#LAN")
    for i, blue_wins in enumerate((True, True, False, True)):
        m = make_match(f"A{i}", queue=420, win=blue_wins, champion="Ahri",
                       created=BASE_TIME_MS + i * 3 * HOUR)
        if i >= 2:
            swap_puuid(m, THIRD, "stranger-5")
        store.save_match(f"A{i}", m, make_timeline())
    for i in range(4):
        m = make_match(f"B{i}", queue=420, win=True, my_pid=8, champion="Zed",
                       created=BASE_TIME_MS + (10 + i) * 3 * HOUR)
        swap_puuid(m, THIRD, "stranger-5")
        swap_puuid(m, "puuid-3", "stranger-3")
        store.save_match(f"B{i}", m, None)
    store.close()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    # A configured install: without key and Riot ID every page redirects to /configuracion.
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Yo#LAN\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def app(root):
    add_clan_games(root)
    return web.create_app(root)


@pytest.fixture
def client(app):
    return app.test_client()


def text(resp):
    return resp.get_data(as_text=True)


def write_env(root, key="RGAPI-one"):
    (root / ".env").write_text(f"RIOT_API_KEY={key}\nRIOT_ID=Yo#LAN\n", encoding="utf-8")


# ----------------------------------------------------------------- picker
def test_player_picker_lists_players_and_marks_current(client):
    html = text(client.get("/"))
    options = re.findall(r'<option value="([^"]+)"( selected)?>([^<]+)</option>',
                         html.split('id="jugador-select"')[1].split("</select>")[0])
    assert [o[0] for o in options] == ["yo", "amigo-fiel", "rival", "tercero"]
    assert "(tú)" in options[0][2]
    assert [o[0] for o in options if o[1]] == [options[0][0]]            # main selected
    html = text(client.get("/?jugador=amigo-fiel"))
    selected = re.findall(r'<option value="([^"]+)" selected>', html)
    assert selected == ["amigo-fiel"]


def test_picker_hidden_with_a_single_player(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.close()
    html = text(web.create_app(root).test_client().get("/"))
    assert 'id="jugador-select"' not in html


def test_each_page_works_per_player(client):
    main = text(client.get("/partidas"))
    assert "/partida/A0" in main and "Ahri" in main
    friend = text(client.get("/partidas?jugador=amigo-fiel"))
    assert "Champ4" in friend and "Ahri" not in friend
    assert "/partida/A0?cola=solo&amp;jugador=amigo-fiel" in friend
    assert "jugador=" not in main.split("<main")[1]                    # main player: clean links

    assert client.get("/?jugador=amigo-fiel").status_code == 200
    resp = client.get("/campeon/Champ4?jugador=amigo-fiel")
    assert resp.status_code == 200 and "Partidas con Champ4" in text(resp)
    resp = client.get("/campeon/Ahri?jugador=amigo-fiel")
    assert resp.status_code == 404 and "No hay partidas con el campeón" in text(resp)
    card = text(client.get("/partida/A1?jugador=amigo-fiel"))
    assert "Champ4" in card
    resp = client.get("/partida/A1?jugador=rival")                     # Rival played A1 too
    assert resp.status_code == 200
    resp = client.get("/partida/B0?jugador=tercero")                   # Tercero is not in B0
    assert resp.status_code == 404 and "Tercero#LAN" in text(resp)


def test_unknown_player_falls_back_to_main_and_queue_switcher_keeps_player(client):
    assert text(client.get("/partidas?jugador=nadie")) == text(client.get("/partidas"))
    html = text(client.get("/partidas?cola=flex&jugador=amigo-fiel"))
    seg = html.split('aria-label="Cola"')[1].split("</div>")[0]
    hrefs = re.findall(r'href="([^"]+)"', seg)
    assert len(hrefs) == 3 and all("jugador=amigo-fiel" in h for h in hrefs)
    nav = html.split('aria-label="Secciones"')[1].split("</nav>")[0]
    assert nav.count("jugador=amigo-fiel") == 7                         # every nav link keeps it
    # Picker form posts to the same page and carries the queue.
    assert 'action="/partidas"' in html and 'name="cola" value="flex"' in html


def test_empty_state_for_player_without_matches(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player("puuid-nada", "Nuevo#LAN")
    store.save_match("A0", make_match("A0", queue=420), None)
    store.close()
    c = web.create_app(root).test_client()
    html = text(c.get("/?jugador=nuevo"))
    assert "Aún no hay partidas" in html and "Nuevo#LAN" in html
    assert c.get("/clan").status_code == 200


# ------------------------------------------------------------------ cache
def test_cache_is_per_player_and_reloads_only_that_player(root):
    add_clan_games(root)
    cache = web.GamesCache(root / "data" / "lol.db")
    mine, theirs = cache.get(ME_PUUID, 420), cache.get(FRIEND, 420)
    assert len(mine.games) == 8 and len(theirs.games) == 8
    assert {g["champion"] for g in mine.games} == {"Ahri", "Zed"}
    assert {g["champion"] for g in theirs.games} == {"Champ4"}
    assert cache.get(None).games == [] and cache.get(None).total == 0
    # A new game that only the friend played (the main player is replaced).
    store = Store(root / "data" / "lol.db")
    m = swap_puuid(make_match("C0", queue=420, created=BASE_TIME_MS + 99 * HOUR), ME_PUUID, "stranger")
    store.save_match("C0", m, None)
    store.close()
    assert cache.get(ME_PUUID, 420).games is mine.games                  # not reloaded
    assert len(cache.get(FRIEND, 420).games) == 9
    cache.forget(FRIEND)
    assert len(cache.get(FRIEND, 420).games) == 9
    cache.invalidate()
    assert len(cache.get(ME_PUUID, 420).games) == 8


# ------------------------------------------------------------------- /clan
def test_clan_table_has_every_player(client):
    html = text(client.get("/clan"))
    assert 'class="sortable"' in html
    table = html.split("Comparación")[1].split("Jugando juntos")[0]
    for name in ("Yo#LAN", "Amigo Fiel#LAN", "Rival#LAN", "Tercero#LAN"):
        assert name in table
    for header in ("Partidas", "Winrate", "KDA", "Muertes/10 min", "Muertes en solitario/partida",
                   "Muertes antes del min 14/partida", "CS/min", "Visión/min", "Rol más jugado",
                   "Campeón más jugado"):
        assert header in table
    me = re.search(r"<tr>\s*<td><a href=\"/\?cola=solo\">Yo#LAN</a>.*?</tr>", table, re.S).group(0)
    assert 'data-v="8"' in me and "Mid" in me and "Ahri" in me and "(4)" in me and "(tú)" in me
    friend = re.search(r"<tr>\s*<td><a href=\"[^\"]*jugador=amigo-fiel\">Amigo Fiel#LAN</a>.*?</tr>",
                       table, re.S).group(0)
    assert "Champ4" in friend and 'data-v="8"' in friend
    assert "Solo/Dúo" in html and "clan" in html.lower()


def test_clan_pairs_count_only_same_team_games_with_three_or_more(client):
    html = text(client.get("/clan"))
    pairs = html.split("Jugando juntos")[1]
    assert "al menos 3 partidas" in pairs
    row = re.search(r"<tr>\s*<td>.*?Yo#LAN</a> \+ <a[^>]*>Amigo Fiel#LAN</a>.*?</tr>", pairs, re.S)
    assert row, pairs
    row = row.group(0)
    # 4 games together on the same team (A0-A3); the 4 games on opposite teams don't count.
    assert 'data-v="4"' in row and 'data-v="0.75"' in row and "75%" in row
    # Main player overall (A: 3 wins, B: 4 losses) and friend overall (A: 3, B: 4 wins) in the queue.
    assert "Yo 38%" in row and "Amigo Fiel 88%" in row
    assert "Tercero" not in pairs.split("</table>")[0]          # only 2 games with them: hidden
    assert "Rival" not in pairs.split("</table>")[0]            # opposite team: never together
    assert pairs.count("<tr>") == 2                              # header + a single pair


def test_clan_other_queue_and_single_player(client, root):
    html = text(client.get("/clan?cola=flex"))
    assert "0" in html and "Ninguna pareja tiene todavía 3 partidas juntos" in html
    other = root / "other"
    other.mkdir()
    (other / ".env").write_text((root / ".env").read_text())
    store = Store(other / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.close()
    html = text(web.create_app(other).test_client().get("/clan"))
    assert "Agrega al menos otro jugador" in html


def test_clan_page_with_no_players(root):
    html = text(web.create_app(root).test_client().get("/clan"))
    assert "Aún no hay jugadores" in html


# ------------------------------------------------------------- /jugadores
class AccountsFake:
    """RiotClient stand-in: accounts, per-player match ids; flags to fail on demand."""
    accounts = {}
    ids = {}
    not_found = set()
    expired = False
    keys = []
    limiters = []
    fetched = []

    def __init__(self, api_key, region="americas", limiter=None):
        AccountsFake.keys.append(api_key)
        AccountsFake.limiters.append(limiter)

    def get_puuid(self, name, tag):
        key = f"{name}#{tag}"
        if AccountsFake.expired:
            raise InvalidApiKeyError("La API key es inválida o expiró. Genera una nueva en "
                                     "developer.riotgames.com y actualiza .env.")
        known = {k.casefold(): k for k in AccountsFake.accounts}
        key = known.get(key.casefold(), key)
        if key in AccountsFake.not_found or key not in AccountsFake.accounts:
            raise NotFoundError("404")
        return AccountsFake.accounts[key]

    def get_riot_id(self, puuid):
        if AccountsFake.expired:
            raise InvalidApiKeyError("La API key es inválida o expiró. Genera una nueva en "
                                     "developer.riotgames.com y actualiza .env.")
        key = next((k for k, v in AccountsFake.accounts.items() if v == puuid), None)
        if key is None or key in AccountsFake.not_found:
            raise NotFoundError("404")
        return key

    def list_match_ids(self, puuid, start_time=None, queue=None):
        return list(AccountsFake.ids.get(puuid, []))

    def get_match(self, match_id):
        AccountsFake.fetched.append(match_id)
        return make_match(match_id, queue=420, created=BASE_TIME_MS + 500 * HOUR + len(AccountsFake.fetched))

    def get_timeline(self, match_id):
        return make_timeline()


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(web, "RiotClient", AccountsFake)
    AccountsFake.accounts = {"Yo#LAN": ME_PUUID, "Amigo Fiel#LAN": FRIEND, "Nuevo Amigo#LAN": "puuid-9",
                             "Rival#LAN": RIVAL, "Tercero#LAN": THIRD}
    AccountsFake.ids, AccountsFake.not_found, AccountsFake.expired = {}, set(), False
    AccountsFake.keys, AccountsFake.limiters, AccountsFake.fetched = [], [], []


def test_players_page_lists_counts_and_remove_buttons(client):
    html = text(client.get("/jugadores"))
    assert "Yo#LAN" in html and 'class="badge main"' in html and "Amigo Fiel#LAN" in html
    rows = re.findall(r'<tr data-puuid="([^"]+)" data-riot-id="([^"]+)">(.*?)</tr>', html, re.S)
    by_name = {name: (puuid, body) for puuid, name, body in rows}
    assert set(by_name) == {"Yo#LAN", "Amigo Fiel#LAN", "Rival#LAN", "Tercero#LAN"}
    assert 'data-action="ask"' not in by_name["Yo#LAN"][1]               # the main player can't be removed
    assert 'data-action="ask"' in by_name["Amigo Fiel#LAN"][1]
    stored = {name: re.search(r'<td class="num">(\d+)</td>', body).group(1)
              for name, (_, body) in by_name.items()}
    assert stored == {"Yo#LAN": "8", "Amigo Fiel#LAN": "8", "Rival#LAN": "4", "Tercero#LAN": "2"}
    assert 'id="add-form"' in html and "misma API key" in html


def test_players_page_empty(root):
    html = text(web.create_app(root).test_client().get("/jugadores"))
    assert "Aún no hay jugadores registrados" in html


def test_add_player_requires_header_json_and_valid_id(client, root, fake_api):
    write_env(root)
    assert client.post("/api/jugadores", json={"riot_id": "A#B"}).status_code == 403
    assert client.post("/api/jugadores", data={"riot_id": "A#B"}).status_code == 403
    assert client.post("/api/jugadores", data={"riot_id": "A#B"}, headers=HEADERS).status_code == 400
    for bad in ("", "sin tag", "#x", "x#", None, 5):
        resp = client.post("/api/jugadores", json={"riot_id": bad}, headers=HEADERS)
        assert resp.status_code == 400 and "Nombre#TAG" in resp.get_json()["error"]
    assert AccountsFake.keys == []                                    # nothing reached the API


def test_add_player_flow_and_errors(client, root, fake_api):
    write_env(root)
    resp = client.post("/api/jugadores", json={"riot_id": " Nuevo Amigo#LAN "}, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["riot_id"] == "Nuevo Amigo#LAN" and body["ya_registrado"] is False
    assert AccountsFake.keys == ["RGAPI-one"] and AccountsFake.limiters[-1] is not None
    store = Store(root / "data" / "lol.db")
    assert store.get_player(puuid="puuid-9").riot_id == "Nuevo Amigo#LAN"
    assert store.get_player(puuid="puuid-9").is_main is False
    store.close()
    assert "Nuevo Amigo#LAN" in text(client.get("/jugadores"))
    assert client.get("/?jugador=nuevo-amigo").status_code == 200

    again = client.post("/api/jugadores", json={"riot_id": "nuevo amigo#lan"}, headers=HEADERS)
    assert again.status_code == 200 and again.get_json()["ya_registrado"] is True
    assert again.get_json()["riot_id"] == "Nuevo Amigo#LAN"           # keeps how it was saved

    resp = client.post("/api/jugadores", json={"riot_id": "Fantasma#LAN"}, headers=HEADERS)
    assert resp.status_code == 404 and "No se encontró la cuenta Fantasma#LAN" in resp.get_json()["error"]

    AccountsFake.expired = True
    resp = client.post("/api/jugadores", json={"riot_id": "Fantasma#LAN"}, headers=HEADERS)
    assert resp.status_code == 401
    assert "developer.riotgames.com" in resp.get_json()["error"] and "reiniciar" in resp.get_json()["error"]


def test_add_player_without_key_is_friendly(client, root):
    (root / ".env").write_text("RIOT_ID=Yo#LAN\n", encoding="utf-8")
    resp = client.post("/api/jugadores", json={"riot_id": "Nuevo Amigo#LAN"}, headers=HEADERS)
    assert resp.status_code == 401 and "Configuración" in resp.get_json()["error"]


def test_remove_player(client, root):
    store = Store(root / "data" / "lol.db")
    before = store.count_matches()
    store.close()
    url = f"/api/jugadores/{FRIEND}"
    assert client.delete(url).status_code == 403                        # needs the header
    assert client.delete(f"/api/jugadores/{ME_PUUID}", headers=HEADERS).status_code == 400
    assert client.delete("/api/jugadores/nadie", headers=HEADERS).status_code == 404
    resp = client.delete(url, headers=HEADERS)
    assert resp.status_code == 200 and "Amigo Fiel#LAN" in resp.get_json()["mensaje"]
    store = Store(root / "data" / "lol.db")
    assert store.get_player(puuid=FRIEND) is None
    assert store.count_matches() == before and store.count_matches(FRIEND) == 8   # matches stay
    store.close()
    assert "Amigo Fiel#LAN" not in text(client.get("/jugadores")).split('id="add-form"')[0]
    assert client.get("/?jugador=amigo-fiel").status_code == 200       # old bookmark -> main player
    assert client.delete(url, headers=HEADERS).status_code == 404


def test_foreign_host_cannot_use_the_players_api(client, fake_api):
    assert client.post("/api/jugadores", json={"riot_id": "A#B"}, headers={**HEADERS, "Host": "evil.com"}
                       ).status_code == 400
    assert client.delete(f"/api/jugadores/{FRIEND}", headers={**HEADERS, "Host": "evil.com"}
                         ).status_code == 400
    assert client.get("/clan", headers={"Host": "evil.com"}).status_code == 400


# -------------------------------------------------------------------- sync
def wait_sync(app):
    job = app.extensions["lolan"]["sync"]
    job.join(10)
    assert not job.running
    return job.snapshot()


def test_sync_target_clan(app, client, root, fake_api):
    write_env(root)
    AccountsFake.ids = {ME_PUUID: ["S1", "S2"], FRIEND: ["S2", "S3"], RIVAL: [], THIRD: ["S4"]}
    resp = client.post("/api/sync", json={"cola": "solo", "jugador": "clan"}, headers=HEADERS)
    assert resp.status_code == 202
    status = wait_sync(app)
    assert status["error"] is None
    assert AccountsFake.fetched == ["S1", "S2", "S3", "S4"]           # S2 shared: downloaded once
    lines = status["lines"]
    for header in ("=== Yo#LAN (1/4) ===", "=== Amigo Fiel#LAN (2/4) ===", "=== Rival#LAN (3/4) ===",
                   "=== Tercero#LAN (4/4) ==="):
        assert header in lines
    assert status["current"] == {"riot_id": "Tercero#LAN", "index": 4, "count": 4}
    res = status["result"]
    assert res["downloaded"] == 4
    assert [(j["riot_id"], j.get("downloaded")) for j in res["jugadores"]] == [
        ("Yo#LAN", 2), ("Amigo Fiel#LAN", 1), ("Rival#LAN", 0), ("Tercero#LAN", 1)]
    # Everyone's data is visible afterwards (cache invalidated).
    assert "S3" in text(client.get("/partidas?jugador=amigo-fiel"))


def test_sync_target_single_player_and_default(app, client, root, fake_api):
    write_env(root)
    AccountsFake.ids = {ME_PUUID: ["S1"], FRIEND: ["S2"]}
    assert client.post("/api/sync", json={"cola": "solo", "jugador": "amigo-fiel"},
                       headers=HEADERS).status_code == 202
    status = wait_sync(app)
    assert AccountsFake.fetched == ["S2"] and status["result"]["jugadores"][0]["riot_id"] == "Amigo Fiel#LAN"

    AccountsFake.fetched = []
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202   # "yo"
    status = wait_sync(app)
    assert AccountsFake.fetched == ["S1"] and status["result"]["jugadores"][0]["riot_id"] == "Yo#LAN"

    resp = client.post("/api/sync", json={"cola": "solo", "jugador": "nadie"}, headers=HEADERS)
    assert resp.status_code == 400 and "Jugadores" in resp.get_json()["error"]


def test_sync_clan_reports_a_missing_account_and_keeps_going(app, client, root, fake_api):
    write_env(root)
    AccountsFake.ids = {ME_PUUID: ["S1"], THIRD: ["S4"]}
    AccountsFake.not_found = {"Amigo Fiel#LAN"}
    client.post("/api/sync", json={"cola": "solo", "jugador": "clan"}, headers=HEADERS)
    status = wait_sync(app)
    assert status["error"] is None and AccountsFake.fetched == ["S1", "S4"]
    broken = [j for j in status["result"]["jugadores"] if j["error"]]
    assert [j["riot_id"] for j in broken] == ["Amigo Fiel#LAN"]
    assert "No se encontró la cuenta" in broken[0]["error"]


def test_sync_clan_stops_when_the_key_expires(app, client, root, fake_api):
    write_env(root)
    AccountsFake.expired = True
    client.post("/api/sync", json={"cola": "solo", "jugador": "clan"}, headers=HEADERS)
    status = wait_sync(app)
    assert "developer.riotgames.com" in status["error"] and status["result"] is None


def test_sync_page_offers_every_target(client):
    html = text(client.get("/sincronizar"))
    assert 'name="jugador"' in html
    assert '<option value="yo" selected>' in html
    assert 'value="amigo-fiel"' in html and "Todo el clan (4 jugadores)" in html
    assert text(client.get("/sincronizar?jugador=amigo-fiel")).count("selected") >= 1
