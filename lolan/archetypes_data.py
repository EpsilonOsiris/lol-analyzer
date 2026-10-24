"""Decide the ambiguous champion types from the Diamond+ sample (`python -m lolan arquetipos`).

Riot's subclass cannot tell some champions apart (an early jungler from a farming one, a bruiser
from a split pusher, an assassin from a melee duelist). For those, `comps.PENDING_DATA` lists the
judgment calls and this module measures each champion against its lane's average.

Evidence, from the rows of the "diamante" source in the 3 newest patches that have any (rows
written before the style fields existed lack them and simply count for nothing there):
  * early-ness (jungle): early kill participation (kp14, else ka14: kills + assists before 14:00)
    and the game-length slope (win rate in games under 25 minutes minus the one in games of 35+
    minutes), equal weights. Gold+xp against the enemy jungler is NOT used: it mostly rewards
    clear speed, not early pressure (Karthus and Zyra came out "very early" with it). Rows
    without ka14/kp14 cannot feed the first part: with none of them only the slope is used, and
    the report says so;
  * lane-ness (mid): gold+xp gained per minute against the opponent at own levels 1-5, diff14 and
    the same slope, used with tank-ness for melee vs assassin;
  * tank-ness: share of the team's damage taken + self-mitigated, and seconds of CC per minute;
  * split-ness: damage to buildings per minute, turret plates, and a LOWER kill participation;
  * carry: share of the team's damage to champions (shown, not used to decide).
Each value is a z-score against the lane: the mean and the standard deviation are taken over the
champions of that lane with at least MIN_GAMES games, so "0" is a typical champion of the lane.
A score is the mean of the z-scores of its parts that are available.

Decision rules (M = DECISION_MARGIN; a champion needs MIN_GAMES games in the lane and the scores
its lane uses, otherwise it stays "pendiente"):
  * JUNGLE  early if early-ness >= +M, farm if <= -M, otherwise inconclusive.
  * TOP     tanque if tank >= +M and tank - split >= M; split if split >= +M and split - tank >= M;
            bruiser otherwise (the middle ground between the two).
  * MIDDLE  lean = tank - EARLY_WEIGHT * lane (melee duelists take damage, assassins burst early):
            melee if lean >= +M, asesino if lean <= -M, otherwise inconclusive.
Sanity report (no changes): champions with MIN_GAMES games whose current type is contradicted by
a score beyond STRONG_Z (see `contradicts`).
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict

from . import comps, samples
from .lanephase import STYLE_KEYS

MIN_GAMES = 30              # games in the lane for a champion to be measured or decided
MIN_CHAMPIONS = 5           # champions with MIN_GAMES a lane needs to have a mean and a deviation
PATCH_WINDOW = 3            # newest patches with Diamond+ rows
SOURCE = samples.SOURCE_DIAMOND
DECISION_MARGIN = 0.25      # z-score a score must reach to decide
STRONG_Z = 1.0              # z-score beyond which the evidence contradicts a type (sanity report)
EARLY_WEIGHT = 0.5          # weight of early-ness against tank-ness for melee vs assassin
EARLY_BRACKETS = ("1-2", "3-5")        # own levels 1-5
SHORT_GAME_MIN, LONG_GAME_MIN = 25, 35
MIN_BUCKET_GAMES = 8        # games under 25 / over 35 minutes for the win rate gap to count
# Lanes with a data rule and the types they can end up with.
DECIDABLE = {"JUNGLE": ("early", "farm"), "TOP": ("tanque", "bruiser", "split"),
             "MIDDLE": ("asesino", "melee")}
LANE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Raw measures per champion; "n" is how many games each one needs to exist.
MEASURES = ("early_gain", "diff14", "wr_gap", *STYLE_KEYS)


# ------------------------------------------------------------------ reading rows
def window_patches(store: samples.LineStore, n=PATCH_WINDOW, source=SOURCE) -> list[str]:
    """The `n` newest patches that have a non-empty rows file of `source`."""
    found = []
    for patch in store.patches():
        path = store.rows_dir / patch / f"{source}.jsonl"
        try:
            if path.stat().st_size > 0:
                found.append(patch)
        except OSError:
            continue
        if len(found) == n:
            break
    return found


def iter_rows(store: samples.LineStore, patches, source=SOURCE):
    """Rows of `source` in `patches`, streamed one at a time (the files are tens of MB), one per
    (match, role, side); damaged lines are skipped."""
    seen = set()
    for patch in patches:
        path = store.rows_dir / patch / f"{source}.jsonl"
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "role" not in row or "champ" not in row:
                    continue
                key = (row.get("match_id"), row.get("role"), row.get("side"))
                if key in seen:
                    continue
                seen.add(key)
                yield row


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Acc:
    """Running sums of one champion in one lane."""

    def __init__(self):
        self.games = 0
        self.sums = defaultdict(float)       # measure -> sum
        self.counts = defaultdict(int)       # measure -> rows with a value
        self.short = [0, 0]                  # [wins, games] under SHORT_GAME_MIN
        self.long = [0, 0]                   # [wins, games] of LONG_GAME_MIN or more

    def _put(self, name, value):
        if value is not None:
            self.sums[name] += value
            self.counts[name] += 1

    def add(self, row):
        self.games += 1
        minutes = 0
        gain = 0.0
        for name, bracket in (row.get("brackets") or {}).items():
            if name in EARLY_BRACKETS and isinstance(bracket, dict):
                minutes += _number(bracket.get("minutes")) or 0
                gain += _number(bracket.get("gain")) or 0
        if minutes:
            self._put("early_gain", gain / minutes)         # per game, so every game weighs the same
        self._put("diff14", _number(row.get("diff14")))
        for key in STYLE_KEYS:
            self._put(key, _number(row.get(key)))
        length, win = _number(row.get("minutes")), row.get("win")
        if length is not None and win in (0, 1):
            if length < SHORT_GAME_MIN:
                self.short[0] += win
                self.short[1] += 1
            elif length >= LONG_GAME_MIN:
                self.long[0] += win
                self.long[1] += 1

    def measures(self) -> dict:
        """measure -> mean over the rows that have it (None when fewer than MIN_GAMES do)."""
        out = {}
        for name in MEASURES:
            if name == "wr_gap":
                ok = self.short[1] >= MIN_BUCKET_GAMES and self.long[1] >= MIN_BUCKET_GAMES
                out[name] = (self.short[0] / self.short[1] - self.long[0] / self.long[1]) if ok else None
            else:
                out[name] = self.sums[name] / self.counts[name] if self.counts[name] >= MIN_GAMES else None
        return out


def collect(store: samples.LineStore, patches=None, source=SOURCE) -> tuple[dict, list[str]]:
    """({lane: {championId: Acc}}, patches used) from the sample."""
    patches = window_patches(store, source=source) if patches is None else list(patches)
    accs: dict = {lane: {} for lane in LANE_ORDER}
    for row in iter_rows(store, patches, source):
        lane = accs.get(row["role"])
        if lane is not None:
            lane.setdefault(row["champ"], Acc()).add(row)
    return accs, patches


# ------------------------------------------------------------------- evidence
def lane_stats(measured: dict) -> dict:
    """measure -> (mean, standard deviation) over the champions {id: measures} that have it and
    enough games; absent when fewer than MIN_CHAMPIONS have it."""
    out = {}
    for name in MEASURES:
        values = [m[name] for m, games in measured.values() if games >= MIN_GAMES and m[name] is not None]
        if len(values) >= MIN_CHAMPIONS:
            mean = sum(values) / len(values)
            var = sum((v - mean) ** 2 for v in values) / len(values)
            if var > 0:
                out[name] = (mean, var ** 0.5)
    return out


def _z(value, stat):
    return None if value is None or stat is None else (value - stat[0]) / stat[1]


def _mean(parts, required):
    """Mean of the available z-scores; None when a required one (or all) is missing."""
    if any(parts.get(name) is None for name in required):
        return None
    known = [v for v in parts.values() if v is not None]
    return sum(known) / len(known) if known else None


def evidence(measures: dict, stats: dict) -> dict:
    """{"z": {measure: z}, "early", "tank", "split", "carry"} of one champion against its lane."""
    z = {name: _z(measures.get(name), stats.get(name)) for name in MEASURES}
    kp = None if z["kp"] is None else -z["kp"]             # split pushers take part in fewer kills
    kills = z["kp14"] if z["kp14"] is not None else z["ka14"]
    early_parts = {"kills": kills, "slope": z["wr_gap"]}
    return {
        "z": z,
        "early": _mean(early_parts, ()),
        "early_parts": [name for name, v in early_parts.items() if v is not None],
        "lane": _mean({"g": z["early_gain"], "d": z["diff14"], "w": z["wr_gap"]}, ("g", "d")),
        "tank": _mean({"t": z["tank_share"], "c": z["cc_pm"]}, ("t",)),
        "split": _mean({"b": z["bld_pm"], "p": z["turret_plates"], "k": kp}, ("b",)),
        "carry": z["dmg_share"],
    }


def decide(lane: str, ev: dict) -> tuple[str | None, str]:
    """(type, reason): the type the rules of `lane` pick, or None with the reason there is none."""
    early, tank, split = ev.get("early"), ev.get("tank"), ev.get("split")
    m = DECISION_MARGIN
    if lane == "JUNGLE":
        if early is None:
            return None, "faltan la pendiente de victorias y las kills tempranas"
        only = "" if "kills" in (ev.get("early_parts") or ()) else " (solo la pendiente de victorias: faltan ka14/kp14)"
        if early >= m:
            return "early", f"early-ness {early:+.2f}{only}"
        if early <= -m:
            return "farm", f"early-ness {early:+.2f}{only}"
        return None, f"evidencia ambigua (early-ness {early:+.2f}){only}"
    if lane == "TOP":
        if tank is None or split is None:
            return None, "faltan los campos de estilo (daño recibido, edificios)"
        if tank >= m and tank - split >= m:
            return "tanque", f"tank {tank:+.2f} contra split {split:+.2f}"
        if split >= m and split - tank >= m:
            return "split", f"split {split:+.2f} contra tank {tank:+.2f}"
        return "bruiser", f"punto medio (tank {tank:+.2f}, split {split:+.2f})"
    if lane == "MIDDLE":
        lane_z = ev.get("lane")
        if tank is None or lane_z is None:
            return None, "faltan los campos de estilo (daño recibido) o los de línea"
        lean = tank - EARLY_WEIGHT * lane_z
        if lean >= m:
            return "melee", f"tank {tank:+.2f}, línea {lane_z:+.2f}"
        if lean <= -m:
            return "asesino", f"tank {tank:+.2f}, línea {lane_z:+.2f}"
        return None, f"evidencia ambigua (tank {tank:+.2f}, línea {lane_z:+.2f})"
    return None, "sin regla de datos para esta línea"


def contradicts(lane: str, kind: str, ev: dict) -> str | None:
    """Why the evidence strongly contradicts `kind` in `lane`, or None (the sanity report)."""
    early, tank, split = ev.get("early"), ev.get("tank"), ev.get("split")
    s = STRONG_Z
    if lane == "JUNGLE":
        if kind == "early" and early is not None and early <= -s:
            return f"early-ness {early:+.2f}"
        if kind == "farm" and early is not None and early >= s:
            return f"early-ness {early:+.2f}"
        if kind == "tanque" and tank is not None and tank <= -s:
            return f"tank {tank:+.2f}"
    elif lane == "TOP":
        if kind == "tanque" and tank is not None and tank <= -s:
            return f"tank {tank:+.2f}"
        if kind == "split" and ((tank is not None and tank >= s) or (split is not None and split <= -s)):
            return f"tank {tank if tank is not None else 0:+.2f}, split {split if split is not None else 0:+.2f}"
        if kind == "bruiser" and tank is not None and split is not None and (
                (tank >= s and split <= 0) or (split >= s and tank <= 0)):
            return f"tank {tank:+.2f}, split {split:+.2f}"
        if kind == "mago" and tank is not None and tank >= s:
            return f"tank {tank:+.2f}"
    elif lane == "MIDDLE" and tank is not None:
        lean = tank - EARLY_WEIGHT * (ev.get("lane") or 0)
        if kind in ("asesino", "control") and lean >= s:
            return f"tank {tank:+.2f}"
        if kind == "melee" and lean <= -s:
            return f"tank {tank:+.2f}"
    elif lane == "UTILITY" and tank is not None:
        if kind == "engage" and tank <= -s:
            return f"tank {tank:+.2f}"
        if kind in ("peel", "poke") and tank >= s:
            return f"tank {tank:+.2f}"
    return None


# ---------------------------------------------------------------------- report
def load_classes(path=None) -> dict:
    return comps.load_classes(comps.CLASSES_PATH if path is None else path)


def analyze(store: samples.LineStore, classes: dict, lanes=LANE_ORDER, patches=None) -> dict:
    """The whole report: {"patches", "candidates": [...], "contradictions": [...]}.

    A candidate is a judgment call (comps.PENDING_DATA) or an entry already decided by data.
    Both lists hold dicts with lane, champion, games, type (the current one), source, ev, and, for
    candidates, "decision" (type or None) and "reason".
    """
    accs, used = collect(store, patches)
    names = store.names()
    tables = comps.build_tables(classes)
    candidates, contradictions = [], []
    pending = {lane: set(champions) for lane, champions in comps.PENDING_DATA.items()}
    for name, entry in classes.items():
        key = comps.table_key(name, entry)
        for lane, prop in (entry.get("proposal") or {}).items():
            if prop.get("basis") == "datos" and lane in DECIDABLE:
                pending.setdefault(lane, set()).add(key)
    for lane in lanes:
        if lane not in accs:
            continue
        measured = {cid: (acc.measures(), acc.games) for cid, acc in accs[lane].items()}
        stats = lane_stats(measured)
        by_name = {}
        for cid, (measures, games) in measured.items():
            champion = names.get(cid)
            if champion:
                by_name[comps.canonical_champion(champion)] = (measures, games)
        for champion, (measures, games) in sorted(by_name.items()):
            current = tables[lane].get(champion)
            if games < MIN_GAMES or current is None:
                continue
            ev = evidence(measures, stats)
            why = contradicts(lane, current["type"], ev)
            if why:
                contradictions.append({"lane": lane, "champion": champion, "games": games, "type": current["type"],
                                       "source": current["source"], "ev": ev, "why": why})
        for champion in sorted(pending.get(lane, ())):
            current = tables[lane].get(champion)
            measures, games = by_name.get(champion, ({name: None for name in MEASURES}, 0))
            ev = evidence(measures, stats)
            if games < MIN_GAMES:
                kind, reason = None, f"solo {games} partidas (hacen falta {MIN_GAMES})"
            else:
                kind, reason = decide(lane, ev)
            candidates.append({"lane": lane, "champion": champion, "games": games, "ev": ev,
                               "type": current["type"] if current else "otro",
                               "source": current["source"] if current else None,
                               "decision": kind, "reason": reason, "measures": measures})
    return {"patches": used, "candidates": candidates, "contradictions": contradictions}


def _fmt(value) -> str:
    return "-" if value is None else f"{value:+.2f}"


def report_lines(report: dict, decided=False) -> list[str]:
    """Plain text of an `analyze` result."""
    patches = ", ".join(report["patches"]) or "(ninguno)"
    out = [f"Muestra Diamante+ · parches {patches} · mínimo {MIN_GAMES} partidas por campeón y línea.", ""]
    if not report["patches"]:
        out.append("No hay partidas Diamante+ descargadas (python -m lolan muestras).")
    out.append("Casos dudosos (puntajes z contra el promedio de su línea; + = más de eso):")
    header = f"{'Línea':<8}{'Campeón':<14}{'Hoy':<9}{'Partidas':>8}  {'Temprano':>8}{'Oro línea':>10}{'Tanque':>8}{'Split':>8}{'Carry':>8}  Decisión"
    out.append(header)
    for c in report["candidates"]:
        ev = c["ev"]
        verdict = c["decision"] or f"pendiente: {c['reason']}"
        if c["decision"]:
            verdict = f"{c['decision']}" + (" (cambia)" if c["decision"] != c["type"] else " (igual)") + f" · {c['reason']}"
        out.append(f"{samples.ROLE_LABELS[c['lane']]:<8}{c['champion']:<14}{c['type']:<9}{c['games']:>8}  "
                   f"{_fmt(ev['early']):>8}{_fmt(ev['lane']):>10}{_fmt(ev['tank']):>8}{_fmt(ev['split']):>8}{_fmt(ev['carry']):>8}  {verdict}")
    if not report["candidates"]:
        out.append("  (ninguno)")
    decidable = [c for c in report["candidates"] if c["decision"]]
    out.append("")
    out.append(f"{len(decidable)} de {len(report['candidates'])} se pueden decidir con los datos de hoy"
               + (" y se escribieron en lolan/data/champion_classes.json." if decided
                  else " (usa --decidir para escribirlos)."))
    out += ["", f"Contradicciones fuertes (|z| >= {STRONG_Z:g}) entre la evidencia y el tipo actual, "
                f"campeones con {MIN_GAMES}+ partidas (solo informativo, no cambia nada):"]
    if report["contradictions"]:
        out.append(f"{'Línea':<8}{'Campeón':<14}{'Tipo':<9}{'Origen':<22}{'Partidas':>8}  Evidencia")
        for c in report["contradictions"]:
            out.append(f"{samples.ROLE_LABELS[c['lane']]:<8}{c['champion']:<14}{c['type']:<9}"
                       f"{comps.SOURCE_LABELS.get(c['source'], '-'):<22}{c['games']:>8}  {c['why']}")
    else:
        out.append("  (ninguna con los datos de hoy)")
    return out


# ------------------------------------------------------------------- writing
def apply_decisions(classes: dict, report: dict, today=None) -> list[dict]:
    """Write every decided candidate into `classes` (in place); returns the ones written.

    proposal.type changes, basis becomes "datos" and the evidence, games, patches and date are kept
    next to the old value (`previous`); a candidate with no decision is left untouched.
    """
    today = today or time.strftime("%Y-%m-%d")
    by_key = {comps.table_key(name, entry): entry for name, entry in classes.items()}
    written = []
    for c in report["candidates"]:
        if not c["decision"]:
            continue
        prop = by_key[c["champion"]]["proposal"][c["lane"]]
        before = prop["type"]
        ev = c["ev"]
        prop.update({
            "type": c["decision"], "basis": "datos",
            "note": (f"Decidido por datos (Diamante+, parches {', '.join(report['patches'])}, "
                     f"{c['games']} partidas): {c['reason']}."),
            "evidence": {"early": _round(ev["early"]), "tank": _round(ev["tank"]),
                         "split": _round(ev["split"]), "carry": _round(ev["carry"]),
                         "measures": {k: _round(v, 4) for k, v in c["measures"].items()}},
            "n": c["games"], "patches": list(report["patches"]), "date": today, "previous": before})
        written.append({**c, "previous": before})
    return written


def _round(value, digits=2):
    return None if value is None else round(value, digits)


def save_classes(classes: dict, path=None) -> None:
    """Replace the JSON atomically, keeping its format (2-space indent, UTF-8, no trailing newline)."""
    path = comps.CLASSES_PATH if path is None else path
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(classes, indent=2, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
