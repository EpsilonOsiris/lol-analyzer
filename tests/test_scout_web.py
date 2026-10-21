"""Rivals scouting in the web panel: payload, endpoints, live stream, shared limiters, page and script."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import ME_PUUID as STORED_ME_PUUID
from conftest import make_match
from live_fixtures import FakeClient, ev, make_allgamedata, with_events
from scout_fixtures import ALLIES, ENEMIES, ME, ME_PUUID, World, league, mastery, seed_ddragon, spectator_game

from lolan import live, web
from lolan.riot import InvalidApiKeyError
from lolan.store import Store

HEADERS = {"X-Lolan": "1"}
TESTS = Path(__file__).resolve().parent
STATIC = TESTS.parent / "lolan" / "static"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY=RGAPI-one\nRIOT_ID={ME}\n", encoding="utf-8")
    return tmp_path


def poller_with(*script):
    p = live.LivePoller(FakeClient(list(script)))
    for _ in script:
        p.poll_once()
    return p


def waiting():
    return poller_with(("down", None))


def make_app(root, monkeypatch, poller=None, icons=True, game=True):
    world = World()
    world.stock(spectator_game() if game else None)
    made = []

    def fake_riot(key, region="americas", limiter=None, **kw):
        made.append((region, limiter))
        return world.client(region)

    monkeypatch.setattr(web, "RiotClient", fake_riot)
    seed_ddragon(root, icons=icons)
    app = web.create_app(root, live=poller or waiting())
    app.world, app.made = world, made
    return app


def scout_now(app, c=None, **body):
    c = c or app.test_client()
    resp = c.post("/api/rivales/buscar", json=body, headers=HEADERS)
    app.extensions["lolan"]["scout"].join(10)
    return resp


def init_json(html):
    return json.loads(html.split('id="live-init" type="application/json">')[1].split("</script>")[0])


# ------------------------------------------------------------------ payload
def test_payload_has_names_icons_spells_runes_and_labels(root, monkeypatch):
    app = make_app(root, monkeypatch)
    c = app.test_client()
    assert scout_now(app, c).status_code == 202
    rep = c.get("/api/rivales").get_json()["report"]
    me, _rana, beta, hidden, _mono = rep["teams"]["ally"]
    assert me["champion_name"] == "Jinx" and me["icon"].endswith("/ddragon/16.20.1/img/champion/Jinx.png")
    assert [s["name"] for s in me["spells"]] == ["Destello", "Curación"]
    assert me["spells"][0]["icon"].endswith("/ddragon/16.20.1/img/spell/SummonerFlash.png")
    assert me["keystone"]["name"] == "Paso Fugaz" and me["keystone"]["icon"].endswith("LethalTempoTemp.png")
    assert me["secondary"]["name"] == "Brujería"
    assert beta["clan"] is True and me["display_name"] == ME
    assert hidden["display_name"] == "Jugador oculto" and hidden["keystone"] is None
    assert hidden["champion_name"] == "Lulu" and [s["name"] for s in hidden["spells"]] == ["Destello", "Ignición"]
    bot = rep["teams"]["enemy"][3]
    assert bot["display_name"] == "Bot" and bot["champion_name"] == "Kai'Sa"
    assert [b["champion_name"] for b in rep["bans"]["ally"]] == ["Aatrox"]
    assert rep["requests"]["used"] == 36 and rep["requests"]["budget"] == 45     # 34 + account + spectator
    blob = json.dumps(c.get("/api/rivales").get_json())
    assert "P-toro" not in blob and "puuid" not in blob.lower()


def test_payload_without_icons_asks_data_dragon_once(root, monkeypatch):
    app = make_app(root, monkeypatch, icons=False)
    calls = []
    monkeypatch.setattr(app.extensions["lolan"]["ddragon"], "prepare", lambda *a, **k: calls.append(k))
    c = app.test_client()
    scout_now(app, c)
    c.get("/api/rivales")
    c.get("/api/rivales")
    assert len(calls) == 1
    assert 222 in calls[0]["champion_keys"] and 4 in calls[0]["spells"] and 8008 in calls[0]["perks"]
    assert 8000 in calls[0]["perks"] and calls[0]["minimap"] is False
    rep = c.get("/api/rivales").get_json()["report"]
    assert rep["teams"]["ally"][0]["icon"] is None and rep["teams"]["ally"][0]["champion_name"] == "Jinx"


def test_partial_report_is_marked(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.world.fail["spectator"] = lambda puuid: app.world.waits.update(la1=120.0)      # busy from now on
    c = app.test_client()
    scout_now(app, c)
    rep = c.get("/api/rivales").get_json()["report"]
    assert rep["complete"] is False and rep["requests"]["stopped"] == "limite"
    assert rep["teams"]["enemy"][0]["status"] == "partial" and rep["teams"]["enemy"][0]["rank"] is None


def test_live_payload_always_carries_the_scout_state(root, monkeypatch):
    app = make_app(root, monkeypatch)
    data = app.test_client().get("/api/en-vivo").get_json()
    assert data["scout"]["status"] == "idle" and data["scout"]["report"] is None and data["scout"]["budget"] == 45
    scout_now(app)
    data = app.test_client().get("/api/en-vivo").get_json()
    assert data["status"] == "waiting" and data["game"] is None and data["scout"]["report"]["game"]["game_id"]


# ---------------------------------------------------------------- endpoints
def test_search_endpoint_guard_and_errors(root, monkeypatch):
    app = make_app(root, monkeypatch)
    c = app.test_client()
    assert c.post("/api/rivales/buscar", json={}).status_code == 403
    assert c.post("/api/rivales/buscar", data="x", headers=HEADERS).status_code == 400
    assert c.post("/api/rivales/buscar", json={"jugador": "Nadie#X"}, headers=HEADERS).status_code == 400
    assert c.post("/api/rivales/buscar", json={}, headers={**HEADERS, "Host": "evil.example"}).status_code == 400
    assert c.get("/api/rivales", headers={"Host": "evil.example"}).status_code == 400
    resp = c.post("/api/rivales/buscar", json={"jugador": ME.lower()}, headers=HEADERS)
    app.extensions["lolan"]["scout"].join(10)
    assert resp.status_code == 202 and resp.get_json()["status"] == "searching"


def test_sync_and_scout_never_run_together(root, monkeypatch):
    app = make_app(root, monkeypatch)
    c = app.test_client()
    started, release = threading.Event(), threading.Event()

    def slow_sync(client, store, riot_ids, **kw):
        started.set()
        release.wait(10)
        return []

    monkeypatch.setattr(web, "sync_players", slow_sync)
    assert c.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).status_code == 202
    assert started.wait(5)
    resp = c.post("/api/rivales/buscar", json={}, headers=HEADERS)
    assert resp.status_code == 409 and "sincronización en curso" in resp.get_json()["error"]
    assert c.post("/api/sync", json={"cola": "solo"}, headers=HEADERS).get_json()["error"] == \
        "Ya hay una sincronización en curso."
    release.set()
    app.extensions["lolan"]["sync"].join(10)
    assert scout_now(app, c).status_code == 202                       # free again

    hold, go = threading.Event(), threading.Event()

    def blocked(puuid):
        hold.set()
        go.wait(10)

    store = Store(root / "data" / "lol.db")
    store.conn.execute("DELETE FROM scout_cache")            # the next scout has to ask Riot again
    store.conn.commit()
    store.close()
    app.world.fail["league"] = blocked
    assert c.post("/api/rivales/buscar", json={"jugador": ""}, headers=HEADERS).status_code == 202
    assert hold.wait(5)
    resp = c.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    assert resp.status_code == 409 and "rivales" in resp.get_json()["error"]
    go.set()
    app.extensions["lolan"]["scout"].join(10)
    assert app.extensions["lolan"]["sync"].slot.holder is None


def test_one_limiter_per_host_shared_by_sync_scout_and_lookups(root, monkeypatch):
    app = make_app(root, monkeypatch)
    c = app.test_client()
    monkeypatch.setattr(web, "sync_players", lambda *a, **k: [])
    c.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    app.extensions["lolan"]["sync"].join(10)
    scout_now(app, c)
    c.post("/api/jugadores", json={"riot_id": "Alguien#LAN"}, headers=HEADERS)
    by_host = {}
    for region, limiter in app.made:
        by_host.setdefault(region, set()).add(id(limiter))
    assert set(by_host) == {"americas", "la1"}
    assert all(len(ids) == 1 for ids in by_host.values())              # sync, scout and lookups: one limiter each host
    assert by_host["americas"] != by_host["la1"]
    limiters = app.extensions["lolan"]["limiters"]
    assert limiters.for_host("la1") is limiters.for_host("LA1")
    assert limiters.for_host("americas") is not limiters.for_host("la1")


def test_invalid_key_is_reported_and_remembered(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.world.fail["spectator"] = InvalidApiKeyError("expiró")
    c = app.test_client()
    scout_now(app, c)
    data = c.get("/api/rivales").get_json()
    assert data["status"] == "error" and "inválida o expiró" in data["message"]
    assert app.extensions["lolan"]["health"].is_invalid("RGAPI-one")


# ----------------------------------------------------------------------- SSE
def test_report_is_pushed_through_the_live_stream(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.config["LIVE_HEARTBEAT_S"] = 0.05
    resp = app.test_client().get("/api/en-vivo/stream", buffered=False)
    gen = iter(resp.response)
    assert next(gen).decode() == "retry: 2000\n\n"
    first = json.loads(next(gen).decode()[len("data: "):])
    assert first["scout"]["report"] is None
    assert app.test_client().post("/api/rivales/buscar", json={}, headers=HEADERS).status_code == 202
    got = None
    for _ in range(40):
        msg = next(gen).decode()
        if msg.startswith("data: "):
            payload = json.loads(msg[len("data: "):])
            if payload["scout"]["report"]:
                got = payload
                break
    resp.close()
    assert got is not None
    assert got["status"] == "waiting" and got["scout"]["status"] == "ready"
    assert got["scout"]["report"]["teams"]["enemy"][0]["rank"]["solo"]["label"] == "Oro II"


def test_stream_while_playing_keeps_sending_the_report(root, monkeypatch):
    app = make_app(root, monkeypatch, poller=poller_with(("ok", make_allgamedata(t=100.0))))
    scout = app.extensions["lolan"]["scout"]
    code, _ = scout.request(None, "playing", app.extensions["lolan"]["live"].game_seq)
    scout.join(10)
    data = app.test_client().get("/api/en-vivo").get_json()
    assert data["status"] == "playing" and data["game"] is not None
    assert data["scout"]["report"] is not None                        # kept for the whole game
    app.extensions["lolan"]["live"].game_seq += 1                     # the next game starts
    assert app.test_client().get("/api/en-vivo").get_json()["scout"]["report"] is None


# ------------------------------------------------------------------- the page
def test_page_has_the_rivals_section_with_controls_and_the_policy_note(root, monkeypatch):
    app = make_app(root, monkeypatch)
    store = Store(root / "data" / "lol.db")
    store.add_player(STORED_ME_PUUID, ME, is_main=True)
    store.add_player("p2", "Rana Veloz#T002")
    store.close()
    html = app.test_client().get("/en-vivo").get_data(as_text=True)
    assert 'id="live-scout"' in html and "Buscar partida ahora" in html and 'id="scout-target"' in html
    assert f'<option value="{ME}">{ME} (tú)</option>' in html and 'value="Rana Veloz#T002"' in html
    assert "Datos de Riot (Spectator) desde la pantalla de carga" in html and "~45 peticiones" in html
    assert "caché" in html and "información pública" in html
    stream = app.test_client().get("/en-vivo?stream=1").get_data(as_text=True)
    assert 'id="live-scout"' in stream and "Buscar partida ahora" not in stream and 'id="scout-target"' not in stream


def test_script_never_shows_live_cooldowns_or_advice():
    js = (STATIC / "envivo.js").read_text(encoding="utf-8").lower()
    for forbidden in ("cooldown", "dodge", "enfriamiento", "te recomendamos", "deberías", "evita jugar"):
        assert forbidden not in js


# ------------------------------------------------- the script, run with Node
node = shutil.which("node")


def render(payload, mode="normal"):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        out = subprocess.run([node, str(TESTS / "scout_harness.js"), str(STATIC / "envivo.js"), str(path), mode],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def page_payload(app):
    return init_json(app.test_client().get("/en-vivo").get_data(as_text=True))


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_renders_a_full_report(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.world.leagues["P-toro"] = league(solo=("DIAMOND", "II", 12, 200, 150, True),
                                         flex=("PLATINUM", "I", 50, 20, 20, False))
    app.world.masteries[("P-toro", 58)] = mastery(250_000, level=10)
    app.world.masteries[("P-gato", 64)] = None
    store = Store(root / "data" / "lol.db")
    m = make_match("LA1_H1", win=True)
    for p in m["info"]["participants"]:
        p["riotIdGameName"], p["riotIdTagline"] = f"Z{p['participantId']}", "0"
    m["info"]["participants"][0].update({"riotIdGameName": "Yo", "riotIdTagline": "LAN", "teamId": 100, "win": True})
    m["info"]["participants"][5].update({"riotIdGameName": "Toro Rojo", "riotIdTagline": "T006", "teamId": 200,
                                        "championName": "Renekton"})
    store.save_match("LA1_H1", m)
    store.close()
    scout_now(app)
    out = render(page_payload(app))
    html = out["body"]
    assert out["boxHidden"] is False and out["scoutFlag"] == "1" and out["open"] is True
    for needle in ("Tu equipo", "Equipo enemigo", "Toro Rojo#T006", "Diamante II", "12 PL", "57%", "Platino I",
                   "250k", "nivel 10", "main", "poca experiencia con este campeón", "nunca lo ha jugado",
                   "de tu clan", "Epsilon Beta#LAN", "Jugador oculto", "Bot", "Antes: 1V-0D contra ti",
                   "Destello", "Paso Fugaz", "Bans de tu equipo", "Bans enemigos", "Peticiones a Riot: 36 de 45",
                   "/ddragon/16.20.1/img/champion/Jinx.png", "en racha", "Clasificatoria Solo/Dúo", "tú"):
        assert needle in html, needle
    assert "Solo se muestra su campeón y sus hechizos." in html
    assert "P-toro" not in html and "undefined" not in html and "null" not in html
    assert "Buscando tu partida en Riot cada" not in out["msg"]


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_renders_partial_data_without_breaking(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.world.fail["spectator"] = lambda puuid: app.world.waits.update(la1=200.0)
    scout_now(app)
    out = render(page_payload(app))
    html = out["body"]
    assert "rango: sin datos" in html and "maestría: sin datos" in html and "Faltó:" in html
    assert "Rango o maestría de 8 jugadores" in html and "más de 60 s" in html
    assert "Jugador oculto" in html and "undefined" not in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_with_nothing_found_yet(root, monkeypatch):
    app = make_app(root, monkeypatch)
    out = render(page_payload(app))
    assert out["body"] == "" and "cada 30 segundos" in out["msg"] and out["scoutFlag"] == "0"
    stream = render(page_payload(app), "stream")
    assert stream["boxHidden"] is False and "cada 30 segundos" in stream["body"]
    app.world.games.clear()
    scout_now(app)
    out = render(page_payload(app))
    assert "no está en una partida" in out["msg"]


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_in_stream_mode_is_compact_and_folded_during_the_game(root, monkeypatch):
    app = make_app(root, monkeypatch, poller=poller_with(("ok", make_allgamedata(t=100.0))))
    scout = app.extensions["lolan"]["scout"]
    scout.request(None, "playing", app.extensions["lolan"]["live"].game_seq)
    scout.join(10)
    payload = page_payload(app)
    assert payload["status"] == "playing" and payload["scout"]["report"]
    stream = render(payload, "stream")
    assert stream["boxHidden"] is False and stream["open"] is False       # folded while playing; opens by hand
    assert "Jugador oculto" in stream["body"] and stream["noteHidden"] is False
    normal = render(payload)
    assert normal["open"] is False
    payload["status"] = "loading"
    assert render(payload, "stream")["open"] is True                      # open on the loading screen


# ---------------------------------------------------------- the live poller side
def test_poller_counts_games_and_touch_pushes_a_new_version_without_changing_the_game():
    end = ("ok", with_events(make_allgamedata(t=1800.0), ev(99, "GameEnd", 1799.0, Result="Win")))
    p = poller_with(("down", None), ("loading", None), ("ok", make_allgamedata(t=100.0)))
    assert p.game_seq == 1 and p.status == "playing"
    p._client.script[:] = [end]
    p.poll_once()
    assert p.status == "finished" and p.game_seq == 1
    p._client.script[:] = [("ok", make_allgamedata(t=30.0))]
    p.poll_once()
    assert p.status == "playing" and p.game_seq == 2                  # the next game
    before = p.snapshot()
    p.touch()
    after = p.snapshot()
    assert after["version"] == before["version"] + 1 and after["game"] is before["game"]
    assert after["status"] == before["status"]
