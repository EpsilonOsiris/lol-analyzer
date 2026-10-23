"""Estimated win probability of a game from the lane matchups, the bot duo and the ranks.

    logit(P) = w0 + w_lane * sum(logit(wr_lane)) + w_syn * (logit(syn_ally) - logit(syn_enemy))
                  + w_rank * (average rank of the allies - of the enemies, in tiers)

The five lane win rates are NOT independent (the same patch, the same drafts), so their sum is
damped with a conservative weight (0.5), every rate is shrunk toward 50% with PRIOR_GAMES
imaginary games, and the answer is clamped to 25 to 75 %. It is a rough estimate, not a forecast:
the page says so until it has been checked against real results.

Calibration over time: every shown estimate is stored (table `predictions`); when the game is later
synced, `resolve_outcomes` records whether the scouted player's team won. `calibration` gives the
hit rate and the Brier score once there are CALIBRATION_MIN results. From FIT_MIN results
`maybe_refit` fits the weights by L2-regularised logistic regression (pure Python; the penalty
pulls toward the default weights, the intercept is free), holding out the most recent games, and
saves them in data/winprob_model.json only when they beat the defaults on that hold-out.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

MODEL_VERSION = "v1"
DEFAULT_WEIGHTS = {"w0": 0.0, "w_lane": 0.5, "w_syn": 0.5, "w_rank": 0.1}
WEIGHT_KEYS = ("w0", "w_lane", "w_syn", "w_rank")
CLAMP = (0.25, 0.75)
PRIOR_GAMES = 200                # a rate is (wins + 100) / (games + 200)
MIN_GAMES_USED = 30              # pairs with fewer games do not count
MIN_LANES = 3                    # at least this many lanes with data to show an estimate
RANK_UNIT = 400.0                # rank points per tier
RANK_LIMIT = 3.0                 # tiers of difference that count at most
CALIBRATION_MIN = 20
FIT_MIN = 100
REFIT_EVERY = 20                 # results between two fitting attempts
HOLDOUT = 0.25
L2 = 10.0
MIN_GAIN = 0.002                 # the fit must beat the defaults by this much log-loss per game on the hold-out
MIN_Z = 1.64                     # ... and by at least this many standard errors (one-sided 95%: noise rarely switches it)
PENDING_KEEP_S = 30 * 24 * 3600  # predictions with no stored game after this long are dropped
# Before 15 min a game can only end by a remake or an early surrender with someone AFK; neither says
# anything about the draft, so its prediction is dropped instead of counted.
SHORT_GAME_S = 15 * 60

TIER_INDEX = {"IRON": 0, "BRONZE": 1, "SILVER": 2, "GOLD": 3, "PLATINUM": 4, "EMERALD": 5, "DIAMOND": 6,
              "MASTER": 7, "GRANDMASTER": 7, "CHALLENGER": 7}
DIVISION_INDEX = {"IV": 0, "III": 1, "II": 2, "I": 3}


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def clamp(p: float, lo: float = CLAMP[0], hi: float = CLAMP[1]) -> float:
    return min(max(p, lo), hi)


def shrunk(wins: int, games: int, prior: int = PRIOR_GAMES) -> float:
    """Win rate pulled toward 50% by `prior` imaginary games."""
    return (wins + prior / 2) / (games + prior)


# ------------------------------------------------------------------ rank
def rank_points(rank) -> float | None:
    """Points of a scouted player's rank ({"solo": .., "flex": ..}): solo queue, else flex."""
    if not isinstance(rank, dict):
        return None
    entry = rank.get("solo") or rank.get("flex")
    if not isinstance(entry, dict):
        return None
    tier = str(entry.get("tier") or "").upper()
    if tier not in TIER_INDEX:
        return None
    lp = entry.get("lp") if isinstance(entry.get("lp"), (int, float)) else 0
    if TIER_INDEX[tier] >= 7:
        return 7 * RANK_UNIT + min(max(lp, 0), 1000) * 0.4          # apex: the LP keeps climbing
    return TIER_INDEX[tier] * RANK_UNIT + DIVISION_INDEX.get(str(entry.get("division") or "").upper(), 0) * 100 \
        + min(max(lp, 0), 100)


def team_rank(players) -> float | None:
    pts = [rank_points(p.get("rank")) for p in players if p.get("rank")]
    pts = [x for x in pts if x is not None]
    return sum(pts) / len(pts) if pts else None


# -------------------------------------------------------------- features
def features(lanes, syn_ally, syn_enemy, ally_rank, enemy_rank) -> dict:
    """The numbers the model uses. `lanes`: [{"games", "wins"} | None] ally's view; syn_*: same or None."""
    lane_sum, used = 0.0, 0
    for row in lanes:
        if row and row["games"] >= MIN_GAMES_USED:
            lane_sum += logit(shrunk(row["wins"], row["games"]))
            used += 1
    syn_used = bool(syn_ally and syn_enemy and syn_ally["games"] >= MIN_GAMES_USED
                    and syn_enemy["games"] >= MIN_GAMES_USED)
    syn = (logit(shrunk(syn_ally["wins"], syn_ally["games"])) - logit(shrunk(syn_enemy["wins"], syn_enemy["games"]))
           if syn_used else 0.0)
    rank_used = ally_rank is not None and enemy_rank is not None
    rank = max(-RANK_LIMIT, min(RANK_LIMIT, (ally_rank - enemy_rank) / RANK_UNIT)) if rank_used else 0.0
    return {"lane": round(lane_sum, 5), "syn": round(syn, 5), "rank": round(rank, 5),
            "lanes_used": used, "syn_used": syn_used, "rank_used": rank_used}


def raw_probability(f: dict, weights: dict) -> float:
    z = (weights["w0"] + weights["w_lane"] * f["lane"] + weights["w_syn"] * f["syn"] + weights["w_rank"] * f["rank"])
    return sigmoid(z)


def predict(f: dict, weights: dict | None = None) -> float | None:
    """Clamped win probability of the scouted team; None with too few lanes of data."""
    if f.get("lanes_used", 0) < MIN_LANES:
        return None
    return clamp(raw_probability(f, weights or DEFAULT_WEIGHTS))


# ---------------------------------------------------------------- metrics
def log_loss(pairs) -> float:
    pairs = list(pairs)
    if not pairs:
        return float("inf")
    return -sum(math.log(min(max(p if y else 1 - p, 1e-9), 1)) for p, y in pairs) / len(pairs)


def calibration(rows) -> dict | None:
    """Hit rate and Brier score of the resolved predictions; None below CALIBRATION_MIN."""
    rows = [r for r in rows if r.get("outcome") in (0, 1)]
    if len(rows) < CALIBRATION_MIN:
        return None
    hits = sum(1 for r in rows if (r["p"] >= 0.5) == bool(r["outcome"]))
    brier = sum((r["p"] - r["outcome"]) ** 2 for r in rows) / len(rows)
    return {"n": len(rows), "accuracy": hits / len(rows), "brier": brier,
            "accuracy_text": f"{round(100 * hits / len(rows))}%", "brier_text": f"{brier:.2f}".replace(".", ",")}


# --------------------------------------------------------------- the model file
class ModelFile:
    """data/winprob_model.json: the fitted weights (when they beat the defaults) and the fitting log."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def weights(self) -> tuple[dict, str]:
        """(weights, model name) in use now."""
        data = self.read()
        w = data.get("weights")
        if data.get("active") and isinstance(w, dict) and all(isinstance(w.get(k), (int, float)) for k in WEIGHT_KEYS):
            return {k: float(w[k]) for k in WEIGHT_KEYS}, f"{MODEL_VERSION}-ajustado-{int(data.get('n') or 0)}"
        return dict(DEFAULT_WEIGHTS), f"{MODEL_VERSION}-por-defecto"

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# -------------------------------------------------------------- the fitting
def _solve(a, b):
    """Solve a x = b (small dense system) by Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ZeroDivisionError("singular")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _vector(f: dict) -> list[float]:
    return [1.0, float(f.get("lane", 0.0)), float(f.get("syn", 0.0)), float(f.get("rank", 0.0))]


def fit_weights(rows, l2: float = L2, start: dict | None = None, iterations: int = 30) -> dict:
    """L2-regularised logistic regression by Newton's method; the penalty pulls toward `start`
    (the defaults) except for the intercept."""
    prior = [(start or DEFAULT_WEIGHTS)[k] for k in WEIGHT_KEYS]
    data = [(_vector(r["features"]), float(r["outcome"])) for r in rows]
    w = prior[:]
    for _ in range(iterations):
        grad = [0.0] * 4
        hess = [[0.0] * 4 for _ in range(4)]
        for x, y in data:
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            for i in range(4):
                grad[i] += (p - y) * x[i]
                for j in range(4):
                    hess[i][j] += p * (1 - p) * x[i] * x[j]
        for i in range(1, 4):
            grad[i] += l2 * (w[i] - prior[i])
            hess[i][i] += l2
        hess[0][0] += 1e-6
        try:
            step = _solve(hess, grad)
        except ZeroDivisionError:
            break
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return dict(zip(WEIGHT_KEYS, w))


def _loss(rows, weights) -> float:
    return log_loss((clamp(raw_probability(r["features"], weights)), r["outcome"]) for r in rows)


def _gain(rows, candidate, baseline) -> tuple[float, float]:
    """(mean log-loss improvement per game of `candidate` over `baseline`, in standard errors)."""
    diffs = [log_loss([(clamp(raw_probability(r["features"], baseline)), r["outcome"])])
             - log_loss([(clamp(raw_probability(r["features"], candidate)), r["outcome"])]) for r in rows]
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / max(1, len(diffs) - 1)
    se = math.sqrt(var / len(diffs))
    return mean, (mean / se if se > 0 else (math.inf if mean > 0 else 0.0))


def maybe_refit(rows, model: ModelFile, now: float | None = None) -> dict | None:
    """Try new weights when there are enough results; returns what was written (None: nothing to do).

    The most recent HOLDOUT of the results is held out: weights fitted on the older ones must beat
    the defaults there; only then are they fitted again on everything and made active."""
    rows = [r for r in rows if r.get("outcome") in (0, 1) and (r.get("features") or {}).get("lanes_used", 0) >= MIN_LANES]
    rows.sort(key=lambda r: (r["created_at"], r["match_id"]))
    n = len(rows)
    if n < FIT_MIN:
        return None
    previous = model.read()
    if n < int(previous.get("tried_n") or 0) + REFIT_EVERY:
        return None
    cut = int(n * (1 - HOLDOUT))
    train, hold = rows[:cut], rows[cut:]
    candidate = fit_weights(train)
    ll_fit, ll_default = _loss(hold, candidate), _loss(hold, DEFAULT_WEIGHTS)
    gain, z = _gain(hold, candidate, DEFAULT_WEIGHTS)
    better = gain > MIN_GAIN and z >= MIN_Z
    out = {"version": MODEL_VERSION, "trained_at": int(time.time() if now is None else now), "n": n, "tried_n": n,
           "active": bool(better), "weights": fit_weights(rows) if better else dict(DEFAULT_WEIGHTS),
           "holdout": {"n": len(hold), "log_loss_fit": round(ll_fit, 5), "log_loss_default": round(ll_default, 5),
                           "gain_z": round(z, 2)}}
    model.write(out)
    return out


# ------------------------------------------------------- predictions and outcomes
def match_id_for(platform: str, game_id) -> str:
    return f"{str(platform).upper()}_{game_id}"


def record_prediction(store, platform: str, game_id, scouted: str, f: dict, p: float, model: str,
                      now: float | None = None) -> str:
    match_id = match_id_for(platform, game_id)
    store.put_prediction(match_id, game_id, scouted, f, p, model, now)
    return match_id


def resolve_outcomes(store, now: float | None = None) -> int:
    """Close the predictions whose game is now stored: did the scouted player's team win?"""
    from .scout import find_participant

    now = time.time() if now is None else now
    store.prune_predictions(PENDING_KEEP_S, now)
    done = 0
    for pred in store.pending_predictions():
        got = store.get_match(pred["match_id"])
        if not got:
            continue
        info = (got[0] or {}).get("info") or {}
        duration = info.get("gameDuration") or 0
        if duration and duration < SHORT_GAME_S:
            store.drop_prediction(pred["match_id"])      # remake / AFK surrender: says nothing about the draft
            continue
        part, _info = find_participant(got[0], riot_id=pred["scouted"])
        if part is None or not isinstance(part.get("win"), bool):
            continue
        store.resolve_prediction(pred["match_id"], part["win"], now)
        done += 1
    return done
