"""Synthetic lane rows with a level-2/6 power spike and an item spike, written to a tmp data/lineas."""
from __future__ import annotations

from lane_fixtures import row

from lolan.samples import LineStore


def series(spike_levels=(2, 6), item_minute=20, item_gain=40):
    """Per-minute [minute, my level, opp level, diff] up to 25: +80 diff per minute at the spike levels,
    +item_gain per minute after minute item_minute + 1 (None: no item pace)."""
    out, diff = [], 0
    for m in range(26):
        level = 1 + m // 2
        if m:
            prev_level = 1 + (m - 1) // 2
            diff += 80 if prev_level in spike_levels else 0
            if item_minute is not None and m > item_minute:
                diff += item_gain
        out.append([m, level, level, diff])
    return out


def lane_rows(champ, opp, n, wins, role="MIDDLE", source="propia", person=None, patch="16.21", prefix="M",
              minutes=(20.0, 40.0), spikes=True, **kw):
    """`n` rows of `champ` against `opp` (wins of them won the lane); wins are also game wins at minutes[0]."""
    rows = []
    for i in range(n):
        won = i < wins
        extra = {"series": series(), "items": [13, 22, None], "brackets": {
            "1-2": {"gain": 160, "minutes": 2}, "3-5": {"gain": 0, "minutes": 3}, "6-8": {"gain": -240, "minutes": 3}}} if spikes else {}
        rows.append(row(match_id=f"{prefix}-{source}-{role}-{champ}-{opp}-{i}", source=source, patch=patch, role=role,
                        champ=champ, opp=opp, result="win" if won else "loss", diff14=600 if won else -600,
                        gold14=400 if won else -400, xp14=200 if won else -200, person=person,
                        win=1 if won else 0, minutes=minutes[0] if won else minutes[1], **extra, **kw))
    return rows


def write_lines(base, *row_groups, rebuild=True, names=None):
    store = LineStore(base)
    for rows in row_groups:
        store.append_rows(rows)
    if names:
        store.remember_names(names)
    if rebuild:
        store.rebuild()
    return store
