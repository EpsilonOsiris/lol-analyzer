"""Turns a game analysis (deaths.analyze_game, detail=True) into what the match page renders.

No Flask here: the caller passes small lookups (`label_for`, `icon_for`) so the same code works with
or without Data Dragon.
"""
from __future__ import annotations

from . import deaths as dx
from . import mapview
from .analyze import profile_for_role, role_name

DAMAGE_TYPES = {"fisico": "Daño físico", "magico": "Daño mágico", "verdadero": "Daño verdadero"}


def _kill_text(f_kill, parts, my_pid, label_for):
    """"Ezreal mató a Tú · asistió Brand" for one kill of a fight."""

    def who(pid):
        p = parts.get(pid)
        if not p:
            return "una torre, minions o un monstruo"
        name = label_for(p["champion"])
        return f"{name} (tú)" if pid == my_pid else name

    killer = f_kill["killer"]
    text = f"{who(killer) if killer else 'Una torre, minions o un monstruo'} mató a {who(f_kill['victim'])}"
    if f_kill["assisters"]:
        text += " · asistió " + dx.join_es(who(a) for a in f_kill["assisters"])
    return text


def _player_step(event, team):
    """"Moriste a las 3:50, en tu lado del río" for the first / last thing the player did in a fight."""
    if not event:
        return None
    where = dx.place(*(event["pos"] or (None, None)), team)["label"] if event["pos"] else "sin posición"
    verb = {"murió": "Moriste", "mató": "Mataste", "asistió": "Asististe"}[event["what"]]
    return f"{verb} a las {event['clock']} ({where})"


def _chart(sources, label_for, icon_for, my_team):
    """Rows for the stacked damage bars: one per source, widths relative to the biggest source."""
    biggest = max((s["total"] for s in sources), default=0)
    rows = []
    for s in sources:
        if s["kind"] == "champion":
            name = label_for(s["champion"])
            side = "aliado" if s["team"] == my_team else "enemigo"
        else:
            name, side = s["label"], None
        segments = [{"label": a["label"], "total": a["total"], "type": a["type"],
                     "type_label": DAMAGE_TYPES[a["type"]]} for a in s["abilities"]]
        rows.append({
            "name": name, "side": side, "total": s["total"],
            "icon": icon_for(s["champion"]) if s["kind"] == "champion" else None,
            "initials": mapview.initials(name), "team": s["team"],
            "width": round(s["total"] * 100 / biggest, 1) if biggest else 0,
            "segments": segments,
            "caption": " · ".join(f"{a['label']} {a['total']}" for a in s["abilities"]),
        })
    return rows


def build_view(analysis, label_for, icon_for, map_url, uid="g") -> dict:
    """Render-ready dict: context line, deaths (facts, chart, minimap), fights and lost objectives."""
    parts = analysis["participants"]
    my_pid, my_team = analysis["pid"], analysis["team"]
    labels = {pid: label_for(p["champion"]) for pid, p in parts.items()}
    icons = {pid: icon_for(p["champion"]) for pid, p in parts.items()}
    ctx = analysis["context"]

    last_t = max([d["t"] for d in analysis["deaths"]] + [1])
    duration_ms = max((analysis.get("duration_s") or 0) * 1000, last_t) or 1
    deaths_view = []
    for d in analysis["deaths"]:
        killer = d["killer"]
        who = (f"Te mató {labels[killer['pid']]}" + (f" (+{dx.join_es(labels[a['pid']] for a in d['assisters'])})"
                                                     if d["assisters"] else "")
               if d["kind"] == "campeon" and killer else
               {"torre": "Te mató una torre", "minion": "Te mataron los minions",
                "monstruo": "Te mató un monstruo"}.get(d["kind"], "Moriste sin un campeón"))
        badges = []
        if d["enemy_jungler_involved"] and analysis["role"] != "JUNGLE":
            badges.append("Jungla enemiga")
        if d["after_own_kill"]:
            badges.append("Tras una kill tuya")
        if d["unspent_1000"]:
            badges.append("1000+ oro sin gastar")
        if d["level_diff"] is not None and d["level_diff"] < 0:
            badges.append("Menos nivel")
        sources = d.get("damage_sources") or []
        deaths_view.append({
            "n": d["n"], "clock": d["clock"], "at": round(min(100, d["t"] / duration_ms * 100), 1), "who": who, "place": d["place"], "badges": badges,
            "facts": dx.describe_death(d, analysis, label_for), "fight": d["fight"],
            "chart": _chart(sources, label_for, icon_for, my_team),
            "svg": mapview.death_map(d, parts, my_pid, my_team, labels, icons, map_url, f"{uid}d{d['n']}"),
            "frame_clock": dx.fmt_clock(d["frame_ts"]) if d.get("frame_ts") is not None else None,
            "pos_clock": dx.fmt_clock(d["positions_frame_ts"]) if d.get("positions_frame_ts") is not None else None,
        })

    fights_view = []
    for f in analysis.get("fights", []):
        kills = [{"clock": k["clock"], "text": _kill_text(k, parts, my_pid, label_for),
                  "mine": k["victim"] == my_pid or k["killer"] == my_pid or my_pid in k["assisters"],
                  "victim_mine": k["victim_team"] == my_team}
                 for k in f["kills"]]
        after = []
        for o in f["aftermath"]:
            who = "Tu equipo" if o["by"] == "aliado" else "El equipo enemigo"
            when = f"{abs(o['dt_s'])} s después de la última muerte" if o["dt_s"] >= 0 else "durante la pelea"
            after.append(f"{who} {dx.OBJECTIVE_DONE[o['kind']]} ({when}).")
        pl = [_player_step(e, my_team) for e in (f["player_first"], f["player_last"])]
        fights_view.append({
            "id": f["id"], "clock": f["clock"], "duration_s": f["duration_s"], "place": f["place"],
            "result": f["result"], "score": f"{f['my_team_kills']}-{f['enemy_team_kills']}",
            "summary": dx.describe_fight(f, analysis, label_for),
            "player_died": f["player_died"], "player_involved": f["player_involved"],
            "kills": kills, "aftermath": after,
            "first": pl[0], "last": pl[1],
            "frame_clock": dx.fmt_clock(f["positions_frame_ts"]) if f.get("positions_frame_ts") is not None else None,
            "svg": mapview.fight_map(f, parts, my_pid, labels, icons, map_url, f"{uid}f{f['id']}"),
        })

    lost = []
    for o in analysis["objectives_lost"]:
        if o["dead"]:
            where = (f"Estabas muerto (moriste {int(round(o['dead_for_s']))} s antes)"
                     if o["dead_for_s"] is not None else "Estabas muerto")
        elif o["far"]:
            where = f"Estabas lejos: a unas {int(round(o['distance'], -2))} unidades del foso"
        elif o["distance"] is not None:
            where = f"Estabas cerca: a unas {int(round(o['distance'], -2))} unidades del foso"
        else:
            where = "Sin posición registrada"
        lost.append({**o, "where": where})

    return {
        "my_champion": label_for(analysis["champion"]),
        "role": role_name(analysis["role"]),
        "rival_label": profile_for_role(analysis["role"]).rival,
        "lane_opponent": label_for(ctx["lane_opponent"]) if ctx["lane_opponent"] else None,
        # a jungler's enemy jungler is already the rival above
        "enemy_jungler": (label_for(ctx["enemy_jungler"])
                          if ctx["enemy_jungler"] and analysis["role"] != "JUNGLE" else None),
        "enemy_bot": [label_for(c) for c in ctx["enemy_bot"]],
        "deaths": deaths_view, "fights": fights_view, "lost": lost,
    }
