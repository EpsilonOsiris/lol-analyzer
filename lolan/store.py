"""SQLite storage of raw Riot JSON so analysis can be re-run without re-downloading."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    puuid TEXT PRIMARY KEY,
    riot_id TEXT,
    updated_at INTEGER
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
"""


class Store:
    def __init__(self, path):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --------------------------------------------------------------- account
    def save_account(self, puuid: str, riot_id: str) -> None:
        self.conn.execute(
            "INSERT INTO account(puuid, riot_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(puuid) DO UPDATE SET riot_id=excluded.riot_id, updated_at=excluded.updated_at",
            (puuid, riot_id, int(time.time())),
        )
        self.conn.commit()

    def get_account(self):
        """Most recently updated account as (puuid, riot_id), or None."""
        row = self.conn.execute(
            "SELECT puuid, riot_id FROM account ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row

    def get_puuid(self) -> str | None:
        acc = self.get_account()
        return acc[0] if acc else None

    # --------------------------------------------------------------- matches
    def existing_match_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT match_id FROM matches")}

    def has_match(self, match_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM matches WHERE match_id=?", (match_id,)
        ).fetchone() is not None

    def count_matches(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    def save_match(self, match_id: str, match: dict, timeline: dict | None = None) -> None:
        """Store a match (and its timeline if any) in one transaction."""
        info = (match or {}).get("info") or {}
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO matches(match_id, game_creation, queue_id, json) VALUES (?, ?, ?, ?)",
                (match_id, info.get("gameCreation"), info.get("queueId"), json.dumps(match, separators=(",", ":"))),
            )
            if timeline is not None:
                self.conn.execute(
                    "INSERT OR REPLACE INTO timelines(match_id, json) VALUES (?, ?)",
                    (match_id, json.dumps(timeline, separators=(",", ":"))),
                )

    def iter_matches(self):
        """Yield (match_id, match_dict, timeline_dict_or_None) ordered by game_creation."""
        cur = self.conn.execute(
            "SELECT m.match_id, m.json, t.json FROM matches m "
            "LEFT JOIN timelines t ON t.match_id = m.match_id "
            "ORDER BY m.game_creation, m.match_id"
        )
        for match_id, mjson, tjson in cur:
            yield match_id, json.loads(mjson), (json.loads(tjson) if tjson else None)
