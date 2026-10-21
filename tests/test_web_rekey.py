"""Panel: the key-change notice and the "Actualizar mis datos" job (no network)."""
import threading

import pytest
from conftest import ME_PUUID, AppRiot, seed_app_store, world_matches

from lolan import sync as sync_module
from lolan import web
from lolan.riot import InvalidApiKeyError
from lolan.store import Store
from lolan.sync import SyncResult

HEADERS = {"X-Lolan": "1"}
CARD_HIDDEN = 'id="rekey-card" hidden'
CARD_SHOWN = 'id="rekey-card">'


@pytest.fixture
def world():
    return world_matches()


@pytest.fixture
def root(tmp_path, monkeypatch, world):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "RIOT_API_KEY=RGAPI-old\nRIOT_ID=Yo#LAN\nPLATFORM=la1\n", encoding="utf-8")
    store = Store(tmp_path / "data" / "lol.db")
    seed_app_store(store, world, "A")
    store.close()
    return tmp_path


def use_riot(monkeypatch, fake):
    monkeypatch.setattr(web, "RiotClient", lambda api_key, region="americas", **kw: fake)
    return fake


def text(resp):
    return resp.get_data(as_text=True)


def wait(job):
    job.join(10)
    assert not job.snapshot()["running"]
    return job.snapshot()


def jobs(app):
    ext = app.extensions["lolan"]
    return ext["sync"], ext["rekey"]


# ------------------------------------------------------------------ guards
def test_rekey_post_requires_header_and_json(root):
    client = web.create_app(root).test_client()
    assert client.post("/api/rekey", json={}).status_code == 403
    assert client.post("/api/rekey", data="no json", headers=HEADERS).status_code == 400
    assert client.post("/api/rekey", json=[1], headers=HEADERS).status_code == 400
    status = client.get("/api/rekey").get_json()
    assert status == {"running": False, "lines": [], "progress": None, "result": None,
                      "error": None, "needed": False}


def test_notice_is_hidden_until_needed(root):
    client = web.create_app(root).test_client()
    for url in ("/sincronizar", "/configuracion"):
        html = text(client.get(url))
        assert CARD_HIDDEN in html and "Cambiaste de API key" in html
        assert "Actualizar mis datos" in html


# ----------------------------------------------------------------- the job
def test_job_migrates_in_the_background_and_refreshes_the_cache(root, world, monkeypatch):
    fake = use_riot(monkeypatch, AppRiot("B", world))
    app = web.create_app(root)
    client = app.test_client()
    sync_job, rekey_job = jobs(app)
    rekey_job.needed = True

    resp = client.post("/api/rekey", json={}, headers=HEADERS)
    assert resp.status_code == 202
    status = wait(rekey_job)

    assert status["error"] is None and status["needed"] is False
    result = status["result"]
    assert result["actualizadas"] == 6 and result["ok"] and not result["problemas"]
    assert result["jugadores_actualizados"] == 2 and result["respaldo"]
    assert status["progress"] == {"done": 6, "total": 6}
    assert any(line.startswith("[6/6]") for line in status["lines"])
    assert len(fake.fetched("get_match")) == 6
    # The panel works on the new puuids right away (same cache object, invalidated).
    store = Store(root / "data" / "lol.db")
    assert store.get_player(riot_id="Yo#LAN").puuid == f"{ME_PUUID}@B"
    store.close()
    assert "LA1_3" in text(client.get("/partidas"))
    assert len(list((root / "data").glob("lol.db.antes-rekey-*"))) == 1
    # The rekey job reuses the sync job's rate limiter: one key, one budget.
    assert rekey_job.limiters is sync_job.limiters and rekey_job.slot is sync_job.slot


def test_job_reports_an_invalid_key_and_can_be_retried(root, world, monkeypatch):
    class Expired(AppRiot):
        def get_puuid(self, name, tag):
            raise InvalidApiKeyError("expired")

    use_riot(monkeypatch, Expired("B", world))
    app = web.create_app(root)
    client = app.test_client()
    _, rekey_job = jobs(app)
    assert client.post("/api/rekey", json={}, headers=HEADERS).status_code == 202
    status = wait(rekey_job)
    assert "developer.riotgames.com" in status["error"] and status["result"] is None
    use_riot(monkeypatch, AppRiot("B", world))
    assert client.post("/api/rekey", json={}, headers=HEADERS).status_code == 202
    assert wait(rekey_job)["result"]["actualizadas"] == 6


def test_job_without_a_key_is_friendly(root):
    (root / ".env").write_text("RIOT_ID=Yo#LAN\n", encoding="utf-8")
    app = web.create_app(root)
    app.test_client().post("/api/rekey", json={}, headers=HEADERS)
    assert "Configuración" in wait(jobs(app)[1])["error"]


def test_rekey_and_sync_exclude_each_other(root, world, monkeypatch):
    gate = threading.Event()
    use_riot(monkeypatch, AppRiot("B", world, gate=gate))
    app = web.create_app(root)
    client = app.test_client()
    _, rekey_job = jobs(app)
    assert client.post("/api/rekey", json={}, headers=HEADERS).status_code == 202
    again = client.post("/api/rekey", json={}, headers=HEADERS)
    assert again.status_code == 409 and "actualización" in again.get_json()["error"]
    blocked = client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    assert blocked.status_code == 409 and "actualizando tu base de datos" in blocked.get_json()["error"]
    gate.set()
    assert wait(rekey_job)["error"] is None
    # Once it finished, sync can run again.
    use_riot(monkeypatch, AppRiot("B", world))
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202
    wait(jobs(app)[0])


def test_rekey_refused_while_a_sync_runs(root, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_sync(client, store, riot_id, out=print, **kwargs):
        started.set()
        release.wait(10)
        return SyncResult(ME_PUUID, 0, 0, 0, 0, 0)

    monkeypatch.setattr(web, "RiotClient", lambda *a, **kw: object())
    monkeypatch.setattr(sync_module, "sync", slow_sync)
    app = web.create_app(root)
    client = app.test_client()
    sync_job, rekey_job = jobs(app)
    assert client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202
    assert started.wait(10)
    resp = client.post("/api/rekey", json={}, headers=HEADERS)
    assert resp.status_code == 409 and "sincronización" in resp.get_json()["error"]
    assert not rekey_job.snapshot()["running"]
    release.set()
    wait(sync_job)


# ------------------------------------------------- where the notice comes from
def test_sync_that_hits_the_other_app_error_shows_the_notice(root, world, monkeypatch):
    use_riot(monkeypatch, AppRiot("B", world))
    app = web.create_app(root)
    client = app.test_client()
    sync_job, rekey_job = jobs(app)
    client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    status = wait(sync_job)
    assert status["error_code"] == "rekey" and status["result"] is None
    assert "Actualizar mis datos" in status["error"]
    assert rekey_job.needed
    for url in ("/sincronizar", "/configuracion"):
        html = text(client.get(url))
        assert CARD_SHOWN in html and CARD_HIDDEN not in html
    # A sync that fails for another reason carries no such code.
    use_riot(monkeypatch, AppRiot("A", world))
    client.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    assert "error_code" not in wait(sync_job)


def save_config(client, key):
    return client.post("/api/configuracion", headers=HEADERS, json={
        "riot_id": "Yo#LAN", "plataforma": "la1", "riot_api_key": key, "gemini_api_key": ""})


def test_saving_a_key_of_another_app_shows_the_notice(root, world, monkeypatch):
    fake = use_riot(monkeypatch, AppRiot("B", world))
    app = web.create_app(root)
    client = app.test_client()
    body = save_config(client, "RGAPI-new").get_json()
    assert body["ok"] and body["cambio_key"] is True and body["rekey_minutos"] >= 1
    assert fake.fetched("get_riot_id")                       # one probe, no match downloads
    assert fake.fetched("get_match") == []
    assert jobs(app)[1].needed
    assert CARD_SHOWN in text(client.get("/configuracion"))


def test_saving_a_new_key_of_the_same_app_or_the_same_key_does_not(root, world, monkeypatch):
    fake = use_riot(monkeypatch, AppRiot("A", world))        # a regenerated development key
    client = web.create_app(root).test_client()
    assert save_config(client, "RGAPI-regenerated").get_json()["cambio_key"] is False
    calls = len(fake.calls)
    assert save_config(client, "").get_json()["cambio_key"] is False     # blank = keep
    assert save_config(client, "RGAPI-regenerated").get_json()["cambio_key"] is False
    assert len(fake.calls) == calls                           # no probe without a key change


def test_unreachable_riot_while_saving_never_fails_the_save(root, monkeypatch):
    class Broken:
        def get_riot_id(self, puuid):
            raise RuntimeError("boom")

    use_riot(monkeypatch, Broken())
    client = web.create_app(root).test_client()
    resp = save_config(client, "RGAPI-new")
    assert resp.status_code == 200 and resp.get_json()["cambio_key"] is False
