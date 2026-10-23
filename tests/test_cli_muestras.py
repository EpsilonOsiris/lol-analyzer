import pytest
from lane_fixtures import BLUE_MID, bought, make_lane_match, make_lane_timeline, mid_edge
from test_sampler import FakeApi, FakeDD, FakeOpgg, board

from lolan import sampler
from lolan.__main__ import main
from lolan.riot import InvalidApiKeyError
from lolan.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOLAN_LCU_LOCKFILE", str(tmp_path / "no-lockfile"))
    return tmp_path


def fill(home):
    store = Store(home / "data" / "lol.db")
    store.add_player("puuid-3", "Epsilon Osiris#LAN", is_main=True)
    gold, xp = mid_edge(100, 30)
    for i in range(3):
        store.save_match(f"LA1_{i}", make_lane_match(f"LA1_{i}", names={3: "Epsilon Osiris"}, puuids={3: "puuid-3"},
                                                     created=1_700_000_000_000 + i),
                         make_lane_timeline(gold=gold, xp=xp, events=[bought(BLUE_MID, 3031, 600_000)]))
    store.close()


@pytest.fixture
def fakes(monkeypatch):
    """Riot / OP.GG / Data Dragon replaced; returns the per-region FakeApi objects."""
    apis = {"kr": FakeApi(players={"P-a1": ["KR_1", "KR_2"]}), "euw": FakeApi(players={"P-b1": ["EUW1_1"]})}
    opgg = FakeOpgg({("KR", 103): board("a1"), ("EUW", 103): board("b1")})
    monkeypatch.setattr(sampler, "RiotRegionApi", lambda region, key, limiters: apis[region.name])
    monkeypatch.setattr(sampler, "OpggLeaderboards", lambda: opgg)
    monkeypatch.setattr("lolan.ddragon.DDragon", lambda path: FakeDD())
    return apis


def test_help_is_in_spanish(capsys):
    with pytest.raises(SystemExit):
        main(["muestras", "--help"])
    out = capsys.readouterr().out
    assert "--horas" in out and "--regiones" in out and "--max-partidas" in out and "--solo-pool" in out
    assert "--estado" in out and "se detiene" in out


def test_estado_without_a_download(home, capsys):
    assert main(["muestras", "--estado"]) == 0
    assert "Todavía no hay descarga" in capsys.readouterr().out


def test_errors_are_friendly(home, capsys):
    assert main(["muestras", "--regiones", "marte"]) == 1
    assert "Región desconocida" in capsys.readouterr().err
    assert main(["muestras"]) == 1
    assert "RIOT_API_KEY" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["muestras", "--horas", "0"])


def test_solo_pool_needs_a_pool(home, monkeypatch, capsys):
    monkeypatch.setenv("RIOT_API_KEY", "KEY")
    assert main(["muestras", "--solo-pool"]) == 1
    assert "pool del clan" in capsys.readouterr().err


def test_run_downloads_both_default_regions_and_reports(home, monkeypatch, fakes, capsys):
    monkeypatch.setenv("RIOT_API_KEY", "KEY")
    fill(home)
    assert main(["muestras", "--solo-pool", "--max-partidas", "30"]) == 0
    out = capsys.readouterr().out
    assert "KR, EUW" in out and "pool del clan: 1 campeones" in out and "Descarga terminada" in out
    assert "Esta vez: 3 partidas" in out and "/h)" in out and "Filas por campeón del pool" in out
    assert (home / "data" / "lineas" / "descarga.json").is_file()
    assert (home / "data" / "lineas" / "duelos" / "MIDDLE" / "103.json").is_file()
    assert main(["muestras", "--estado"]) == 0
    status = capsys.readouterr().out
    assert "Partidas descargadas: 3" in status and "Región KR" in status and "Región EUW" in status
    assert main(["lineas", "Champ3", "--linea", "mid"]) == 0
    assert "Champ3 (Mid)" in capsys.readouterr().out


def test_regions_option_limits_the_regions(home, monkeypatch, fakes, capsys):
    monkeypatch.setenv("RIOT_API_KEY", "KEY")
    fill(home)
    assert main(["muestras", "--solo-pool", "--regiones", "euw1"]) == 0
    out = capsys.readouterr().out
    assert "EUW" in out.splitlines()[0] and "KR" not in out.splitlines()[0]
    assert not fakes["kr"].log


def test_invalid_key_exits_with_a_message_and_keeps_progress(home, monkeypatch, fakes, capsys):
    monkeypatch.setenv("RIOT_API_KEY", "KEY")
    fill(home)
    fakes["kr"].fail["match"] = InvalidApiKeyError("x")
    assert main(["muestras", "--solo-pool", "--regiones", "kr"]) == 1
    out = capsys.readouterr().out
    assert "expiró" in out and "quedó guardado" in out
    assert (home / "data" / "lineas" / "descarga.json").is_file()
