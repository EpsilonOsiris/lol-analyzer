"""Duelos in the panel: background job after the scout, live push, payload names/icons, UI rendering."""
import copy
import json
import threading

import pytest
from opgg_fixtures import FakeMcp, FakeResponse
from scout_fixtures import ME
from test_scout_web import (HEADERS, init_json, make_app, node, page_payload, poller_with, render, root,  # noqa: F401
                            scout_now, waiting)

from lolan import duelos as dl
from lolan.opgg import McpClient
from lolan.store import Store

NOW = 1_800_000_000


def attach(app, root, fake=None):
    fake = fake or FakeMcp()
    svc = dl.DuelosService(lambda: Store(root / "data" / "lol.db"), root / "data" / "winprob_model.json",
                           client=McpClient(session=fake), patch_func=lambda: "16.20", clock=lambda: NOW)
    app.extensions["lolan"]["scout"].duelos = svc
    return fake, svc


def duelos_of(app):
    return app.test_client().get("/api/rivales").get_json()["report"]["duelos"]


# --------------------------------------------------------------- the job
def test_report_is_pushed_first_and_duelos_follow_in_the_background(root, monkeypatch):
    app = make_app(root, monkeypatch)
    fake, _svc = attach(app, root)
    gate = threading.Event()
    fake.fail_tools["lol_list_champions"] = lambda a: (gate.wait(10), None)[1]
    scout = app.extensions["lolan"]["scout"]
    seen = []
    scout._notify = lambda: seen.append(scout.snapshot()["report"] and scout.snapshot()["report"].get("duelos", {}).get("status"))
    assert app.test_client().post("/api/rivales/buscar", json={}, headers=HEADERS).status_code == 202
    for _ in range(200):
        if "pending" in seen:
            break
        threading.Event().wait(0.02)
    snap = scout.snapshot()
    assert snap["status"] == "ready" and snap["report"]["duelos"] == {"status": "pending"}
    assert len(snap["report"]["teams"]["enemy"]) == 5                          # the scout did not wait for OP.GG
    gate.set()
    scout.join(10)
    assert duelos_of(app)["status"] == "ok" and seen[-1] == "ok"


def test_opgg_failure_keeps_the_scout_report_and_says_so(root, monkeypatch):
    app = make_app(root, monkeypatch)
    fake, _ = attach(app, root)
    fake.raise_on_post = ConnectionError("sin red")
    scout_now(app)
    rep = app.test_client().get("/api/rivales").get_json()["report"]
    assert rep["duelos"] == {"status": "error", "message": "Sin datos de OP.GG"}
    assert rep["teams"]["enemy"][0]["rank"]["solo"]["label"] == "Oro II" and rep["complete"] is True


def test_a_bug_in_the_duelos_code_cannot_break_the_scout(root, monkeypatch, capsys):
    app = make_app(root, monkeypatch)
    _fake, svc = attach(app, root)
    monkeypatch.setattr(svc, "build", lambda *a, **k: 1 / 0)
    scout_now(app)
    assert duelos_of(app) == {"status": "error", "message": "Sin datos de OP.GG"}
    assert app.extensions["lolan"]["scout"].snapshot()["status"] == "ready"


def test_a_result_for_a_game_that_is_gone_is_dropped(root, monkeypatch):
    app = make_app(root, monkeypatch)
    fake, _ = attach(app, root)
    gate = threading.Event()
    fake.fail_tools["lol_list_champions"] = lambda a: (gate.wait(10), None)[1]
    scout = app.extensions["lolan"]["scout"]
    app.test_client().post("/api/rivales/buscar", json={}, headers=HEADERS)
    for _ in range(200):
        if scout.snapshot()["report"] is not None:
            break
        threading.Event().wait(0.02)
    scout._set(report=None, status="idle", message="")                         # the next game started
    gate.set()
    scout.join(10)
    assert scout.snapshot()["report"] is None


def test_without_a_duelos_service_the_report_has_no_block(root, monkeypatch):
    app = make_app(root, monkeypatch)
    scout_now(app)
    assert "duelos" not in app.test_client().get("/api/rivales").get_json()["report"]


def test_the_panel_has_no_duelos_service_when_opgg_is_turned_off(root, monkeypatch):
    app = make_app(root, monkeypatch)
    assert app.extensions["lolan"]["scout"].duelos is None                       # LOLAN_OPGG=off in the tests
    monkeypatch.setenv("LOLAN_OPGG", "on")
    assert dl.make_service(root) is not None
    monkeypatch.setenv("LOLAN_OPGG", "off")
    assert dl.make_service(root) is None


def test_sorry_no_lanes_in_aram_means_no_block_at_all(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.world.games["P-me"]["gameMode"] = "ARAM"
    app.world.games["P-me"]["mapId"] = 12
    attach(app, root)
    scout_now(app)
    assert "duelos" not in app.test_client().get("/api/rivales").get_json()["report"]


# ------------------------------------------------------------- the payload
def test_payload_names_and_icons_for_the_lane_champions(root, monkeypatch):
    app = make_app(root, monkeypatch)
    attach(app, root)
    scout_now(app)
    d = duelos_of(app)
    top = next(l for l in d["lanes"] if l["role"] == "TOP")
    assert top["ally"]["champion_name"] == "Garen" and top["ally"]["icon"].endswith("/img/champion/Garen.png")
    assert top["enemy"]["champion_name"] == "Renekton" and top["label"] == "Arriba"
    assert d["duo"]["ally"]["adc_name"] == "Jinx" and d["duo"]["ally"]["support_name"] == "Lulu"
    assert "puuid" not in json.dumps(d).lower()
    assert 0.25 <= d["prob"]["value"] <= 0.75


def test_duelos_arrive_through_the_live_stream(root, monkeypatch):
    app = make_app(root, monkeypatch)
    app.config["LIVE_HEARTBEAT_S"] = 0.05
    attach(app, root)
    resp = app.test_client().get("/api/en-vivo/stream", buffered=False)
    gen = iter(resp.response)
    next(gen)
    json.loads(next(gen).decode()[len("data: "):])
    assert app.test_client().post("/api/rivales/buscar", json={}, headers=HEADERS).status_code == 202
    got = None
    for _ in range(80):
        msg = next(gen).decode()
        if msg.startswith("data: "):
            rep = json.loads(msg[len("data: "):])["scout"]["report"]
            if rep and rep.get("duelos", {}).get("status") == "ok":
                got = rep
                break
    resp.close()
    assert got is not None and got["duelos"]["prob"]["text"].endswith("%")


# ------------------------------------------------------------------- the UI
def with_duelos(root, monkeypatch, fake=None, mutate=None):
    app = make_app(root, monkeypatch)
    attach(app, root, fake)
    scout_now(app)
    payload = page_payload(app)
    if mutate:
        mutate(payload["scout"]["report"])
    return payload


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_renders_the_lanes_chips_and_the_probability(root, monkeypatch):
    out = render(with_duelos(root, monkeypatch))
    html = out["body"]
    for needle in ("Probabilidad estimada:", "estimación con datos de OP.GG; aún sin calibrar", "Duelos por línea",
                   "OP.GG, todos los rangos, parche 16.20", "Arriba", "Jungla", "Centro", "Tirador", "Soporte",
                   "Garen contra Renekton", "54%", "2400 partidas", "Favorable", "Desfavorable", "Parejo", "Pocos datos",
                   "Dúo de línea", "Jinx + Lulu", "no filtra por rango", 'class="prob-bar"', "width:"):
        assert needle in html, needle
    assert html.index("Probabilidad estimada") < html.index("Duelos por línea") < html.index('class="sc-teams"')
    assert "undefined" not in html and "null" not in html and "NaN" not in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_hides_the_percentage_of_thin_samples_and_marks_doubtful_roles(root, monkeypatch):
    def mutate(report):
        lanes = report["duelos"]["lanes"]
        lanes[4]["low_confidence"] = True
        lanes[3]["personal"] = {"games": 4, "wins": 3, "text": "3V-1D"}
    html = render(with_duelos(root, monkeypatch, mutate=mutate))["body"]
    sup = html.split("Soporte")[1].split("duel-chip")[0]
    assert "%" not in sup and "150 partidas" in sup
    assert "posición dudosa" in html and "Tu historial: 3V-1D (4)" in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_with_partial_data(root, monkeypatch):
    fake = FakeMcp()
    del fake.matchups[("mid", "Ahri", "Zed")]
    html = render(with_duelos(root, monkeypatch, fake))["body"]
    assert "Sin datos" in html and "Faltan datos de algunos duelos" in html and "Garen contra Renekton" in html
    assert "undefined" not in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_when_opgg_is_down_the_rest_of_the_report_stays(root, monkeypatch):
    fake = FakeMcp()
    fake.raise_on_post = ConnectionError("x")
    html = render(with_duelos(root, monkeypatch, fake))["body"]
    assert "Sin datos de OP.GG" in html and "Probabilidad estimada" not in html and "Duelos por línea" not in html
    assert "Equipo enemigo" in html and "Diamante" not in html and "Oro II" in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_pending_ineligible_and_old_reports(root, monkeypatch):
    payload = with_duelos(root, monkeypatch)
    pending = copy.deepcopy(payload)
    pending["scout"]["report"]["duelos"] = {"status": "pending"}
    assert "Calculando duelos con OP.GG" in render(pending)["body"]
    for state in ({"status": "na"}, None):
        other = copy.deepcopy(payload)
        if state is None:
            del other["scout"]["report"]["duelos"]
        else:
            other["scout"]["report"]["duelos"] = state
        html = render(other)["body"]
        assert "OP.GG" not in html and "Duelos" not in html and "Equipo enemigo" in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_probability_shows_the_track_record_once_calibrated(root, monkeypatch):
    def mutate(report):
        report["duelos"]["prob"]["label"] = "estimación con datos de OP.GG · Acierta el 58% (34 partidas, Brier 0,24)"
        report["duelos"]["prob"]["calibrated"] = True
    html = render(with_duelos(root, monkeypatch, mutate=mutate))["body"]
    assert "Acierta el 58% (34 partidas, Brier 0,24)" in html and "aún sin calibrar" not in html


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_ui_stream_mode_keeps_the_duelos(root, monkeypatch):
    out = render(with_duelos(root, monkeypatch), "stream")
    assert "Probabilidad estimada" in out["body"] and "duel-rows" in out["body"] and out["boxHidden"] is False


def test_script_and_css_have_the_duelos_hooks_and_no_advice():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "lolan" / "static"
    css = (static / "envivo.css").read_text(encoding="utf-8")
    assert ".stream .duel-rows" in css and ".duel-chip.tone-fav" in css and ".prob-bar" in css
    js = (static / "envivo.js").read_text(encoding="utf-8").lower()
    for word in ("banea", "evita", "elige", "recomend", "deberías", "dodge"):
        assert word not in js
