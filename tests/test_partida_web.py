"""/partida page, /api/partida and its event stream, fed by sanitized real captures and synthetic drafts."""
import json
from pathlib import Path

import pytest
from test_draft_live import FLEX_GAMEFLOW, flex_session, load
from test_web_comps import add_comp_games

from lolan import comps, draft_live as dl, lcu, web

CHAMPIONS = {"Ashe": 22, "Aatrox": 266, "Fiora": 114, "Viego": 234, "Lulu": 117, "Darius": 122, "Syndra": 134,
             "Garen": 86, "Malphite": 54, "Leona": 89, "Zed": 238, "Ahri": 103, "LeeSin": 64}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Epsilon Me#LAN\n", encoding="utf-8")
    base = tmp_path / "data" / "ddragon" / "16.20.1"
    (base / "data" / "es_MX").mkdir(parents=True)
    (base.parent / "versions.json").write_text('["16.20.1"]', encoding="utf-8")
    data = {name: {"key": str(key), "name": name} for name, key in CHAMPIONS.items()}
    (base / "data" / "es_MX" / "champion.json").write_text(json.dumps({"data": data}), encoding="utf-8")
    (base / "img" / "champion").mkdir(parents=True)
    (base / "img" / "champion" / "Ashe.png").write_bytes(b"\x89PNG")
    return tmp_path


@pytest.fixture
def app(root):
    add_comp_games(root)
    return web.create_app(root)


def text(resp):
    return resp.get_data(as_text=True)


def feed_of(app):
    return app.extensions["lolan"]["partida"]


def payload(app, query=""):
    return app.test_client().get("/api/partida" + query).get_json()


# ---------------------------------------------------------------------- page
def test_page_and_menu(app):
    html = text(app.test_client().get("/partida"))
    assert 'aria-current="page">Partida</a>' in html
    for needle in ("Esperando la selección de campeones", "Ir a En vivo", "Partida terminada", 'id="sel-rol"',
                   "/api/partida/stream", "seleccion.js", "seleccion.css"):
        assert needle in html
    assert 'href="/partida"' in text(app.test_client().get("/composiciones"))        # in the menu everywhere
    assert app.test_client().get("/static/seleccion.js").status_code == 200
    assert app.test_client().get("/partida", headers={"Host": "evil.example"}).status_code == 400


def test_match_page_route_is_untouched(app):
    assert app.test_client().get("/partida/LA1_0?cola=flex").status_code in (200, 404)
    assert app.test_client().get("/api/partida", headers={"Host": "evil.example"}).status_code == 400


# ----------------------------------------------------------------- phases
@pytest.mark.parametrize("phase,view,needle", [
    (None, "esperando", "El cliente de League no está abierto"),
    ("None", "esperando", "menú"),
    ("Lobby", "esperando", "sala"),
    ("Matchmaking", "esperando", "Buscando partida"),
    ("ReadyCheck", "esperando", "aceptarla"),
    ("GameStart", "juego", "por empezar"),
    ("InProgress", "juego", "en curso"),
    ("Reconnect", "juego", "reconectando"),
    ("WaitingForStats", "fin", "terminada"),
    ("EndOfGame", "fin", "terminada"),
    ("AlgoNuevo", "esperando", "AlgoNuevo"),
])
def test_phases_map_to_views(app, phase, view, needle):
    feed_of(app).publish(phase, None)
    data = payload(app)
    assert data["view"] == view and needle in data["phase_text"]
    assert data["board"] is None and data["draft"] is None


def test_champ_select_without_a_readable_session_is_still_champ_select(app):
    feed_of(app).publish("ChampSelect", None)
    data = payload(app)
    assert data["view"] == "seleccion" and data["board"] is None


# ------------------------------------------------- Practice Tool (lanes, blind)
def practice_state(index):
    gf, snaps = load("practice")
    return dl.normalize(snaps[index], gf)


def test_practice_tool_keeps_the_recommender_with_my_line_defaulting_to_my_history(app):
    feed_of(app).publish("ChampSelect", practice_state(0))
    data = payload(app)
    board, draft = data["board"], data["draft"]
    assert data["view"] == "seleccion" and board["lanes"] and board["is_custom"]
    assert board["queue_name"].startswith("Herramienta de Práctica")
    me = board["allies"][0]
    assert me["is_me"] and me["name"] == "Epsilon Me" and me["role"] == "BOTTOM" and me["role_guess"]
    assert (me["champion"], me["hover"], me["locked"]) == (None, None, False)
    assert board["enemies"] == [{"champion": None}]
    assert board["timer"]["text"] == "Elección y bans" and board["timer"]["seconds_left"] == 90
    assert draft["status"] == "ok" and draft["main_role"] == "BOTTOM" and draft["role_source"] == "historial"
    assert "tu línea más jugada" in draft["role_source_text"]
    assert "ADC (tú)" in draft["html"] and "Otras líneas" in draft["html"] and "Plan de pelea resultante" in draft["html"]


def test_hover_then_lock_updates_the_board_and_fixes_my_slot(app):
    feed = feed_of(app)
    feed.publish("ChampSelect", practice_state(2))                      # hovering Ashe (22)
    me = payload(app)["board"]["allies"][0]
    assert me["champion"] is None and me["hover"]["name"] == "Ashe" and not me["locked"]
    assert me["hover"]["icon"].startswith("/ddragon/16.20.1/img/champion/Ashe.png")
    feed.publish("ChampSelect", practice_state(3))                      # locked
    data = payload(app)
    me = data["board"]["allies"][0]
    assert me["champion"]["name"] == "Ashe" and me["hover"] is None and me["locked"]
    assert data["board"]["timer"]["text"] == "Últimos ajustes"


def test_my_line_choice_comes_in_the_query_and_changes_the_recommendation(app):
    feed_of(app).publish("ChampSelect", practice_state(0))
    data = payload(app, "?rol=MIDDLE")
    draft = data["draft"]
    assert draft["main_role"] == "MIDDLE" and draft["role_source"] == "elegido"
    assert "ADC (tú)" not in draft["html"]
    assert '<span class="badge main">tú</span>' in draft["html"]                      # my line is a normal entry
    assert "Juegas Mid: tus picks salen de tu historial en Mid" in draft["html"]
    assert data["board"]["allies"][0]["role_label"] == "Mid" and not data["board"]["allies"][0]["role_guess"]
    assert payload(app, "?rol=nope")["draft"]["main_role"] == "BOTTOM"             # bad values are ignored


def test_cola_defaults_to_the_games_queue_and_can_be_forced(app, monkeypatch):
    seen = []
    real = comps.filter_queue
    monkeypatch.setattr(comps, "filter_queue", lambda recs, q: seen.append(q) or real(recs, q))
    feed_of(app).publish("ChampSelect", practice_state(0))                 # queue 3140: not solo or flex
    payload(app)
    payload(app, "?cola=flex")
    state = flex_state()
    feed_of(app).publish("ChampSelect", state)
    payload(app)
    assert seen == [None, 440, 440]


# ----------------------------------------------------------- synthetic flex draft
def flex_state():
    return dl.normalize(flex_session(), FLEX_GAMEFLOW)


def test_flex_draft_fills_the_recommender_from_the_client(app):
    feed_of(app).publish("ChampSelect", flex_state())
    data = payload(app)
    board = data["board"]
    by_role = {a["role"]: a for a in board["allies"]}
    assert by_role["TOP"]["name"] == "Epsilon Alfa" and by_role["JUNGLE"]["name"] == "Epsilon Beta"
    assert by_role["MIDDLE"]["name"] == "Aleatorio" and by_role["UTILITY"]["name"] == "Aleatorio"
    assert by_role["TOP"]["title"] == "Epsilon Alfa#LAN" and by_role["UTILITY"]["title"] is None
    assert by_role["BOTTOM"]["is_me"] and by_role["BOTTOM"]["name"] == "Epsilon Me"
    assert not any(a["role_guess"] for a in board["allies"])                           # all from the client
    assert [e["champion"] and e["champion"]["name"] for e in board["enemies"]] == ["Darius", None, "Ahri", None, None]
    assert board["bans"]["ally"] and board["bans"]["enemy"][0]["name"] == "Campeón 157"      # not in the cache: placeholder
    draft = data["draft"]
    assert draft["role_source"] == "cliente" and draft["main_role"] == "BOTTOM"
    html = draft["html"]
    assert "Malphite ya está escogido" in html and "Leona ya está escogido" in html
    assert draft["hover_roles"] == ["JUNGLE"]               # Beta only hovers Lee Sin: used, but marked tentative


def test_recommender_runs_once_while_only_the_clock_ticks(app, monkeypatch):
    calls = []
    real = comps.recommend_draft
    monkeypatch.setattr(comps, "recommend_draft", lambda *a, **k: calls.append(k) or real(*a, **k))
    feed = feed_of(app)
    state = flex_state()
    for left in (21, 20, 19):
        feed.publish("ChampSelect", {**state, "timer": {**state["timer"], "seconds_left": left}})
        assert payload(app)["board"]["timer"]["seconds_left"] == left
    assert len(calls) == 1 and calls[0]["main_role"] == "BOTTOM"
    feed.publish("ChampSelect", {**state, "enemies": [{"champion_id": 122}] * 5})              # a new enemy pick
    payload(app)
    assert len(calls) == 2
    payload(app, "?rol=TOP")
    assert len(calls) == 3 and calls[-1]["main_role"] == "TOP"


def test_without_stored_games_the_board_works_and_the_draft_says_why(root):
    app = web.create_app(root)
    feed_of(app).publish("ChampSelect", flex_state())
    data = payload(app)
    assert data["board"]["allies"] and data["draft"]["status"] == "sin-datos"
    assert "sincroniza" in data["draft"]["message"].lower() and data["draft"]["html"] == ""


# ------------------------------------------------------------------- ARAM
def test_aram_shows_the_board_and_bench_but_never_the_recommender(app, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the recommender must not run outside Summoner's Rift")

    monkeypatch.setattr(comps, "recommend_draft", boom)
    monkeypatch.setattr(comps, "recommend", boom)
    gf, snaps = load("aram")
    feed_of(app).publish("ChampSelect", dl.normalize(snaps[3], gf))
    data = payload(app)
    board, draft = data["board"], data["draft"]
    assert data["view"] == "seleccion" and not board["lanes"] and board["queue_name"] == "ARAM: Caos"
    assert [a["role"] for a in board["allies"]] == [None] * 5
    assert [a["champion"] and a["champion"]["id"] for a in board["allies"]] == [63, 61, 421, None, 53]
    assert board["allies"][2]["is_me"] and all(a["name"] == "Aleatorio" for a in board["allies"][:2])
    assert board["bench"]["enabled"] and [c["id"] for c in board["bench"]["champions"]] == [122, 50, 99, 54]
    assert board["bench"]["can_reroll"] and board["bench"]["rerolls"] == 0
    assert board["enemies"] == [{"champion": None}] * 5
    assert draft == {"status": "no-aplica", "html": "", "message": web.NOT_RIFT_NOTE}
    assert web.NOT_RIFT_NOTE == "Composiciones solo aplica a la Grieta del Invocador (picks por línea)."
    final = dl.normalize(snaps[-1], gf)
    feed_of(app).publish("ChampSelect", final)
    assert payload(app)["board"]["timer"]["text"] == "La partida está por empezar"


def test_aram_goes_straight_from_champ_select_to_in_progress(app):
    gf, snaps = load("aram")
    feed = feed_of(app)
    feed.publish("ChampSelect", dl.normalize(snaps[-1], gf))
    assert payload(app)["view"] == "seleccion"
    feed.publish("InProgress", None)                         # no GameStart step was seen at 1 s polling
    assert payload(app)["view"] == "juego"


# ---------------------------------------------------------------- safety
def test_names_are_not_trusted_as_html(app):
    state = flex_state()
    state["allies"][2]["riot_id"] = "<img src=x onerror=alert(1)>#LAN"
    feed_of(app).publish("ChampSelect", state)
    html_data = json.dumps(payload(app))
    assert "Aleatorio" in html_data                          # not a clan person: the name is only a tooltip
    js = Path(web.__file__).parent.joinpath("static", "seleccion.js").read_text(encoding="utf-8")
    assert "esc(a.title)" in js and "esc(a.name)" in js      # and the script escapes both


def test_a_failure_building_the_view_does_not_break_the_stream_payload(app, monkeypatch, capsys):
    monkeypatch.setattr(comps, "recommend_draft", lambda *a, **k: 1 / 0)
    feed_of(app).publish("ChampSelect", flex_state())
    data = payload(app)
    assert data["view"] == "seleccion" and "No se pudo armar" in data["error"]
    assert "Aviso: no se pudo armar la selección" in capsys.readouterr().err


# ----------------------------------------------------------------------- SSE
def test_stream_sends_the_payload_on_each_change_and_heartbeats(app):
    app.config["LIVE_HEARTBEAT_S"] = 0.05
    feed = feed_of(app)
    feed.publish("Lobby", None)
    resp = app.test_client().get("/api/partida/stream", buffered=False)
    assert resp.mimetype == "text/event-stream" and resp.headers["Cache-Control"] == "no-store"
    gen = iter(resp.response)
    assert next(gen).decode() == "retry: 2000\n\n"
    first = next(gen).decode()
    assert first.startswith("data: ") and first.endswith("\n\n")
    assert json.loads(first[6:])["view"] == "esperando"
    assert next(gen).decode() == ": latido\n\n"
    feed.publish("ChampSelect", flex_state())
    msgs = [next(gen).decode() for _ in range(2)]
    pushed = next(json.loads(m[6:]) for m in msgs if m.startswith("data: "))
    assert pushed["view"] == "seleccion" and pushed["draft"]["status"] == "ok"
    resp.close()


def test_stream_carries_my_line_choice_and_ends_when_the_feed_stops(app):
    feed = feed_of(app)
    feed.publish("ChampSelect", practice_state(0))
    resp = app.test_client().get("/api/partida/stream?rol=UTILITY", buffered=False)
    gen = iter(resp.response)
    next(gen)
    assert json.loads(next(gen).decode()[6:])["draft"]["main_role"] == "UTILITY"
    feed.stop()
    assert list(gen) == []


# ------------------------------------------------------------------ wiring
def test_start_live_gives_the_recorders_loop_to_the_page_and_follows_without_it(root, monkeypatch):
    monkeypatch.setenv(lcu.LOCKFILE_ENV, str(root / "no-hay-lockfile"))
    started = []
    monkeypatch.setattr(lcu.ChampSelectRecorder, "start", lambda self: started.append(("recorder", self._observer)))
    monkeypatch.setattr(lcu.LcuFollower, "start", lambda self: started.append(("follower", self._observer)))

    class NoLive:
        def start(self):
            pass

    app = web.create_app(root, live=NoLive())
    ext = app.extensions["lolan"]
    ext["scout"], ext["warm_up_async"] = None, None
    web.start_live(app)
    assert started == [("recorder", ext["partida"])]                       # one loop: records and feeds the page
    monkeypatch.setenv(lcu.DISABLE_ENV, "off")
    web.start_live(app)
    assert started[1] == ("follower", ext["partida"]) and len(started) == 2   # recording off, page still works


def test_composiciones_still_renders_the_shared_draft_partial(app):
    html = text(app.test_client().get(
        "/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa&p_JUNGLE=Epsilon+Beta&c_TOP=Ornn"))
    for needle in ("<h4>ADC (tú)</h4>", "<h3>Otras líneas</h3>", "<h3>Plan de pelea resultante</h3>", "Ornn ya está escogido"):
        assert needle in html
    assert '<span class="badge main">tú</span>' not in html and html.count('<div class="draft-role">') == 4
