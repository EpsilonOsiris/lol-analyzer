"""Duelos por línea: the lane matchups of a scouted game (OP.GG) and an estimated win probability.

Built in the background after the scouting report (never blocks it) and pushed through the same
live stream. Policy: data only, no advice about picks or dodging. Every OP.GG failure ends in
"Sin datos de OP.GG"; the rest of the report stays as it was.

Steps (see `DuelosService.build`):
  1. role assignment (Spectator has no positions): Smite means jungle, OP.GG's role rates per
     champion, and the most played role of registered players from our own database; the best of
     the 5! assignments of each team, with a confidence per lane;
  2. one OP.GG matchup per lane (ally champion against enemy champion), the ADC + support duo of
     both teams;
  3. the win probability (winprob.py), stored for later calibration.
"""
from __future__ import annotations

import itertools
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import winprob
from .opgg import MAX_PARALLEL, McpClient, OpggData, OpggError, patch_of
from .opgg import _norm as norm_name
from .scout import split_riot_id

ROLE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
OPGG_POSITION = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}
ROLE_LABELS = {"TOP": "Arriba", "JUNGLE": "Jungla", "MIDDLE": "Centro", "BOTTOM": "Tirador", "UTILITY": "Soporte"}
OPGG_ROLE = {v: k for k, v in OPGG_POSITION.items()}

SMITE_ID = 11
SMITE_MATCH, SMITE_MISS = 1.0, 0.03          # likelihood factor: jungler with / without Smite
ROLE_FLOOR = 0.02                            # OP.GG role rate for a lane the champion is not listed in
UNLISTED_RATE = 0.2                          # champion missing from the OP.GG lane list: any lane
PERSONAL_MIN = 5                             # games before a registered player's role history counts
PERSONAL_FULL = 15                           # ... and with all its weight
LOW_CONFIDENCE = 0.75                        # lane marginal below this is marked "posición dudosa"

FAVORABLE_WR = 0.52                          # >= : favorable
UNFAVORABLE_WR = 0.48                        # <= : unfavorable
FEW_GAMES = 200                              # fewer games: the % is hidden ("pocos datos")
PERSONAL_GAMES = 3                           # own record against that champion shown from here
RECORD_MAX_LENGTH_S = 120                    # estimates made later in the game are shown but not scored

SCOPE_TEXT = "todos los rangos"
SOURCE_NOTE = ("Datos de OP.GG de todos los rangos (OP.GG no filtra por rango los duelos entre dos campeones); "
               "el % es la tasa de victorias del campeón de tu equipo en esa línea.")
NO_DATA_MESSAGE = "Sin datos de OP.GG"
LABEL_UNCALIBRATED = "estimación con datos de OP.GG; aún sin calibrar"


# ------------------------------------------------------------------- roles
def role_scores(player: dict, rates: dict | None, counts: Counter | None, smite_known: bool = True) -> dict:
    """Log-likelihood of each role for one player (`smite_known` False: spells unknown, as for enemies in champion select)."""
    has_smite = SMITE_ID in (player.get("spell_ids") or [])
    n = sum(counts.values()) if counts else 0
    weight = 0.0 if n < PERSONAL_MIN else min(1.0, n / PERSONAL_FULL)
    scores = {}
    for role in ROLE_ORDER:
        if rates:
            p = max(rates.get(OPGG_POSITION[role], 0.0), ROLE_FLOOR)
        else:
            p = UNLISTED_RATE
        s = math.log(p)
        if smite_known:
            s += math.log(SMITE_MATCH if (role == "JUNGLE") == has_smite else SMITE_MISS)
        if weight:
            s += weight * math.log(((counts.get(role, 0) + 0.5) / (n + 2.5)) / UNLISTED_RATE)
        scores[role] = s
    return scores


def assign_roles(scores: list[dict]) -> tuple[list[str], list[float]]:
    """Best assignment of the five roles to five players, and for each the probability that the
    chosen role is right (marginal over all 5! assignments)."""
    perms = list(itertools.permutations(ROLE_ORDER))
    totals = [sum(scores[i][role] for i, role in enumerate(perm)) for perm in perms]
    best = max(range(len(perms)), key=lambda k: totals[k])
    top = totals[best]
    weights = [math.exp(t - top) for t in totals]
    z = sum(weights)
    chosen = perms[best]
    conf = []
    for i, role in enumerate(chosen):
        conf.append(sum(w for perm, w in zip(perms, weights) if perm[i] == role) / z)
    return list(chosen), conf


def infer_roles(champion_ids, rates_of) -> list[dict | None]:
    """Roles of the champions already revealed by a team in champion select (0 = not revealed yet).

    Same likelihood as the scout (OP.GG role rates, every lane once) but without Smite (spells are not
    visible) and with the unrevealed slots as neutral players, so a lone champion is placed by its own
    rates. `rates_of(champion_id)` -> {opgg position: share} or None. Returns, per input slot,
    {"role", "conf", "low"} (conf: probability that the role is right; low: below LOW_CONFIDENCE) or None.
    """
    ids = list(champion_ids)[:len(ROLE_ORDER)]
    neutral = {role: 0.0 for role in ROLE_ORDER}
    scores = [role_scores({}, rates_of(cid), None, smite_known=False) if cid else dict(neutral) for cid in ids]
    scores += [dict(neutral) for _ in range(len(ROLE_ORDER) - len(scores))]
    roles, conf = assign_roles(scores)
    return [{"role": roles[i], "conf": conf[i], "low": conf[i] < LOW_CONFIDENCE} if cid else None
            for i, cid in enumerate(ids)]


# --------------------------------------------------------- own history (DB)
class PersonalIndex:
    """What our stored matches say about registered players: the roles they play and how they did
    against each champion of the enemy in their own lane. One pass over the stored matches, rebuilt
    only when their number or the set of players changes."""

    def __init__(self, store_factory):
        self._store_factory = store_factory
        self._lock = threading.Lock()
        self._stamp = None
        self._roles: dict = {}
        self._versus: dict = {}

    @staticmethod
    def key(riot_id) -> tuple[str, str]:
        name, tag = split_riot_id(riot_id)
        return (name.casefold(), tag.casefold())

    def load(self, riot_ids) -> None:
        wanted = {self.key(r) for r in riot_ids if r and "#" in str(r)}
        store = self._store_factory()
        try:
            stamp = (store.count_matches(), frozenset(wanted))
            with self._lock:
                if stamp == self._stamp:
                    return
            roles: dict = {}
            versus: dict = {}
            if wanted:
                for _mid, match in store.iter_match_json():
                    parts = [p for p in ((match or {}).get("info") or {}).get("participants") or [] if isinstance(p, dict)]
                    for me in parts:
                        k = (str(me.get("riotIdGameName") or "").casefold(), str(me.get("riotIdTagline") or "").casefold())
                        role = me.get("teamPosition")
                        if k not in wanted or role not in ROLE_ORDER:
                            continue
                        roles.setdefault(k, Counter())[role] += 1
                        opp = next((p for p in parts if p.get("teamId") != me.get("teamId")
                                    and p.get("teamPosition") == role and isinstance(p.get("championId"), int)), None)
                        if opp is not None:
                            cell = versus.setdefault((k, role, opp["championId"]), [0, 0])
                            cell[0] += 1
                            cell[1] += 1 if me.get("win") else 0
            with self._lock:
                self._stamp, self._roles, self._versus = stamp, roles, versus
        finally:
            store.close()

    def roles(self, riot_id) -> Counter | None:
        with self._lock:
            return self._roles.get(self.key(riot_id))

    def record(self, riot_id, role, opponent_champion_id) -> dict | None:
        """{"games", "wins"} of the player in `role` against that champion in their lane, if any."""
        with self._lock:
            cell = self._versus.get((self.key(riot_id), role, opponent_champion_id))
        return {"games": cell[0], "wins": cell[1]} if cell else None


# ------------------------------------------------------------------ helpers
def tone_of(games: int, wins: int) -> str:
    """"fav" / "even" / "unfav" by the OP.GG win rate, "few" with fewer than FEW_GAMES games."""
    if games < FEW_GAMES:
        return "few"
    wr = wins / games
    if wr >= FAVORABLE_WR:
        return "fav"
    if wr <= UNFAVORABLE_WR:
        return "unfav"
    return "even"


def pct_text(value: float) -> str:
    return f"{round(100 * value)}%"


def eligible(report) -> bool:
    """Only Summoner's Rift games with five players a side have lanes."""
    try:
        game = report["game"]
        return (game.get("map_id") == 11 and str(game.get("mode") or "").upper() == "CLASSIC"
                and len(report["teams"]["ally"]) == 5 and len(report["teams"]["enemy"]) == 5)
    except (KeyError, TypeError, AttributeError):
        return False


def record_text(rec) -> str:
    return f"{rec['wins']}V-{rec['games'] - rec['wins']}D"


# ------------------------------------------------------------------ service
class DuelosService:
    def __init__(self, store_factory, model_path, client: McpClient | None = None, patch_func=None,
                 clock=time.time, pool=MAX_PARALLEL):
        self._store_factory = store_factory
        self.model = winprob.ModelFile(model_path)
        self.client = client or McpClient()
        self._patch_func = patch_func or (lambda: None)
        self._clock = clock
        self._pool = pool
        self.personal = PersonalIndex(store_factory)

    # ---- pieces
    def _data(self) -> OpggData:
        return OpggData(self.client, self._store_factory, patch=self._patch_func(), clock=self._clock)

    def _teams(self, report, data: OpggData, registered) -> dict:
        """{"ally": {role: player}, "enemy": {role: player}} with `_role_conf` on every player."""
        meta = data.lane_meta()
        out = {}
        for side in ("ally", "enemy"):
            players = report["teams"][side]
            scores = []
            for p in players:
                rates = meta.get(norm_name(data.name_of(p["champion_id"])))
                counts = None
                if p.get("riot_id") and p["riot_id"].casefold() in registered:
                    counts = self.personal.roles(p["riot_id"])
                scores.append(role_scores(p, rates, counts))
            roles, conf = assign_roles(scores)
            out[side] = {role: {**p, "_conf": c} for role, p, c in zip(roles, players, conf)}
        return out

    def build(self, report: dict, platform: str, viewer_riot_id: str, registered_ids=()) -> dict:
        """The "duelos" block of a report. Never raises for OP.GG problems."""
        if not eligible(report):
            return {"status": "na"}
        started_calls = self.client.calls
        try:
            return self._build(report, platform, viewer_riot_id, registered_ids, started_calls)
        except OpggError:
            return {"status": "error", "message": NO_DATA_MESSAGE}

    def _bookkeeping(self, store) -> dict | None:
        """Close the predictions whose game was synced, refit when there are enough, give the track record."""
        try:
            winprob.resolve_outcomes(store, self._clock())
            rows = store.resolved_predictions()
            winprob.maybe_refit(rows, self.model, self._clock())
            return winprob.calibration(rows)
        except Exception:          # a broken history must not take the section down
            return None

    def _build(self, report, platform, viewer_riot_id, registered_ids, started_calls) -> dict:
        data = self._data()
        registered = {r.casefold() for r in registered_ids if r}
        registered.add(str(viewer_riot_id or "").casefold())
        self.personal.load([*registered_ids, viewer_riot_id])
        data.champions()                                   # raises OpggError when OP.GG is down
        teams = self._teams(report, data, registered)

        def one(role):
            a, e = teams["ally"][role], teams["enemy"][role]
            try:
                return data.matchup(OPGG_POSITION[role], a["champion_id"], e["champion_id"])
            except OpggError:
                return "error"

        with ThreadPoolExecutor(max_workers=self._pool) as pool:
            found = dict(zip(ROLE_ORDER, pool.map(one, ROLE_ORDER)))
        failed = sum(1 for v in found.values() if v == "error")
        if failed == len(ROLE_ORDER):
            raise OpggError("no hubo ningún duelo")
        duo = {}
        for side in ("ally", "enemy"):
            adc, sup = teams[side]["BOTTOM"], teams[side]["UTILITY"]
            try:
                duo[side] = data.synergy(adc["champion_id"], sup["champion_id"])
            except OpggError:
                duo[side] = None

        lanes, lane_data = [], []
        for role in ROLE_ORDER:
            a, e = teams["ally"][role], teams["enemy"][role]
            mu = found[role] if isinstance(found[role], dict) else None
            lane_data.append(mu)
            row = {"role": role, "label": ROLE_LABELS[role],
                   "ally": {"champion_id": a["champion_id"], "is_viewer": bool(a.get("is_viewer"))},
                   "enemy": {"champion_id": e["champion_id"]},
                   "games": mu["games"] if mu else None, "wr": None, "wr_text": "", "tone": "none",
                   "low_confidence": min(a["_conf"], e["_conf"]) < LOW_CONFIDENCE, "personal": None}
            if mu:
                row["tone"] = tone_of(mu["games"], mu["wins"])
                if mu["games"] >= FEW_GAMES:
                    row["wr"] = mu["wins"] / mu["games"]
                    row["wr_text"] = pct_text(row["wr"])
            if a.get("is_viewer") and viewer_riot_id:
                rec = self.personal.record(viewer_riot_id, role, e["champion_id"])
                if rec and rec["games"] >= PERSONAL_GAMES:
                    row["personal"] = {**rec, "text": record_text(rec)}
            lanes.append(row)

        duo_out = {}
        for side in ("ally", "enemy"):
            s = duo[side]
            duo_out[side] = {
                "adc_id": teams[side]["BOTTOM"]["champion_id"], "support_id": teams[side]["UTILITY"]["champion_id"],
                "games": s["games"] if s else None,
                "wr": (s["wins"] / s["games"]) if s and s["games"] >= FEW_GAMES else None,
                "wr_text": pct_text(s["wins"] / s["games"]) if s and s["games"] >= FEW_GAMES else "",
            }

        weights, model_name = self.model.weights()
        f = winprob.features(lane_data, duo["ally"], duo["enemy"], winprob.team_rank(report["teams"]["ally"]),
                             winprob.team_rank(report["teams"]["enemy"]))
        p = winprob.predict(f, weights)
        store = self._store_factory()
        try:
            calib = self._bookkeeping(store)
            if p is not None and report["game"].get("game_id") is not None \
                    and report["game"].get("length_s", 0) <= RECORD_MAX_LENGTH_S:
                try:
                    winprob.record_prediction(store, platform, report["game"]["game_id"],
                                              report["scouted"]["riot_id"], f, p, model_name, self._clock())
                except Exception:
                    pass
        finally:
            store.close()
        prob = None
        if p is not None:
            label = LABEL_UNCALIBRATED if not calib else (
                f"estimación con datos de OP.GG · Acierta el {calib['accuracy_text']} "
                f"({calib['n']} partidas, Brier {calib['brier_text']})")
            prob = {"value": p, "text": pct_text(p), "model": model_name, "label": label,
                    "calibrated": bool(calib), "lanes_used": f["lanes_used"], "rank_used": f["rank_used"],
                    "syn_used": f["syn_used"], "syn_sides": f["syn_sides"]}
        known = sum(1 for m in lane_data if m)
        status = "ok" if known == 5 else "partial" if known else "nodata"
        return {"status": status, "message": NO_DATA_MESSAGE if status == "nodata" else "", "source": "OP.GG",
                "scope": SCOPE_TEXT, "note": SOURCE_NOTE, "patch": data.patch, "lanes": lanes, "duo": duo_out,
                "prob": prob, "calibration": calib, "calls": self.client.calls - started_calls}


class LaneOpgg:
    """OP.GG for champion select: answers at once from the cache, fetches what is missing in the background.

    `service` is a function returning the DuelosService (or None when OP.GG is off); it is called each
    time so it can be swapped. `notify` runs in the worker thread whenever something new has arrived (the
    page wakes its event stream). `enabled` starts False: start_live turns it on, so tests and imports
    never call OP.GG by accident. Every failure is soft: the pair shows "error" for RETRY_S, then may be asked again.
    """
    RETRY_S = 60.0

    def __init__(self, service, notify=None, clock=time.monotonic):
        self._service, self._notify, self._clock = service, notify or (lambda: None), clock
        self.enabled = False
        self._lock = threading.Lock()
        self._data: OpggData | None = None
        self._data_for = None
        self._busy: set = set()
        self._failed: dict = {}
        self.threads: list[threading.Thread] = []

    # ---- plumbing
    def _opgg(self) -> OpggData | None:
        svc = self._service() if self.enabled else None
        if svc is None:
            return None
        with self._lock:
            stamp = (id(svc), svc._patch_func())
            if self._data is None or self._data_for != stamp:
                self._data, self._data_for = svc._data(), stamp
            return self._data

    def _start(self, key, job) -> None:
        with self._lock:
            if key in self._busy or self._clock() < self._failed.get(key, 0.0):
                return
            self._busy.add(key)
        t = threading.Thread(target=self._run, args=(key, job), daemon=True, name="lolan-partida-opgg")
        self.threads = [x for x in self.threads if x.is_alive()] + [t]
        t.start()

    def _run(self, key, job) -> None:
        try:
            job()
        except Exception:                              # OpggError and anything unexpected: soft
            with self._lock:
                self._failed[key] = self._clock() + self.RETRY_S
        finally:
            with self._lock:
                self._busy.discard(key)
            try:
                self._notify()
            except Exception:
                pass

    def _failed_now(self, key) -> bool:
        with self._lock:
            return self._clock() < self._failed.get(key, 0.0)

    # ---- enemy roles
    def enemy_roles(self, champion_ids) -> list[dict | None] | None:
        """`infer_roles` for the revealed champions of the enemy team; None while OP.GG's champion list and lane
        meta are not cached (they are fetched in the background) or when OP.GG is off."""
        data = self._opgg()
        if data is None:
            return None
        names, meta = data.peek_names(), data.peek_lane_meta()
        if names is None or meta is None:
            if data.client.available():
                self._start(("tables",), lambda: (data.champions(), data.lane_meta()))
            return None
        return infer_roles(champion_ids, lambda cid: meta.get(norm_name(names.get(str(cid)))))

    def roles_pending(self) -> bool:
        return self.enabled and ("tables",) in self._busy

    # ---- pairs
    def lookup(self, role: str, my_id: int, opp_id: int, fetch: bool) -> dict:
        """{"status": "off" | "idle" | "pending" | "error" | "none" | "ok", **matchup_full}.

        "idle": not cached and not asked for (`fetch` False); with `fetch` the call starts in the background
        and the answer is "pending" until it lands in the cache (then `notify` runs)."""
        data = self._opgg()
        if data is None:
            return {"status": "off"}
        position = OPGG_POSITION[role]
        cached, value = data.peek_matchup(position, my_id, opp_id)
        if cached:
            return {"status": "ok", **value} if value else {"status": "none"}
        key = ("mu", position, my_id, opp_id)
        if self._failed_now(key) or not data.client.available():
            return {"status": "error"}
        with self._lock:
            if key in self._busy:
                return {"status": "pending"}
        if not fetch:
            return {"status": "idle"}

        def job():
            data.matchup_full(position, my_id, opp_id)
            if not data.peek_matchup(position, my_id, opp_id)[0]:          # unknown champion ids: nothing was stored
                raise OpggError("OP.GG no conoce a alguno de los campeones")

        self._start(key, job)
        return {"status": "pending"}


def make_service(root, dd=None, **kw) -> DuelosService | None:
    """The service of the panel; None when LOLAN_OPGG=off (no OP.GG at all)."""
    if str(os.environ.get("LOLAN_OPGG", "")).strip().lower() in ("off", "0", "no"):
        return None
    from .store import Store
    db_path = root / "data" / "lol.db"

    def patch():
        return patch_of(dd.latest()) if dd is not None else None
    return DuelosService(lambda: Store(db_path), root / "data" / "winprob_model.json", patch_func=patch, **kw)
