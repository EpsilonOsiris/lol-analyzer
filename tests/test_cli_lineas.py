import pytest
from lane_fixtures import BLUE_MID, bought, make_lane_match, make_lane_timeline, mid_edge

from lolan.__main__ import main
from lolan.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def fill(home, matches=3):
    store = Store(home / "data" / "lol.db")
    store.add_player("puuid-3", "Epsilon Osiris#LAN", is_main=True)
    gold, xp = mid_edge(100, 30)
    for i in range(matches):
        store.save_match(f"LA1_{i}", make_lane_match(f"LA1_{i}", names={3: "Epsilon Osiris"},
                                                     puuids={3: "puuid-3"}, created=1_700_000_000_000 + i),
                         make_lane_timeline(gold=gold, xp=xp, events=[bought(BLUE_MID, 3031, 600_000)]))
    store.save_match("LA1_short", make_lane_match("LA1_short", duration=700), make_lane_timeline())
    store.save_match("LA1_bare", make_lane_match("LA1_bare"), None)
    store.close()


def test_lineas_without_data_is_friendly(home, capsys):
    assert main(["lineas", "--reconstruir"]) == 0
    assert "No hay partidas" in capsys.readouterr().out
    assert main(["lineas", "Champ3"]) == 0
    assert "--reconstruir" in capsys.readouterr().out
    assert main(["lineas"]) == 0
    assert "Indica un campeón" in capsys.readouterr().out


def test_reconstruir_reports_counts_skips_and_time(home, capsys):
    fill(home)
    assert main(["lineas", "--reconstruir"]) == 0
    out = capsys.readouterr().out
    assert "Partidas guardadas: 5 | analizadas: 3 | filas de línea: 30" in out
    assert "omitidas (partida de menos de 15 min): 1" in out and "sin timeline: 1" in out
    assert "Agregados: 1" in out and "Tiempo:" in out
    assert (home / "data" / "lineas" / "duelos" / "MIDDLE" / "103.json").is_file()
    assert main(["lineas", "--reconstruir"]) == 0                 # idempotent
    assert "filas de línea: 30" in capsys.readouterr().out


def test_lineas_matchup_overall_and_filters(home, capsys):
    fill(home)
    main(["lineas", "--reconstruir"])
    capsys.readouterr()
    assert main(["lineas", "Champ3", "--vs", "Champ8", "--linea", "mid"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Champ3 vs Champ8 (Mid) · parches 16.21")
    assert "Ganas la línea 100% (3 partidas" in out
    assert main(["lineas", "Champ3"]) == 0                        # the line is the one it plays most
    out = capsys.readouterr().out
    assert "Champ3 (Mid) contra todos" in out and "Mejores cruces:" in out and "Champ8: la ganas 100%" in out
    assert main(["lineas", "Champ5", "--linea", "soporte"]) == 0
    assert "(Soporte)" in capsys.readouterr().out
    assert main(["lineas", "Champ3", "--persona", "Osiris", "--parches", "todos"]) == 0
    assert "solo Epsilon Osiris" in capsys.readouterr().out
    assert main(["lineas", "Champ3", "--personas"]) == 0
    assert "Epsilon Osiris: 3" in capsys.readouterr().out


def test_lineas_errors_are_spanish(home, capsys):
    fill(home)
    main(["lineas", "--reconstruir"])
    capsys.readouterr()
    for args, fragment in [(["Nadie"], "No conozco al campeón"),
                           (["Champ3", "--vs", "Nadie"], "No conozco al campeón"),
                           (["Champ3", "--linea", "xyz"], "La línea debe ser"),
                           (["Champ3", "--persona", "Fulano"], "Personas con muestras: Epsilon Osiris"),
                           (["Champ3", "--parches", "mucho"], "--parches debe ser")]:
        assert main(["lineas", *args]) == 1
        assert fragment in capsys.readouterr().err
