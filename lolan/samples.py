"""Lane samples: JSON files with how each lane matchup went, and the numbers computed from them.

Nothing here uses a database. Everything lives under data/lineas/ (deletable, it grows with the
Diamond+ sample that `lolan.sampler` downloads):

  filas/<patch>/<source>.jsonl   one compact JSON row per perspective (two per lane pair), append-only
  procesadas.json                match ids already turned into rows (or skipped), so re-runs and the
                                 downloader are idempotent
  campeones.json                 championId -> name, for output
  duelos/<ROLE>/<championId>.json  precomputed aggregates, pretty-printed: what the Partida page reads
  indice.json                    when they were built, patches and row counts per source

Rows are appended and flushed one line at a time and every other file is replaced atomically, so
an interrupted run leaves valid files; a truncated last line is skipped on read. A row:
  match_id, source ("propia" | "diamante"), platform, patch, tier (None for "propia"), queue,
  game_creation, role, side (100/200), champ, opp (championIds), diff14, gold14, xp14, result,
  brackets {name: {gain, minutes}}, first2, first6 (1/0/None), series [[minute, my level, opp level,
  diff]] up to minute 25, items (minute of the 1st/2nd/3rd legendary, null without item data for the
  patch), win (1/0), minutes (game length), the style fields bld_pm / tank_share / dmg_share / cc_pm /
  kp / turret_plates / ka14 / kp14 (see `lanephase.STYLE_KEYS`; absent or null in older rows), person (a clan member's name
  as Composiciones shows it, else None). No puuids and no names of other players are stored.

The API (matchup / champion_overall / matchups_of / best_into / text_summary) computes from the
rows; `rebuild` writes the aggregates.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

from .ddragon import LANG, champion_display, norm, version_key
from .lanephase import (BRACKET_NAMES, ITEM_COUNT, LANE_MINUTE, LANE_ROLES, STYLE_KEYS, analyze_match,
                        legendary_items, patch_of)

SOURCE_OWN = "propia"
SOURCE_DIAMOND = "diamante"
PRIOR_GAMES = 10          # weight (in games) of the champion's overall lane win rate in the shrinkage
LOW_SAMPLE_GAMES = 10     # fewer games than this and the matchup is flagged as a thin sample
RECENT_PATCHES = 3        # default window: the newest patches present for the role
ALL_PATCHES = "todos"     # patches= value that disables the window
MIN_LIST_GAMES = 2        # matchups_of lists opponents seen at least this many times
BRACKET_NOTABLE = 10      # gold+xp per minute from which a level range is called strong / weak
BRACKET_MIN_MINUTES = 5   # a level range needs this many minutes of data to be mentioned
REPLACE_TRIES = 8         # attempts at swapping a written file into place (Windows locks)
SAVE_EVERY = 100         # rows are appended this many matches at a time
SPIKE_LEVELS = 11         # level_spikes covers own levels 1..11 (minutes before 15)
ITEM_WINDOW = 3           # minutes before / after an item completion compared in item_spikes
ITEM_SPIKE_NOTABLE = 15   # gold+xp per minute gained after an item beyond the gain before it
ITEM_SPIKE_MIN_WINDOWS = 3  # games with a usable window before an item spike is mentioned
SPIKE_MIN_GAMES = 5       # matchups with fewer games get no spike blocks in the aggregate files
GAME_LENGTH_BUCKETS = (("<25", 0, 25), ("25-30", 25, 30), ("30-35", 30, 35), ("35+", 35, 10 ** 6))
GAME_LENGTH_MIN = 3       # games a length bucket needs to be mentioned
GAME_LENGTH_EDGE = 0.05   # how much better than overall a length bucket must be to be mentioned
LENGTH_LABELS = {"<25": "partidas cortas (<25 min", "25-30": "partidas medias (25-30 min",
                 "30-35": "partidas largas (30-35 min", "35+": "partidas muy largas (35+ min"}
ITEM_ORDINALS = {1: "1er item", 2: "2.º item", 3: "3er item"}
SPIKE_KEYS = ("level_spikes", "item_spikes", "game_length")

ROLE_ALIASES = {
    "top": "TOP", "jungle": "JUNGLE", "jungla": "JUNGLE", "jg": "JUNGLE", "jng": "JUNGLE",
    "mid": "MIDDLE", "medio": "MIDDLE", "middle": "MIDDLE", "centro": "MIDDLE",
    "adc": "BOTTOM", "bot": "BOTTOM", "bottom": "BOTTOM", "tirador": "BOTTOM",
    "soporte": "UTILITY", "support": "UTILITY", "supp": "UTILITY", "sup": "UTILITY",
    "utility": "UTILITY",
}
ROLE_LABELS = {"TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Soporte"}


def parse_role(text) -> str | None:
    """"mid" / "jungla" / "adc" / "soporte" (or the English names) -> "MIDDLE" ... ; None if unknown."""
    key = str(text or "").strip().casefold()
    return ROLE_ALIASES.get(key) or (key.upper() if key.upper() in LANE_ROLES else None)


def _patch_key(patch) -> tuple:
    try:
        return tuple(int(p) for p in str(patch).split("."))
    except ValueError:
        return (-1,)


# ------------------------------------------------------------------ files
def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                     # Windows: a reader or the antivirus holds the file
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_json(path: Path, data, indent=None) -> None:
    _write_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent))


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _row_key(row) -> tuple:
    return row.get("match_id"), row.get("role"), row.get("side")


class LineStore:
    """The data/lineas folder: rows in, aggregates out."""

    def __init__(self, base):
        self.base = Path(base)
        self._file_cache: dict = {}

    # ---- paths
    @property
    def rows_dir(self) -> Path:
        return self.base / "filas"

    @property
    def processed_path(self) -> Path:
        return self.base / "procesadas.json"

    @property
    def names_path(self) -> Path:
        return self.base / "campeones.json"

    @property
    def duelos_dir(self) -> Path:
        return self.base / "duelos"

    @property
    def index_path(self) -> Path:
        return self.base / "indice.json"

    def duelo_path(self, role, champ) -> Path:
        return self.duelos_dir / role / f"{int(champ)}.json"

    # ---- rows
    def append_rows(self, rows) -> int:
        """Append rows to filas/<patch>/<source>.jsonl, flushing each line; returns how many."""
        groups: dict = {}
        for row in rows:
            groups.setdefault((row["patch"] or "sin-parche", row["source"]), []).append(row)
        for (patch, source), group in groups.items():
            path = self.rows_dir / patch / f"{source}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                if fh.tell() > 0:
                    with open(path, "rb") as probe:
                        probe.seek(-1, os.SEEK_END)
                        if probe.read(1) != b"\n":          # an interrupted run cut the last line
                            fh.write(b"\n")
                for row in group:
                    fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                             + b"\n")
                    fh.flush()
        self._file_cache.clear()
        return sum(len(g) for g in groups.values())

    def _read_file(self, path: Path) -> list[dict]:
        try:
            st = path.stat()
        except OSError:
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._file_cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        rows = []
        with open(path, "rb") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                                # truncated or damaged line
                if isinstance(row, dict) and "role" in row and "champ" in row and "opp" in row:
                    rows.append(row)
        self._file_cache[path] = (stamp, rows)
        return rows

    def patches(self) -> list[str]:
        """Patches with rows, newest first."""
        if not self.rows_dir.is_dir():
            return []
        found = [d.name for d in self.rows_dir.iterdir() if d.is_dir() and any(d.glob("*.jsonl"))]
        return sorted(found, key=_patch_key, reverse=True)

    def sources(self) -> list[str]:
        if not self.rows_dir.is_dir():
            return []
        return sorted({f.stem for f in self.rows_dir.glob("*/*.jsonl")})

    def read_rows(self, patches=None, sources=None) -> list[dict]:
        """Rows of those patches / sources (None = all), one per (match, role, side)."""
        out: dict = {}
        for patch in (self.patches() if patches is None else patches):
            folder = self.rows_dir / patch
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.jsonl")):
                if sources is not None and path.stem not in sources:
                    continue
                for row in self._read_file(path):
                    out[_row_key(row)] = row
        return list(out.values())

    def wipe_source(self, source) -> None:
        """Forget every row of a source and its processed matches (before recomputing it)."""
        for path in list(self.rows_dir.glob(f"*/{source}.jsonl")) if self.rows_dir.is_dir() else []:
            path.unlink()
        self._file_cache.clear()
        done = self.processed()
        kept = {k: v for k, v in done.items() if v[0] != source}
        if len(kept) != len(done):
            self._save_processed(kept)

    # ---- processed matches
    def processed(self) -> dict:
        """match_id -> [source, status] ("ok" or the skip reason)."""
        data = _read_json(self.processed_path, {}).get("matches", {})
        return {k: v for k, v in data.items() if isinstance(v, list) and len(v) == 2}

    def _save_processed(self, matches: dict) -> None:
        _write_json(self.processed_path, {"matches": matches})

    def mark_processed(self, entries: dict) -> None:
        if entries:
            merged = self.processed()
            merged.update(entries)
            self._save_processed(merged)

    # ---- champion names
    def names(self) -> dict:
        return {int(k): v for k, v in _read_json(self.names_path, {}).items() if str(k).isdigit()}

    def remember_names(self, names: dict) -> None:
        known = self.names()
        if all(known.get(k) == v for k, v in names.items()):
            return
        known.update(names)
        _write_json(self.names_path, {str(k): v for k, v in sorted(known.items())}, indent=1)

    def people(self) -> Counter:
        """Clan members that have rows, with how many."""
        return Counter(r["person"] for r in self.read_rows() if r.get("person"))

    def find_person(self, text, prefix="") -> str | None:
        """The clan member a user typed: the full name, or the name without the clan prefix."""
        wanted = str(text or "").strip().casefold()
        people = list(self.people())
        for p in people:
            if p.casefold() == wanted:
                return p
        short = [p for p in people
                 if prefix and p.casefold().startswith(prefix.casefold())
                 and p[len(prefix):].strip().casefold() == wanted]
        if len(short) == 1:
            return short[0]
        hits = [p for p in people if wanted and wanted in p.casefold()]
        return hits[0] if len(hits) == 1 else None

    def champion_name(self, champ_id) -> str:
        name = self.names().get(int(champ_id))
        return champion_display(name) if name else f"#{champ_id}"

    def champion_id(self, text) -> int | None:
        """Id of a champion typed as a name (accents, case and spaces ignored) or as a number."""
        text = str(text or "").strip()
        names = self.names()
        if text.isdigit() and int(text) in names:
            return int(text)
        wanted = norm(text)
        for cid, name in names.items():
            if wanted and wanted in (norm(name), norm(champion_display(name))):
                return cid
        return None

    # ---- aggregation
    def resolve_patches(self, role, patches=None, sources=None, person=None, recent=RECENT_PATCHES) -> list[str]:
        """Patch list to use: explicit list, ALL_PATCHES, or the `recent` newest with rows for the role."""
        if patches == ALL_PATCHES:
            return self.patches()
        if patches is not None:
            return list(patches)
        picked = []
        for patch in self.patches():
            if any(r["role"] == role and _wanted(r, sources, person)
                   for r in self.read_rows([patch])):
                picked.append(patch)
                if len(picked) == recent:
                    break
        return picked

    def _role_rows(self, role, patches, sources, person) -> list[dict]:
        return [r for r in self.read_rows(patches) if r["role"] == role and _wanted(r, sources, person)]

    def matchup(self, role, champ, opp, patches=None, sources=None, person=None, recent=RECENT_PATCHES) -> dict:
        """Lane stats of `champ` against `opp` in `role` (championIds).

        patches: None = the `recent` newest patches present for the role, a list, or ALL_PATCHES.
        sources / person filter the rows (person: a clan member's name, case-insensitive).
        Returns {role, champ, opp, patches, games, wins, losses, even, lane_wr (wins + half the
        evens over games), avg_diff14, avg_gold14, avg_xp14, brackets [{bracket, gain_per_min,
        minutes}], first6_rate, first2_rate (share of games where known), shrunk_lane_wr (pulled
        with PRIOR_GAMES toward the champion's overall lane_wr, itself pulled toward 50%),
        low_sample, level_spikes, item_spikes, game_length}. Empty stats are None.
        """
        pts = self.resolve_patches(role, patches, sources, person, recent)
        mine = [r for r in self._role_rows(role, pts, sources, person) if r["champ"] == int(champ)]
        out = _matchup_stats(mine, int(opp))
        out.update({"role": role, "champ": int(champ), "opp": int(opp), "patches": pts})
        return out

    def champion_overall(self, role, champ, patches=None, sources=None, person=None, recent=RECENT_PATCHES) -> dict:
        """The same stats against everyone (the prior of `matchup`); shrunk_lane_wr is pulled toward 50%."""
        pts = self.resolve_patches(role, patches, sources, person, recent)
        mine = [r for r in self._role_rows(role, pts, sources, person) if r["champ"] == int(champ)]
        out = _overall_stats(mine)
        out.update({"role": role, "champ": int(champ), "patches": pts})
        return out

    def matchups_of(self, role, champ, patches=None, sources=None, person=None,
                    min_games=MIN_LIST_GAMES, recent=RECENT_PATCHES) -> list[dict]:
        """`matchup` results for every opponent of `champ`, best shrunk_lane_wr first."""
        pts = self.resolve_patches(role, patches, sources, person, recent)
        mine = [r for r in self._role_rows(role, pts, sources, person) if r["champ"] == int(champ)]
        out = []
        for opp in sorted({r["opp"] for r in mine}):
            m = _matchup_stats(mine, opp)
            if m["games"] >= min_games:
                m.update({"role": role, "champ": int(champ), "opp": opp, "patches": pts})
                out.append(m)
        out.sort(key=lambda m: (-m["shrunk_lane_wr"], -m["games"], m["opp"]))
        return out

    def best_into(self, role, opp, candidates, patches=None, sources=None, person=None,
                  recent=RECENT_PATCHES) -> list[dict]:
        """`candidates` (championIds) ranked by shrunk_lane_wr against `opp`, with their games.

        Every item is a `matchup` result (champ, games, lane_wr, shrunk_lane_wr, low_sample ...);
        candidates never seen against `opp` still rank, at their champion's overall rate, with 0 games.
        """
        pts = self.resolve_patches(role, patches, sources, person, recent)
        rows = self._role_rows(role, pts, sources, person)
        out = []
        for champ in dict.fromkeys(int(c) for c in candidates):
            m = _matchup_stats([r for r in rows if r["champ"] == champ], int(opp))
            m.update({"role": role, "champ": champ, "opp": int(opp), "patches": pts})
            out.append(m)
        out.sort(key=lambda m: (-m["shrunk_lane_wr"], -m["games"], m["champ"]))
        return out

    # ---- precomputed aggregates
    def _window(self, recent) -> tuple[dict, list[str]]:
        """({role: its `recent` newest patches with rows}, patches read to find out).

        Folders are read newest first and reading stops once every role has its window, so a store
        that keeps many patches only parses the recent ones.
        """
        window = {role: [] for role in LANE_ROLES}
        read: list[str] = []
        for patch in self.patches():
            if all(len(p) >= recent for p in window.values()):
                break
            read.append(patch)
            for role in {r["role"] for r in self.read_rows([patch])}:
                if role in window and len(window[role]) < recent:
                    window[role].append(patch)
        return {role: pts for role, pts in window.items() if pts}, read

    def _count_lines(self, patch) -> Counter:
        """Rows per source of a patch folder by counting lines (cheap; duplicates count twice)."""
        counts: Counter = Counter()
        for path in (self.rows_dir / patch).glob("*.jsonl"):
            with open(path, "rb") as fh:
                counts[path.stem] += sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b""))
        return counts

    def rebuild(self, recent=RECENT_PATCHES, now=None) -> dict:
        """Rewrite duelos/ and indice.json from the rows; returns the index.

        Each role uses its `recent` newest patches (all sources, everybody) and only those patch
        folders are parsed. Files of champions that no longer have rows in the window are removed.
        The index counts rows per source: exact for the parsed patches, one per line for the rest.
        """
        window, read = self._window(recent)
        names = self.names()
        written: set = set()
        counts: Counter = Counter()
        for patch in read:
            counts.update(r["source"] for r in self.read_rows([patch]))
        for patch in self.patches():
            if patch not in read:
                counts.update(self._count_lines(patch))
        for role, pts in window.items():
            by_champ: dict = {}
            for r in self.read_rows(pts):
                if r["role"] == role:
                    by_champ.setdefault(r["champ"], []).append(r)
            for champ, mine in by_champ.items():
                overall = _overall_stats(mine)
                by_opp: dict = {}
                for r in mine:
                    by_opp.setdefault(r["opp"], []).append(r)
                prior = _prior(mine)
                vs = {}
                for opp in sorted(by_opp):
                    entry = _matchup_stats(mine, opp, prior=prior, vs=by_opp[opp])
                    if entry["games"] < SPIKE_MIN_GAMES:        # too few games for spikes: omit them
                        entry = {k: v for k, v in entry.items() if k not in SPIKE_KEYS}
                    entry = {"opp_name": champion_display(names.get(opp)) if opp in names else f"#{opp}",
                             **entry}
                    vs[str(opp)] = entry
                data = {"champion": champ,
                        "champion_name": champion_display(names[champ]) if champ in names else f"#{champ}",
                        "role": role, "patches": pts,
                        "sources": sorted({r["source"] for r in mine}),
                        "overall": overall, "vs": vs}
                path = self.duelo_path(role, champ)
                _write_json(path, data, indent=1)
                written.add(path)
        if self.duelos_dir.is_dir():
            for stale in self.duelos_dir.glob("*/*.json"):
                if stale not in written:
                    stale.unlink()
        index = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now or time.time())),
                 "patches": self.patches(), "recent_patches": window,
                 "rows": dict(sorted(counts.items())), "files": len(written)}
        _write_json(self.index_path, index, indent=1)
        return index

    def read_duelo(self, role, champ) -> dict | None:
        """The precomputed aggregate of a champion in a role (None when there is none)."""
        data = _read_json(self.duelo_path(role, champ), {})
        return data or None


def _wanted(row, sources, person) -> bool:
    if sources is not None and row.get("source") not in sources:
        return False
    if person is not None and str(row.get("person") or "").casefold() != str(person).casefold():
        return False
    return True


# ------------------------------------------------------------------ statistics
def _stats(rows) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    even = n - wins - losses

    def mean(key):
        return round(sum(r[key] for r in rows) / n, 1) if n else None

    def rate(key):
        known = [r[key] for r in rows if r.get(key) in (0, 1)]
        return round(sum(known) / len(known), 3) if known else None

    gain, minutes = Counter(), Counter()
    for r in rows:
        for name, b in (r.get("brackets") or {}).items():
            gain[name] += b.get("gain", 0)
            minutes[name] += b.get("minutes", 0)
    brackets = [{"bracket": name, "gain_per_min": round(gain[name] / minutes[name], 1),
                 "minutes": minutes[name]} for name in BRACKET_NAMES if minutes[name]]
    return {"games": n, "wins": wins, "losses": losses, "even": even,
            "lane_wr": round((wins + 0.5 * even) / n, 3) if n else None,
            "avg_diff14": mean("diff14"), "avg_gold14": mean("gold14"), "avg_xp14": mean("xp14"),
            "brackets": brackets, "first6_rate": rate("first6"), "first2_rate": rate("first2"),
            "low_sample": n < LOW_SAMPLE_GAMES,
            "level_spikes": _level_spikes(rows), "item_spikes": _item_spikes(rows),
            "game_length": _game_length(rows)}


def _style_means(rows) -> dict:
    """Mean of each style field over the rows that have it (None when none does); see STYLE_KEYS."""
    out = {}
    for key in STYLE_KEYS:
        known = [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
        out[key] = round(sum(known) / len(known), 4) if known else None
    return out


def _by_minute(row) -> dict:
    """minute -> [minute, my level, opp level, diff] of a row's series."""
    return {e[0]: e for e in row.get("series") or [] if isinstance(e, list) and len(e) == 4}


def _level_spikes(rows) -> list[dict]:
    """Gold+xp diff gained per minute at each own level 1..SPIKE_LEVELS (minutes before 15 only)."""
    gain, minutes = Counter(), Counter()
    for r in rows:
        s = _by_minute(r)
        for m in range(1, LANE_MINUTE + 1):
            if m in s and m - 1 in s:
                level = s[m - 1][1]
                if 1 <= level <= SPIKE_LEVELS:
                    gain[level] += s[m][3] - s[m - 1][3]
                    minutes[level] += 1
    return [{"level": lv, "gain_per_min": round(gain[lv] / minutes[lv], 1), "minutes": minutes[lv]}
            for lv in range(1, SPIKE_LEVELS + 1) if minutes[lv]]


def _diff_at(series: dict, t: float):
    """The diff at minute `t` (linear between whole minutes); None outside the series."""
    lo = int(t)
    if lo not in series:
        return None
    if lo == t:
        return series[lo][3]
    if lo + 1 not in series:
        return None
    return series[lo][3] + (series[lo + 1][3] - series[lo][3]) * (t - lo)


def _median(values) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _item_spikes(rows) -> list[dict]:
    """For the 1st..3rd legendary: median completion minute and the diff gained per minute in the
    ITEM_WINDOW minutes before vs after it (averaged over the games where both windows exist)."""
    out = []
    for k in range(ITEM_COUNT):
        times, before, after = [], [], []
        for r in rows:
            items = r.get("items")
            t = items[k] if isinstance(items, list) and len(items) > k else None
            if t is None:
                continue
            times.append(t)
            s = _by_minute(r)
            d0, d_prev, d_next = (_diff_at(s, x) for x in (t, t - ITEM_WINDOW, t + ITEM_WINDOW))
            if t - ITEM_WINDOW >= 0 and None not in (d0, d_prev, d_next):
                before.append((d0 - d_prev) / ITEM_WINDOW)
                after.append((d_next - d0) / ITEM_WINDOW)
        if times:
            out.append({"item": k + 1, "games": len(times), "median_minute": round(_median(times), 1),
                        "gain_before": round(sum(before) / len(before), 1) if before else None,
                        "gain_after": round(sum(after) / len(after), 1) if after else None,
                        "windows": len(before)})
    return out


def _game_length(rows) -> dict:
    """Win rate of the game (not the lane) by game length: overall and per bucket."""
    known = [r for r in rows if r.get("win") in (0, 1) and isinstance(r.get("minutes"), (int, float))]

    def cell(group):
        wins = sum(r["win"] for r in group)
        return {"games": len(group), "wins": wins, "win_rate": round(wins / len(group), 3)}

    buckets = []
    for name, low, high in GAME_LENGTH_BUCKETS:
        group = [r for r in known if low <= r["minutes"] < high]
        if group:
            buckets.append({"bucket": name, **cell(group)})
    return {"overall": cell(known) if known else None, "buckets": buckets}


def _shrink(stats, prior) -> float:
    games = stats["games"]
    score = (stats["wins"] + 0.5 * stats["even"]) if games else 0
    return round((score + PRIOR_GAMES * prior) / (games + PRIOR_GAMES), 3)


def _overall_stats(rows) -> dict:
    stats = _stats(rows)
    stats["shrunk_lane_wr"] = _shrink(stats, 0.5)
    stats.update(_style_means(rows))            # per champion and role only, not per matchup
    return stats


def _prior(champ_rows) -> float:
    """The prior of a champion's matchups: its overall lane_wr pulled toward 50% (see `_matchup_stats`)."""
    n = len(champ_rows)
    if not n:
        return 0.5
    wins = sum(1 for r in champ_rows if r["result"] == "win")
    losses = sum(1 for r in champ_rows if r["result"] == "loss")
    return round((wins + 0.5 * (n - wins - losses) + PRIOR_GAMES * 0.5) / (n + PRIOR_GAMES), 3)


def _matchup_stats(champ_rows, opp, prior=None, vs=None) -> dict:
    """Stats against `opp` out of all of a champion's rows, shrunk toward its overall rate.

    The prior is the champion's overall lane_wr itself pulled toward 50% (a champion seen in two
    games, both in this matchup, would otherwise be its own prior and keep its 100%). `prior` and
    `vs` (the rows against `opp`) can be given when they were already computed.
    """
    if vs is None:
        vs = [r for r in champ_rows if r["opp"] == opp]
    stats = _stats(vs)
    stats["shrunk_lane_wr"] = _shrink(stats, _prior(champ_rows) if prior is None else prior)
    return stats


# ------------------------------------------------------------------ text
def signed(value) -> str:
    return ("+" if value >= 0 else "−") + f"{abs(round(value))}"


def text_summary(result: dict) -> list[str]:
    """Short plain Spanish lines about a `matchup` / `champion_overall` result."""
    games = result.get("games") or 0
    if not games:
        return ["Sin partidas de este cruce en las muestras."]
    wr = result["lane_wr"]
    pct = round(wr * 100)
    detail = f"{games} {'partida' if games == 1 else 'partidas'}, {signed(result['avg_diff14'])} oro+exp al 14"
    if wr >= 0.55:
        lines = [f"Ganas la línea {pct}% ({detail})"]
    elif wr <= 0.45:
        lines = [f"Pierdes la línea: solo la ganas {pct}% ({detail})"]
    else:
        lines = [f"La línea va pareja: la ganas {pct}% ({detail})"]
    usable = [b for b in result.get("brackets") or [] if b["minutes"] >= BRACKET_MIN_MINUTES]
    strong = max(usable, key=lambda b: b["gain_per_min"], default=None)
    weak = min(usable, key=lambda b: b["gain_per_min"], default=None)
    parts = []
    if strong and strong["gain_per_min"] >= BRACKET_NOTABLE:
        parts.append(f"Fuerte en niveles {strong['bracket']} ({signed(strong['gain_per_min'])}/min)")
    if weak and weak["gain_per_min"] <= -BRACKET_NOTABLE:
        parts.append(f"débil en {weak['bracket']} ({signed(weak['gain_per_min'])}/min)"
                     if parts else f"Débil en niveles {weak['bracket']} ({signed(weak['gain_per_min'])}/min)")
    if parts:
        lines.append(" · ".join(parts))
    first6, first2 = result.get("first6_rate"), result.get("first2_rate")
    if first6 is not None:
        extra = f" (a nivel 2, el {round(first2 * 100)}%)" if first2 is not None else ""
        lines.append(f"Llegas antes a nivel 6 el {round(first6 * 100)}%{extra}")
    elif first2 is not None:
        lines.append(f"Llegas antes a nivel 2 el {round(first2 * 100)}%")
    lines += _spike_lines(result)
    shrunk = result.get("shrunk_lane_wr")
    if result.get("low_sample"):
        note = "Pocas partidas: tómalo con cautela"
        if shrunk is not None:
            note += f"; ajustado a lo que rinde el campeón en general sería {round(shrunk * 100)}%"
        lines.append(note + ".")
    return lines


def _spike_lines(result) -> list[str]:
    """Plain lines about the power spikes and the game length; empty when nothing stands out."""
    lines = []
    thin = " (pocas partidas)" if result.get("low_sample") else ""
    spikes = result.get("level_spikes") or []
    total = sum(b["minutes"] for b in spikes)
    base = sum(b["gain_per_min"] * b["minutes"] for b in spikes) / total if total else 0
    levels = [b for b in spikes                   # clearly above this champion's own average pace
              if b["minutes"] >= BRACKET_MIN_MINUTES and b["gain_per_min"] - base >= BRACKET_NOTABLE]
    levels = sorted(sorted(levels, key=lambda b: -b["gain_per_min"])[:2], key=lambda b: b["level"])
    parts = []
    if levels:
        parts.append("nivel " + " y ".join(str(b["level"]) for b in levels))
    for it in result.get("item_spikes") or []:
        if (it["windows"] >= ITEM_SPIKE_MIN_WINDOWS
                and it["gain_after"] - it["gain_before"] >= ITEM_SPIKE_NOTABLE):
            parts.append(f"{ITEM_ORDINALS[it['item']]} (min {it['median_minute']:.0f})")
            break
    if parts:
        lines.append(f"Picos{thin}: " + "; ".join(parts))
    gl = result.get("game_length") or {}
    overall = gl.get("overall")
    ok = [b for b in gl.get("buckets") or [] if b["games"] >= GAME_LENGTH_MIN]
    if overall and ok:
        best = max(ok, key=lambda b: (b["win_rate"], b["games"]))
        if best["win_rate"] - overall["win_rate"] >= GAME_LENGTH_EDGE:
            few = ", pocas partidas" if best["games"] < LOW_SAMPLE_GAMES else ""
            lines.append(f"Mejor en {LENGTH_LABELS[best['bucket']]}: {round(best['win_rate'] * 100)}%{few})")
    return lines


# ------------------------------------------------------------------ rows from matches
def pair_rows(pair, source=SOURCE_OWN, tier=None, person_of=None) -> list[dict]:
    """The two row dicts of a lane pair; `person_of(view)` gives the clan member's name or None."""
    platform = str(pair["match_id"]).split("_")[0].lower() if "_" in str(pair["match_id"]) else ""
    rows = []
    for v in pair["sides"]:
        rows.append({
            "match_id": pair["match_id"], "source": source, "platform": platform,
            "patch": pair["patch"], "tier": tier, "queue": pair["queue"],
            "game_creation": pair["game_creation"], "role": pair["role"], "side": v["side"],
            "champ": v["champ"], "opp": v["opp"], "diff14": v["diff14"], "gold14": v["gold14"],
            "xp14": v["xp14"], "result": v["result"], "brackets": v["brackets"],
            "first2": v["first2"], "first6": v["first6"], "series": v["series"],
            "items": v["items"], "win": v["win"], "minutes": v["minutes"],
            **{k: v.get(k) for k in STYLE_KEYS},
            "person": person_of(v) if person_of else None})
    return rows


class PersonLabeler:
    """Names the clan members like Composiciones does (`comps.resolve_people`).

    Clan = a registered player, or anyone whose game name starts with the clan prefix (CLAN_PREFIX).
    Everybody else stays anonymous.
    """

    def __init__(self, tracked: dict, clan):
        self.tracked = dict(tracked)          # puuid -> "Nombre#TAG" of the registered players
        self.clan = clan

    @classmethod
    def from_store(cls, lol_store, root=None) -> "PersonLabeler":
        from .comps import ClanConfig
        from .players import load_roster
        from .settings import read_settings
        st = read_settings(root) if root is not None else None
        roster = load_roster(lol_store, st.riot_id if st else "")
        tracked = {p.puuid: p.riot_id for p in roster.players}
        clan = ClanConfig.build(st.clan_prefix if st else "", st.clan_aliases if st else "", tracked)
        return cls(tracked, clan)

    def label_all(self, pairs) -> dict:
        """(match_id, role, side) -> person for the clan members seen in `pairs`.

        A puuid is clan if it is registered or any of its game names starts with the prefix; it is
        shown under its latest game name after the aliases, so two accounts aliased to each other
        become one person (the rule of `comps.resolve_people`). Registered players without a name
        in `pairs` fall back to their Riot ID's name.
        """
        latest, clan_ids = {}, set()
        for pair in sorted(pairs, key=lambda p: p["game_creation"] or 0):
            for v in pair["sides"]:
                if v["name"]:
                    latest[v["puuid"]] = v["name"]
                    if self.clan.matches_prefix(v["name"]):
                        clan_ids.add(v["puuid"])
        out = {}
        for pair in pairs:
            for v in pair["sides"]:
                puuid = v["puuid"]
                if puuid in self.tracked or puuid in clan_ids:
                    name = latest.get(puuid) or self.tracked.get(puuid, "").split("#")[0]
                    if name:
                        out[(pair["match_id"], pair["role"], v["side"])] = self.clan.person(name)
        return out


def ddragon_legendary(dd):
    """`legendary_for(gameVersion)` backed by a DDragon cache: the completed-item ids of that patch.

    None when the cache has no item list for the patch itself (no guessing from another patch).
    """
    cache: dict = {}

    def get(game_version):
        patch = patch_of(game_version)
        if patch not in cache:
            found = None
            if patch:
                for v in sorted((v for v in dd.versions() if v.startswith(patch + ".")),
                                key=version_key, reverse=True):
                    data = dd.read_json(f"{v}/data/{LANG}/item.json")
                    if data:
                        found = legendary_items(data)
                        break
            cache[patch] = found
        return cache[patch]
    return get


def add_matches(store: LineStore, matches, names=None, source=SOURCE_OWN, tier=None, labeler=None,
                legendary_for=None) -> dict:
    """Turn (match_id, match, timeline) items into rows, skipping match ids already processed.

    Returns {"matches", "rows", "skipped": Counter(reason), "champions": Counter(championId: rows
    written)}. The batch is held in memory (so
    people can be labelled by their latest name), the rows are appended and then the ids marked
    as processed: an interruption in between only repeats work, rows are deduplicated on read.
    """
    done = store.processed()
    pairs, entries, skipped, champ_names = [], {}, Counter(), {}
    for match_id, match, timeline in matches:
        if match_id in done:
            continue
        legendary = legendary_for((match.get("info") or {}).get("gameVersion")) if legendary_for else None
        found, reason = analyze_match(match, timeline, legendary)
        if reason == "sin_timeline":
            skipped[reason] += 1
            continue                      # it may get its timeline later: not marked as processed
        entries[match_id] = [source, reason or "ok"]
        if reason:
            skipped[reason] += 1
        pairs.extend(found)
        for p in (match.get("info") or {}).get("participants") or []:
            if p.get("championId") and p.get("championName"):
                champ_names[int(p["championId"])] = p["championName"]
    people = labeler.label_all(pairs) if labeler else {}
    count = 0
    champions = Counter(v["champ"] for pair in pairs for v in pair["sides"])
    for i in range(0, len(pairs), SAVE_EVERY):
        chunk = pairs[i:i + SAVE_EVERY]
        rows = []
        for pair in chunk:
            rows += pair_rows(pair, source, tier,
                              lambda v, pair=pair: people.get((pair["match_id"], pair["role"], v["side"])))
        count += store.append_rows(rows)
    store.remember_names({**(names or {}), **champ_names})
    store.mark_processed(entries)
    return {"matches": len(entries), "rows": count, "skipped": skipped, "champions": champions}


def update_from_lol(lol_store, data_dir, root=None, full=False, source=SOURCE_OWN) -> dict:
    """Rows for the stored matches with a timeline that have none yet, then `rebuild`.

    full=True first forgets this source's rows and recomputes every stored match (what
    `lineas --reconstruir` does). `data_dir` is the folder holding lol.db; files go in data_dir/lineas.
    """
    store = LineStore(Path(data_dir) / "lineas")
    if full:
        store.wipe_source(source)
    done = store.processed()
    labeler = PersonLabeler.from_store(lol_store, root)
    total = lol_store.count_matches()
    from .ddragon import DDragon
    summary = add_matches(store, lol_store.iter_matches(only_timeline=True, skip=done),
                          source=source, labeler=labeler,
                          legendary_for=ddragon_legendary(DDragon(Path(data_dir) / "ddragon")))
    summary["sin_timeline"] = max(0, total - summary["matches"] - sum(
        1 for v in done.values() if v[0] == source))
    summary["index"] = store.rebuild()
    return summary
