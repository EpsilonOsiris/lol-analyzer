"""Switching to a key of another Riot app: the migration, its safety nets and the CLI (no network)."""
import json
import shutil
from collections import Counter
from datetime import datetime

import pytest
from conftest import (FRIEND_PUUID, ME_PUUID, AppRiot, make_match, seed_app_store, world_matches)

from lolan import analyze as an
from lolan import rekey as rk
from lolan.__main__ import main
from lolan.riot import InvalidApiKeyError, NotFoundError, PuuidFromOtherAppError, RiotError
from lolan.store import Store
from lolan.sync import sync


@pytest.fixture
def world():
    return world_matches()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "lol.db")
    yield s
    s.close()


def old_app_store(store, world, **kw):
    seed_app_store(store, world, "A", **kw)
    return store


def run(client, store, **kw):
    lines = []
    kw.setdefault("backup", False)
    result = rk.rekey(client, store, out=lines.append, **kw)
    return result, lines


def stored_text(store):
    """Every stored JSON (matches and timelines) as one string."""
    rows = store.conn.execute("SELECT json FROM matches UNION ALL SELECT json FROM timelines")
    return "\n".join(r[0] for r in rows)


def queue_counts(store, puuid):
    return Counter(g["queue_id"] for g in an.load_games(store, puuid))


# ------------------------------------------------------------- the migration
def test_migrates_json_links_and_registry(store, world):
    old_app_store(store, world)
    before_added = {p.riot_id: p.added_at for p in store.list_players()}
    assert "@A" in stored_text(store)
    me_before = queue_counts(store, f"{ME_PUUID}@A")
    assert me_before == {420: 4, 440: 2}

    client = AppRiot("B", world)
    result, lines = run(client, store)

    text = stored_text(store)
    assert "@A" not in text and "@B" in text                      # matches AND timelines
    players = {p.riot_id: p for p in store.list_players()}
    assert players["Yo#LAN"].puuid == f"{ME_PUUID}@B" and players["Yo#LAN"].is_main
    assert players["Amigo#LAN"].puuid == f"{FRIEND_PUUID}@B" and not players["Amigo#LAN"].is_main
    assert {r: p.added_at for r, p in players.items()} == before_added
    # match_players points only at new puuids; both players keep every game.
    assert not any(p.endswith("@A") for p in store.match_counts())
    assert store.count_matches(f"{ME_PUUID}@B") == 6
    assert store.count_matches(f"{FRIEND_PUUID}@B") == 4          # shared with ME in 4 games
    assert queue_counts(store, f"{ME_PUUID}@B") == me_before
    assert result.ok and result.verified and result.updated == 6 and result.registry_updated == 2
    # Two requests per match plus one lookup per registered player.
    assert len(client.fetched("get_match")) == 6 and len(client.fetched("get_timeline")) == 6
    assert len(client.fetched("get_puuid")) == 2
    assert any(line.startswith("[6/6]") and "faltan" in line for line in lines)
    assert any("Partidas por actualizar: 6" in line for line in lines)


def test_second_run_is_a_noop_and_same_app_key_needs_nothing(store, world):
    old_app_store(store, world)
    run(AppRiot("B", world), store)
    client = AppRiot("B", world)
    result, lines = run(client, store)
    assert result.nothing_to_do and client.fetched("get_match") == []
    assert any("no hay nada que actualizar" in line for line in lines)

    # A key of the same app that stored the data: nothing to do either (and nothing changes).
    other = Store(store.path.parent / "otra.db")
    old_app_store(other, world)
    client = AppRiot("A", world)
    result, _ = run(client, other)
    assert result.nothing_to_do and client.fetched("get_match") == []
    assert other.list_players()[0].puuid.endswith("@A")
    other.close()


def test_other_tables_with_a_puuid_column_follow(store, world):
    old_app_store(store, world)
    store.conn.execute("CREATE TABLE video_reviews (id INTEGER PRIMARY KEY, puuid TEXT, note TEXT)")
    store.conn.execute("CREATE TABLE sin_puuid (id INTEGER PRIMARY KEY, nota TEXT)")
    store.conn.executemany("INSERT INTO video_reviews(puuid, note) VALUES (?, ?)",
                           [(f"{ME_PUUID}@A", "uno"), (f"{FRIEND_PUUID}@A", "dos"),
                            ("ajeno@A", "tres")])
    store.conn.commit()
    assert store.puuid_tables() == ["video_reviews"]
    run(AppRiot("B", world), store)
    rows = store.conn.execute("SELECT puuid, note FROM video_reviews ORDER BY id").fetchall()
    assert rows == [(f"{ME_PUUID}@B", "uno"), (f"{FRIEND_PUUID}@B", "dos"), ("ajeno@A", "tres")]


def test_works_without_extra_tables(store, world):
    old_app_store(store, world)
    assert store.puuid_tables() == []
    assert run(AppRiot("B", world), store)[0].ok


def test_player_already_added_under_the_new_key_is_merged(store, world):
    old_app_store(store, world)
    store.add_player(f"{ME_PUUID}@B", "Yo#LAN")        # `jugadores agregar` before rekey
    result, _ = run(AppRiot("B", world), store)
    players = store.list_players()
    assert [p.puuid for p in players] == [f"{ME_PUUID}@B", f"{FRIEND_PUUID}@B"]
    assert players[0].is_main and result.ok


# -------------------------------------------------------------- resumability
def test_interruption_by_an_invalid_key_keeps_progress_and_resumes(store, world):
    old_app_store(store, world)
    client = AppRiot("B", world, fail_key_after=3)
    lines = []
    with pytest.raises(InvalidApiKeyError):
        rk.rekey(client, store, out=lines.append, backup=False)
    assert any("Se actualizaron 3 partidas" in line for line in lines)
    # Registry untouched, three newest matches already carry new puuids.
    assert all(p.puuid.endswith("@A") for p in store.list_players())
    migrated = [m for m, ps in store.participants_by_match().items()
                if any(p.endswith("@B") for p in ps)]
    assert sorted(migrated) == ["LA1_3", "LA1_4", "LA1_5"]          # newest first

    second = AppRiot("B", world)
    result, _ = run(second, store)
    assert sorted(second.fetched("get_match")) == ["LA1_0", "LA1_1", "LA1_2"]   # only the rest
    assert result.ok and result.updated == 3
    assert "@A" not in stored_text(store)
    assert all(p.puuid.endswith("@B") for p in store.list_players())
    assert store.count_matches(f"{ME_PUUID}@B") == 6
    assert queue_counts(store, f"{ME_PUUID}@B") == {420: 4, 440: 2}


def test_invalid_key_while_looking_players_up_changes_nothing(store, world):
    old_app_store(store, world)

    class Expired(AppRiot):
        def get_puuid(self, name, tag):
            raise InvalidApiKeyError("expired")

    snapshot = stored_text(store)
    with pytest.raises(InvalidApiKeyError):
        run(Expired("B", world), store)
    assert stored_text(store) == snapshot
    assert all(p.puuid.endswith("@A") for p in store.list_players())


def test_resume_does_not_make_a_second_backup(store, world):
    old_app_store(store, world)
    stamp = datetime(2026, 10, 20, 23, 5)
    with pytest.raises(InvalidApiKeyError):
        rk.rekey(AppRiot("B", world, fail_key_after=2), store, out=lambda _l: None,
                 backup_now=stamp)
    backups = rk.existing_backups(store)
    assert [b.name for b in backups] == ["lol.db.antes-rekey-20261020-2305"]
    rk.rekey(AppRiot("B", world), store, out=lambda _l: None, backup_now=datetime(2026, 10, 20, 23, 30))
    assert rk.existing_backups(store) == backups


# ------------------------------------------------------------ partial outcomes
def test_match_riot_no_longer_has_is_kept_as_is_and_reported(store, world):
    old_app_store(store, world)
    client = AppRiot("B", world, missing_matches={"LA1_2"})
    result, lines = run(client, store)
    assert result.not_found == ["LA1_2"] and not result.ok
    assert result.updated == 5 and not result.problems         # verification accounts for it
    kept = store.get_match("LA1_2")[0]
    assert json.dumps(kept).count("@A") > 0 and "@B" not in json.dumps(kept)
    assert store.count_matches(f"{ME_PUUID}@B") == 5
    assert any("LA1_2" in line and "se conserva" in line for line in lines)
    assert all(p.puuid.endswith("@B") for p in store.list_players())


def test_missing_timeline_keeps_the_stored_one(store, world):
    old_app_store(store, world)
    result, _ = run(AppRiot("B", world, missing_timelines={"LA1_1"}), store)
    assert result.missing_timelines == 1 and result.ok
    assert store.get_match("LA1_1")[1] is not None
    assert store.count_matches(f"{ME_PUUID}@B") == 6


def test_riot_id_that_no_longer_exists_is_skipped_and_kept(store, world):
    old_app_store(store, world)
    client = AppRiot("B", world, accounts={"Yo#LAN": ME_PUUID})        # Amigo#LAN is gone
    result, lines = run(client, store)
    assert [s[0] for s in result.skipped] == ["Amigo#LAN"] and not result.ok
    players = {p.riot_id: p.puuid for p in store.list_players()}
    assert players == {"Yo#LAN": f"{ME_PUUID}@B", "Amigo#LAN": f"{FRIEND_PUUID}@A"}
    assert store.count_matches(f"{ME_PUUID}@B") == 6 and not result.problems
    assert any("Amigo#LAN" in line and "AVISO" in line for line in lines)


def test_matches_without_a_registered_player_are_left_alone(store, world):
    old_app_store(store, world)
    orphan = make_match("LA1_OLD", created=1)
    for p in orphan["info"]["participants"]:
        p["puuid"] = f"{p['puuid']}@A"
        if p["puuid"] in (f"{ME_PUUID}@A", f"{FRIEND_PUUID}@A"):
            p["puuid"] = "nadie@A" if "me" in p["puuid"] else "nadie2@A"
    orphan["metadata"]["participants"] = [p["puuid"] for p in orphan["info"]["participants"]]
    store.save_match("LA1_OLD", orphan)
    client = AppRiot("B", world)
    result, _ = run(client, store)
    assert result.untouched == 1 and "LA1_OLD" not in client.fetched("get_match")
    assert result.ok


def test_a_run_of_failures_aborts_but_keeps_what_was_done(store, world):
    old_app_store(store, world)

    class Broken(AppRiot):
        def get_match(self, match_id):
            if match_id in ("LA1_5",):
                return super().get_match(match_id)
            raise RiotError("500")

    with pytest.raises(RiotError, match="Demasiados errores"):
        run(Broken("B", world), store)
    assert store.count_matches(f"{ME_PUUID}@B") == 1


# -------------------------------------------------------------------- backup
def test_backup_is_a_consistent_copy_with_the_timestamped_name(store, world):
    old_app_store(store, world)
    path = rk.backup_database(store, now=datetime(2026, 10, 20, 23, 5))
    assert path.name == "lol.db.antes-rekey-20261020-2305"
    copy = Store(path)
    assert copy.count_matches() == 6 and copy.list_players()[0].puuid.endswith("@A")
    copy.close()
    assert rk.backup_database(store, now=datetime(2026, 10, 20, 23, 5)).name.endswith("-2305-2")


def test_not_enough_disk_space_refuses_before_changing_anything(store, world, monkeypatch):
    old_app_store(store, world)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 99, 1))
    client = AppRiot("B", world)
    snapshot = stored_text(store)
    with pytest.raises(rk.RekeyError, match="espacio en disco"):
        rk.rekey(client, store, out=lambda _l: None)
    assert stored_text(store) == snapshot and client.fetched("get_match") == []
    assert rk.existing_backups(store) == []
    assert all(p.puuid.endswith("@A") for p in store.list_players())


def test_estimate_minutes(store, world):
    old_app_store(store, world)
    assert rk.estimate_minutes(store) == 1
    assert rk.key_app_changed(AppRiot("B", world), store) is True
    assert rk.key_app_changed(AppRiot("A", world), store) is False


# ------------------------------------------------- sync stops with a clear error
def test_sync_of_a_registered_member_raises_the_specific_error(store, world):
    old_app_store(store, world)
    lines = []
    with pytest.raises(PuuidFromOtherAppError) as err:
        sync(AppRiot("B", world), store, "Amigo#LAN", out=lines.append)
    assert "rekey" in str(err.value) and "Actualizar mis datos" in str(err.value)
    assert [p.puuid for p in store.list_players()] == [f"{ME_PUUID}@A", f"{FRIEND_PUUID}@A"]


def test_sync_of_the_main_player_does_not_register_a_duplicate(store, world):
    old_app_store(store, world)
    with pytest.raises(PuuidFromOtherAppError):
        sync(AppRiot("B", world), store, "Yo#LAN", main=True, out=lambda _l: None)
    assert len(store.list_players()) == 2 and store.list_players()[0].is_main


def test_sync_works_normally_with_the_same_app(store, world):
    old_app_store(store, world)
    client = AppRiot("A", world)
    client.list_match_ids = lambda puuid, start_time=None, queue=None: list(world)
    result = sync(client, store, "Yo#LAN", main=True, out=lambda _l: None)
    assert result.downloaded == 0 and result.already_stored == 6


# ----------------------------------------------------------------------- CLI
@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RIOT_API_KEY", "RGAPI-test")
    monkeypatch.setenv("RIOT_ID", "Yo#LAN")
    return tmp_path


def patch_client(monkeypatch, client):
    from lolan import __main__ as cli
    monkeypatch.setattr(cli, "RiotClient", lambda *a, **kw: client)


def test_cli_rekey_migrates_and_writes_a_backup(cli_home, world, monkeypatch, capsys):
    s = Store(cli_home / "data" / "lol.db")
    old_app_store(s, world)
    s.close()
    patch_client(monkeypatch, AppRiot("B", world))
    assert main(["rekey"]) == 0
    out = capsys.readouterr().out
    assert "[6/6]" in out and "Respaldo de la base de datos" in out and "Verificación" in out
    assert len(list((cli_home / "data").glob("lol.db.antes-rekey-*"))) == 1
    s = Store(cli_home / "data" / "lol.db")
    assert s.list_players()[0].puuid == f"{ME_PUUID}@B"
    s.close()


def test_cli_sync_after_a_key_change_points_to_rekey(cli_home, world, monkeypatch, capsys):
    s = Store(cli_home / "data" / "lol.db")
    old_app_store(s, world)
    s.close()
    patch_client(monkeypatch, AppRiot("B", world))
    assert main(["sync", "--jugador", "Amigo#LAN"]) == 1
    err = capsys.readouterr().err
    assert "python -m lolan rekey" in err
    assert main(["sync"]) == 1
    assert "python -m lolan rekey" in capsys.readouterr().err


def test_cli_rekey_exit_code_reports_leftovers(cli_home, world, monkeypatch, capsys):
    s = Store(cli_home / "data" / "lol.db")
    old_app_store(s, world)
    s.close()
    patch_client(monkeypatch, AppRiot("B", world, missing_matches={"LA1_0"}))
    assert main(["rekey"]) == 1
    assert "LA1_0" in capsys.readouterr().out


def test_cli_help_lists_rekey(capsys):
    assert main([]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "rekey" in out and "actualiza tus datos después de cambiar de API key" in out
