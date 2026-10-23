"""TLS to the game client: chain checked against Riot's root, host name not, only on loopback.

The throwaway CA and server certificates are generated here with `cryptography` (tests are skipped
when it is not installed; it is not a dependency of the panel).

Some antivirus programs (Norton on the author's PC) re-sign even local TLS connections. The tests
that use real sockets then skip themselves; the same configuration is also proven with in-memory
handshakes, which nothing can intercept.
"""
import datetime
import http.server
import ipaddress
import json
import socket
import ssl
import threading

import pytest
import requests
from live_fixtures import make_allgamedata

from lolan import live

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn="Raiz de prueba"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_server_cert(ca_key, ca_cert, dns_names=("localhost",)):
    """A server certificate that is NOT valid for 127.0.0.1 (skipping the name check is what lets it in)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name("juego")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def pem_cert(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


@pytest.fixture
def game_server(tmp_path):
    """HTTPS server on 127.0.0.1 posing as the game client, signed by a throwaway CA."""
    ca_key, ca_cert = make_ca()
    key, cert = make_server_cert(ca_key, ca_cert)
    (tmp_path / "ca.pem").write_bytes(pem_cert(ca_cert))
    (tmp_path / "srv.pem").write_bytes(pem_cert(cert) + pem_key(key))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(make_allgamedata()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = live._stdlib_context_class()(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "srv.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"https://127.0.0.1:{server.server_address[1]}", "port": server.server_address[1],
           "ca": tmp_path / "ca.pem", "dir": tmp_path, "der": cert.public_bytes(serialization.Encoding.DER)}
    server.shutdown()
    server.server_close()


def require_untouched_tls(server):
    """Skip when an antivirus re-signs local TLS: the certificate the client sees is not ours."""
    probe = live._LoopbackContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    with probe.wrap_socket(socket.create_connection(("127.0.0.1", server["port"]), timeout=3),
                           server_hostname="x") as conn:
        seen = conn.getpeercert(True)
    if seen != server["der"]:
        pytest.skip("Un antivirus reescribe el TLS local en este equipo; la configuración se prueba "
                    "en memoria en test_in_memory_handshake_*")


@pytest.fixture
def truststore_injected():
    """The panel runs with truststore injected (main()); the live client must work like that too."""
    import truststore

    truststore.inject_into_ssl()
    try:
        yield
    finally:
        truststore.extract_from_ssl()


# ----------------------------------------------------------- real sockets
def test_chain_is_verified_and_host_name_is_not(game_server):
    require_untouched_tls(game_server)
    client = live.LiveClient(game_server["url"], ca_file=game_server["ca"])
    kind, data = client.fetch()
    assert kind == "ok" and data["gameData"]["gameMode"] == "CLASSIC" and client.problem is None
    assert client.tls_mode == "riot"


def test_works_with_truststore_injected(game_server, truststore_injected):
    require_untouched_tls(game_server)
    assert ssl.SSLContext.__module__ != "ssl"
    client = live.LiveClient(game_server["url"], ca_file=game_server["ca"])
    assert client.fetch()[0] == "ok" and client.tls_mode == "riot"
    # ...while a plain request through the injected store (no Riot root) is refused
    with pytest.raises(requests.exceptions.SSLError):
        requests.get(game_server["url"] + "/liveclientdata/allgamedata", timeout=2)


def test_a_certificate_from_another_root_is_refused(game_server):
    other_key, other_ca = make_ca("Otra raiz")
    path = game_server["dir"] / "other.pem"
    path.write_bytes(pem_cert(other_ca))
    client = live.LiveClient(game_server["url"], ca_file=path, system_fallback=False, unverified_fallback=False)
    assert client.fetch() == ("down", None)
    assert client.problem == "tls"                       # the page tells the user the certificate did not match


def test_poller_reports_the_certificate_problem(game_server):
    other_key, other_ca = make_ca("Otra raiz")
    path = game_server["dir"] / "other.pem"
    path.write_bytes(pem_cert(other_ca))
    poller = live.LivePoller(live.LiveClient(game_server["url"], ca_file=path, system_fallback=False, unverified_fallback=False))
    poller.poll_once()
    assert poller.snapshot()["status"] == "waiting" and poller.snapshot()["problem"] == "tls"


def test_closed_port_is_simply_down(game_server):
    with socket.socket() as probe:                       # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dead = live.LiveClient(f"https://127.0.0.1:{port}", ca_file=game_server["ca"], timeout=0.5)
    kind, _ = dead.fetch()
    assert kind == "down" and dead.problem is None


# ------------------------------------------------- in memory (cannot be intercepted)
def handshake(client_ctx, server_ctx, hostname="127.0.0.1"):
    """TLS handshake between two contexts entirely in memory (no sockets)."""
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=hostname)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(20):
        done = 0
        for side in (client, server):
            try:
                side.do_handshake()
                done += 1
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if done == 2:
            return client
    raise AssertionError("el saludo TLS no terminó")


def memory_pair(tmp_path, dns=("localhost",)):
    ca_key, ca_cert = make_ca()
    key, cert = make_server_cert(ca_key, ca_cert, dns)
    (tmp_path / "ca.pem").write_bytes(pem_cert(ca_cert))
    (tmp_path / "srv.pem").write_bytes(pem_cert(cert) + pem_key(key))
    server_ctx = live._stdlib_context_class()(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "srv.pem")
    return server_ctx, tmp_path / "ca.pem"


def foreign_root(tmp_path):
    other_key, other_ca = make_ca("Otra raiz")
    (tmp_path / "other.pem").write_bytes(pem_cert(other_ca))
    return tmp_path / "other.pem"


def test_in_memory_handshake_verifies_chain_but_not_host_name(tmp_path):
    server_ctx, ca = memory_pair(tmp_path)               # certificate only valid for "localhost"
    conn = handshake(live.make_ssl_context(ca), server_ctx, hostname="127.0.0.1")
    assert conn.version().startswith("TLS")


def test_in_memory_handshake_rejects_a_foreign_root(tmp_path):
    server_ctx, _ca = memory_pair(tmp_path)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(live.make_ssl_context(foreign_root(tmp_path)), server_ctx)


def test_in_memory_handshake_works_with_truststore_injected(tmp_path, truststore_injected):
    # truststore replaces ssl.SSLContext and ignores certificate files; it also makes the stdlib
    # setters recurse forever unless the context is built the way live.py does it
    assert ssl.SSLContext.__module__ != "ssl"
    server_ctx, ca = memory_pair(tmp_path)
    ctx = live.make_ssl_context(ca)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is False
    assert handshake(ctx, server_ctx).version().startswith("TLS")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(live.make_ssl_context(foreign_root(tmp_path)), server_ctx)


def test_system_store_fallback_when_the_antivirus_replaces_the_certificate():
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return make_allgamedata()

    class Session:
        def __init__(self, refuse):
            self.refuse = refuse
            self.calls = 0

        def get(self, url, timeout):
            self.calls += 1
            if self.refuse:
                raise requests.exceptions.SSLError("certificado reescrito")
            return Resp()

    client = live.LiveClient("https://127.0.0.1:2999")
    assert [m for m, _ in client._sessions] == ["riot", "system", "unverified"]
    riot, system = Session(True), Session(False)
    client._sessions = [("riot", riot), ("system", system)]
    kind, _ = client.fetch()
    assert kind == "ok" and client.tls_mode == "system" and client.problem is None
    assert [m for m, _ in client._sessions] == ["system", "riot"]       # keeps using what worked
    client.fetch()
    assert riot.calls == 1 and system.calls == 2
    client._sessions = [("riot", Session(True)), ("system", Session(True))]
    assert client.fetch() == ("down", None) and client.problem == "tls"  # both refuse: a certificate problem


def test_unverified_fallback_is_last_and_only_for_loopback():
    # Norton signs the game's certificate with its "Untrusted Root" (trusted by nobody): then the
    # loopback-only unverified context is the one that works.
    class Resp:
        status_code = 200

        def json(self):
            return {"allPlayers": [{"championName": "Jinx"}]}

    class Session:
        def __init__(self, refuse):
            self.refuse, self.calls = refuse, 0

        def get(self, *a, **k):
            self.calls += 1
            if self.refuse:
                raise requests.exceptions.SSLError("raiz no confiable")
            return Resp()

    client = live.LiveClient("https://127.0.0.1:2999")
    client._sessions = [("riot", Session(True)), ("system", Session(True)), ("unverified", Session(False))]
    kind, _ = client.fetch()
    assert client.tls_mode == "unverified" and client.problem is None and kind != "down"
    ctx = live.make_unverified_loopback_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    with pytest.raises(ValueError):
        live.LiveClient("https://example.com:2999")                      # never for a remote host


def test_fallback_can_be_disabled_and_checks_chain_not_name():
    assert [m for m, _ in live.LiveClient("https://127.0.0.1:2999", system_fallback=False, unverified_fallback=False)._sessions] == ["riot"]
    ctx = live.make_system_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is False


# ------------------------------------------------------------ configuration
def test_adapter_configuration_without_a_server():
    client = live.LiveClient("https://127.0.0.1:2999")
    adapter = client.session.get_adapter("https://127.0.0.1:2999/liveclientdata/allgamedata")
    assert isinstance(adapter, live.LoopbackAdapter)
    # every other https address keeps the normal, fully verifying adapter
    assert not isinstance(client.session.get_adapter("https://example.com/x"), live.LoopbackAdapter)
    assert not isinstance(client.session.get_adapter("https://127.0.0.1:3000/x"), live.LoopbackAdapter)
    ctx = adapter._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is False
    assert client.session.trust_env is False


def test_bundled_riot_root_loads_and_is_the_only_trust_anchor():
    assert live.CA_FILE.is_file()
    anchors = live.make_ssl_context().get_ca_certs()
    assert len(anchors) == 1 and "Riot Games" in str(anchors[0]["subject"])


def test_only_loopback_addresses_are_ever_used(monkeypatch):
    with pytest.raises(ValueError):
        live.LiveClient("https://example.com:2999")
    monkeypatch.setenv(live.TEST_URL_ENV, "https://example.com:2999")
    assert live.live_base_url() == live.DEFAULT_URL            # a foreign override is ignored
    monkeypatch.setenv(live.TEST_URL_ENV, "ftp://127.0.0.1:2999")
    assert live.live_base_url() == live.DEFAULT_URL
    monkeypatch.setenv(live.TEST_URL_ENV, "http://127.0.0.1:5555/")
    assert live.live_base_url() == "http://127.0.0.1:5555"
    monkeypatch.delenv(live.TEST_URL_ENV)
    assert live.live_base_url() == "https://127.0.0.1:2999"
    assert ipaddress.ip_address("127.0.0.1").is_loopback


def test_client_status_mapping_with_a_plain_http_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        mode = "404"

        def do_GET(self):
            mode = type(self).mode
            if mode == "404":
                self.send_response(404)
                body = b'{"errorCode":"RPC_ERROR","httpStatus":404}'
            elif mode == "garbage":
                self.send_response(200)
                body = b"<html>no json"
            elif mode == "empty":
                self.send_response(200)
                body = b"{}"
            else:
                self.send_response(200)
                body = json.dumps(make_allgamedata()).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = live.LiveClient(f"http://127.0.0.1:{server.server_address[1]}")
        for mode, expected in (("404", "loading"), ("garbage", "loading"), ("empty", "loading"), ("ok", "ok")):
            Handler.mode = mode
            assert client.fetch()[0] == expected, mode
    finally:
        server.shutdown()
        server.server_close()
