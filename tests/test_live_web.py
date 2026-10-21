import json

import pytest
from conftest import ME_PUUID, make_match, make_timeline
from live_fixtures import FakeClient, ev, make_allgamedata, with_events

from lolan import live, web
from lolan.store import Store

ITEMS = {"1055": 450, "2003": 50, "3340": 0, "1001": 300, "3020": 1100, "1056": 400, "3006": 1100,
         "1038": 1300, "2055": 75, "3850": 400}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Yo#LAN\n", encoding="utf-8")
    return tmp_path


def add_games(root, n=7):
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    for i in range(n):
        store.save_match(f"LA1_{i}", make_match(f"LA1_{i}", queue=420, champion="Ahri", win=bool(i % 2),
                                                created=1_700_000_000_000 + i * 10_000_000),
                         make_timeline())
    store.close()


def add_item_prices(root):
    """A cached Data Dragon with item prices (what a previous sync leaves behind)."""
    base = root / "data" / "ddragon"
    (base / "16.20.1" / "data" / "es_MX").mkdir(parents=True)
    (base / "versions.json").write_text('["16.20.1"]', encoding="utf-8")
    data = {k: {"name": f"Objeto {k}", "gold": {"total": v}} for k, v in ITEMS.items()}
    (base / "16.20.1" / "data" / "es_MX" / "item.json").write_text(json.dumps({"data": data}), encoding="utf-8")
    (base / "16.20.1" / "data" / "es_MX" / "champion.json").write_text(
        json.dumps({"data": {"Jinx": {"name": "Jinx"}, "Kaisa": {"name": "Kai'Sa"}}}), encoding="utf-8")
    icon = base / "16.20.1" / "img" / "champion"
    icon.mkdir(parents=True)
    (icon / "Jinx.png").write_bytes(b"\x89PNG")


def poller_with(*script):
    p = live.LivePoller(FakeClient(list(script)))
    for _ in script:
        p.poll_once()
    return p


def playing(**kw):
    return poller_with(("ok", make_allgamedata(**kw)))


def client_for(root, poller):
    return web.create_app(root, live=poller).test_client()


def text(resp):
    return resp.get_data(as_text=True)


def init_json(html):
    return json.loads(html.split('id="live-init" type="application/json">')[1].split("</script>")[0])


# ------------------------------------------------------------------ the page
def test_waiting_page_and_navigation_without_badge(root):
    html = text(client_for(root, poller_with(("down", None))).get("/en-vivo"))
    assert 'data-status="waiting"' in html and "Esperando partida…" in html
    assert "Herramienta de práctica" in html and "empieza sola" in html
    assert "Modo stream (para compartir en Discord)" in html and "stream=1" in html
    assert "segundo monitor" in html                                   # the one-line tip
    assert 'id="nav-live"' in html and "live-dot" not in html.split("</header>")[0]
    assert 'aria-current="page"' in html.split('id="nav-live"')[1].split("</a>")[0]


def test_loading_page(root):
    html = text(client_for(root, poller_with(("down", None), ("loading", None))).get("/en-vivo"))
    assert 'data-status="loading"' in html and "Cargando partida…" in html
    assert init_json(html)["status"] == "loading"


def test_playing_page_embeds_state_and_shows_the_badge_everywhere(root):
    add_games(root)
    c = client_for(root, playing())
    html = text(c.get("/en-vivo"))
    data = init_json(html)
    assert data["status"] == "playing" and data["game"]["clock"] == "14:05"
    assert data["game"]["me"]["champion_name"] == "Jinx"
    assert 'data-status="playing"' in html and "En partida" in html
    assert 'class="live-dot"' in html.split("</header>")[0]
    for url in ("/", "/partidas", "/clan", "/configuracion"):
        assert 'class="live-dot"' in text(c.get(url)).split("</header>")[0]
    assert "envivo.js" in html and "envivo.css" in html
    assert c.get("/static/envivo.js").status_code == 200 and c.get("/static/envivo.css").status_code == 200


def test_finished_page_keeps_the_final_summary(root):
    end = ("ok", with_events(make_allgamedata(t=1800.0), ev(99, "GameEnd", 1799.0, Result="Win")))
    c = client_for(root, poller_with(end))
    html = text(c.get("/en-vivo"))
    assert 'data-status="finished"' in html
    assert "sincronizar la partida" in html and "análisis completo" in html
    assert init_json(html)["game"]["result"] == "win"
    assert 'class="live-dot"' not in html.split("</header>")[0]       # no longer "en partida"


def test_tls_problem_is_flagged(root):
    client = FakeClient([("down", None)])
    client.problem = "tls"
    p = live.LivePoller(client)
    p.poll_once()
    assert init_json(text(client_for(root, p).get("/en-vivo")))["problem"] == "tls"


def test_stream_mode_has_no_chrome(root):
    add_games(root)
    c = client_for(root, playing())
    html = text(c.get("/en-vivo?stream=1"))
    assert '<body class="stream">' in html and 'content="dark"' in html
    assert "topbar" not in html and "<footer" not in html and "Modo stream" not in html
    assert 'id="key-banner"' not in html and 'data-stream="1"' in html
    assert "envivo.js" in html
    normal = text(c.get("/en-vivo"))
    assert "topbar" in normal and "<footer" in normal and 'data-stream="0"' in normal
    waiting = text(client_for(root, poller_with(("down", None))).get("/en-vivo?stream=1"))
    assert "Esperando partida…" in waiting and "topbar" not in waiting


def test_stream_mode_shows_the_key_banner_nowhere(root):
    (root / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Yo#LAN\nRIOT_API_KEY_SAVED_AT=1\n", encoding="utf-8")
    c = client_for(root, playing())
    assert 'id="key-banner"' in text(c.get("/"))
    assert 'id="key-banner"' not in text(c.get("/en-vivo?stream=1"))


def test_page_needs_a_configured_install_like_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID"):
        monkeypatch.delenv(var, raising=False)
    resp = web.create_app(tmp_path, live=poller_with(("down", None))).test_client().get("/en-vivo")
    assert resp.status_code == 302 and "/configuracion" in resp.headers["Location"]


# ----------------------------------------------------------------- JSON API
def test_api_snapshot_with_item_prices_values_and_gold_estimate(root):
    add_item_prices(root)
    c = client_for(root, playing())
    resp = c.get("/api/en-vivo")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["game"]["prices_available"] is True
    ally = data["game"]["teams"]["ally"]["players"]
    adc = ally[3]
    assert adc["value"] == 1100 + 1300 + 75 and adc["icon"].endswith("/ddragon/16.20.1/img/champion/Jinx.png")
    assert adc["items"][0]["name"] == "Objeto 3006"                    # Spanish names from Data Dragon
    assert adc["value_diff"] == 75
    assert adc["gold_est"] % 50 == 0 and adc["unspent_est"] is None
    assert data["game"]["teams"]["enemy"]["players"][3]["unspent_est"] % 100 == 0
    assert data["game"]["gold_diff"] == (data["game"]["teams"]["ally"]["totals"]["gold_est"]
                                         - data["game"]["teams"]["enemy"]["totals"]["gold_est"])
    assert "Riot solo da el oro real de quien está jugando" in data["game"]["gold_model"]["legend"]
    assert data["game"]["me"]["gold"] == 1234                           # the real current gold


def test_api_without_cached_prices_shows_dashes_not_zeros(root):
    data = client_for(root, playing()).get("/api/en-vivo").get_json()
    assert data["game"]["prices_available"] is False
    assert all(p["value"] is None for p in data["game"]["teams"]["ally"]["players"])
    assert data["game"]["teams"]["ally"]["totals"]["value"] is None
    assert data["game"]["teams"]["ally"]["players"][0]["gold_est"] is not None      # the estimate still works


def test_api_context_uses_the_players_own_history(root):
    add_games(root)
    data = client_for(root, playing()).get("/api/en-vivo").get_json()
    cs = data["context"]["cs"]
    assert cs["now"] == 112 and cs["has_base"] and cs["games"] == 7
    assert cs["scope_label"] == "en todas tus partidas"                # never played Jinx, no ADC games
    assert [p["minute"] for p in cs["points"]] == [7, 14, 21]
    assert data["context"]["plan"]["enough"] is False                  # fewer than 8 games with timeline


def test_api_without_history(root):
    data = client_for(root, playing()).get("/api/en-vivo").get_json()
    assert data["context"]["has_games"] is False and data["context"]["cs"]["has_base"] is False


def test_api_waiting_has_no_game(root):
    data = client_for(root, poller_with(("down", None))).get("/api/en-vivo").get_json()
    assert data["status"] == "waiting" and data["game"] is None and data["context"] is None


def test_status_endpoint_and_host_check(root):
    c = client_for(root, playing())
    assert c.get("/api/en-vivo/estado").get_json() == {"status": "playing"}
    assert c.get("/api/en-vivo", headers={"Host": "evil.example"}).status_code == 400
    assert c.get("/api/en-vivo/stream", headers={"Host": "evil.example"}).status_code == 400


def test_icons_are_requested_in_the_background_without_waiting(root, monkeypatch):
    app = web.create_app(root, live=playing())
    calls = []
    dd = app.extensions["lolan"]["ddragon"]
    monkeypatch.setattr(dd, "prepare", lambda *a, **k: calls.append((a, k)))
    c = app.test_client()
    c.get("/api/en-vivo")
    c.get("/api/en-vivo")
    assert len(calls) == 1                                              # same champions and items: asked once
    assert calls[0][1]["items"] and "Jinx" in calls[0][0][1] and calls[0][1]["minimap"] is False


# ---------------------------------------------------------------------- SSE
def chunks(resp, n):
    out = []
    for chunk in resp.response:
        out.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        if len(out) == n:
            break
    return out


def test_stream_sends_state_then_heartbeats_and_counts_listeners(root):
    poller = playing()
    app = web.create_app(root, live=poller)
    app.config["LIVE_HEARTBEAT_S"] = 0.05
    resp = app.test_client().get("/api/en-vivo/stream", buffered=False)
    assert resp.mimetype == "text/event-stream" and resp.headers["Cache-Control"] == "no-store"
    first = chunks(resp, 3)
    assert first[0] == "retry: 2000\n\n"
    assert first[1].startswith("data: ") and first[1].endswith("\n\n")
    payload = json.loads(first[1][len("data: "):])
    assert payload["status"] == "playing" and payload["game"]["clock"] == "14:05"
    assert first[2] == ": latido\n\n"                                   # quiet: only a keep-alive comment
    assert poller.listeners == 1
    resp.close()
    assert poller.listeners == 0                                        # the listener is released


def test_stream_pushes_each_new_snapshot(root):
    poller = poller_with(("ok", make_allgamedata(t=100.0)), ("ok", make_allgamedata(t=101.0)))
    app = web.create_app(root, live=poller)
    app.config["LIVE_HEARTBEAT_S"] = 0.05
    resp = app.test_client().get("/api/en-vivo/stream", buffered=False)
    gen = iter(resp.response)
    next(gen)                                                           # retry line
    assert '"clock": "1:41"' in next(gen).decode()
    poller._client.script[:] = [("ok", make_allgamedata(t=102.0))]
    poller.poll_once()
    msgs = [next(gen).decode() for _ in range(2)]
    assert any('"clock": "1:42"' in m for m in msgs)
    resp.close()


def test_stream_ends_when_the_poller_stops(root):
    poller = playing()
    app = web.create_app(root, live=poller)
    resp = app.test_client().get("/api/en-vivo/stream", buffered=False)
    gen = iter(resp.response)
    next(gen), next(gen)
    poller.stop()
    assert list(gen) == []
    assert poller.listeners == 0


def test_player_names_are_escaped_in_the_embedded_json(root):
    raw = make_allgamedata()
    raw["allPlayers"][0]["riotIdGameName"] = "</script><b>x"
    poller = poller_with(("ok", raw))
    html = text(client_for(root, poller).get("/en-vivo"))
    assert "</script><b>x" not in html                                  # tojson escapes the angle brackets
    assert init_json(html)["game"]["teams"]["ally"]["players"][0]["name"] == "</script><b>x"


def test_script_labels_the_estimates_and_item_value_clearly(root):
    js = text(client_for(root, playing()).get("/static/envivo.js"))
    for label in ("Oro total ≈", "sin gastar ≈", "Valor en objetos", "Oro actual",
                  "Diferencia de oro total estimado", "no es el oro actual",
                  "Riot solo muestra el oro actual del jugador que está jugando"):
        assert label in js or label.lower() in js.lower(), label
    assert "legend" in js                                   # the model legend is shown as tooltip and note


def test_payload_legend_has_the_agreed_wording(root):
    legend = client_for(root, playing()).get("/api/en-vivo").get_json()["game"]["gold_model"]["legend"]
    assert legend.startswith("Estimado con tu CS, kills, asistencias y el minuto, ")
    assert "error típico ±" in legend and legend.endswith("Riot solo da el oro real de quien está jugando.")
