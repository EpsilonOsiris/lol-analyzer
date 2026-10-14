"""CLI behavior with several players: jugadores, --jugador, --clan."""
import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan import __main__ as cli
from lolan.__main__ import main
from lolan.riot import InvalidApiKeyError, NotFoundError
from lolan.store import Store

FRIEND = "puuid-4"          # blue-side teammate in every make_match game
HOUR = 3600_000


class FakeRiot:
    """Stands in for RiotClient in the CLI; configure through the class attributes."""
    accounts = {"Yo#LAN": ME_PUUID, "Amigo Fiel#LAN": FRIEND, "Otro#LAN": "puuid-5"}
    match_ids = {}
    not_found = set()
    expired_for = None
    created = []
    matches_fetched = []

    def __init__(self, api_key, region="americas", **kw):
        FakeRiot.created.append((api_key, region))

    def get_puuid(self, name, tag):
        # Like the real API, Riot IDs are matched case-insensitively.
        known = {k.casefold(): k for k in FakeRiot.accounts}
        key = known.get(f"{name}#{tag}".casefold(), f"{name}#{tag}")
        if key == FakeRiot.expired_for:
            raise InvalidApiKeyError("La API key es inválida o expiró.")
        if key in FakeRiot.not_found or key not in FakeRiot.accounts:
            raise NotFoundError("404")
        return FakeRiot.accounts[key]

    def get_riot_id(self, puuid):
        key = next((k for k, v in FakeRiot.accounts.items() if v == puuid), None)
        if key is not None and key == FakeRiot.expired_for:
            raise InvalidApiKeyError("La API key es inválida o expiró.")
        if key is None or key in FakeRiot.not_found:
            raise NotFoundError("404")
        return key

    def list_match_ids(self, puuid, start_time=None, queue=None):
        return list(FakeRiot.match_ids.get(puuid, []))

    def get_match(self, match_id):
        FakeRiot.matches_fetched.append(match_id)
        return make_match(match_id, created=BASE_TIME_MS + int(match_id.split("_")[1]) * HOUR)

    def get_timeline(self, match_id):
        return make_timeline()


@pytest.fixture
def clan(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RIOT_API_KEY", "RGAPI-test")
    monkeypatch.setenv("RIOT_ID", "Yo#LAN")
    monkeypatch.setattr(cli, "RiotClient", FakeRiot)
    FakeRiot.match_ids, FakeRiot.not_found, FakeRiot.expired_for = {}, set(), None
    FakeRiot.created, FakeRiot.matches_fetched = [], []
    return tmp_path


def seed_clan(root, games=6):
    """Main player + a clan member + another registered player; every game has all three."""
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player(FRIEND, "Amigo Fiel#LAN")
    store.add_player("puuid-5", "Otro#LAN")
    for i in range(games):
        m = make_match(f"LA1_{i}", queue=420 if i % 2 else 440, win=bool(i % 3),
                       created=BASE_TIME_MS + i * HOUR)
        store.save_match(f"LA1_{i}", m, make_timeline())
    store.close()


def register(root, *players):
    store = Store(root / "data" / "lol.db")
    for puuid, riot_id in players:
        store.add_player(puuid, riot_id)
    store.close()


# ---------------------------------------------------------------- jugadores
def test_jugadores_agregar_listar_quitar(clan, capsys):
    assert main(["jugadores"]) == 0
    assert "No hay jugadores registrados" in capsys.readouterr().out

    seed = Store(clan / "data" / "lol.db")
    seed.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    seed.save_match("LA1_1", make_match("LA1_1"), None)
    seed.close()

    assert main(["jugadores", "agregar", "Amigo Fiel#LAN"]) == 0
    out = capsys.readouterr().out
    assert "Jugador agregado: Amigo Fiel#LAN" in out and "guardadas" in out
    assert "sync --jugador" in out
    assert FakeRiot.created == [("RGAPI-test", "americas")]
    assert main(["jugadores", "agregar", "amigo fiel#lan"]) == 0
    assert "Ya estaba registrado" in capsys.readouterr().out

    assert main(["jugadores", "listar"]) == 0
    out = capsys.readouterr().out
    assert "Jugadores registrados (2)" in out
    rows = [ln for ln in out.splitlines() if "#LAN" in ln]
    assert "Yo#LAN" in rows[0] and "(tú)" in rows[0] and "1 partidas" in rows[0]
    assert "Amigo Fiel#LAN" in rows[1] and "1 partidas" in rows[1] and "(tú)" not in rows[1]

    assert main(["jugadores", "quitar", "Yo#LAN"]) == 1
    assert "jugador principal" in capsys.readouterr().err
    assert main(["jugadores", "quitar", "Nadie#LAN"]) == 1
    assert "Registrados" in capsys.readouterr().err
    assert main(["jugadores", "quitar", "AMIGO FIEL#lan"]) == 0
    assert "siguen guardadas" in capsys.readouterr().out
    store = Store(clan / "data" / "lol.db")
    assert [p.riot_id for p in store.list_players()] == ["Yo#LAN"]
    assert store.count_matches() == 1                      # matches are kept
    store.close()


def test_jugadores_agregar_errors_are_friendly(clan, capsys, monkeypatch):
    assert main(["jugadores", "agregar", "sin_tag"]) == 1
    assert "Nombre#TAG" in capsys.readouterr().err
    assert main(["jugadores", "agregar", "Fantasma#LAN"]) == 1
    assert "No se encontró la cuenta Fantasma#LAN" in capsys.readouterr().err
    FakeRiot.expired_for = "Amigo Fiel#LAN"
    assert main(["jugadores", "agregar", "Amigo Fiel#LAN"]) == 1
    assert "expiró" in capsys.readouterr().err
    monkeypatch.delenv("RIOT_API_KEY")
    assert main(["jugadores", "agregar", "Amigo Fiel#LAN"]) == 1
    assert "RIOT_API_KEY" in capsys.readouterr().err


# ------------------------------------------------------------------ analyze
def test_analyze_player_paths(clan):
    seed_clan(clan)
    reports, data = clan / "reports", clan / "data"
    assert main(["analyze"]) == 0                                  # main: today's layout
    assert (reports / "resumen.md").exists() and (data / "games.csv").exists()
    assert (reports / "campeones" / "Ahri.md").exists()
    assert not (reports / "jugadores").exists()

    assert main(["analyze", "--jugador", "amigo fiel#lan"]) == 0
    base = reports / "jugadores" / "amigo-fiel"
    assert (base / "resumen.md").exists() and (base / "campeones" / "Champ4.md").exists()
    assert not (base / "campeones" / "Ahri.md").exists()
    assert (data / "games-amigo-fiel.csv").exists()

    assert main(["analyze", "--jugador", "Amigo Fiel#LAN", "--cola", "solo"]) == 0
    assert (base / "solo" / "partidas").is_dir()
    assert (data / "games-amigo-fiel-solo.csv").exists()
    assert main(["analyze", "--cola", "flex"]) == 0
    assert (reports / "flex" / "resumen.md").exists() and (data / "games-flex.csv").exists()

    # Re-analyzing the main player (which wipes its own campeones/ and partidas/) leaves the
    # other players' reports alone.
    marker = base / "partidas" / "marker.md"
    marker.write_text("x", encoding="utf-8")
    assert main(["analyze"]) == 0
    assert marker.exists() and (base / "solo" / "partidas").is_dir()
    assert "Champ4" not in (data / "games.csv").read_text(encoding="utf-8")
    assert "Ahri" not in (data / "games-amigo-fiel.csv").read_text(encoding="utf-8")


def test_analyze_clan_writes_everyone(clan, capsys):
    seed_clan(clan)
    register(clan, ("puuid-nada", "Sin Partidas#LAN"))
    assert main(["analyze", "--clan"]) == 0
    out = capsys.readouterr().out
    assert "=== Yo#LAN (tú) (1/4) ===" in out
    assert "Sin Partidas#LAN" in out and "Sin partidas guardadas" in out
    assert "sync --jugador" in out
    reports = clan / "reports"
    assert (reports / "resumen.md").exists()
    assert (reports / "jugadores" / "amigo-fiel" / "resumen.md").exists()
    assert (reports / "jugadores" / "otro" / "resumen.md").exists()
    assert not (reports / "jugadores" / "sin-partidas").exists()

    assert main(["analyze", "--clan", "--cola", "solo"]) == 0
    assert (reports / "solo" / "resumen.md").exists()
    assert (reports / "jugadores" / "otro" / "solo" / "resumen.md").exists()
    assert (clan / "data" / "games-otro-solo.csv").exists()


def test_clan_and_jugador_are_mutually_exclusive(clan):
    with pytest.raises(SystemExit):
        main(["analyze", "--clan", "--jugador", "Yo#LAN"])
    with pytest.raises(SystemExit):
        main(["sync", "--clan", "--jugador", "Yo#LAN"])


def test_player_flag_errors(clan, capsys):
    seed_clan(clan)
    assert main(["analyze", "--jugador", "Nadie#LAN"]) == 1
    err = capsys.readouterr().err
    assert "Nadie#LAN" in err and "jugadores agregar" in err
    # A registered player with no stored matches: friendly pointer to sync.
    register(clan, ("puuid-nada", "Sin Partidas#LAN"))
    assert main(["analyze", "--jugador", "Sin Partidas#LAN"]) == 0
    assert 'sync --jugador "Sin Partidas#LAN"' in capsys.readouterr().out


def test_no_main_player_is_reported_not_guessed(clan, capsys, monkeypatch):
    store = Store(clan / "data" / "lol.db")
    store.add_player(FRIEND, "Amigo Fiel#LAN")                  # registered, but not the main one
    store.save_match("LA1_1", make_match("LA1_1"), None)
    store.close()
    monkeypatch.setenv("RIOT_ID", "Otro Yo#LAN")
    assert main(["analyze"]) == 1
    assert "jugador principal" in capsys.readouterr().err
    assert not (clan / "reports").exists()                      # nothing written for "someone"


def test_game_and_campeon_take_a_player(clan, capsys):
    seed_clan(clan)
    assert main(["game", "ultima", "--jugador", "Amigo Fiel#LAN"]) == 0
    assert "Champ4" in capsys.readouterr().out
    assert main(["game", "ultima"]) == 0
    assert "Ahri" in capsys.readouterr().out
    assert main(["campeon", "Champ4", "--jugador", "Amigo Fiel#LAN"]) == 0
    assert "Champ4" in capsys.readouterr().out
    assert main(["campeon", "Champ4"]) == 1                    # the main player never played it
    assert "Campeones jugados: Ahri" in capsys.readouterr().out


# --------------------------------------------------------------------- sync
def test_sync_default_jugador_and_clan(clan, capsys):
    FakeRiot.match_ids = {ME_PUUID: ["LA1_1", "LA1_2"], FRIEND: ["LA1_2", "LA1_3"],
                          "puuid-5": ["LA1_9"]}
    register(clan, (FRIEND, "Amigo Fiel#LAN"), ("puuid-5", "Otro#LAN"))

    assert main(["sync"]) == 0                                    # main only, as before
    assert sorted(FakeRiot.matches_fetched) == ["LA1_1", "LA1_2"]
    out = capsys.readouterr().out
    assert "===" not in out and out.rstrip().endswith("python -m lolan analyze")

    assert main(["sync", "--jugador", "amigo fiel#lan"]) == 0     # the shared LA1_2 is not refetched
    assert sorted(FakeRiot.matches_fetched) == ["LA1_1", "LA1_2", "LA1_3"]
    assert 'analyze --jugador "Amigo Fiel#LAN"' in capsys.readouterr().out

    assert main(["sync", "--jugador", "Desconocido#LAN"]) == 1
    assert "jugadores agregar" in capsys.readouterr().err

    assert main(["sync", "--clan", "--max", "1"]) == 0
    out = capsys.readouterr().out
    assert "=== Yo#LAN (1/3) ===" in out and "=== Otro#LAN (3/3) ===" in out
    assert "Resumen por jugador" in out and "analyze --clan" in out
    assert "LA1_9" in FakeRiot.matches_fetched


def test_sync_clan_survives_not_found_and_stops_on_bad_key(clan, capsys):
    FakeRiot.match_ids = {ME_PUUID: ["LA1_1"], FRIEND: ["LA1_2"], "puuid-5": ["LA1_3"]}
    register(clan, (FRIEND, "Amigo Fiel#LAN"), ("puuid-5", "Otro#LAN"))

    FakeRiot.not_found = {"Amigo Fiel#LAN"}
    assert main(["sync", "--clan"]) == 1                          # something failed -> exit code 1
    out = capsys.readouterr().out
    assert "NO SINCRONIZADO" in out and "Amigo Fiel#LAN" in out
    assert sorted(FakeRiot.matches_fetched) == ["LA1_1", "LA1_3"]  # the others went through

    FakeRiot.not_found, FakeRiot.expired_for = set(), "Amigo Fiel#LAN"
    FakeRiot.matches_fetched = []
    register(clan, ("puuid-6", "Zeta#LAN"))
    FakeRiot.accounts["Zeta#LAN"] = "puuid-6"
    FakeRiot.match_ids["puuid-6"] = ["LA1_8"]
    assert main(["sync", "--clan"]) == 1
    assert "expiró" in capsys.readouterr().err
    assert "LA1_8" not in FakeRiot.matches_fetched               # nothing after the bad key
