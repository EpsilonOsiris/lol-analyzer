import json
import sqlite3

import pytest
from conftest import BASE_TIME_MS, ME_PUUID, make_match, make_timeline

from lolan.store import SCHEMA, Store, participant_puuids

FRIEND = "puuid-4"      # blue-side teammate of the default player
RIVAL = "puuid-8"       # red-side lane opponent


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "lol.db")
    yield s
    s.close()


def links(store):
    return {tuple(r) for r in store.conn.execute("SELECT match_id, puuid FROM match_players")}


def test_participant_puuids_prefers_metadata_and_falls_back_to_info():
    m = make_match("LA1_1")
    assert len(participant_puuids(m)) == 10 and ME_PUUID in participant_puuids(m)
    del m["metadata"]["participants"]
    assert set(participant_puuids(m)) == {p["puuid"] for p in m["info"]["participants"]}
    assert participant_puuids({}) == []


def test_save_match_links_all_ten_participants(store):
    store.save_match("LA1_1", make_match("LA1_1"), None)
    assert {p for _, p in links(store)} == {ME_PUUID} | {f"puuid-{i}" for i in range(1, 11) if i != 3}
    assert store.count_matches(ME_PUUID) == 1 and store.count_matches(FRIEND) == 1
    assert store.count_matches("nobody") == 0 and store.count_matches() == 1
    # Saving again (e.g. re-download) doesn't duplicate links.
    store.save_match("LA1_1", make_match("LA1_1"), None)
    assert len(links(store)) == 10


def test_iter_matches_filters_by_player(store):
    store.save_match("LA1_1", make_match("LA1_1", created=BASE_TIME_MS), make_timeline())
    other = make_match("LA1_2", created=BASE_TIME_MS + 1)
    for p in other["info"]["participants"]:          # a game the default player is NOT in
        p["puuid"] = "x" + p["puuid"]
    other["metadata"]["participants"] = [p["puuid"] for p in other["info"]["participants"]]
    store.save_match("LA1_2", other, None)
    assert [m for m, _, _ in store.iter_matches()] == ["LA1_1", "LA1_2"]
    assert [m for m, _, _ in store.iter_matches(ME_PUUID)] == ["LA1_1"]
    assert [m for m, _, _ in store.iter_matches("xpuuid-1")] == ["LA1_2"]
    assert list(store.iter_matches("nobody")) == []
    mid, match, tl = next(store.iter_matches(ME_PUUID))
    assert match["metadata"]["matchId"] == "LA1_1" and tl is not None


def test_iter_matches_only_parses_that_players_matches(store, monkeypatch):
    store.save_match("LA1_1", make_match("LA1_1"), None)
    other = make_match("LA1_2", created=BASE_TIME_MS + 1)
    other["metadata"]["participants"] = ["a", "b"]
    store.save_match("LA1_2", other, None)
    parsed = []
    real = json.loads
    monkeypatch.setattr("lolan.store.json.loads", lambda s, *a, **k: (parsed.append(1), real(s))[1])
    assert len(list(store.iter_matches(ME_PUUID))) == 1
    assert len(parsed) == 1


def test_players_registry(store):
    assert store.list_players() == []
    p = store.add_player("p1", "José Luis#LAN", is_main=True)
    assert p.is_main and p.riot_id == "José Luis#LAN" and p.added_at
    store.add_player("p2", "Ángel#LAN")
    store.add_player("p3", "Beto#LAN")
    listed = [x.puuid for x in store.list_players()]
    assert listed[0] == "p1" and set(listed) == {"p1", "p2", "p3"}      # main first
    # Lookups by puuid and by Riot ID, case-insensitive (also for non-ASCII letters).
    assert store.get_player(puuid="p3").riot_id == "Beto#LAN"
    assert store.get_player(riot_id="beto#lan").puuid == "p3"
    assert store.get_player(riot_id="  ÁNGEL#lan ").puuid == "p2"
    assert store.get_player(riot_id="nadie#x") is None and store.get_player() is None
    # Re-adding with the same puuid renames instead of duplicating; added_at is kept.
    again = store.add_player("p3", "Beto Nuevo#LAN")
    assert len(store.list_players()) == 3 and again.riot_id == "Beto Nuevo#LAN"
    assert again.added_at == store.get_player(puuid="p3").added_at
    # Only one main player at a time.
    store.add_player("p2", "Ángel#LAN", is_main=True)
    assert [x.puuid for x in store.list_players() if x.is_main] == ["p2"]


def test_remove_player_keeps_matches(store):
    store.add_player(ME_PUUID, "Yo#LAN", is_main=True)
    store.add_player(FRIEND, "Amigo#LAN")
    store.save_match("LA1_1", make_match("LA1_1"), None)
    assert store.remove_player(FRIEND) is True and store.remove_player(FRIEND) is False
    assert [p.puuid for p in store.list_players()] == [ME_PUUID]
    assert store.count_matches() == 1 and store.count_matches(FRIEND) == 1   # still stored/linked
    assert store.match_counts()[ME_PUUID] == 1


def make_old_db(path, n=3):
    """A database as the single-player version left it: no new columns, no match_players."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE account (puuid TEXT PRIMARY KEY, riot_id TEXT, updated_at INTEGER);
        CREATE TABLE matches (match_id TEXT PRIMARY KEY, game_creation INTEGER, queue_id INTEGER, json TEXT);
        CREATE TABLE timelines (match_id TEXT PRIMARY KEY, json TEXT);
    """)
    conn.execute("INSERT INTO account VALUES ('old-puuid', 'Viejo#LAN', 100)")
    conn.execute("INSERT INTO account VALUES (?, 'Yo#LAN', 200)", (ME_PUUID,))
    for i in range(n):
        m = make_match(f"LA1_{i}", created=BASE_TIME_MS + i)
        conn.execute("INSERT INTO matches VALUES (?, ?, 420, ?)",
                     (f"LA1_{i}", BASE_TIME_MS + i, json.dumps(m)))
        conn.execute("INSERT INTO timelines VALUES (?, ?)", (f"LA1_{i}", json.dumps(make_timeline())))
    conn.commit()
    conn.close()


def test_open_old_database_migrates_and_backfills(tmp_path):
    path = tmp_path / "lol.db"
    make_old_db(path, n=3)
    s = Store(path)
    try:
        # Every stored match is linked to its 10 participants.
        assert len(links(s)) == 30
        assert s.count_matches(ME_PUUID) == 3 and s.count_matches(FRIEND) == 3
        assert [m for m, _, _ in s.iter_matches(ME_PUUID)] == ["LA1_0", "LA1_1", "LA1_2"]
        # Registry columns were added; the most recently updated account becomes the main one.
        players = {p.puuid: p for p in s.list_players()}
        assert players[ME_PUUID].is_main and not players["old-puuid"].is_main
        assert players[ME_PUUID].added_at == 200
        # Raw data is untouched.
        assert s.count_matches() == 3
        assert s.conn.execute("SELECT COUNT(*) FROM timelines").fetchone()[0] == 3
    finally:
        s.close()
    # Opening again changes nothing (idempotent) and doesn't re-flag anything.
    s = Store(path)
    try:
        assert len(links(s)) == 30
        assert [p.puuid for p in s.list_players() if p.is_main] == [ME_PUUID]
    finally:
        s.close()


def test_backfill_only_touches_unlinked_matches(tmp_path):
    path = tmp_path / "lol.db"
    s = Store(path)
    s.save_match("LA1_A", make_match("LA1_A"), None)
    s.close()
    conn = sqlite3.connect(str(path))                  # an old-version write: no links
    conn.execute("INSERT INTO matches VALUES ('LA1_B', ?, 420, ?)",
                 (BASE_TIME_MS, json.dumps(make_match("LA1_B"))))
    conn.commit()
    conn.close()
    s = Store(path)
    try:
        assert {m for m, _ in links(s)} == {"LA1_A", "LA1_B"}
    finally:
        s.close()


def test_schema_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.executescript(SCHEMA)
