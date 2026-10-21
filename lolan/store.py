"""SQLite storage of raw Riot JSON so analysis can be re-run without re-downloading."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    puuid TEXT PRIMARY KEY,
    riot_id TEXT,
    updated_at INTEGER,
    added_at INTEGER,
    is_main INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game_creation INTEGER,
    queue_id INTEGER,
    json TEXT
);
CREATE TABLE IF NOT EXISTS timelines (
    match_id TEXT PRIMARY KEY,
    json TEXT
);
CREATE TABLE IF NOT EXISTS match_players (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS idx_match_players_puuid ON match_players(puuid);
"""


class Player(NamedTuple):
    """A registered player (row of the `account` table)."""
    puuid: str
    riot_id: str
    is_main: bool
    added_at: int | None
    updated_at: int | None


def participant_puuids(match: dict) -> list[str]:
    """Puuids of everyone in a match: metadata.participants, else info.participants[].puuid."""
    meta = (match or {}).get("metadata") or {}
    puuids = [p for p in (meta.get("participants") or []) if isinstance(p, str) and p]
    if not puuids:
        info = (match or {}).get("info") or {}
        puuids = [p["puuid"] for p in (info.get("participants") or [])
                  if isinstance(p, dict) and p.get("puuid")]
    return list(dict.fromkeys(puuids))


class Store:
    def __init__(self, path):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Generous timeout: the web panel and a CLI sync can touch the file at the same time.
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- migration
    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(account)")}
        with self.conn:
            if "added_at" not in cols:
                self.conn.execute("ALTER TABLE account ADD COLUMN added_at INTEGER")
                self.conn.execute("UPDATE account SET added_at = updated_at WHERE added_at IS NULL")
            if "is_main" not in cols:
                self.conn.execute("ALTER TABLE account ADD COLUMN is_main INTEGER DEFAULT 0")
                # The single-player versions used the most recently updated account as "you".
                self.conn.execute(
                    "UPDATE account SET is_main = 1 WHERE puuid = "
                    "(SELECT puuid FROM account ORDER BY updated_at DESC LIMIT 1)")
        self._backfill_match_players()

    def _backfill_match_players(self) -> None:
        """Link already stored matches to their participants (only for matches with no links)."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT match_id FROM matches WHERE match_id NOT IN (SELECT match_id FROM match_players)")]
        if not ids:
            return
        with self.conn:
            for match_id in ids:
                raw = self.conn.execute(
                    "SELECT json FROM matches WHERE match_id=?", (match_id,)).fetchone()[0]
                try:
                    puuids = participant_puuids(json.loads(raw))
                except (TypeError, ValueError):
                    continue
                self.conn.executemany(
                    "INSERT OR IGNORE INTO match_players(match_id, puuid) VALUES (?, ?)",
                    [(match_id, p) for p in puuids])

    # --------------------------------------------------------------- players
    @staticmethod
    def _player(row) -> Player:
        return Player(row[0], row[1] or "", bool(row[2]), row[3], row[4])

    _PLAYER_COLS = "puuid, riot_id, is_main, added_at, updated_at"

    def list_players(self) -> list[Player]:
        """Registered players, the main one first, then by Riot ID."""
        rows = self.conn.execute(
            f"SELECT {self._PLAYER_COLS} FROM account "
            "ORDER BY is_main DESC, lower(riot_id), puuid").fetchall()
        return [self._player(r) for r in rows]

    def get_player(self, puuid: str | None = None, riot_id: str | None = None) -> Player | None:
        """Look a player up by puuid or by Riot ID (case-insensitive)."""
        if puuid is not None:
            row = self.conn.execute(
                f"SELECT {self._PLAYER_COLS} FROM account WHERE puuid=?", (puuid,)).fetchone()
        elif riot_id is not None:
            # Compared in Python: SQLite's lower() only folds ASCII letters.
            wanted = riot_id.strip().casefold()
            rows = self.conn.execute(
                f"SELECT {self._PLAYER_COLS} FROM account ORDER BY updated_at DESC").fetchall()
            row = next((r for r in rows if (r[1] or "").casefold() == wanted), None)
        else:
            return None
        return self._player(row) if row else None

    def add_player(self, puuid: str, riot_id: str, is_main: bool = False) -> Player:
        """Register a player, or refresh the Riot ID of an already registered one."""
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO account(puuid, riot_id, updated_at, added_at, is_main) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(puuid) DO UPDATE SET riot_id=excluded.riot_id, "
                "updated_at=excluded.updated_at, added_at=COALESCE(added_at, excluded.added_at)",
                (puuid, riot_id, now, now),
            )
        if is_main:
            self.set_main(puuid)
        return self.get_player(puuid=puuid)

    def set_main(self, puuid: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE account SET is_main = (puuid = ?)", (puuid,))

    def remove_player(self, puuid: str) -> bool:
        """Drop a player from the registry. Stored matches stay: other players may share them."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM account WHERE puuid=?", (puuid,))
        return cur.rowcount > 0

    def replace_player_puuid(self, old: str, new: str) -> None:
        """Swap a player's puuid for the one the current Riot app issues (API key change).

        Keeps riot_id / is_main / added_at. If `new` is already registered (the player was added
        again under the new key) both rows are merged into it. Every other table with a `puuid`
        column (found with PRAGMA table_info, so tables of later features are covered) follows.
        `match_players` is not touched: it is rebuilt per match from the re-downloaded JSON.
        """
        if old == new:
            return
        with self.conn:
            old_row = self.conn.execute(
                "SELECT riot_id, is_main, added_at FROM account WHERE puuid=?", (old,)).fetchone()
            new_row = self.conn.execute(
                "SELECT riot_id, is_main, added_at FROM account WHERE puuid=?", (new,)).fetchone()
            if old_row is not None and new_row is None:
                self.conn.execute("UPDATE account SET puuid=? WHERE puuid=?", (new, old))
            elif old_row is not None:
                added = [a for a in (old_row[2], new_row[2]) if a is not None]
                self.conn.execute(
                    "UPDATE account SET riot_id=?, is_main=?, added_at=? WHERE puuid=?",
                    (new_row[0] or old_row[0], int(bool(old_row[1] or new_row[1])),
                     min(added) if added else None, new))
                self.conn.execute("DELETE FROM account WHERE puuid=?", (old,))
            for table in self.puuid_tables():
                quoted = '"' + table.replace('"', '""') + '"'
                self.conn.execute(f"UPDATE OR IGNORE {quoted} SET puuid=? WHERE puuid=?", (new, old))
                self.conn.execute(f"DELETE FROM {quoted} WHERE puuid=?", (old,))

    def puuid_tables(self) -> list[str]:
        """Tables (other than account / match_players) that have a `puuid` column."""
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        found = []
        for name in names:
            if name in ("account", "match_players"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            if any(r[1] == "puuid" for r in self.conn.execute(f"PRAGMA table_info({quoted})")):
                found.append(name)
        return found

    # --------------------------------------------------------------- matches
    def match_ids_newest_first(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT match_id FROM matches ORDER BY game_creation DESC, match_id DESC")]

    def participants_by_match(self) -> dict[str, set[str]]:
        """match_id -> puuids linked to it (from match_players)."""
        result: dict[str, set[str]] = {}
        for match_id, puuid in self.conn.execute("SELECT match_id, puuid FROM match_players"):
            result.setdefault(match_id, set()).add(puuid)
        return result

    def linked_match_ids(self, *puuids: str) -> set[str]:
        """Ids of the matches linked to any of `puuids`."""
        if not puuids:
            return set()
        marks = ",".join("?" * len(puuids))
        return {r[0] for r in self.conn.execute(
            f"SELECT match_id FROM match_players WHERE puuid IN ({marks})", puuids)}

    def existing_match_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT match_id FROM matches")}

    def has_match(self, match_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM matches WHERE match_id=?", (match_id,)
        ).fetchone() is not None

    def count_matches(self, puuid: str | None = None) -> int:
        """Stored matches, all of them or only those `puuid` played."""
        if puuid is None:
            return self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM match_players WHERE puuid=?", (puuid,)).fetchone()[0]

    def match_counts(self) -> dict[str, int]:
        """puuid -> number of stored matches that player appears in."""
        return dict(self.conn.execute("SELECT puuid, COUNT(*) FROM match_players GROUP BY puuid"))

    def save_match(self, match_id: str, match: dict, timeline: dict | None = None) -> None:
        """Store a match (and its timeline if any) and link its participants, in one transaction."""
        info = (match or {}).get("info") or {}
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO matches(match_id, game_creation, queue_id, json) VALUES (?, ?, ?, ?)",
                (match_id, info.get("gameCreation"), info.get("queueId"), json.dumps(match, separators=(",", ":"))),
            )
            self.conn.execute("DELETE FROM match_players WHERE match_id=?", (match_id,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO match_players(match_id, puuid) VALUES (?, ?)",
                [(match_id, p) for p in participant_puuids(match)],
            )
            if timeline is not None:
                self.conn.execute(
                    "INSERT OR REPLACE INTO timelines(match_id, json) VALUES (?, ?)",
                    (match_id, json.dumps(timeline, separators=(",", ":"))),
                )

    def get_match(self, match_id: str):
        """(match_dict, timeline_dict_or_None) of one stored match, or None if it is not stored."""
        row = self.conn.execute(
            "SELECT m.json, t.json FROM matches m LEFT JOIN timelines t ON t.match_id = m.match_id "
            "WHERE m.match_id = ?", (match_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), (json.loads(row[1]) if row[1] else None)

    def iter_matches(self, puuid: str | None = None):
        """Yield (match_id, match_dict, timeline_dict_or_None) ordered by game_creation.

        With `puuid`, only the matches that player took part in are read and parsed.
        """
        sql = ("SELECT m.match_id, m.json, t.json FROM matches m "
               "LEFT JOIN timelines t ON t.match_id = m.match_id ")
        params: tuple = ()
        if puuid is not None:
            sql += "JOIN match_players mp ON mp.match_id = m.match_id AND mp.puuid = ? "
            params = (puuid,)
        sql += "ORDER BY m.game_creation, m.match_id"
        for match_id, mjson, tjson in self.conn.execute(sql, params):
            yield match_id, json.loads(mjson), (json.loads(tjson) if tjson else None)
