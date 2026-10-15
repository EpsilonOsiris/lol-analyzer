"""Configuration page, first-run redirect, expired-key banner and the key test (no network)."""
import time

import pytest
import requests
from conftest import ME_PUUID
from dotenv import dotenv_values

from lolan import settings, web
from lolan.config import PLATFORM_REGIONS
from lolan.riot import InvalidApiKeyError, RiotClient, RiotConnectionError
from lolan.store import Store

HEADERS = {"X-Lolan": "1"}
RIOT_KEY = "RGAPI-aaaa1111-bbbb-2222-cccc-3333dddd4444"
GEMINI_KEY = "AIzaSyFAKE_gemini-key-0000000000000abcd"
BANNER = "Tu key de Riot probablemente expiró"
HOUR = 3600


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def write_env(root, text):
    (root / ".env").write_text(text, encoding="utf-8")


def env(root):
    return dotenv_values(root / ".env")


def configured(root, saved_at=None, extra=""):
    lines = f"RIOT_API_KEY={RIOT_KEY}\nRIOT_ID=Yo#LAN\nPLATFORM=la1\n{extra}"
    if saved_at is not None:
        lines += f"RIOT_API_KEY_SAVED_AT={int(saved_at)}\n"
    write_env(root, lines)


@pytest.fixture
def app(root):
    configured(root)
    return web.create_app(root)


@pytest.fixture
def client(app):
    return app.test_client()


def text(resp):
    return resp.get_data(as_text=True)


def save(client, **fields):
    body = {"riot_id": "Yo#LAN", "plataforma": "la1", "riot_api_key": "", "gemini_api_key": ""}
    body.update(fields)
    return client.post("/api/configuracion", json=body, headers=HEADERS)


# ------------------------------------------------------------------- page
def test_config_page_renders_and_masks_keys(root):
    configured(root, saved_at=time.time() - 2 * HOUR, extra=f"GEMINI_API_KEY={GEMINI_KEY}\n")
    html = text(web.create_app(root).test_client().get("/configuracion"))
    assert "Configuración" in html and 'value="Yo#LAN"' in html
    assert "termina en ••••4444" in html and "termina en ••••abcd" in html
    assert "hace 2 horas" in html
    assert RIOT_KEY not in html and GEMINI_KEY not in html
    assert "https://developer.riotgames.com" in html
    assert "https://aistudio.google.com/apikey" in html
    assert "Probar key de Riot" in html
    assert "Bienvenido" not in html                    # already configured
    assert '<option value="la1" selected>LAN' in html


def test_every_platform_has_a_friendly_choice():
    assert {p for p, _ in settings.PLATFORM_CHOICES} == set(PLATFORM_REGIONS)
    assert dict(settings.PLATFORM_CHOICES)["la1"].startswith("LAN")


def test_nav_link_footer_notice_on_every_page(client):
    html = text(client.get("/sincronizar"))
    assert 'href="/configuracion' in html
    assert "lol-analyzer isn't endorsed by Riot Games and doesn't reflect the views or opinions" in html
    assert "trademarks or registered trademarks of Riot Games, Inc." in html


# --------------------------------------------------------------- first run
def test_first_run_redirects_everything_to_configuracion(root):
    c = web.create_app(root).test_client()
    for url in ("/", "/partidas", "/clan", "/jugadores", "/sincronizar", "/campeon/Ahri",
                "/partida/LA1_1", "/no-existe"):
        resp = c.get(url)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/configuracion"), url
    page = c.get("/configuracion")
    assert page.status_code == 200
    assert "Bienvenido, configura tu cuenta para empezar" in text(page)
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/api/sync").status_code == 200       # API calls are not redirected


def test_riot_id_without_key_still_redirects(root):
    write_env(root, "RIOT_ID=Yo#LAN\n")
    assert web.create_app(root).test_client().get("/").status_code == 302
    write_env(root, f"RIOT_API_KEY={RIOT_KEY}\n")
    assert web.create_app(root).test_client().get("/").status_code == 302


def test_first_run_save_unlocks_the_panel(root):
    c = web.create_app(root).test_client()
    assert c.get("/").status_code == 302
    resp = save(c, riot_api_key=RIOT_KEY)
    assert resp.status_code == 200 and resp.get_json()["listo"] is True
    assert c.get("/sincronizar").status_code == 200    # no restart needed
    assert "Bienvenido" not in text(c.get("/configuracion"))


def test_save_without_key_reports_not_ready(root):
    c = web.create_app(root).test_client()
    data = save(c).get_json()
    assert data["ok"] is True and data["listo"] is False
    assert env(root)["RIOT_ID"] == "Yo#LAN"
    assert c.get("/").status_code == 302


# ------------------------------------------------------------------- saving
def test_save_requires_header_and_json(client, root):
    before = (root / ".env").read_text()
    body = {"riot_id": "Yo#LAN", "plataforma": "la1"}
    assert client.post("/api/configuracion", json=body).status_code == 403
    assert client.post("/api/configuracion", data=body).status_code == 403
    assert client.post("/api/configuracion", data=body, headers=HEADERS).status_code == 400
    assert client.post("/api/configuracion/probar", json={}).status_code == 403
    assert client.post("/api/configuracion", json=body,
                       headers={**HEADERS, "Host": "evil.example.com"}).status_code == 400
    assert (root / ".env").read_text() == before


def test_save_preserves_unknown_keys_and_comments(root):
    write_env(root, "# mis cosas\r\nRIOT_API_KEY_OTRA=otra-secreta\r\n\r\n"
                    f"RIOT_API_KEY={RIOT_KEY}\r\nRIOT_ID=Viejo#LAN\r\nPLATFORM=la1\r\n"
                    "# fin\r\nOTRA=1\r\n")
    c = web.create_app(root).test_client()
    resp = save(c, riot_id="Nuevo#TAG", plataforma="euw1", gemini_api_key=GEMINI_KEY)
    assert resp.status_code == 200
    raw = (root / ".env").read_bytes().decode()
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")      # EOL style kept
    lines = raw.splitlines()
    assert lines[0] == "# mis cosas" and "# fin" in lines and "OTRA=1" in lines
    assert "RIOT_API_KEY_OTRA=otra-secreta" in lines
    values = env(root)
    assert values["RIOT_ID"] == "Nuevo#TAG" and values["PLATFORM"] == "euw1"
    assert values["REGION"] == "europe"
    assert values["GEMINI_API_KEY"] == GEMINI_KEY and values["RIOT_API_KEY"] == RIOT_KEY
    assert not list(root.glob(".env.*.tmp"))                          # temp file swapped in


def test_blank_keys_keep_the_saved_ones(root):
    configured(root, saved_at=1000, extra=f"GEMINI_API_KEY={GEMINI_KEY}\n")
    c = web.create_app(root).test_client()
    assert save(c, riot_id="Otro#LAN").status_code == 200
    values = env(root)
    assert values["RIOT_API_KEY"] == RIOT_KEY and values["GEMINI_API_KEY"] == GEMINI_KEY
    assert values["RIOT_API_KEY_SAVED_AT"] == "1000"


def test_saved_at_changes_only_when_riot_key_changes(root):
    configured(root, saved_at=1000)
    c = web.create_app(root).test_client()
    assert save(c, riot_api_key=RIOT_KEY).status_code == 200          # same key typed again
    assert env(root)["RIOT_API_KEY_SAVED_AT"] == "1000"
    assert save(c, gemini_api_key=GEMINI_KEY).status_code == 200      # other key: not the Riot one
    assert env(root)["RIOT_API_KEY_SAVED_AT"] == "1000"
    before = time.time()
    assert save(c, riot_api_key="RGAPI-new-key-9999").status_code == 200
    values = env(root)
    assert values["RIOT_API_KEY"] == "RGAPI-new-key-9999"
    assert before - 1 <= float(values["RIOT_API_KEY_SAVED_AT"]) <= time.time() + 1


def test_responses_never_contain_keys(root, caplog):
    configured(root)
    c = web.create_app(root).test_client()
    resp = save(c, riot_api_key="RGAPI-brand-new-7777", gemini_api_key=GEMINI_KEY)
    body = text(resp)
    assert "RGAPI-brand-new-7777" not in body and GEMINI_KEY not in body and RIOT_KEY not in body
    data = resp.get_json()
    assert data["riot_key_fin"] == "••••7777" and data["gemini_key_fin"] == "••••abcd"
    page = text(c.get("/configuracion"))
    assert "RGAPI-brand-new-7777" not in page and GEMINI_KEY not in page
    assert "RGAPI-brand-new-7777" not in caplog.text and GEMINI_KEY not in caplog.text


@pytest.mark.parametrize("fields", [
    {"riot_id": "SinTag"}, {"riot_id": ""}, {"plataforma": "xx9"},
    {"riot_api_key": "RGAPI con espacios"}, {"gemini_api_key": "clave-ñandú"},
])
def test_invalid_input_is_rejected_without_touching_env(root, fields):
    configured(root)
    before = (root / ".env").read_text()
    c = web.create_app(root).test_client()
    resp = save(c, **fields)
    assert resp.status_code == 400 and resp.get_json()["error"]
    assert (root / ".env").read_text() == before


def test_riot_id_with_spaces_and_hash_round_trips(root):
    configured(root)
    c = web.create_app(root).test_client()
    assert save(c, riot_id="Amigo Fiel #1#LAN").status_code == 200
    assert env(root)["RIOT_ID"] == "Amigo Fiel #1#LAN"
    assert 'value="Amigo Fiel #1#LAN"' in text(c.get("/configuracion"))


def test_permanent_key_flag_is_saved(root):
    configured(root)
    c = web.create_app(root).test_client()
    save(c, key_permanente=True)
    assert env(root)["RIOT_API_KEY_PERMANENT"] == "1"
    assert "checked" in text(c.get("/configuracion"))
    save(c, key_permanente=False)
    assert env(root)["RIOT_API_KEY_PERMANENT"] == "0"


def test_update_env_file_creates_file_and_keeps_lf(tmp_path):
    path = tmp_path / "sub" / ".env"
    settings.update_env_file(path, {"A": "1", "B": "x y"})
    assert dotenv_values(path) == {"A": "1", "B": "x y"}
    path.write_bytes(b"# c\nA=1\n")
    settings.update_env_file(path, {"A": "2", "C": "it's"})
    assert path.read_bytes().startswith(b"# c\nA=2\n") and b"\r" not in path.read_bytes()
    assert dotenv_values(path) == {"A": "2", "C": "it's"}


# ------------------------------------------------------------------ banner
def test_banner_when_key_is_old(root):
    configured(root, saved_at=time.time() - 24 * HOUR)
    c = web.create_app(root).test_client()
    html = text(c.get("/sincronizar"))
    assert BANNER in html and "hace 24 horas" in html
    assert 'href="https://developer.riotgames.com"' in html and 'href="/configuracion' in html
    assert BANNER in text(c.get("/configuracion"))


def test_no_banner_for_fresh_unknown_or_permanent_keys(root):
    for extra, saved in (("", time.time() - 22 * HOUR), ("", None),
                         ("RIOT_API_KEY_PERMANENT=1\n", time.time() - 99 * HOUR)):
        configured(root, saved_at=saved, extra=extra)
        assert BANNER not in text(web.create_app(root).test_client().get("/sincronizar"))


def test_banner_after_sync_with_invalid_key_and_cleared_by_new_key(root, monkeypatch):
    configured(root, saved_at=time.time())

    class RejectingClient:
        def __init__(self, api_key, region="americas", limiter=None):
            pass

        def get_puuid(self, name, tag):
            raise InvalidApiKeyError("expired")

    monkeypatch.setattr(web, "RiotClient", RejectingClient)
    app = web.create_app(root)
    c = app.test_client()
    assert BANNER not in text(c.get("/sincronizar"))
    c.post("/api/sync", json={"cola": "solo"}, headers=HEADERS)
    app.extensions["lolan"]["sync"].join(10)
    html = text(c.get("/sincronizar"))
    assert BANNER in html and "Riot la rechazó" in html
    # A different key (saved here or edited by hand in .env) is not the rejected one.
    assert save(c, riot_api_key="RGAPI-fresh-1234").status_code == 200
    assert BANNER not in text(c.get("/sincronizar"))


def test_no_network_needed_to_render_pages(root, monkeypatch):
    configured(root, saved_at=time.time() - 30 * HOUR)

    def boom(*a, **k):
        raise AssertionError("page render must not use the network")

    monkeypatch.setattr(requests.Session, "request", boom)
    c = web.create_app(root).test_client()
    assert c.get("/configuracion").status_code == 200 and c.get("/").status_code == 200


# ------------------------------------------------------------- key test
class ProbeClient:
    """Stands in for RiotClient in the key test; `outcome` is what the status call does."""
    outcome = None
    calls = []

    def __init__(self, api_key, region="americas", **kwargs):
        ProbeClient.calls.append((api_key, region, kwargs))

    def get_platform_status(self):
        if isinstance(ProbeClient.outcome, Exception):
            raise ProbeClient.outcome
        return {"id": "LA1"}


@pytest.fixture
def probe(monkeypatch):
    ProbeClient.calls = []
    ProbeClient.outcome = None
    monkeypatch.setattr(web, "RiotClient", ProbeClient)
    return ProbeClient


def test_probe_ok_uses_saved_key_and_platform(client, probe):
    resp = client.post("/api/configuracion/probar", json={"plataforma": "euw1"}, headers=HEADERS)
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True and data["estado"] == "ok"
    assert probe.calls[0][:2] == (RIOT_KEY, "euw1")
    assert RIOT_KEY not in text(resp)


def test_probe_prefers_typed_key_and_saves_nothing(client, root, probe):
    before = (root / ".env").read_text()
    resp = client.post("/api/configuracion/probar", json={"riot_api_key": "RGAPI-typed-1"},
                       headers=HEADERS)
    assert resp.get_json()["ok"] is True and probe.calls[0][0] == "RGAPI-typed-1"
    assert (root / ".env").read_text() == before


def test_probe_invalid_key(client, probe):
    probe.outcome = InvalidApiKeyError("nope")
    data = client.post("/api/configuracion/probar", json={}, headers=HEADERS).get_json()
    assert data["ok"] is False and data["estado"] == "invalida"
    assert "inválida o ya expiró" in data["mensaje"]
    assert BANNER in text(client.get("/sincronizar"))          # remembered for the banner
    probe.outcome = None
    assert client.post("/api/configuracion/probar", json={}, headers=HEADERS).get_json()["ok"]
    assert BANNER not in text(client.get("/sincronizar"))


def test_probe_connection_error(client, probe):
    probe.outcome = RiotConnectionError("down")
    data = client.post("/api/configuracion/probar", json={}, headers=HEADERS).get_json()
    assert data["ok"] is False and data["estado"] == "sin_conexion" and "Sin conexión" in data["mensaje"]
    assert BANNER not in text(client.get("/sincronizar"))      # offline is not an expired key


def test_probe_without_any_key(root, probe):
    write_env(root, "RIOT_ID=Yo#LAN\n")
    c = web.create_app(root).test_client()
    resp = c.post("/api/configuracion/probar", json={}, headers=HEADERS)
    assert resp.status_code == 400 and probe.calls == []


def test_probe_through_real_client_classifies_http_errors():
    class Session:
        def __init__(self, outcome):
            self.outcome, self.urls = outcome, []

        def get(self, url, **kwargs):
            self.urls.append(url)
            if isinstance(self.outcome, Exception):
                raise self.outcome

            class R:
                status_code, headers = self.outcome, {}

                def json(self):
                    return {}
            return R()

    session = Session(403)
    client = RiotClient("K", region="la1", session=session, max_retries=1)
    with pytest.raises(InvalidApiKeyError):
        client.get_platform_status()
    assert session.urls == ["https://la1.api.riotgames.com/lol/status/v4/platform-data"]
    client = RiotClient("K", region="la1", session=Session(requests.ConnectionError()), max_retries=1)
    with pytest.raises(RiotConnectionError):
        client.get_platform_status()
    assert RiotClient("K", region="la1", session=Session(200)).get_platform_status() == {}


# --------------------------------------------------------- fresh RIOT_ID
def test_main_player_follows_riot_id_without_restart(root):
    configured(root)
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player("puuid-4", "Amigo#LAN")
    store.close()
    c = web.create_app(root).test_client()

    def main_row(html):
        row = html.split('data-riot-id="')[1:]
        return [r.split('"')[0] for r in row if "Tú</span>" in r.split("</tr>")[0]]

    assert main_row(text(c.get("/jugadores"))) == ["Yo#LAN"]
    assert "Yo (Yo#LAN)" in text(c.get("/sincronizar"))
    assert save(c, riot_id="Amigo#LAN").status_code == 200
    assert main_row(text(c.get("/jugadores"))) == ["Amigo#LAN"]
    assert "Yo (Amigo#LAN)" in text(c.get("/sincronizar"))
    assert "Amigo#LAN (tú)</option>" in text(c.get("/partidas")).split("selected>")[1]
