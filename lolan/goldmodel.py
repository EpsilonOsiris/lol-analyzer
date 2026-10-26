"""Estimated total gold of a player in a live game, calibrated with the stored timelines.

Riot's Live Client API only gives the real gold of the player who is playing. For everybody else we
estimate the TOTAL gold earned so far with a linear model fitted (ordinary least squares, pure
Python) on every timeline frame of every stored solo/flex game:

    total_gold ~ 1 + minute + cs + kills + assists + is_support + is_support * minute

`cs` is lane + jungle minions together, because that is what the Live Client reports
(`scores.creepScore`). Two quirks of that number, verified against Riot's timeline of the same game:
it is rounded DOWN to a multiple of 10 for all ten players (so the live code adds LIVE_CS_OFFSET,
the middle of the 0-9 gap), and the enemy jungler's camps only count when the player's team sees
them, so for that player a second model uses the champion level (exact, shown on TAB) instead:

    total_gold ~ 1 + minute + level + kills + assists + is_support + is_support * minute
 The fit is by-game holdout validated: 80% of the games fit the model and the
other 20% give the typical error per phase of the game (shown to the user as a "±" figure).

The coefficients live in data/gold_model.json (not in git). They are refitted in a background
thread when the number of stored matches changed by MIN_NEW_MATCHES or more since the last fit, or
when the file is missing; until then conservative defaults are used. Nothing here blocks a request.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import zlib
from pathlib import Path

N_FEATURES = 7
FEATURE_NAMES = ["base", "minuto", "cs", "kills", "asistencias", "soporte", "soporte_x_minuto"]
MIN_FIT_MINUTE = 2.0            # earlier frames are mostly the 500 starting gold
START_GOLD = 500
QUEUES = (420, 440, 710)        # solo/duo, flex and ranked 5-premade
HOLDOUT_EVERY = 5               # 1 game in 5 is held out to measure the error
MIN_NEW_MATCHES = 20            # stored matches added since the last fit that trigger a refit
CHECK_EVERY_S = 60.0            # how often the page asks whether a refit is due
MIN_FIT_GAMES = 20
MIN_FIT_ROWS = 1000
EST_STEP = 50                   # the estimate is shown rounded to 50 gold
UNSPENT_STEP = 100              # and "sin gastar" to 100
AUTOFIT = True                  # tests switch the background refit off

# (key, label, upper minute bound exclusive)
PHASES = (("early", "min 2-10", 10.0), ("mid", "min 10-20", 20.0), ("late", "min 20-30", 30.0),
          ("end", "min 30+", float("inf")))
# Conservative starting point (fit on the author's own games, CS combined) used until the first fit.
DEFAULT_COEF = [-101.0, 135.9, 26.5, 380.0, 102.0, 185.0, 53.0]
DEFAULT_ERRORS = {"early": 183.0, "mid": 265.0, "late": 462.0, "end": 734.0}
# Same, with the level in place of CS (for the enemy jungler, whose CS the client under-reports).
DEFAULT_COEF_LEVEL = [-259.0, 316.8, 31.2, 403.1, 68.0, 253.6, -93.8]
DEFAULT_ERRORS_LEVEL = {"early": 201.0, "mid": 383.0, "late": 687.0, "end": 1029.0}
LIVE_CS_STEP = 10               # the Live Client reports creepScore rounded down to tens
LIVE_CS_OFFSET = 4.5            # middle of the 0-9 CS hidden by that rounding


def phase_of(minute: float) -> str:
    for key, _label, upper in PHASES:
        if minute < upper:
            return key
    return PHASES[-1][0]


def phase_label(key: str) -> str:
    return next((label for k, label, _ in PHASES if k == key), "")


def features(minute, cs, kills, assists, support) -> tuple:
    s = 1.0 if support else 0.0
    return (1.0, float(minute), float(cs), float(kills), float(assists), s, s * float(minute))


def features_level(minute, level, kills, assists, support) -> tuple:
    s = 1.0 if support else 0.0
    return (1.0, float(minute), float(level), float(kills), float(assists), s, s * float(minute))


def round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


# ---------------------------------------------------------------------- OLS
class NormalEquations:
    """X'X and X'y accumulated row by row (so 76k frames never sit in memory as a matrix)."""

    def __init__(self, n=N_FEATURES):
        self.n = n
        self.xtx = [[0.0] * n for _ in range(n)]
        self.xty = [0.0] * n
        self.rows = 0

    def add(self, x, y) -> None:
        n = self.n
        for i in range(n):
            xi = x[i]
            if xi == 0.0:
                continue
            row = self.xtx[i]
            for j in range(i, n):
                row[j] += xi * x[j]
            self.xty[i] += xi * y
        self.rows += 1

    def merge(self, other: "NormalEquations") -> "NormalEquations":
        out = NormalEquations(self.n)
        for i in range(self.n):
            out.xty[i] = self.xty[i] + other.xty[i]
            for j in range(i, self.n):
                out.xtx[i][j] = self.xtx[i][j] + other.xtx[i][j]
        out.rows = self.rows + other.rows
        return out

    def solve(self) -> list[float]:
        n = self.n
        a = [[self.xtx[min(i, j)][max(i, j)] for j in range(n)] + [self.xty[i]] for i in range(n)]
        return solve_linear(a)


def solve_linear(aug: list[list[float]]) -> list[float]:
    """Gaussian elimination with partial pivoting on an augmented matrix; ValueError if singular."""
    n = len(aug)
    a = [row[:] for row in aug]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-9:
            raise ValueError("matriz singular: faltan datos distintos para ajustar el modelo")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (a[i][n] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return x


def is_holdout(game_id) -> bool:
    """Stable by-game split: every frame of a game lands on the same side."""
    return zlib.crc32(str(game_id).encode("utf-8")) % HOLDOUT_EVERY == 0


def fit(games) -> dict:
    """Model dict from `games`: iterable of (game_id, rows) with rows
    (minute, cs, kills, assists, support, total_gold[, level]).

    Raises ValueError with too little data. The holdout games measure the error of a model fitted
    only on the others; the saved coefficients are then fitted on all games. When the rows carry the
    level, the level-based model (see the module docstring) is fitted the same way.
    """
    cs_fit, lvl_fit = _Fit(), _Fit()
    n_games = n_held = 0
    for game_id, rows in games:
        held_out = is_holdout(game_id)
        n_games += 1
        n_held += held_out
        for row in rows:
            minute, cs, kills, assists, support, gold = row[:6]
            if minute < MIN_FIT_MINUTE:
                continue
            cs_fit.add(features(minute, cs, kills, assists, support), gold, minute, held_out)
            if len(row) > 6 and isinstance(row[6], (int, float)):
                lvl_fit.add(features_level(minute, row[6], kills, assists, support), gold, minute, held_out)
    total = cs_fit.train.merge(cs_fit.test)
    if n_games < MIN_FIT_GAMES or total.rows < MIN_FIT_ROWS:
        raise ValueError("muy pocas partidas guardadas con línea de tiempo para calibrar")
    coef, mae, all_abs = cs_fit.result(DEFAULT_ERRORS)
    out = {"version": 2, "coef": coef, "errors": mae, "games": n_games, "held_out_games": n_held,
           "rows": total.rows, "mae": (sum(all_abs) / len(all_abs)) if all_abs else None,
           "median_abs": sorted(all_abs)[len(all_abs) // 2] if all_abs else None,
           "feature_names": FEATURE_NAMES}
    if lvl_fit.train.merge(lvl_fit.test).rows >= MIN_FIT_ROWS:
        try:
            out["coef_level"], out["errors_level"], _ = lvl_fit.result(DEFAULT_ERRORS_LEVEL)
        except ValueError:
            pass
    return out


class _Fit:
    """Train/holdout normal equations for one model, plus the held-out rows to measure its error."""

    def __init__(self):
        self.train, self.test = NormalEquations(), NormalEquations()
        self.held: list[tuple] = []

    def add(self, x, gold, minute, held_out) -> None:
        if held_out:
            self.test.add(x, gold)
            self.held.append((x, gold, minute))
        else:
            self.train.add(x, gold)

    def result(self, default_errors):
        coef = self.train.merge(self.test).solve()
        errors = {k: [] for k, _, _ in PHASES}
        if self.held and self.train.rows >= MIN_FIT_ROWS:
            holdout_coef = self.train.solve()
            for x, gold, minute in self.held:
                errors[phase_of(minute)].append(abs(gold - sum(c * v for c, v in zip(holdout_coef, x))))
        mae = {k: (sum(v) / len(v) if v else default_errors[k]) for k, v in errors.items()}
        return coef, mae, [e for v in errors.values() for e in v]


class GoldModel:
    def __init__(self, coef=None, errors=None, games=0, source="default", stored_matches=None, fitted_at=None,
                 coef_level=None, errors_level=None):
        self.coef = list(coef or DEFAULT_COEF)
        self.errors = dict(errors or DEFAULT_ERRORS)
        self.coef_level = list(coef_level or DEFAULT_COEF_LEVEL)
        self.errors_level = dict(errors_level or DEFAULT_ERRORS_LEVEL)
        self.games = games
        self.source = source
        self.stored_matches = stored_matches
        self.fitted_at = fitted_at

    @classmethod
    def from_dict(cls, data: dict) -> "GoldModel":
        coef = data.get("coef")
        errors = data.get("errors")
        if (not isinstance(coef, list) or len(coef) != N_FEATURES
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in coef)
                or not isinstance(errors, dict)):
            raise ValueError("gold_model.json no tiene el formato esperado")
        coef_level, errors_level = data.get("coef_level"), data.get("errors_level")
        if (not isinstance(coef_level, list) or len(coef_level) != N_FEATURES
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in coef_level)
                or not isinstance(errors_level, dict)):
            coef_level, errors_level = None, None          # older file: defaults until the next fit
        else:
            errors_level = {k: float(errors_level.get(k, DEFAULT_ERRORS_LEVEL[k])) for k in DEFAULT_ERRORS_LEVEL}
        return cls(coef, {k: float(errors.get(k, DEFAULT_ERRORS[k])) for k in DEFAULT_ERRORS},
                   int(data.get("games") or 0), "fit", data.get("stored_matches"), data.get("fitted_at"),
                   coef_level, errors_level)

    def raw(self, minute, cs, kills, assists, support) -> float:
        return sum(c * v for c, v in zip(self.coef, features(minute, cs, kills, assists, support)))

    def estimate(self, minute, cs, kills, assists, support) -> int:
        """Estimated total gold earned so far (starting gold included), rounded to 50."""
        return round_to(max(float(START_GOLD), self.raw(minute, cs, kills, assists, support)), EST_STEP)

    def estimate_by_level(self, minute, level, kills, assists, support) -> int:
        """Like `estimate` but from the champion level, for players whose CS the client under-reports."""
        raw = sum(c * v for c, v in zip(self.coef_level, features_level(minute, level, kills, assists, support)))
        return round_to(max(float(START_GOLD), raw), EST_STEP)

    def error_at(self, minute, by_level=False) -> int:
        return int(round((self.errors_level if by_level else self.errors)[phase_of(minute)]))

    def legend(self, minute) -> str:
        err = self.error_at(minute)
        if self.source == "fit" and self.games:
            base = f"calibrado con {self.games} partidas guardadas"
        else:
            base = "con valores de referencia (aún no hay partidas guardadas suficientes para calibrarlo)"
        return (f"Estimado con tu CS, kills, asistencias y el minuto, {base} "
                f"(error típico ±{err} en esta fase). Riot solo da el oro real de quien está jugando.")


def unspent_estimate(estimate: int, item_value) -> int | None:
    """Rough gold not yet spent: estimate minus the value of the items, never below 0, rounded to 100."""
    if item_value is None:
        return None
    return round_to(max(0, estimate - item_value), UNSPENT_STEP)


# ---------------------------------------------------------------- stored games
def rows_from_timeline(match: dict, timeline: dict) -> list[tuple]:
    """(minute, cs, kills, assists, is_support, total_gold, level) for every player and frame of a game."""
    info = (match or {}).get("info") or {}
    support = {p.get("participantId"): p.get("teamPosition") == "UTILITY"
               for p in info.get("participants") or []}
    frames = [f for f in ((timeline or {}).get("info") or {}).get("frames") or []
              if isinstance(f.get("timestamp"), (int, float))]
    frames.sort(key=lambda f: f["timestamp"])
    kills: dict[int, int] = {}
    assists: dict[int, int] = {}
    rows = []
    for frame in frames:
        for ev in frame.get("events") or []:
            if ev.get("type") != "CHAMPION_KILL":
                continue
            killer = ev.get("killerId")
            if isinstance(killer, int) and 1 <= killer <= 10:
                kills[killer] = kills.get(killer, 0) + 1
            for a in ev.get("assistingParticipantIds") or []:
                if isinstance(a, int) and 1 <= a <= 10:
                    assists[a] = assists.get(a, 0) + 1
        minute = frame["timestamp"] / 60000.0
        if minute < MIN_FIT_MINUTE:
            continue
        for key, pf in (frame.get("participantFrames") or {}).items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
            gold = pf.get("totalGold") if isinstance(pf, dict) else None
            if not isinstance(gold, (int, float)):
                continue
            cs = (pf.get("minionsKilled") or 0) + (pf.get("jungleMinionsKilled") or 0)
            level = pf.get("level")
            rows.append((minute, cs, kills.get(pid, 0), assists.get(pid, 0),
                         bool(support.get(pid)), float(gold),
                         float(level) if isinstance(level, (int, float)) else None))
    return rows


def games_from_db(db_path):
    """Yield (match_id, rows) for every stored solo/flex game that has a timeline."""
    from .store import Store

    store = Store(db_path)
    try:
        for match_id, match, timeline in store.iter_matches():
            if not timeline or ((match.get("info") or {}).get("queueId") not in QUEUES):
                continue
            rows = rows_from_timeline(match, timeline)
            if rows:
                yield match_id, rows
    finally:
        store.close()


def fit_from_db(db_path) -> dict:
    return fit(games_from_db(db_path))


# ----------------------------------------------------------------- persistence
class GoldModelStore:
    """Loads data/gold_model.json (or the defaults) and refits it in the background when due."""

    def __init__(self, path, fit_func=None, count_func=None, clock=time.monotonic):
        self.path = Path(path)
        self._fit_func = fit_func            # () -> model dict (see fit)
        self._count_func = count_func        # () -> stored matches
        self._clock = clock
        self._lock = threading.Lock()
        self._model: GoldModel | None = None
        self._loaded_stamp = None
        self._thread: threading.Thread | None = None
        self._last_check = -CHECK_EVERY_S
        self._attempt_count: int | None = None
        self.last_error: str | None = None

    # ---- reading
    def model(self) -> GoldModel:
        """The current model; never fits, never raises."""
        with self._lock:
            try:
                stamp = self.path.stat().st_mtime_ns
            except OSError:
                stamp = None
            if self._model is None or stamp != self._loaded_stamp:
                self._loaded_stamp = stamp
                self._model = self._read() if stamp is not None else GoldModel()
            return self._model

    def _read(self) -> GoldModel:
        try:
            return GoldModel.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return GoldModel()

    # ---- refitting
    def due(self, stored: int) -> bool:
        if stored <= 0:
            return False
        try:
            fitted_at = json.loads(self.path.read_text(encoding="utf-8")).get("stored_matches")
        except (OSError, ValueError, AttributeError):
            fitted_at = None
        if not isinstance(fitted_at, int):
            fitted_at = None
        reference = fitted_at
        if fitted_at is None and self._attempt_count is not None:
            reference = self._attempt_count          # a failed try is not repeated until data grows
        return reference is None or abs(stored - reference) >= MIN_NEW_MATCHES

    @property
    def fitting(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def maybe_refit(self) -> bool:
        """Start a background fit if one is due (checked at most once a minute). True if started."""
        if not AUTOFIT or self._fit_func is None or self._count_func is None:
            return False
        now = self._clock()
        with self._lock:
            if now - self._last_check < CHECK_EVERY_S or self.fitting:
                return False
            self._last_check = now
        try:
            stored = int(self._count_func())
        except Exception:
            return False
        if not self.due(stored):
            return False
        with self._lock:
            if self.fitting:
                return False
            self._attempt_count = stored
            self._thread = threading.Thread(target=self._run_fit, args=(stored,), daemon=True,
                                            name="lolan-goldfit")
            self._thread.start()
        return True

    def join(self, timeout=None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run_fit(self, stored: int) -> None:
        try:
            data = self._fit_func()
            data["stored_matches"] = stored
            data["fitted_at"] = int(time.time())
            self.save(data)
            self.last_error = None
        except Exception as exc:      # too few games, a corrupt row...: keep what we have
            self.last_error = str(exc)

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".gold_model.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Use the saved model right away: two saves can land on the same mtime, and then the
        # reload-on-change check in model() would keep serving the previous one.
        with self._lock:
            try:
                self._model = GoldModel.from_dict(data)
            except ValueError:
                self._model = None
            try:
                self._loaded_stamp = self.path.stat().st_mtime_ns
            except OSError:
                self._loaded_stamp = None
