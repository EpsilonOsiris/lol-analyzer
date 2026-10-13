"""Markdown reports and CSV export."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from . import analyze as an

RECENT_GAME_REPORTS = 30


def _table(headers, rows) -> str:
    if not rows:
        return "_Sin datos._\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _fmt(v, digits=2):
    return "-" if v is None else f"{v:.{digits}f}"


def _group_rows(groups, label_header="Grupo"):
    headers = [label_header, "Partidas", "Winrate", "KDA", "CS/min", "Muertes/10min",
               "Dif. oro @14", "Dif. CS @14"]
    rows = [[
        g["label"], g["games"], an.fmt_pct(g["winrate"]), _fmt(g["kda"]), _fmt(g["cs_per_min"]),
        _fmt(g["deaths_per_10"]), _fmt(g["gold_diff_14"], 0), _fmt(g["cs_diff_14"], 1),
    ] for g in groups]
    return headers, rows


def _insight_lines(ins) -> str:
    if not ins["enough"]:
        return (f"_Hay {ins['games']} partidas con este campeón ({ins['wins']} victorias, "
                f"{ins['losses']} derrotas); se necesitan al menos {an.MIN_CHAMPION_GAMES} partidas, "
                "con 2 o más victorias y 2 o más derrotas, para comparar._\n")

    def block(entries):
        rows = [[e["label"], an.fmt_value(e["key"], e["mean_win"]),
                 an.fmt_value(e["key"], e["mean_loss"]), f"{e['effect']:+.2f}"] for e in entries]
        return _table(["Métrica", "Promedio en victorias", "Promedio en derrotas", "Tamaño del efecto"], rows)

    return ("**Más alto en tus victorias** (lo que suele acompañar tus victorias):\n\n"
            + block(ins["higher_in_wins"])
            + "\n**Más alto en tus derrotas** (lo que suele acompañar tus derrotas):\n\n"
            + block(ins["higher_in_losses"])
            + "\n_Tamaño del efecto = (promedio en victorias - promedio en derrotas) / desviación "
              "estándar combinada. Valores de 0.5 o más son un efecto mediano; 0.8 o más, grande. "
              "Es correlación, no causalidad._\n")


# --------------------------------------------------------------- summary
def render_summary(games) -> str:
    s = an.summarize(games)
    o = s["overall"]
    lines = ["# Resumen general\n"]
    lines.append(
        f"Partidas analizadas: **{o['games']}** ({o['wins']} victorias, {o['games'] - o['wins']} derrotas). "
        f"Winrate: **{an.fmt_pct(o['winrate'])}**. KDA global: {_fmt(o['kda'])}. "
        f"CS/min promedio: {_fmt(o['cs_per_min'])}. Muertes por 10 min: {_fmt(o['deaths_per_10'])}.\n")
    if games:
        lines.append(f"Periodo: {games[0]['date']} a {games[-1]['date']}.\n")

    for title, key, header in (("Por cola", "by_queue", "Cola"), ("Por rol", "by_role", "Rol")):
        lines.append(f"## {title}\n")
        lines.append(_table(*_group_rows(s[key], header)))

    lines.append("## Por campeón\n")
    lines.append(_table(*_group_rows(s["by_champion"], "Campeón")))

    lines.append("## Tendencias\n")
    lines.append("### Por mes\n")
    lines.append(_table(*_group_rows(an.trend_by_month(games), "Mes")))
    lines.append("### Por día de la semana\n")
    lines.append(_table(*_group_rows(an.trend_by_weekday(games), "Día")))
    sess = an.trend_by_session_game(games)
    lines.append(f"### Por número de partida dentro de la sesión\n")
    lines.append(f"Una sesión es una racha de partidas con menos de {an.SESSION_GAP_MIN} minutos de "
                 f"separación. Sesiones detectadas: {sess['sessions']}.\n")
    lines.append(_table(*_group_rows(sess["by_position"], "Posición")))
    if sess["after_win"] and sess["after_loss"]:
        lines.append(
            f"Winrate justo después de una victoria: {an.fmt_pct(sess['after_win']['winrate'])} "
            f"({sess['after_win']['games']} partidas). Justo después de una derrota: "
            f"{an.fmt_pct(sess['after_loss']['winrate'])} ({sess['after_loss']['games']} partidas).\n")

    lines.append("## Lo que decide tus partidas, por campeón\n")
    lines.append(f"Campeones con al menos {an.MIN_CHAMPION_GAMES} partidas. Detalle completo en "
                 "`campeones/<Campeón>.md`.\n")
    shown = False
    for row in s["by_champion"]:
        if row["games"] < an.MIN_CHAMPION_GAMES:
            continue
        ins = an.champion_insights(games, row["key"])
        if not ins["enough"]:
            continue
        shown = True
        up = ", ".join(e["label"] for e in ins["higher_in_wins"][:3]) or "-"
        down = ", ".join(e["label"] for e in ins["higher_in_losses"][:3]) or "-"
        lines.append(f"- **{row['key']}** ({row['games']} partidas, {an.fmt_pct(row['winrate'])} winrate). "
                     f"Más alto al ganar: {up}. Más alto al perder: {down}.")
    if not shown:
        lines.append("_Aún no hay campeones con suficientes partidas._")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------- champion
def render_champion(games, champion) -> str:
    ins = an.champion_insights(games, champion)
    st = ins["stats"]
    lines = [f"# {ins['champion']}\n"]
    if not st:
        return lines[0] + "\nNo hay partidas con este campeón.\n"
    lines.append(f"Partidas: **{st['games']}** ({ins['wins']} victorias, {ins['losses']} derrotas). "
                 f"Winrate: **{an.fmt_pct(st['winrate'])}**.\n")
    lines.append("## Promedios\n")
    lines.append(_table(["Métrica", "Valor"], [
        ["KDA", _fmt(st["kda"])],
        ["CS por minuto", _fmt(st["cs_per_min"])],
        ["Oro por minuto", _fmt(st["gold_per_min"], 0)],
        ["Muertes por 10 min", _fmt(st["deaths_per_10"])],
        ["Participación en kills", an.fmt_pct(st["kp"])],
        ["Visión por minuto", _fmt(st["vision_per_min"])],
        ["Dif. de oro @14", _fmt(st["gold_diff_14"], 0)],
        ["Dif. de CS @14", _fmt(st["cs_diff_14"], 1)],
        ["Dif. de XP @14", _fmt(st["xp_diff_14"], 0)],
    ]))
    lines.append("## Qué decide tus partidas con este campeón\n")
    lines.append(_insight_lines(ins))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ game card
def render_card_markdown(card) -> str:
    res = "Victoria" if card["win"] else "Derrota"
    lines = [f"# Partida {card['match_id']}\n",
             f"**{card['champion']}** ({an.role_name(card['role'])}) - {res} - "
             f"{an.fmt_minutes(card['duration_min'])} - KDA {card['kda_text']} - {card['date']}\n"]
    if card.get("opp_champion"):
        lines.append(f"Rival de línea: **{card['opp_champion']}**.\n")
    if not card["has_timeline"]:
        lines.append("_Esta partida no tiene timeline; faltan métricas por minuto y por fase._\n")
    lines.append("## Fortalezas\n")
    lines += [f"- {s['text']}" for s in card["strengths"]] or ["_Ninguna destacada._"]
    lines.append("\n## Debilidades\n")
    lines += [f"- {w['text']}" for w in card["weaknesses"]] or ["_Ninguna destacada._"]
    lines.append("\n## Detalle\n")
    rows = []
    for r in card["rows"]:
        pct = "-" if r["goodness"] is None else f"{r['goodness']:.0f} ({r['scope']}, n={r['n']})"
        rows.append([r["label"], r["fmt"], r["opp_fmt"], pct, r["verdict"] or "-"])
    lines.append(_table(["Métrica", "Tú", "Rival de línea", "Percentil propio (mayor = mejor)", "Veredicto"], rows))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ files
def _safe_filename(name) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "desconocido"


def write_csv(games, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for g in games:
        for k in g:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for g in games:
            w.writerow({k: ("" if v is None else v) for k, v in g.items()})
    return path


def write_reports(games, reports_dir, data_dir) -> dict:
    reports_dir, data_dir = Path(reports_dir), Path(data_dir)
    for sub in ("campeones", "partidas"):
        (reports_dir / sub).mkdir(parents=True, exist_ok=True)
        # Drop reports from a previous run so a narrower run (e.g. --cola) leaves no stale files.
        for old in (reports_dir / sub).glob("*.md"):
            old.unlink()

    csv_path = write_csv(games, data_dir / "games.csv")
    (reports_dir / "resumen.md").write_text(render_summary(games), encoding="utf-8")

    champions = sorted({g["champion"] for g in games if g.get("champion")})
    for champ in champions:
        (reports_dir / "campeones" / f"{_safe_filename(champ)}.md").write_text(
            render_champion(games, champ), encoding="utf-8")

    recent = games[-RECENT_GAME_REPORTS:]
    for g in recent:
        card = an.game_report_card(g, games)
        (reports_dir / "partidas" / f"{_safe_filename(g['match_id'])}.md").write_text(
            render_card_markdown(card), encoding="utf-8")

    return {"csv": csv_path, "games": len(games), "champions": len(champions), "cards": len(recent)}
