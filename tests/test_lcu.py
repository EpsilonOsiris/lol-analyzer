"""League client (LCU) recorder: lockfile, client (auth, loopback, TLS) and the champion-select capture."""
import base64
import http.server
import json
import ssl
import threading
import time

import pytest
import requests

from lolan import lcu, live
from lolan.lcu import ChampSelectRecorder, LcuClient, parse_lockfile

GOOD = "LeagueClient:4242:51234:s3cr3t-Pass_word:https"


# ------------------------------------------------------------------ lockfile
def test_parse_lockfile_good():
    lock = parse_lockfile(GOOD + "\n")
    assert (lock.pid, lock.port, lock.password, lock.protocol) == (4242, 51234, "s3cr3t-Pass_word", "https")
    assert "s3cr3t" not in repr(lock)                    # the password never shows up in logs


@pytest.mark.parametrize("text", [
    "", "   ", None, "LeagueClient", "LeagueClient:1:2:pw",                   # empty / cut
    "LeagueClient:x:51234:pw:https", "LeagueClient:1:port:pw:https",           # not numbers
    "LeagueClient:1:0:pw:https", "LeagueClient:1:70000:pw:https",              # port out of range
    "LeagueClient:1:51234::https", "LeagueClient:1:51234:pw:ftp",              # no password / odd protocol
    "LeagueClient:1:51234:pw:https:extra",
])
def test_parse_lockfile_malformed(text):
    assert parse_lockfile(text) is None


def test_read_lockfile_missing_and_present(tmp_path):
    assert lcu.read_lockfile(tmp_path / "lockfile") is None            # client closed
    (tmp_path / "lockfile").write_text(GOOD, encoding="utf-8")
    assert lcu.read_lockfile(tmp_path / "lockfile").port == 51234
    assert lcu.read_lockfile(tmp_path) is None                         # a folder is not a lockfile


def test_lockfile_path_order(tmp_path, monkeypatch):
    monkeypatch.delenv(lcu.LOCKFILE_ENV, raising=False)
    assert lcu.lockfile_path() == lcu.DEFAULT_LOCKFILE
    (tmp_path / ".env").write_text(f"LOL_DIR={tmp_path / 'juego'}\n", encoding="utf-8")
    monkeypatch.delenv("LOL_DIR", raising=False)
    assert lcu.lockfile_path(tmp_path) == tmp_path / "juego" / "lockfile"
    monkeypatch.setenv(lcu.LOCKFILE_ENV, str(tmp_path / "otro"))
    assert lcu.lockfile_path(tmp_path) == tmp_path / "otro"            # the test override wins


# -------------------------------------------------------------------- client
def test_only_loopback_addresses_are_used():
    with pytest.raises(ValueError):
        LcuClient(host="example.com")
    assert LcuClient(host="127.0.0.1").host == "127.0.0.1"


def test_no_lockfile_is_a_closed_client_not_an_error(tmp_path):
    client = LcuClient(lockfile=tmp_path / "lockfile")
    assert client.get(lcu.PHASE_PATH) == ("down", None) and client.phase() is None
    assert client.problem is None


def test_closed_port_is_simply_down(tmp_path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (tmp_path / "lockfile").write_text(f"LeagueClient:1:{port}:pw:https", encoding="utf-8")
    client = LcuClient(lockfile=tmp_path / "lockfile", timeout=0.5)
    assert client.phase() is None and client.problem is None


class FakeLcu:
    """HTTP(S) server on 127.0.0.1 posing as the League client: basic auth `riot:<password>`."""

    def __init__(self, routes, password="s3cr3t", tls=None):
        self.routes, self.password, self.seen = routes, password, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                want = "Basic " + base64.b64encode(f"riot:{outer.password}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                route = outer.routes.get(self.path)
                body = b'{"httpStatus":404}' if route is None else json.dumps(route).encode()
                self.send_response(404 if route is None else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls is not None:
            ctx = live._stdlib_context_class()(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def lockfile(self, folder, protocol="https", password=None):
        path = folder / "lockfile"
        path.write_text(f"LeagueClient:99:{self.port}:{password or self.password}:{protocol}", encoding="utf-8")
        return path

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_client_sends_basic_auth_and_maps_statuses_over_http(tmp_path):
    fake = FakeLcu({lcu.PHASE_PATH: "Lobby", lcu.LOBBY_PATH: {"gameConfig": {}}})
    try:
        client = LcuClient(lockfile=fake.lockfile(tmp_path, protocol="http"))
        assert client.phase() == "Lobby"
        expect = "Basic " + base64.b64encode(b"riot:s3cr3t").decode()
        assert fake.seen[0] == (lcu.PHASE_PATH, expect)
        assert client.lobby() == {"gameConfig": {}}
        assert client.get(lcu.CHAMP_SELECT_PATH) == ("none", None)      # 404: not in champion select
        assert client.champ_select() is None
        wrong = LcuClient(lockfile=fake.lockfile(tmp_path, protocol="http", password="otra"))
        assert wrong.phase() is None and wrong.problem == "auth"        # stale lockfile
    finally:
        fake.close()


def test_client_follows_a_restarted_client_with_a_new_port(tmp_path):
    one, two = FakeLcu({lcu.PHASE_PATH: "None"}), FakeLcu({lcu.PHASE_PATH: "Lobby"})
    try:
        client = LcuClient(lockfile=tmp_path / "lockfile")
        one.lockfile(tmp_path, "http")
        assert client.phase() == "None"
        two.lockfile(tmp_path, "http")
        assert client.phase() == "Lobby"
    finally:
        one.close()
        two.close()


def test_client_talks_https_to_a_server_signed_by_the_riot_root(tmp_path):
    ca = pytest.importorskip("test_live_tls")
    ca_key, ca_cert = ca.make_ca()
    key, cert = ca.make_server_cert(ca_key, ca_cert)
    (tmp_path / "ca.pem").write_bytes(ca.pem_cert(ca_cert))
    (tmp_path / "srv.pem").write_bytes(ca.pem_cert(cert) + ca.pem_key(key))
    fake = FakeLcu({lcu.PHASE_PATH: "ChampSelect"}, tls=tmp_path / "srv.pem")
    try:
        client = LcuClient(lockfile=fake.lockfile(tmp_path), ca_file=tmp_path / "ca.pem")
        assert client.phase() == "ChampSelect"
        assert client.tls_mode in ("riot", "system", "unverified")      # an antivirus may re-sign local TLS
        assert fake.seen[0][1].startswith("Basic ")
        # a certificate from another root is refused when no fallback is allowed
        other_key, other_ca = ca.make_ca("Otra raiz")
        (tmp_path / "other.pem").write_bytes(ca.pem_cert(other_ca))
        strict = LcuClient(lockfile=tmp_path / "lockfile", ca_file=tmp_path / "other.pem",
                           system_fallback=False, unverified_fallback=False)
        assert strict.phase() is None and strict.problem == "tls"
    finally:
        fake.close()


# ------------------------------------------------------------------ recorder
class FakeClient:
    """Scripted League client: each step() of the recorder consumes one entry of `script`."""

    def __init__(self):
        self.phase_now = None
        self.problem = None
        self.champ = None
        self.lobby_data = {"lobby": "x"}
        self.gameflow = {"gameData": {"gameId": 0}}
        self.calls = []

    def phase(self):
        self.calls.append("phase")
        return self.phase_now

    def champ_select(self):
        self.calls.append("champ_select")
        return self.champ

    def lobby(self):
        return self.lobby_data

    def gameflow_session(self):
        return self.gameflow

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        self.now += 1
        return self.now


def session(time_left, counter=1, **extra):
    return {"counter": counter, "timer": {"adjustedTimeLeftInPhase": time_left, "internalNowInEpochMs": 1000 + time_left,
                                          "phase": "BAN_PICK", "totalTimeInPhase": 30000}, "myTeam": [], **extra}


@pytest.fixture
def rig(tmp_path):
    class Rig:
        client = FakeClient()
        clock = Clock()
        said, warned, phases = [], [], []
        out = tmp_path / "capturas"

        def __init__(self):
            self.rec = ChampSelectRecorder(self.client, out_dir=self.out, clock=self.clock, say=self.said.append,
                                           warn=self.warned.append, on_phase=self.phases.append)

        def go(self, phase, champ=None):
            self.client.phase_now, self.client.champ = phase, champ
            return self.rec.step()

        def lines(self, index=-1):
            files = sorted(self.out.glob("seleccion-*.jsonl"))
            return [json.loads(x) for x in files[index].read_text(encoding="utf-8").splitlines()]

    Rig.client, Rig.clock = FakeClient(), Clock()
    Rig.said, Rig.warned, Rig.phases = [], [], []
    return Rig()


def test_poll_cadence_fast_when_the_client_answers_slow_when_closed(rig):
    assert rig.go(None) == lcu.IDLE_S
    assert rig.go("Lobby") == lcu.FAST_S
    assert not rig.out.exists()                                         # nothing recorded outside champion select


def test_lobby_to_champ_select_to_game_writes_one_file(rig):
    rig.go("Lobby")
    rig.go("Matchmaking")
    rig.go("ChampSelect", session(30000))
    rig.go("ChampSelect", session(29000))                               # only the clock moved: not written
    rig.go("ChampSelect", session(28000, counter=2, myTeam=[{"cellId": 0}]))
    rig.client.gameflow = {"gameData": {"gameId": 777}}
    rig.go("InProgress")
    lines = rig.lines()
    assert [x["kind"] for x in lines] == ["phase", "gameflow_session", "lobby", "champ_select", "champ_select",
                                          "phase", "gameflow_session", "end"]
    assert lines[0]["phase"] == "ChampSelect" and lines[1]["session"] == {"gameData": {"gameId": 0}}
    assert lines[2]["lobby"] == {"lobby": "x"}
    assert lines[3]["session"] == session(30000)                        # the raw session, as received
    assert lines[4]["session"]["myTeam"] == [{"cellId": 0}]
    assert lines[5]["phase"] == "InProgress" and lines[6]["session"]["gameData"]["gameId"] == 777
    assert lines[7] == {"t": lines[7]["t"], "kind": "end", "result": "game", "phase": "InProgress"}
    assert all("t" in x for x in lines)
    assert len(rig.said) == 1 and rig.said[0].startswith("Selección de campeones grabada: data/capturas/seleccion-")
    assert rig.phases == ["Lobby", "Matchmaking", "ChampSelect", "InProgress"]
    assert rig.rec.step() == lcu.FAST_S                                  # still in game: nothing more is written
    assert len(rig.lines()) == 8


def test_dodge_goes_back_to_the_lobby(rig):
    rig.go("Matchmaking")
    rig.go("ChampSelect", session(30000))
    rig.go("Lobby")
    lines = rig.lines()
    assert [x["kind"] for x in lines][-2:] == ["phase", "end"]
    assert lines[-1]["result"] == "dodge" and lines[-2]["phase"] == "Lobby"
    assert not any(x["kind"] == "gameflow_session" for x in lines[3:])   # no second gameflow session on a dodge


def test_a_missing_champ_select_session_still_makes_a_capture(rig):
    rig.go("ChampSelect", None)                                          # the endpoint answers 404 at first
    rig.go("ChampSelect", session(30000))
    assert [x["kind"] for x in rig.lines()] == ["phase", "gameflow_session", "lobby", "champ_select"]


def test_client_closed_mid_select_closes_the_file(rig):
    rig.go("ChampSelect", session(30000))
    assert rig.go(None) == lcu.FAST_S                                    # one missed answer is tolerated
    assert rig.said == []
    rig.go(None)
    assert rig.go(None) == lcu.IDLE_S
    lines = rig.lines()
    assert lines[-1]["kind"] == "end" and lines[-1]["result"] == "closed"
    assert len(rig.said) == 1 and rig.phases[-1] is None
    rig.go("ChampSelect", session(30000))                                # the next select is a new file
    assert len(list(rig.out.glob("seleccion-*.jsonl"))) == 2


def test_a_blip_does_not_split_the_capture(rig):
    rig.go("ChampSelect", session(30000))
    rig.go(None)
    rig.go("ChampSelect", session(29000))
    rig.go("InProgress")
    assert len(list(rig.out.glob("seleccion-*.jsonl"))) == 1


def test_stopping_closes_an_open_capture(rig):
    rig.go("ChampSelect", session(30000))
    rig.rec.close()
    assert rig.lines()[-1]["result"] == "stopped" and len(rig.said) == 1


def test_rotation_keeps_the_newest_30(rig):
    rig.out.mkdir()
    for i in range(35):
        (rig.out / f"seleccion-20260101-0000{i:02d}.jsonl").write_text("{}\n", encoding="utf-8")
    rig.go("ChampSelect", session(30000))
    files = sorted(p.name for p in rig.out.glob("seleccion-*.jsonl"))
    assert len(files) == 30
    assert "seleccion-20260101-000000.jsonl" not in files and "seleccion-20260101-000005.jsonl" not in files
    assert files[0] == "seleccion-20260101-000006.jsonl"
    other = rig.out / "notas.txt"                                        # only capture files are touched
    other.write_text("x", encoding="utf-8")
    rig.go("Lobby")
    assert other.exists() and len(list(rig.out.glob("seleccion-*.jsonl"))) == 30


def test_two_captures_in_the_same_second_do_not_overwrite(rig):
    rig.rec._clock = lambda: 1_800_000_000.0
    rig.go("ChampSelect", session(30000))
    rig.go("Lobby")
    rig.go("ChampSelect", session(30000))
    assert len(list(rig.out.glob("seleccion-*.jsonl"))) == 2


def test_problems_never_raise_and_warn_once(rig):
    class Broken(FakeClient):
        def phase(self):
            raise RuntimeError("boom")

    rig.rec._client = Broken()
    for _ in range(3):
        assert rig.rec.step() == lcu.IDLE_S
    assert len(rig.warned) == 1 and "boom" in rig.warned[0]


def test_unwritable_folder_warns_once_and_keeps_going(rig, tmp_path):
    blocker = tmp_path / "ocupado"
    blocker.write_text("no soy una carpeta", encoding="utf-8")
    rig.rec.out_dir = blocker / "capturas"
    rig.go("ChampSelect", session(30000))
    rig.go("ChampSelect", session(29000, counter=3))
    rig.go("Lobby")
    assert len(rig.warned) == 1 and "grabación" in rig.warned[0] and rig.said == []


def test_certificate_and_auth_problems_are_reported_once(rig):
    rig.client.problem = "tls"
    for _ in range(3):
        rig.go(None)
    assert len(rig.warned) == 1 and "certificado" in rig.warned[0]
    rig.client.problem = "auth"
    rig.go(None)
    rig.go(None)
    assert len(rig.warned) == 2


def test_stable_form_ignores_only_the_ticking_timer_fields():
    a, b = session(30000), session(1000)
    assert lcu.stable_form(a) == lcu.stable_form(b)
    assert lcu.stable_form(a) != lcu.stable_form(session(30000, counter=2))
    changed_phase = session(30000)
    changed_phase["timer"]["phase"] = "FINALIZATION"
    assert lcu.stable_form(a) != lcu.stable_form(changed_phase)
    assert lcu.stable_form("raro") == "raro" and lcu.stable_form({"timer": 5}) == {"timer": 5}


def test_the_thread_follows_the_script_and_stops(tmp_path):
    client = FakeClient()
    client.phase_now, client.champ = "ChampSelect", session(30000)
    rec = ChampSelectRecorder(client, out_dir=tmp_path / "capturas", fast_s=0.01, idle_s=0.01,
                              say=lambda text: None, warn=lambda text: None)
    rec.start()
    deadline = time.time() + 5
    while time.time() < deadline and not list((tmp_path / "capturas").glob("*.jsonl")):
        time.sleep(0.01)
    rec.stop()
    lines = [json.loads(x) for x in next((tmp_path / "capturas").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "phase" and lines[-1]["result"] == "stopped"


# --------------------------------------------------------------------- wiring
def test_start_live_starts_the_recorder_unless_disabled(tmp_path, monkeypatch):
    from lolan import web

    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv(lcu.LOCKFILE_ENV, str(tmp_path / "no-hay-lockfile"))
    started = []
    monkeypatch.setattr(lcu.ChampSelectRecorder, "start", lambda self: started.append(self.out_dir))

    class NoLive:
        def start(self):
            pass

    app = web.create_app(tmp_path, live=NoLive())
    app.extensions["lolan"]["scout"] = None
    app.extensions["lolan"]["warm_up_async"] = None
    web.start_live(app)
    assert started == [tmp_path / "data" / "capturas"]
    monkeypatch.setenv(lcu.DISABLE_ENV, "off")
    web.start_live(app)
    assert len(started) == 1


def test_cli_command_is_registered(tmp_path, monkeypatch, capsys):
    from lolan import __main__ as cli

    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv(lcu.LOCKFILE_ENV, str(tmp_path / "no-hay-lockfile"))
    assert "grabar-seleccion" in cli.COMMANDS
    steps = []

    def fake_sleep(seconds):
        steps.append(seconds)
        if len(steps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert cli.main(["grabar-seleccion"]) == 0
    out = capsys.readouterr().out
    assert steps == [lcu.IDLE_S, lcu.IDLE_S] and "Ctrl+C" in out and "Grabación detenida" in out
