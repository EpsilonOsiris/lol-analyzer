"""Lane cards for champion select: how each lane matchup goes and who spikes when.

Everything here comes from local files (data/lineas) and renders at once; the OP.GG part of a card
is whatever `LaneOpgg.lookup` already has (cached) or "pending" and fills in later.

  * `LaneBook`: the read side of the lane samples. Prefers the Diamond+ sample ("diamante") and says so
    when it falls back to every source; results are remembered until the sample files change.
  * `spike_profile` / `duel_text`: short Spanish comparisons of the power spikes of both sides, from each
    champion's own aggregate in the role and, when the matchup has enough games, from the matchup's own
    level brackets.
  * `build_lanes`: the cards (my lane first and detailed, the others compact), the "Mejores contra" lists
    for allies who have not picked yet, and the state of the enemy roles.

Policy: data only. The text states what the samples say; it never tells anybody what to pick or dodge,
except the ranked list of the player's own champions against the revealed rival ("Mejores contra").
"""
from __future__ import annotations

import threading
import time

from . import duelos as dl
from .analyze import role_name
from .lanephase import LANE_ROLES
from .samples import (BRACKET_MIN_MINUTES, BRACKET_NOTABLE, GAME_LENGTH_EDGE, GAME_LENGTH_MIN, ITEM_SPIKE_MIN_WINDOWS,
                      ITEM_SPIKE_NOTABLE, LOW_SAMPLE_GAMES, SOURCE_DIAMOND, SOURCE_OWN, LineStore, signed)

SOURCE_LABELS = {SOURCE_DIAMOND: "Diamante+", SOURCE_OWN: "muestra propia (clan)"}
NO_DIAMOND_NOTE = "aún sin muestra Diamante+"
NO_DIAMOND_PAIR_NOTE = "sin partidas Diamante+ de este cruce"
OVERALL_MIN_GAMES = 15          # games a champion needs in the Diamond+ sample for its own spike profile
DUEL_MIN_GAMES = LOW_SAMPLE_GAMES   # games a matchup needs before its own level brackets are described
FAVORABLE, UNFAVORABLE = 0.52, 0.48
BEST_SHOWN = 5                  # champions of the pool listed in "Mejores contra"
LINE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
LENGTH_NAMES = {"<25": "<25 min", "25-30": "25-30 min", "30-35": "30-35 min", "35+": "más de 35 min"}
OPGG_LENGTH_NAMES = {0: "<25 min", 25: "25-30 min", 30: "30-35 min", 35: "35-40 min", 40: "más de 40 min"}
OPGG_LENGTH_EDGE = 0.02         # best and worst OP.GG length bucket must differ by this to be mentioned
STYLE_TEXT = {"even": "parejo", "aggressive": "agresivo", "passive": "pasivo", "defensive": "defensivo"}


def pct(value) -> str:
    return f"{round(100 * value)}%"


def tone_of_rate(rate, games) -> str:
    """"fav" / "even" / "unfav", or "few" with fewer than LOW_SAMPLE_GAMES games."""
    if games < LOW_SAMPLE_GAMES:
        return "few"
    return "fav" if rate >= FAVORABLE else "unfav" if rate <= UNFAVORABLE else "even"


# ------------------------------------------------------------------ the samples
class LaneBook:
    """Read side of data/lineas with the Diamond+-first policy; remembers answers until the files change."""

    def __init__(self, base, recheck_s=1.0, clock=time.monotonic):
        self.base = base
        self.store = LineStore(base)
        self.recheck_s, self._clock = recheck_s, clock      # the files are looked at most this often
        self._lock = threading.Lock()
        self._memo: dict = {}
        self._sig = None
        self._checked = -1e9

    # ---- invalidation
    def _signature(self) -> tuple:
        out = []
        try:
            for path in sorted(self.store.rows_dir.glob("*/*.jsonl")):
                st = path.stat()
                out.append((path.parent.name, path.name, st.st_mtime_ns, st.st_size))
            st = self.store.index_path.stat()
            out.append(("indice", st.st_mtime_ns, st.st_size))
        except OSError:
            pass
        return tuple(out)

    def _cached(self, key, build):
        with self._lock:
            now = self._clock()
            stale = now - self._checked >= self.recheck_s
        if stale:
            sig = self._signature()
            with self._lock:
                self._checked = now
                if sig != self._sig:
                    self._memo.clear()
                    self._sig = sig
        with self._lock:
            sig = self._sig
            if key in self._memo:
                return self._memo[key]
        value = build()
        with self._lock:
            if sig == self._sig:
                self._memo[key] = value
        return value

    # ---- what exists
    def has_diamond(self) -> bool:
        return self._cached(("has-diamond",), lambda: SOURCE_DIAMOND in self.store.sources())

    def any_data(self) -> bool:
        return self._cached(("any",), lambda: bool(self.store.sources()))

    def _all_label(self, sources) -> str:
        names = [SOURCE_LABELS.get(s, s) for s in sorted(sources, key=lambda s: s != SOURCE_DIAMOND)]
        return " + ".join(names) if names else "todas las fuentes"

    # ---- aggregates
    def _all_matchup(self, role, champ, opp) -> tuple[dict, str]:
        """(stats, label) of the matchup over every source: the precomputed file when it has the pair."""
        duelo = self.store.read_duelo(role, champ)
        entry = ((duelo or {}).get("vs") or {}).get(str(opp))
        if entry and entry.get("games"):
            return entry, self._all_label(duelo.get("sources") or [])
        return self.store.matchup(role, champ, opp), self._all_label(self.store.sources())

    def _all_overall(self, role, champ) -> tuple[dict, str]:
        duelo = self.store.read_duelo(role, champ)
        if duelo and (duelo.get("overall") or {}).get("games"):
            return duelo["overall"], self._all_label(duelo.get("sources") or [])
        return self.store.champion_overall(role, champ), self._all_label(self.store.sources())

    def matchup(self, role, champ, opp) -> dict:
        """{"kind": "diamante" | "fallback", "label", "stats", "diamond": bool}: `champ` against `opp` in `role`.

        "diamante" when the Diamond+ sample has games of the pair; else every source ("fallback"), and
        `diamond` says whether a Diamond+ sample exists at all."""
        def build():
            diamond = self.has_diamond()
            if diamond:
                stats = self.store.matchup(role, champ, opp, sources=[SOURCE_DIAMOND])
                if stats["games"]:
                    return {"kind": "diamante", "label": SOURCE_LABELS[SOURCE_DIAMOND], "stats": stats, "diamond": True}
            stats, label = self._all_matchup(role, champ, opp)
            return {"kind": "fallback", "label": label, "stats": stats, "diamond": diamond}
        return self._cached(("mu", role, champ, opp), build)

    def overall(self, role, champ) -> dict:
        """The champion's own aggregate in the role: Diamond+ with OVERALL_MIN_GAMES games, else every source."""
        def build():
            if self.has_diamond():
                stats = self.store.champion_overall(role, champ, sources=[SOURCE_DIAMOND])
                if stats["games"] >= OVERALL_MIN_GAMES:
                    return {"kind": "diamante", "label": SOURCE_LABELS[SOURCE_DIAMOND], "stats": stats}
            stats, label = self._all_overall(role, champ)
            return {"kind": "fallback", "label": label, "stats": stats}
        return self._cached(("overall", role, champ), build)

    def person_record(self, role, champ, opp, person) -> dict | None:
        """{"games", "wins"} of a clan person with `champ` against `opp` in `role` (own games only)."""
        if not person:
            return None

        def build():
            stats = self.store.matchup(role, champ, opp, sources=[SOURCE_OWN], person=person)
            return {"games": stats["games"], "wins": stats["wins"]} if stats["games"] else None
        return self._cached(("person", role, champ, opp, str(person).casefold()), build)

    def best_into(self, role, opp, candidates) -> dict:
        """{"kind", "label", "items"}: `candidates` ranked by lane win rate against `opp` (Diamond+ first)."""
        cands = tuple(dict.fromkeys(int(c) for c in candidates))

        def build():
            if self.has_diamond():
                items = self.store.best_into(role, opp, cands, sources=[SOURCE_DIAMOND])
                if any(i["games"] for i in items):
                    return {"kind": "diamante", "label": SOURCE_LABELS[SOURCE_DIAMOND], "items": items}
            items = self.store.best_into(role, opp, cands)
            return {"kind": "fallback", "label": self._all_label(self.store.sources()), "items": items}
        return self._cached(("best", role, opp, cands), build)


# ----------------------------------------------------------------- spike texts
def _join(parts) -> str:
    return " y ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " y " + parts[-1]


def strong_levels(stats) -> list[int]:
    """Own levels (at most two, ascending) where the champion gains clearly above its own average pace."""
    spikes = stats.get("level_spikes") or []
    total = sum(b["minutes"] for b in spikes)
    base = sum(b["gain_per_min"] * b["minutes"] for b in spikes) / total if total else 0
    usable = [b for b in spikes if b["minutes"] >= BRACKET_MIN_MINUTES and b["gain_per_min"] - base >= BRACKET_NOTABLE]
    return sorted(b["level"] for b in sorted(usable, key=lambda b: -b["gain_per_min"])[:2])


def first_item(stats) -> dict | None:
    """{"minute", "spike"}: median minute of the 1st legendary, and whether the lane pace jumps after it."""
    for it in stats.get("item_spikes") or []:
        if it["item"] == 1 and it["games"] >= ITEM_SPIKE_MIN_WINDOWS:
            spike = (it["windows"] >= ITEM_SPIKE_MIN_WINDOWS and it["gain_after"] is not None
                     and it["gain_before"] is not None and it["gain_after"] - it["gain_before"] >= ITEM_SPIKE_NOTABLE)
            return {"minute": round(it["median_minute"]), "spike": bool(spike)}
    return None


def best_length(stats) -> dict | None:
    """{"bucket", "text"}: the game length where the champion's team wins clearly more than its average."""
    gl = stats.get("game_length") or {}
    overall = gl.get("overall")
    ok = [b for b in gl.get("buckets") or [] if b["games"] >= GAME_LENGTH_MIN]
    if not overall or not ok:
        return None
    best = max(ok, key=lambda b: (b["win_rate"], b["games"]))
    if best["win_rate"] - overall["win_rate"] < GAME_LENGTH_EDGE:
        return None
    text = "escala (más de 35 min)" if best["bucket"] == "35+" else f"mejor en {LENGTH_NAMES[best['bucket']]}"
    return {"bucket": best["bucket"], "text": text}


def spike_profile(stats, thin=False) -> dict:
    """{"levels", "item", "length", "parts", "text"} of a champion's own aggregate (see strong_levels ...)."""
    levels, item, length = strong_levels(stats), first_item(stats), best_length(stats)
    parts = []
    if levels:
        parts.append("fuerte en nivel " + _join([str(lv) for lv in levels]))
    if item:
        parts.append(f"1er item min {item['minute']}" + (" (pico)" if item["spike"] else ""))
    if length:
        parts.append(length["text"])
    text = " · ".join(parts) if parts else ("sin datos suficientes" if not stats.get("games") else "sin picos claros")
    if thin and parts:
        text += " (pocas partidas)"
    return {"levels": levels, "item": item, "length": length, "parts": parts, "text": text}


def duel_text(stats) -> str | None:
    """"En este duelo ganas en niveles 1-2 y pierdes en 6-8", from the matchup's own level brackets."""
    if not stats or stats.get("games", 0) < DUEL_MIN_GAMES:
        return None
    usable = [b for b in stats.get("brackets") or [] if b["minutes"] >= BRACKET_MIN_MINUTES]
    wins = [b["bracket"] for b in usable if b["gain_per_min"] >= BRACKET_NOTABLE]
    loses = [b["bracket"] for b in usable if b["gain_per_min"] <= -BRACKET_NOTABLE]
    parts = []
    if wins:
        parts.append("ganas en niveles " + _join(wins))
    if loses:
        parts.append("pierdes en " + _join(loses) if wins else "pierdes en niveles " + _join(loses))
    return "En este duelo " + " y ".join(parts) if parts else None


def opgg_length_text(lengths) -> str | None:
    """Best and worst game length of OP.GG's win rate by length for the ally champion."""
    rows = [(OPGG_LENGTH_NAMES[g["from"]], g["rate"]) for g in lengths or [] if g.get("from") in OPGG_LENGTH_NAMES]
    if len(rows) < 2:
        return None
    best, worst = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
    if best[1] - worst[1] < OPGG_LENGTH_EDGE:
        return "parejo en todas las duraciones"
    return f"mejor en {best[0]} ({pct(best[1])}) · peor en {worst[0]} ({pct(worst[1])})"


# --------------------------------------------------------------- sample block
def sample_block(view) -> dict:
    """The "sample" part of a card: lane win rate (shrunk), games, gold+xp at 14 and where it comes from."""
    stats = view["stats"]
    games = stats.get("games") or 0
    block = {"kind": view["kind"], "label": view["label"], "games": games, "note": None}
    if view["kind"] == "fallback":
        block["note"] = NO_DIAMOND_PAIR_NOTE if view["diamond"] else NO_DIAMOND_NOTE
    if not games:
        block.update(wr=None, wr_text=None, raw_text=None, tone="none", diff14_text=None, thin=True,
                     note=(block["note"] + "; " if block["note"] else "") + "sin partidas de este cruce")
        return block
    shrunk = stats["shrunk_lane_wr"]
    block.update(wr=shrunk, wr_text=pct(shrunk), raw_text=pct(stats["lane_wr"]),
                 tone=tone_of_rate(shrunk, games), thin=games < LOW_SAMPLE_GAMES,
                 diff14_text=f"{signed(stats['avg_diff14'])} oro+exp al 14" if stats.get("avg_diff14") is not None else None)
    return block


def person_block(record, name) -> dict | None:
    if not record or not record["games"]:
        return None
    g, w = record["games"], record["wins"]
    return {"name": name, "games": g, "wins": w,
            "text": f"{w} de {g} {'línea ganada' if w == 1 and g == 1 else 'líneas ganadas'}"}


# --------------------------------------------------------------- OP.GG block
def opgg_block(found, ally_name, enemy_name, mine=True) -> dict | None:
    """The "opgg" part of a card from a `LaneOpgg.lookup` answer (None when OP.GG is off)."""
    status = found.get("status", "off")
    if status == "off":
        return None
    block = {"status": status}
    if status not in ("ok",):
        return block
    games, wins = found.get("games") or 0, found.get("wins") or 0
    block.update(games=games, wr=None, wr_text=None, tone="none")
    if games:
        tone = dl.tone_of(games, wins)
        block["tone"] = tone
        if games >= dl.FEW_GAMES:
            block.update(wr=wins / games, wr_text=dl.pct_text(wins / games))

    def who(side):
        return None if side is None else "Parejo" if side == "even" else ally_name if side == "mine" else enemy_name

    lane, solo = who(found.get("lane_winner")), who(found.get("solo_winner"))
    block["lane"] = None if lane is None else ("Ventaja en la línea: " + lane if lane != "Parejo" else "La línea va pareja")
    block["solo"] = None if solo is None else ("Ventaja de solo kill: " + solo if solo != "Parejo" else "Sin ventaja de solo kill")
    block["lane_side"] = found.get("lane_winner")
    block["tip"] = found.get("tip")
    block["style"] = STYLE_TEXT.get(found.get("style"))
    block["length"] = opgg_length_text(found.get("lengths"))
    return block


# ------------------------------------------------------------ best into a rival
def best_block(book, role, opp_id, pool, taken, name_of, limit=BEST_SHOWN) -> dict | None:
    """"Mejores contra <rival>": the pool of a person ranked by lane win rate against the revealed rival.

    `pool`: [(champion id, own games)]; champions in `taken` (picked or banned) are left out."""
    pool = [(c, g) for c, g in pool if c not in taken]
    if not pool or book is None:
        return None
    result = book.best_into(role, opp_id, [c for c, _g in pool])
    own = dict(pool)
    items = []
    for m in result["items"][:limit]:
        games = m["games"]
        items.append({"champion_id": m["champ"], "name": name_of(m["champ"]), "games": games,
                      "wr_text": pct(m["shrunk_lane_wr"]), "own_games": own.get(m["champ"], 0),
                      "tone": tone_of_rate(m["shrunk_lane_wr"], games), "thin": games < LOW_SAMPLE_GAMES})
    note = None
    if result["kind"] == "fallback":
        note = NO_DIAMOND_NOTE if not book.has_diamond() else NO_DIAMOND_PAIR_NOTE
    return {"label": result["label"], "kind": result["kind"], "note": note, "items": items}


# ----------------------------------------------------------------- the cards
def _pair_card(book, role, mine, ally, enemy, name_of, opgg_lookup, subject, best):
    ally_id = ally["champion_id"] or ally["hover_id"]
    locked = bool(ally["champion_id"])
    enemy_id = enemy["champion_id"]
    ally_name, enemy_name = name_of(ally_id), name_of(enemy_id)
    view = book.matchup(role, ally_id, enemy_id) if book else None
    card = {"role": role, "role_label": role_name(role), "mine": mine, "state": "pair",
            "ally": {"champion_id": ally_id, "name": ally_name, "hover": not locked, "who": subject,
                     "cell_id": ally["cell_id"], "person": ally.get("person")},
            "enemy": {"champion_id": enemy_id, "name": enemy_name, "role_low": bool(enemy.get("role_low")),
                      "role_conf": enemy.get("role_conf")},
            "sample": None, "spikes": None, "person": None, "opgg": None, "length": None, "best": best}
    if view:
        card["sample"] = sample_block(view)
        stats = view["stats"]
        mine_overall, their_overall = book.overall(role, ally_id), book.overall(role, enemy_id)
        a_prof = spike_profile(mine_overall["stats"], mine_overall["stats"].get("low_sample", False))
        e_prof = spike_profile(their_overall["stats"], their_overall["stats"].get("low_sample", False))
        card["spikes"] = {
            "ally": {**a_prof, "label": subject if mine else ally_name, "source": mine_overall["label"]},
            "enemy": {**e_prof, "label": "Rival" if mine else enemy_name, "source": their_overall["label"]},
            "duel": duel_text(stats)}
        record = book.person_record(role, ally_id, enemy_id, ally.get("person"))
        card["person"] = person_block(record, subject if mine else ally.get("person"))
        card["length"] = {"ours": a_prof["length"]["text"] if a_prof["length"] else None, "label": mine_overall["label"]}
    found = opgg_lookup(role, ally_id, enemy_id, locked) if opgg_lookup else {"status": "off"}
    card["opgg"] = opgg_block(found, ally_name, enemy_name)
    if card["opgg"] and card["opgg"].get("length"):
        card["length"] = {**(card["length"] or {"ours": None, "label": None}), "opgg": card["opgg"]["length"]}
    card["summary"] = _summary(card)
    return card


def _summary(card) -> str:
    parts = []
    s = card["sample"]
    if s and s["games"]:
        parts.append(f"{s['wr_text']} en la línea ({s['games']} {'partida' if s['games'] == 1 else 'partidas'} {s['label']})")
    elif s:
        parts.append("sin partidas de este cruce")
    o = card["opgg"]
    if o and o["status"] == "ok":
        parts.append("OP.GG: " + (o["lane"] or (o["wr_text"] and f"{o['wr_text']} de victorias") or "con datos"))
    elif o and o["status"] == "pending":
        parts.append("OP.GG en camino…")
    return " · ".join(parts)


def build_lanes(book, allies, enemies, my_role, name_of, opgg_lookup=None, pool_of=None, taken=(),
                enemy_roles_state="ok") -> dict:
    """The lane cards of a champion select.

    allies: [{"cell_id", "role", "person", "is_me", "name" (label to show), "champion_id" (locked),
    "hover_id"}] with the role the page decided; enemies: [{"champion_id", "role", "role_conf",
    "role_low"}] with the inferred role (None when unknown). `opgg_lookup(role, ally_id, enemy_id, locked)`
    -> LaneOpgg.lookup-like dict, `pool_of(person, role)` -> [(champion id, own games)], `taken`: champion
    ids picked or banned. `enemy_roles_state`: "ok" | "pending" (OP.GG tables on their way) | "off" | "error".
    Returns {"cards": [...], "my_role", "enemy_roles": state, "diamond": bool, "waiting": text | None}.
    """
    by_ally = {a["role"]: a for a in allies if a.get("role")}
    by_enemy = {e["role"]: e for e in enemies if e.get("role") and e["champion_id"]}
    order = ([my_role] if my_role in LANE_ROLES else []) + [r for r in LINE_ORDER if r != my_role]
    cards = []
    for role in order:
        ally, enemy = by_ally.get(role), by_enemy.get(role)
        if ally is None:
            continue
        mine = bool(ally.get("is_me"))
        subject = "Tú" if mine else ally.get("name") or "Aleatorio"
        ally_champ = ally["champion_id"] or ally["hover_id"]
        best = None
        if enemy and ally.get("person") and not ally["champion_id"] and pool_of:
            best = best_block(book, role, enemy["champion_id"], pool_of(ally["person"], role), set(taken), name_of)
        if enemy and ally_champ:
            cards.append(_pair_card(book, role, mine, ally, enemy, name_of, opgg_lookup, subject, best))
        elif enemy and best:
            cards.append({"role": role, "role_label": role_name(role), "mine": mine, "state": "picking",
                          "ally": {"champion_id": 0, "name": None, "hover": False, "who": subject,
                                   "cell_id": ally["cell_id"], "person": ally.get("person")},
                          "enemy": {"champion_id": enemy["champion_id"], "name": name_of(enemy["champion_id"]),
                                    "role_low": bool(enemy.get("role_low")), "role_conf": enemy.get("role_conf")},
                          "best": best, "summary": f"Mejores contra {name_of(enemy['champion_id'])}"})
        elif mine:
            why = ("Deduciendo las líneas del rival con OP.GG…" if enemy_roles_state == "pending" else
                   "Sin OP.GG no se pueden deducir las líneas del rival." if enemy_roles_state == "off" else
                   "OP.GG no respondió: no se pueden deducir las líneas del rival." if enemy_roles_state == "error" else
                   "Aún no se ha revelado el rival de tu línea." if not enemy else "Elige un campeón para ver el cruce.")
            cards.append({"role": role, "role_label": role_name(role), "mine": True, "state": "waiting",
                          "message": why, "summary": why})
    return {"cards": cards, "my_role": my_role, "enemy_roles": enemy_roles_state,
            "diamond": bool(book and book.has_diamond()), "data": bool(book and book.any_data())}
