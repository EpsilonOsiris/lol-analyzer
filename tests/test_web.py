import threading

import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan import analyze as an
from lolan import sync as sync_module
from lolan import web
from lolan.__main__ import main
from lolan.riot import InvalidApiKeyError
from lolan.store import Store
from lolan.sync import SyncResult

HOUR = 3600_000
HEADERS = {"X-Lolan": "1"}


def add_games(root):
    """6 solo-queue Ahri games (alternating results), 1 flex-only Jinx game."""
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    for i in range(6):
        store.save_match(
            f"LA1_{i}",
            make_match(f"LA1_{i}", queue=420, champion="Ahri", win=bool(i % 2), kills=i,
                       deaths=6 - i, assists=i + 1, created=BASE_TIME_MS + i * 3 * HOUR),
            make_timeline())
    store.save_match(
        "LA1_FLEX",
        make_match("LA1_FLEX", queue=440, champion="Jinx", created=BASE_TIME_MS + 30 * HOUR),
        make_timeline())
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
def client(root):
    add_games(root)
    return web.create_app(root).test_client()


@pytest.fixture
def empty_client(root):
    return web.create_app(root).test_client()


def text(resp):
    return resp.get_data(as_text=True)


# ------------------------------------------------------------------ pages
@pytest.mark.parametrize("url", [
    "/", "/partidas", "/campeon/Ahri", "/partida/LA1_3", "/sincronizar",
    "/?cola=todas", "/?cola=flex", "/partidas?cola=todas&pagina=99",
])
def test_pages_render_with_data(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    assert "LoL Analyzer" in text(resp)


def test_summary_content(client):
    html = text(client.get("/"))
    assert "Winrate" in html and "Ahri" in html
    assert "/campeon/Ahri?cola=solo" in html          # champion links keep the queue
    assert "Winrate por mes" in html
    assert "Lo que decide tus partidas" in html


def test_champion_page_lists_its_games(client):
    html = text(client.get("/campeon/ahri"))              # name is not case-sensitive
    assert "/partida/LA1_0" in html and "/partida/LA1_5" in html
    assert "Más alto en tus victorias" in html            # 6 games, 3 wins / 3 losses


def test_game_card_page(client):
    html = text(client.get("/partida/LA1_3"))
    assert "Fortalezas" in html and "Debilidades" in html
    assert "Rival de línea" in html and "Zed" in html


@pytest.mark.parametrize("url", ["/", "/partidas", "/sincronizar", "/campeon/Ahri",
                                 "/partida/LA1_1", "/?cola=todas", "/?cola=flex"])
def test_pages_render_with_empty_db(empty_client, url):
    resp = empty_client.get(url)
    assert resp.status_code == 200
    html = text(resp)
    if url != "/sincronizar":
        assert "Sincronizar" in html and "Aún no hay partidas" in html


def test_queue_switcher_filters(client):
    solo = text(client.get("/partidas"))                    # default is solo
    assert "LA1_0" in solo and "LA1_FLEX" not in solo and "Jinx" not in solo
    flex = text(client.get("/partidas?cola=flex"))
    assert "LA1_FLEX" in flex and "LA1_0" not in flex
    todas = text(client.get("/partidas?cola=todas"))
    assert "LA1_FLEX" in todas and "LA1_0" in todas
    assert text(client.get("/partidas?cola=rara")) == solo   # unknown queue -> default


def test_queue_is_preserved_in_links(client):
    html = text(client.get("/partidas?cola=flex"))
    assert "/partida/LA1_FLEX?cola=flex" in html
    assert "cola=todas" in html and "cola=solo" in html     # the switcher


def test_champion_from_another_queue(client):
    resp = client.get("/campeon/Jinx")                       # solo: Jinx only played flex
    assert resp.status_code == 200
    assert "No tienes partidas con Jinx" in text(resp)
    assert "LA1_FLEX" in text(client.get("/campeon/Jinx?cola=flex"))


def test_match_from_another_queue_falls_back_to_all_games(client):
    resp = client.get("/partida/LA1_FLEX")                   # solo selected
    assert resp.status_code == 200
    assert "no es de Solo/Dúo" in text(resp)


def test_pagination(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    for i in range(web.PAGE_SIZE + 5):
        store.save_match(f"LA1_{i}", make_match(f"LA1_{i}", created=BASE_TIME_MS + i * 3 * HOUR))
    store.close()
    c = web.create_app(root).test_client()
    p1, p2 = text(c.get("/partidas")), text(c.get("/partidas?pagina=2"))
    newest, oldest = f"LA1_{web.PAGE_SIZE + 4}", "LA1_0"
    assert newest in p1 and oldest not in p1
    assert oldest in p2 and newest not in p2
    assert "Página 1 de 2" in p1


def test_404s_are_spanish(client):
    for url in ("/campeon/NoExiste", "/partida/NOPE", "/no-existe"):
        resp = client.get(url)
        assert resp.status_code == 404
        assert "No encontrado" in text(resp)
    assert "NoExiste" in text(client.get("/campeon/NoExiste"))


def test_foreign_host_is_rejected(client):
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200


# ------------------------------------------------------------------ cache
def test_cache_sessions_match_cli_per_queue(root):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    # solo, flex, solo within 45 min of each other: one session overall, two for solo only.
    for i, queue in enumerate((420, 440, 420)):
        store.save_match(f"LA1_{i}", make_match(f"LA1_{i}", queue=queue,
                                                created=BASE_TIME_MS + i * 20 * 60_000))
    expected = [(g["match_id"], g["session_id"], g["session_game"])
                for g in an.load_games(store, ME_PUUID, queue=420)]
    store.close()

    cache = web.GamesCache(root / "data" / "lol.db")
    solo = [(g["match_id"], g["session_id"], g["session_game"]) for g in cache.get(ME_PUUID, 420).games]
    assert solo == expected
    # Building the solo list didn't disturb the all-queues one.
    assert [g["session_game"] for g in cache.get(ME_PUUID, None).games] == [1, 2, 3]


def test_cache_reloads_when_match_count_changes(root):
    add_games(root)
    cache = web.GamesCache(root / "data" / "lol.db")
    assert len(cache.get(ME_PUUID, 420).games) == 6
    store = Store(root / "data" / "lol.db")
    store.save_match("LA1_NEW", make_match("LA1_NEW", queue=420, created=BASE_TIME_MS + 99 * HOUR))
    store.close()
    assert len(cache.get(ME_PUUID, 420).games) == 7
    cache.invalidate()
    assert len(cache.get(ME_PUUID, 420).games) == 7


# -------------------------------------------------------------------- sync
def test_sync_post_requires_header_and_json(client):
    assert client.post("/api/sync", json={"cola": "solo"}).status_code == 403
    # What a cross-site HTML form could send.
    assert client.post("/api/sync", data={"cola": "solo"}).status_code == 403
    assert client.post("/api/sync", data={"cola": "solo"}, headers=HEADERS).status_code == 400
    assert client.post("/api/sync", json={"cola": "xyz"}, headers=HEADERS).status_code == 400
    assert client.post("/api/sync", json={"cola": "solo", "max": 0}, headers=HEADERS).status_code == 400
    assert client.post("/api/sync", json={"cola": "solo", "max": "abc"}, headers=HEADERS).status_code == 400
    status = client.get("/api/sync").get_json()
    assert status == {"running": False, "lines": [], "progress": None, "current": None,
                      "result": None, "error": None}


def wait_sync(app):
    job = app.extensions["lolan"]["sync"]
    job.join(10)
    assert not job.running
    return job.snapshot()


class FakeClient:
    keys = []
    limiters = []

    def __init__(self, api_key, region="americas", limiter=None):
        FakeClient.keys.append((api_key, region))
        FakeClient.limiters.append(limiter)

    def get_puuid(self, name, tag):
        return ME_PUUID

    def list_match_ids(self, puuid, start_time=None, queue=None):
        FakeClient.queue = queue
        return ["LA1_S1", "LA1_S2"]

    def get_match(self, match_id):
        return make_match(match_id, queue=420, created=BASE_TIME_MS + 200 * HOUR)

    def get_timeline(self, match_id):
        return make_timeline()


def write_env(root, key="RGAPI-one"):
    (root / ".env").write_text(f"RIOT_API_KEY={key}\nRIOT_ID=Yo#LAN\n", encoding="utf-8")


def test_sync_run_updates_status_and_invalidates_cache(root, monkeypatch):
    add_games(root)
    write_env(root)
    FakeClient.keys = []
    monkeypatch.setattr(web, "RiotClient", FakeClient)
    app = web.create_app(root)
    client = app.test_client()
    cache = app.extensions["lolan"]["cache"]
    assert len(cache.get(ME_PUUID, 420).games) == 6
    calls = []
    real_invalidate = cache.invalidate
    monkeypatch.setattr(cache, "invalidate", lambda: (calls.append(1), real_invalidate()))

    resp = client.post("/api/sync", json={"cola": "solo", "max": 5, "timeline": True}, headers=HEADERS)
    assert resp.status_code == 202
    status = wait_sync(app)

    assert status["error"] is None
    assert status["result"]["downloaded"] == 2
    assert status["progress"] == {"done": 2, "total": 2}
    assert any("LA1_S1" in line for line in status["lines"])
    assert FakeClient.queue == 420 and FakeClient.keys == [("RGAPI-one", "americas")]
    assert calls                                             # cache invalidated after the run
    assert len(cache.get(ME_PUUID, 420).games) == 8                    # and the new games show up
    assert "LA1_S2" in text(client.get("/partidas"))


def test_sync_all_queues_passes_no_queue_filter(root, monkeypatch):
    write_env(root)
    monkeypatch.setattr(web, "RiotClient", FakeClient)
    app = web.create_app(root)
    app.test_client().post("/api/sync", json={"cola": "todas"}, headers=HEADERS)
    assert wait_sync(app)["error"] is None
    assert FakeClient.queue is None


def test_sync_error_then_retry_rereads_env(root, monkeypatch):
    write_env(root, "RGAPI-expired")
    created = []

    class ExpiringClient(FakeClient):
        def __init__(self, api_key, region="americas", limiter=None):
            created.append(api_key)
            super().__init__(api_key, region, limiter)

        def get_puuid(self, name, tag):
            if created[-1] == "RGAPI-expired":
                raise InvalidApiKeyError("La API key es inválida o expiró. Genera una nueva en "
                                         "developer.riotgames.com y actualiza .env.")
            return super().get_puuid(name, tag)

    monkeypatch.setattr(web, "RiotClient", ExpiringClient)
    app = web.create_app(root)
    client = app.test_client()
    client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    status = wait_sync(app)
    assert "developer.riotgames.com" in status["error"] and "reiniciar" in status["error"]
    assert status["result"] is None

    write_env(root, "RGAPI-fresh")                          # user fixes .env; server keeps running
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202
    status = wait_sync(app)
    assert created == ["RGAPI-expired", "RGAPI-fresh"]
    assert status["error"] is None and status["result"]["downloaded"] == 2
    # Both runs share one limiter: the key's request budget spans syncs.
    assert FakeClient.limiters[-1] is FakeClient.limiters[-2] is not None


def test_sync_without_key_is_friendly(root):
    (root / ".env").write_text("RIOT_ID=Yo#LAN\n", encoding="utf-8")
    app = web.create_app(root)
    app.test_client().post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    assert "Configuración" in wait_sync(app)["error"]


def test_sync_conflict_while_running(root, monkeypatch):
    write_env(root)
    started, release = threading.Event(), threading.Event()

    def slow_sync(client, store, riot_id, out=print, **kwargs):
        out("[1/3] LA1_X Ahri | transcurrido 1 s | faltan ~2 s")
        started.set()
        release.wait(10)
        return SyncResult(ME_PUUID, 3, 0, 0, 0, 0)

    monkeypatch.setattr(web, "RiotClient", FakeClient)
    monkeypatch.setattr(sync_module, "sync", slow_sync)
    app = web.create_app(root)
    client = app.test_client()
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202
    assert started.wait(10)
    running = client.get("/api/sync").get_json()
    assert running["running"] is True and running["progress"] == {"done": 1, "total": 3}
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 409
    release.set()
    assert wait_sync(app)["result"]["downloaded"] == 0


# --------------------------------------------------------------------- CLI
def test_web_command_parses_and_runs(root, monkeypatch, capsys):
    ran = {}

    class FakeApp:
        def run(self, **kwargs):
            ran.update(kwargs)

    monkeypatch.setattr(web, "create_app", lambda r=None: FakeApp())
    assert main(["web", "--puerto", "9123", "--no-abrir"]) == 0
    assert ran["host"] == "127.0.0.1" and ran["port"] == 9123
    assert "http://127.0.0.1:9123/" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["web", "--puerto", "99999"])
