import pytest

from lolan.__main__ import main


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_analyze_on_empty_db_is_friendly(isolated, capsys):
    assert main(["analyze"]) == 0
    out = capsys.readouterr().out
    assert "No hay partidas" in out and "sync" in out


def test_game_and_campeon_on_empty_db(isolated, capsys):
    assert main(["game", "ultima"]) == 0
    assert main(["campeon", "Ahri"]) == 0
    assert capsys.readouterr().out.count("No hay partidas") == 2


def test_sync_without_key_prints_friendly_error(isolated, capsys):
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "RIOT_API_KEY" in err and ".env" in err


def test_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "sync" in capsys.readouterr().out


def test_queue_aliases():
    from lolan.__main__ import _parse_queue
    assert _parse_queue("solo") == 420
    assert _parse_queue("Flex") == 440
    assert _parse_queue("450") == 450


def test_analyze_with_queue_writes_its_own_folder(isolated, capsys):
    from conftest import ME_PUUID, make_match, make_timeline

    from lolan.store import Store
    store = Store(isolated / "data" / "lol.db")
    store.save_account(ME_PUUID, "A#B")
    store.save_match("LA1_1", make_match("LA1_1", queue=420), make_timeline())
    store.save_match("LA1_2", make_match("LA1_2", queue=440, created=1_700_000_000_000), None)
    store.close()
    assert main(["analyze", "--cola", "solo"]) == 0
    assert main(["analyze", "--cola", "flex"]) == 0
    assert (isolated / "reports" / "solo" / "partidas" / "LA1_1.md").exists()
    assert (isolated / "reports" / "flex" / "partidas" / "LA1_2.md").exists()
    assert (isolated / "data" / "games-solo.csv").exists()
    assert (isolated / "data" / "games-flex.csv").exists()
