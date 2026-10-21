"""python -m lolan rivales: compact Spanish report (fake Riot, nothing leaves the PC)."""
import pytest
from scout_fixtures import ME, World, league, mastery, seed_ddragon, spectator_game

from lolan import __main__ as cli
from lolan.__main__ import main
from lolan.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME}\n", encoding="utf-8")
    world = World()
    world.stock(spectator_game())
    made = []

    def fake(key, region="americas", limiter=None, **kw):
        made.append((key, region, limiter))
        return world.client(region)

    monkeypatch.setattr(cli, "RiotClient", fake)
    seed_ddragon(tmp_path)
    world.made = made
    return tmp_path, world


def test_report_lists_both_teams_with_rank_mastery_and_hidden_players(env, capsys):
    root, world = env
    world.leagues["P-toro"] = league(solo=("DIAMOND", "II", 12, 200, 150, True))
    world.masteries[("P-toro", 58)] = mastery(250_000, level=10, days_ago=1)
    assert main(["rivales"]) == 0
    out = capsys.readouterr().out
    assert "Partida de Yo#LAN: Clasificatoria Solo/Dúo · en la pantalla de carga" in out
    assert "Tu equipo" in out and "Equipo enemigo" in out
    assert "Renekton" in out and "Toro Rojo#T006" in out
    assert "Solo Diamante II 12 PL 57% (350) · racha de victorias" in out
    assert "maestría: 250k pts, nivel 10, " in out and "(main)" in out
    assert "Jugador oculto" in out and "Bot" in out and "[de tu clan]" in out
    assert "Destello/Curación" in out and "Paso Fugaz" in out
    assert "Bans (tu equipo): Aatrox" in out and "Bans (enemigos): Lux" in out
    assert "Peticiones a Riot usadas: 36 de 45" in out
    assert "Datos de Riot (Spectator)" in out
    assert "P-toro" not in out
    assert all(region in ("la1", "americas") for _k, region, _l in world.made)
    # one limiter per host, shared by both clients of the same host
    assert len({id(l) for _k, r, l in world.made if r == "la1"}) == 1


def test_second_run_costs_less_thanks_to_the_cache(env, capsys):
    root, world = env
    main(["rivales"])
    capsys.readouterr()
    world.calls.clear()
    assert main(["rivales"]) == 0
    out = capsys.readouterr().out
    assert world.names() == ["spectator"] or world.names() == ["account", "spectator"]
    assert "8 jugadores salieron de la caché de 6 h" in out


def test_not_in_a_game(env, capsys):
    _root, world = env
    world.games.clear()
    assert main(["rivales"]) == 0
    assert "Yo#LAN no está en una partida en este momento" in capsys.readouterr().out


def test_other_registered_player(env, capsys):
    root, world = env
    world.accounts["Rana Veloz#T002"] = "P-rana"
    world.games["P-rana"] = spectator_game(game_id=5)
    store = Store(root / "data" / "lol.db")
    store.add_player("x", "Rana Veloz#T002")
    store.close()
    assert main(["rivales", "--jugador", "rana veloz#t002"]) == 0
    out = capsys.readouterr().out
    assert "Partida de Rana Veloz#T002" in out and "Su equipo" in out
    assert main(["rivales", "--jugador", "Nadie#X"]) == 1
    assert "No hay ningún jugador registrado" in capsys.readouterr().err


def test_missing_key_is_a_friendly_error(env, capsys):
    root, _world = env
    (root / ".env").write_text(f"RIOT_ID={ME}\n", encoding="utf-8")
    assert main(["rivales"]) == 1
    assert "Falta RIOT_API_KEY" in capsys.readouterr().err
