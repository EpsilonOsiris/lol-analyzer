"""Server-side SVG for the minimap views: one death, a fight (step by step) and the death heatmap.

Pure string building (no Flask). Coordinates are Riot map units (0-14820, blue base bottom-left);
the SVG is 1000x1000 with y flipped. Colours live in CSS classes (style.css) so light and dark
themes both work. A champion marker is a circle with the champion's initials and, when the icon is
cached, its picture on top. Solid outline = exact position, dashed outline = approximate.
"""
from __future__ import annotations

import math
from html import escape

from markupsafe import Markup

from .deaths import MAP_SIZE, RIVER_BAND

SIZE = 1000
R = 26                      # marker radius (SVG units)
HEAT_BINS = 40


def px(x, y) -> tuple[float, float]:
    return round(x / MAP_SIZE * SIZE, 1), round((1 - y / MAP_SIZE) * SIZE, 1)


def _pts(points) -> str:
    return " ".join(f"{px(x, y)[0]},{px(x, y)[1]}" for x, y in points)


def background(map_url=None) -> str:
    """The minimap picture when it is cached, else a schematic Summoner's Rift."""
    if map_url:
        return (f'<image class="map-img" href="{escape(map_url)}" x="0" y="0" '
                f'width="{SIZE}" height="{SIZE}" preserveAspectRatio="none"/>')
    river_w = round(RIVER_BAND * 1.2 / MAP_SIZE * SIZE)
    top = [(1000, 4300), (1000, 13700), (4300, 13700), (10600, 13700)]
    bot = [(4300, 1000), (10500, 1000), (13700, 1000), (13700, 4300), (13700, 10500)]
    bases = [(1300, 1300, "blue"), (MAP_SIZE - 1300, MAP_SIZE - 1300, "red")]
    parts = [
        f'<rect class="sm-bg" x="0" y="0" width="{SIZE}" height="{SIZE}"/>',
        f'<line class="sm-river" x1="0" y1="0" x2="{SIZE}" y2="{SIZE}" stroke-width="{river_w}"/>',
        f'<polyline class="sm-lane" points="{_pts(top)}"/>',
        f'<polyline class="sm-lane" points="{_pts(bot)}"/>',
        f'<line class="sm-lane" x1="0" y1="{SIZE}" x2="{SIZE}" y2="0"/>',
    ]
    for x, y, team in bases:
        cx, cy = px(x, y)
        parts.append(f'<circle class="sm-base sm-base-{team}" cx="{cx}" cy="{cy}" r="95"/>')
    return "".join(parts)


def spread(movable: dict, fixed=(), min_dist=2 * R + 6, rounds=60) -> dict:
    """Nudge overlapping approximate markers apart so every one stays visible.

    `movable` maps a key to map coordinates; `fixed` are exact points (never moved). Works in SVG
    units, returns {key: (svg_x, svg_y)}. The shift is small and these positions are approximate
    anyway, so it only changes how the picture reads, not what it says.
    """
    pts = {k: list(px(x, y)) for k, (x, y) in movable.items()}
    anchors = [px(x, y) for x, y in fixed]
    keys = list(pts)
    for _ in range(rounds):
        moved = False
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                moved |= _push(pts[a], pts[b], min_dist, i)
            for fx, fy in anchors:
                moved |= _push(pts[a], [fx, fy], min_dist, i, fixed_b=True)
        if not moved:
            break
    return {k: (round(v[0], 1), round(v[1], 1)) for k, v in pts.items()}


def _push(a, b, min_dist, salt, fixed_b=False) -> bool:
    dx, dy = a[0] - b[0], a[1] - b[1]
    dist = math.hypot(dx, dy)
    if dist >= min_dist:
        return False
    if dist < 0.5:                               # same spot: pick a direction from the index
        angle = salt * 2.4
        dx, dy, dist = math.cos(angle), math.sin(angle), 1.0
    push = (min_dist - dist) / (1 if fixed_b else 2)
    a[0] += dx / dist * push
    a[1] += dy / dist * push
    if not fixed_b:
        b[0] -= dx / dist * push
        b[1] -= dy / dist * push
    return True


def initials(name) -> str:
    return (str(name or "?")[:2]).capitalize()


def _marker(x, y, label, team, uid, icon=None, *, approx=False, me=False, dead=False,
            current=False, note="", svg_xy=None) -> str:
    cx, cy = svg_xy if svg_xy else px(x, y)
    cls = ["mk", f"team-{'blue' if team == 100 else 'red'}"]
    if approx:
        cls.append("approx")
    if me:
        cls.append("me")
    if dead:
        cls.append("dead")
    if current:
        cls.append("cur")
    title = escape(f"{label}{note}")
    out = [f'<g class="{" ".join(cls)}" transform="translate({cx} {cy})"><title>{title}</title>']
    if current:
        out.append(f'<circle class="mk-pulse" r="{R + 14}"/>')
    out.append(f'<circle class="mk-ring" r="{R + 3}"/><circle class="mk-bg" r="{R}"/>')
    out.append(f'<text class="mk-txt" y="6">{escape(initials(label))}</text>')
    if icon:
        out.append(f'<image href="{escape(icon)}" x="{-R}" y="{-R}" width="{2 * R}" height="{2 * R}" '
                   f'clip-path="url(#cp-{uid})"/>')
    if dead:
        out.append(f'<path class="mk-x" d="M{-R} {-R}L{R} {R}M{R} {-R}L{-R} {R}"/>')
    out.append("</g>")
    return "".join(out)


def _svg(inner, uid, label, extra_cls="") -> Markup:
    return Markup(
        f'<svg class="minimap {extra_cls}" viewBox="0 0 {SIZE} {SIZE}" role="img" '
        f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><clipPath id="cp-{uid}"><circle r="{R}"/></clipPath></defs>{inner}</svg>')


def _who(parts, pid, labels, my_pid) -> str:
    name = labels.get(pid) or (parts.get(pid) or {}).get("champion", "?")
    return f"{name} (tú)" if pid == my_pid else name


def death_map(death, parts, my_pid, my_team, labels, icons, map_url, uid) -> Markup:
    """Where the player died (exact) plus the killer and assisters (approximate, nearest minute frame)."""
    inner = [background(map_url)]
    positions = death.get("positions") or {}
    shown = ([death["killer"]] if death.get("killer") else []) + list(death["assisters"])
    movable = {p["pid"]: positions[p["pid"]] for p in shown if p["pid"] in positions}
    fixed = [death["pos"]] if death.get("pos") else []
    spots = spread(movable, fixed)
    for p in shown:
        if p["pid"] in spots:
            inner.append(_marker(0, 0, _who(parts, p["pid"], labels, my_pid), p["team"], uid,
                                 icons.get(p["pid"]), approx=True, svg_xy=spots[p["pid"]],
                                 note=" · posición aproximada (registro del minuto)"))
    if death.get("pos"):
        x, y = death["pos"]
        inner.append(_marker(x, y, _who(parts, my_pid, labels, my_pid), my_team, uid, icons.get(my_pid),
                             me=True, dead=True, current=True, note=f" · moriste aquí a las {death['clock']}"))
    return _svg("".join(inner), uid,
                f"Mapa: dónde moriste a las {death['clock']} ({death['place']}) y dónde estaban "
                "aproximadamente quienes te mataron.")


def fight_map(fight, parts, my_pid, labels, icons, map_url, uid) -> Markup:
    """One <g class="step"> per step: 0 = before the fight, k = after the k-th kill."""
    kills = fight["kills"]
    positions = fight.get("positions") or {}
    exact = [k["pos"] for k in kills if k["pos"]]
    spots = spread({pid: pos for pid, pos in positions.items()}, exact)
    steps = []
    for k in range(len(kills) + 1):
        dead = {kl["victim"]: kl for kl in kills[:k]}
        g = [f'<g class="step" data-step="{k}"' + ("" if k == len(kills) else " hidden") + ">"]
        for pid, p in parts.items():
            who = _who(parts, pid, labels, my_pid)
            if pid in dead and dead[pid]["pos"]:
                x, y = dead[pid]["pos"]
                g.append(_marker(x, y, who, p["team"], uid, icons.get(pid), me=pid == my_pid, dead=True,
                                 current=(k > 0 and kills[k - 1]["victim"] == pid),
                                 note=f" · murió a las {dead[pid]['clock']} (posición exacta)"))
            elif pid in dead:
                continue
            elif pid in spots:
                g.append(_marker(0, 0, who, p["team"], uid, icons.get(pid), approx=True, me=pid == my_pid,
                                 svg_xy=spots[pid], note=" · posición aproximada (registro del minuto)"))
        g.append("</g>")
        steps.append("".join(g))
    return _svg(background(map_url) + "".join(steps), uid,
                f"Mapa de la pelea de las {fight['clock']}: {len(kills)} muertes. Usa los botones para "
                "avanzar paso a paso; la lista de al lado tiene el mismo detalle en texto.", "fight-map")


def heatmap(points, map_url, uid, bins=HEAT_BINS) -> Markup:
    """Death positions as blurred, semi-transparent cells over the minimap."""
    cells: dict[tuple[int, int], int] = {}
    for x, y in points:
        i = min(max(int(x / MAP_SIZE * bins), 0), bins - 1)
        j = min(max(int(y / MAP_SIZE * bins), 0), bins - 1)
        cells[(i, j)] = cells.get((i, j), 0) + 1
    size = SIZE / bins
    top = max(cells.values(), default=1)
    rects = []
    for (i, j), n in sorted(cells.items()):
        opacity = round(0.28 + 0.72 * (n / top) ** 0.5, 2)
        rects.append(f'<rect class="heat-cell" x="{round(i * size, 1)}" y="{round(SIZE - (j + 1) * size, 1)}" '
                     f'width="{round(size, 1)}" height="{round(size, 1)}" fill-opacity="{opacity}"/>')
    inner = (f'<defs><filter id="hb-{uid}" x="-5%" y="-5%" width="110%" height="110%">'
             f'<feGaussianBlur stdDeviation="{round(size * 0.65, 1)}"/></filter></defs>'
             f'{background(map_url)}<g class="heat" filter="url(#hb-{uid})">{"".join(rects)}</g>')
    return _svg(inner, uid, f"Mapa de calor con {len(points)} muertes. Más rojo = más muertes en ese punto.",
                "heat-map")
