"""Lane phase: how each lane matchup goes until minute 14, from the match and its timeline.

For every role with exactly one player per team, the blue player is paired with the red one and
each side gets its own view of the lane:
  * diff14: (gold + xp) of this side minus the opponent's at the minute-14 frame (gold14 / xp14
    are the two parts). `result` is win / even / loss against LANE_EVEN.
  * brackets: the per-minute change of that difference, filed under the level this side had when
    the minute started. Summed later it answers "at which levels do I gain or lose the lane".
  * first2 / first6: who reached level 2 / 6 first (1 me, 0 the opponent, None tied or unknown).
  * series: [minute, my level, opponent's level, diff] for every minute up to 25, the raw material
    for power-spike analysis (per level, around item completions, mid game).
  * items: minute at which this side completed its 1st, 2nd and 3rd legendary item (see
    `legendary_items`), None when item data for the patch is not available.
  * win / minutes: whether this side's team won and how long the game lasted.

Pure functions: nothing here touches the disk. `analyze_match` also says why a match was skipped.
"""
from __future__ import annotations

from .metrics import (SR_QUEUES, _frame_for_minute, _int, _num, game_duration_seconds, is_remake)

LANE_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
LANE_MINUTE = 14
MIN_GAME_S = 15 * 60          # shorter games end before the lane is settled
SERIES_MINUTES = 25           # the per-minute series stops here
ITEM_COUNT = 3                # legendary items tracked per side
LEGENDARY_MIN_GOLD = 2000
# Gold + xp lead at minute 14 beyond which the lane counts as won / lost (about one kill plus a
# few waves of minions); anything closer is "even". The user chose this definition.
LANE_EVEN = 300
# (bracket, lowest level, highest level) of the side's own level at the start of the minute.
LEVEL_BRACKETS = (("1-2", 1, 2), ("3-5", 3, 5), ("6-8", 6, 8), ("9+", 9, 99))
BRACKET_NAMES = tuple(b[0] for b in LEVEL_BRACKETS)

SKIP_LABELS = {
    "sin_timeline": "sin timeline", "remake": "remake", "corta": "partida de menos de 15 min",
    "cola": "cola que no es de la Grieta", "sin_frame_14": "sin frame del minuto 14",
    "frames_incompletos": "frames incompletos", "sin_lineas": "sin líneas emparejables",
}


def bracket_of(level) -> str:
    level = _int(level, 1)
    for name, low, high in LEVEL_BRACKETS:
        if low <= level <= high:
            return name
    return LEVEL_BRACKETS[0][0] if level < 1 else LEVEL_BRACKETS[-1][0]


def patch_of(game_version) -> str:
    """"16.21.824.8524" -> "16.21" ("" when unknown)."""
    parts = str(game_version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]) else ""


def result_of(diff14) -> str:
    if diff14 > LANE_EVEN:
        return "win"
    if diff14 < -LANE_EVEN:
        return "loss"
    return "even"


def _pframe(frame, pid):
    pfs = frame.get("participantFrames") or {}
    pf = pfs.get(str(pid))
    if pf is None:
        pf = pfs.get(pid)
    return pf if isinstance(pf, dict) else None


def _series(frames, pid):
    """([gold], [xp], [level]) per frame, cut at the first frame with a missing value.

    None when the first LANE_MINUTE + 1 frames are not all complete.
    """
    gold, xp, level = [], [], []
    for fr in frames:
        pf = _pframe(fr, pid)
        g, x, lv = (_num(pf.get(k)) if pf else None for k in ("totalGold", "xp", "level"))
        if g is None or x is None or lv is None:
            break
        gold.append(g)
        xp.append(x)
        level.append(lv)
    return (gold, xp, level) if len(gold) > LANE_MINUTE else None


def legendary_items(item_data) -> set[int]:
    """Ids of the "completed" items of a Data Dragon item.json (its "data" dict, or the whole file).

    Rule: buyable, builds into nothing, total cost of at least LEGENDARY_MIN_GOLD, and neither
    boots, a consumable nor a trinket; items marked as not on Summoner's Rift are left out. Edge
    cases: it needs no "mythic" notion (patches without mythics work the same); starters and
    support quest items cost less and never qualify; transformations that are not bought
    (Muramana, Seraph's) are not purchases so they never show up; an item that still builds into
    something only counts once it is its final form.
    """
    data = (item_data or {}).get("data", item_data) or {}
    out = set()
    for key, e in data.items():
        if not isinstance(e, dict) or not str(key).isdigit():
            continue
        gold = e.get("gold") or {}
        tags = set(e.get("tags") or [])
        if (gold.get("purchasable") and not e.get("into")
                and (_num(gold.get("total")) or 0) >= LEGENDARY_MIN_GOLD
                and not tags & {"Boots", "Consumable", "Trinket"}
                and (e.get("maps") or {}).get("11", True)):
            out.add(int(key))
    return out


def _completed_items(timeline, legendary) -> dict:
    """participantId -> minutes (1 decimal) of its first ITEM_COUNT legendary items.

    ITEM_PURCHASED of a legendary id completes it; ITEM_UNDO of that purchase (beforeId) takes it
    back; selling it later does not. The same item is counted once.
    """
    events = [e for fr in ((timeline or {}).get("info") or {}).get("frames") or []
              for e in fr.get("events") or []
              if e.get("type") in ("ITEM_PURCHASED", "ITEM_UNDO") and _num(e.get("timestamp")) is not None]
    events.sort(key=lambda e: e["timestamp"])
    done: dict = {}
    for e in events:
        pid = e.get("participantId")
        if not isinstance(pid, int) or pid < 1:
            continue
        mine = done.setdefault(pid, [])
        if e["type"] == "ITEM_PURCHASED":
            item = e.get("itemId")
            if item in legendary and all(i != item for i, _ in mine):
                mine.append((item, e["timestamp"]))
        else:
            for k in range(len(mine) - 1, -1, -1):
                if mine[k][0] == e.get("beforeId"):
                    del mine[k]
                    break
    return {pid: [round(ts / 60000, 1) for _i, ts in mine[:ITEM_COUNT]] for pid, mine in done.items()}


def _level_times(timeline) -> dict:
    """(participantId, level) -> earliest LEVEL_UP timestamp (ms)."""
    times: dict = {}
    for fr in ((timeline or {}).get("info") or {}).get("frames") or []:
        for ev in fr.get("events") or []:
            if ev.get("type") != "LEVEL_UP":
                continue
            pid, lv, ts = ev.get("participantId"), _num(ev.get("level")), _num(ev.get("timestamp"))
            if isinstance(pid, int) and lv is not None and ts is not None:
                key = (pid, int(lv))
                if key not in times or ts < times[key]:
                    times[key] = ts
    return times


def _first(level_times, me, opp, level):
    """1 when `me` reached `level` first, 0 when the opponent did, None when tied or unknown."""
    mine, theirs = level_times.get((me, level)), level_times.get((opp, level))
    if mine is None and theirs is None:
        return None
    if mine is None:
        return 0                  # the timeline covers the whole game: never reaching it is behind
    if theirs is None:
        return 1
    if mine == theirs:
        return None
    return 1 if mine < theirs else 0


def _side_view(a, b, sa, sb, level_times) -> dict:
    """Lane view of participant `a` against `b`; sa / sb are their (gold, xp, level) series."""
    n = min(len(sa[0]), len(sb[0]))
    diffs = [(sa[0][m] + sa[1][m]) - (sb[0][m] + sb[1][m]) for m in range(n)]
    gold14 = sa[0][LANE_MINUTE] - sb[0][LANE_MINUTE]
    xp14 = sa[1][LANE_MINUTE] - sb[1][LANE_MINUTE]
    brackets = {name: {"gain": 0, "minutes": 0} for name in BRACKET_NAMES}
    for m in range(1, LANE_MINUTE + 1):
        b_ = brackets[bracket_of(sa[2][m - 1])]
        b_["gain"] += diffs[m] - diffs[m - 1]
        b_["minutes"] += 1
    brackets = {k: {"gain": round(v["gain"], 1), "minutes": v["minutes"]}
                for k, v in brackets.items() if v["minutes"]}
    diff14 = gold14 + xp14
    return {"gold14": round(gold14), "xp14": round(xp14), "diff14": round(diff14),
            "result": result_of(diff14), "brackets": brackets,
            "first2": _first(level_times, a, b, 2), "first6": _first(level_times, a, b, 6),
            "series": [[m, int(sa[2][m]), int(sb[2][m]), round(diffs[m])] for m in range(n)]}


def analyze_match(match, timeline, legendary=None) -> tuple[list[dict], str | None]:
    """(pairs, skip_reason): `pairs` as in `lane_pairs`; the reason is None when the match counted.

    `legendary`: set of completed-item ids for the match's patch (`legendary_items`); None = unknown.
    """
    info = (match or {}).get("info") or {}
    frames = [fr for fr in ((timeline or {}).get("info") or {}).get("frames") or []
              if _num(fr.get("timestamp")) is not None]
    if not frames:
        return [], "sin_timeline"
    if is_remake(info):
        return [], "remake"
    dur = game_duration_seconds(info)
    if dur is None or dur < MIN_GAME_S:
        return [], "corta"
    if info.get("queueId") not in SR_QUEUES:
        return [], "cola"
    frames.sort(key=lambda fr: fr["timestamp"])
    if _frame_for_minute(frames, LANE_MINUTE) is None:
        return [], "sin_frame_14"
    upto = [_frame_for_minute(frames, m) for m in range(LANE_MINUTE + 1)]
    if any(fr is None for fr in upto):
        return [], "frames_incompletos"
    for m in range(LANE_MINUTE + 1, min(SERIES_MINUTES, int(dur // 60)) + 1):
        fr = _frame_for_minute(frames, m)
        if fr is None:
            break
        upto.append(fr)

    by_slot: dict = {}
    for p in info.get("participants") or []:
        role = p.get("teamPosition")
        if role in LANE_ROLES and isinstance(p.get("participantId"), int):
            by_slot.setdefault((role, p.get("teamId")), []).append(p)
    level_times = _level_times(timeline)
    completed = _completed_items(timeline, legendary) if legendary is not None else None
    minutes = round(dur / 60, 1)
    match_id = ((match.get("metadata") or {}).get("matchId")) or ""
    pairs = []
    for role in LANE_ROLES:
        blue, red = by_slot.get((role, 100), []), by_slot.get((role, 200), [])
        if len(blue) != 1 or len(red) != 1:
            continue
        pb, pr = blue[0], red[0]
        sb, sr = _series(upto, pb["participantId"]), _series(upto, pr["participantId"])
        if sb is None or sr is None or not pb.get("championId") or not pr.get("championId"):
            continue
        views = []
        for me, op, s_me, s_op in ((pb, pr, sb, sr), (pr, pb, sr, sb)):
            view = _side_view(me["participantId"], op["participantId"], s_me, s_op, level_times)
            win = me.get("win")
            items = None
            if completed is not None:
                items = (completed.get(me["participantId"], []) + [None] * ITEM_COUNT)[:ITEM_COUNT]
            view.update({"win": int(win) if isinstance(win, bool) else None, "minutes": minutes,
                         "items": items, "side": me.get("teamId"), "champ": int(me["championId"]),
                         "opp": int(op["championId"]), "puuid": me.get("puuid") or "",
                         "name": (me.get("riotIdGameName") or "").strip()})
            views.append(view)
        pairs.append({"match_id": match_id, "role": role, "patch": patch_of(info.get("gameVersion")),
                      "queue": info.get("queueId"), "game_creation": info.get("gameCreation"),
                      "sides": views})
    return pairs, (None if pairs else "sin_lineas")


def lane_pairs(match, timeline, legendary=None) -> list[dict]:
    """One dict per lane pair (blue vs red) with both sides' views under "sides".

    Pair: {match_id, role, patch, queue, game_creation, sides: [blue view, red view]}. A view:
    {side (100/200), champ, opp (championIds), puuid, name, diff14, gold14, xp14, result,
    brackets {name: {gain, minutes}}, first2, first6, series [[minute, level, opp level, diff]],
    items [minute of the 1st/2nd/3rd legendary or None; whole value None when `legendary` is],
    win, minutes}. Empty when the match is skipped (shorter
    than 15 min, remake, no minute-14 frame ...; `analyze_match` says which).
    """
    return analyze_match(match, timeline, legendary)[0]
