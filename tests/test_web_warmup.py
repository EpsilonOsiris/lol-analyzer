"""Cache warm-up at panel start and after a sync (start_live only; tests never warm on their own)."""
import pytest
from conftest import ME_PUUID

from lolan import web
from test_web import FakeClient, add_games, wait_sync, write_env

HEADERS = {"X-Lolan": "1"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    write_env(tmp_path)
    return tmp_path


class Spy:
    def __init__(self, cache, fail=False):
        self.calls, self.fail = [], fail
        self.real_get, self.real_comps = cache.get, cache.comps
        cache.get = self.get
        cache.comps = self.comps

    def get(self, puuid, queue=None):
        self.calls.append(("get", puuid, queue))
        if self.fail:
            raise RuntimeError("boom")
        return self.real_get(puuid, queue)

    def comps(self, puuid, prefix, aliases):
        self.calls.append(("comps", puuid, prefix, aliases))
        return self.real_comps(puuid, prefix, aliases)


def test_warm_up_loads_games_and_comps_once(root):
    add_games(root)
    app = web.create_app(root)
    spy = Spy(app.extensions["lolan"]["cache"])
    app.extensions["lolan"]["warm_up"]()
    assert spy.calls == [("get", ME_PUUID, None), ("comps", ME_PUUID, "Epsilon", "")]


def test_warm_up_swallows_errors(root, capsys):
    add_games(root)
    app = web.create_app(root)
    Spy(app.extensions["lolan"]["cache"], fail=True)
    app.extensions["lolan"]["warm_up"]()          # does not raise
    err = capsys.readouterr().err
    assert err.count("Aviso: no se pudo precargar") == 1 and "boom" in err


def test_warm_up_without_main_player_does_nothing(root):
    app = web.create_app(root)
    spy = Spy(app.extensions["lolan"]["cache"])
    app.extensions["lolan"]["warm_up"]()
    assert spy.calls == []


def test_create_app_does_not_warm_up(root):
    add_games(root)
    app = web.create_app(root)
    spy = Spy(app.extensions["lolan"]["cache"])
    assert spy.calls == []


def test_start_live_warms_in_background(root, monkeypatch):
    monkeypatch.setenv("LOLAN_GRABAR_SELECCION", "off")
    add_games(root)
    app = web.create_app(root, live=type("L", (), {"start": lambda self: None})())
    app.extensions["lolan"]["scout"] = None
    spy = Spy(app.extensions["lolan"]["cache"])
    web.start_live(app)
    for t in __import__("threading").enumerate():
        if t.name == "lolan-warmup":
            t.join(10)
    assert [c[0] for c in spy.calls] == ["get", "comps"]


def test_finished_sync_rewarms_only_after_start_live(root, monkeypatch):
    add_games(root)
    monkeypatch.setattr(web, "RiotClient", FakeClient)
    app = web.create_app(root)
    warmed = []
    app.extensions["lolan"]["warm_up_async"] = lambda: warmed.append(1)
    client = app.test_client()
    client.post("/api/sync", json={"cola": "solo", "max": 5, "timeline": True}, headers=HEADERS)
    wait_sync(app)
    assert warmed == []                            # tests never start the live services
    app.extensions["lolan"]["preload"] = True
    client.post("/api/sync", json={"cola": "solo", "max": 5, "timeline": True}, headers=HEADERS)
    wait_sync(app)
    assert warmed == [1]
