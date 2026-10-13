"""Command line interface: python -m lolan {sync,analyze,game,campeon}."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

import truststore

from . import analyze as an
from . import report
from .config import ConfigError, load_config
from .riot import MissingApiKeyError, RiotClient, RiotError
from .store import Store
from .sync import sync as run_sync

EMPTY_DB_MESSAGE = (
    "No hay partidas en la base de datos. Ejecuta primero: python -m lolan sync "
    "(necesitas RIOT_API_KEY y RIOT_ID en .env)."
)


def _parse_date(text: str):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("la fecha debe tener el formato AAAA-MM-DD (por ejemplo, 2024-01-31)")


QUEUE_ALIASES = {"solo": 420, "soloq": 420, "flex": 440, "normal": 400, "blind": 430,
                 "quickplay": 490}


def _parse_queue(text: str) -> int:
    key = text.strip().lower()
    if key in QUEUE_ALIASES:
        return QUEUE_ALIASES[key]
    try:
        return int(key)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "la cola debe ser solo, flex, normal, blind, quickplay o un número de cola (por ejemplo, 420)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lolan",
        description="Descarga tus partidas de League of Legends desde la API de Riot y analiza "
                    "qué haces bien y mal en cada partida y en general.",
    )
    sub = parser.add_subparsers(dest="command", metavar="comando")

    p = sub.add_parser("sync", help="descarga las partidas nuevas (se puede interrumpir y reanudar)")
    p.add_argument("--max", type=int, metavar="N", dest="max_matches",
                   help="descarga como máximo N partidas nuevas")
    p.add_argument("--since", type=_parse_date, metavar="FECHA",
                   help="solo partidas desde esta fecha (AAAA-MM-DD)")
    p.add_argument("--no-timeline", action="store_true",
                   help="no descarga el timeline (más rápido, pero sin métricas por minuto)")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="solo partidas de esa cola: solo (420), flex (440), normal (400), "
                        "blind (430), quickplay (490) o un número de cola")

    p = sub.add_parser("analyze", help="reconstruye data/games.csv y los reportes en reports/")
    p.add_argument("--todas-las-colas", action="store_true",
                   help="incluye colas que no son de la Grieta del Invocador")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="analiza solo esa cola: solo, flex, normal, blind, quickplay o un número")

    p = sub.add_parser("game", help="muestra el reporte de una partida")
    p.add_argument("match_id", help="ID de la partida (por ejemplo LA1_123456) o 'ultima'")
    p.add_argument("--todas-las-colas", action="store_true")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA")

    p = sub.add_parser("campeon", help="muestra qué decide tus partidas con un campeón")
    p.add_argument("nombre", help="nombre del campeón (por ejemplo Ahri)")
    p.add_argument("--todas-las-colas", action="store_true")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA")
    return parser


# ---------------------------------------------------------------- commands
def cmd_sync(args, cfg) -> int:
    if not cfg.api_key:
        raise MissingApiKeyError(
            "Falta RIOT_API_KEY en el archivo .env. Copia .env.example a .env y pega tu API key "
            "(se obtiene en developer.riotgames.com; las keys de desarrollo duran 24 h)."
        )
    if not cfg.riot_id:
        raise ConfigError("Falta RIOT_ID en el archivo .env (formato Nombre#TAG).")
    client = RiotClient(cfg.api_key, region=cfg.region)
    store = Store(cfg.db_path)
    try:
        run_sync(client, store, cfg.riot_id, max_matches=args.max_matches, since=args.since,
                 with_timeline=not args.no_timeline, queue=args.cola)
    finally:
        store.close()
    print("Para analizar lo descargado ejecuta: python -m lolan analyze")
    return 0


def _load(cfg, include_all: bool, queue: int | None = None):
    store = Store(cfg.db_path)
    try:
        if store.count_matches() == 0:
            return None
        return an.load_games(store, include_all_queues=include_all, queue=queue)
    finally:
        store.close()


def cmd_analyze(args, cfg) -> int:
    games = _load(cfg, args.todas_las_colas, args.cola)
    if games is None:
        print(EMPTY_DB_MESSAGE)
        return 0
    if not games:
        print("Hay partidas guardadas pero ninguna es analizable (remakes o colas que no son de la "
              "Grieta del Invocador). Usa --todas-las-colas para incluir otras colas.")
        return 0
    info = report.write_reports(games, cfg.reports_dir, cfg.data_dir)
    o = an.group_stats(games)
    print(f"Partidas analizadas: {info['games']} | winrate {an.fmt_pct(o['winrate'])} | "
          f"KDA {o['kda']:.2f}")
    print(f"CSV: {info['csv']}")
    print(f"Resumen: {cfg.reports_dir / 'resumen.md'}")
    print(f"Campeones: {info['champions']} archivos en {cfg.reports_dir / 'campeones'}")
    print(f"Partidas: {info['cards']} reportes en {cfg.reports_dir / 'partidas'}")
    return 0


def cmd_game(args, cfg) -> int:
    games = _load(cfg, args.todas_las_colas, args.cola)
    if games is None:
        print(EMPTY_DB_MESSAGE)
        return 0
    if not games:
        print("No hay partidas analizables.")
        return 0
    wanted = args.match_id.strip()
    if wanted.lower() in ("ultima", "última", "last"):
        game = games[-1]
    else:
        game = next((g for g in games if str(g["match_id"]).lower() == wanted.lower()), None)
        if game is None:
            print(f"No se encontró la partida {wanted} entre las partidas analizables "
                  "(puede ser un remake o de otra cola; prueba --todas-las-colas).")
            return 1
    print(an.game_report_card(game, games)["text"])
    return 0


def cmd_campeon(args, cfg) -> int:
    games = _load(cfg, args.todas_las_colas, args.cola)
    if games is None:
        print(EMPTY_DB_MESSAGE)
        return 0
    if not an.games_for_champion(games, args.nombre):
        known = sorted({g["champion"] for g in games})
        print(f"No hay partidas con '{args.nombre}'. Campeones jugados: {', '.join(known) or '(ninguno)'}.")
        return 1
    print(report.render_champion(games, args.nombre))
    return 0


COMMANDS = {"sync": cmd_sync, "analyze": cmd_analyze, "game": cmd_game, "campeon": cmd_campeon}


def main(argv=None) -> int:
    # Verify TLS against the OS certificate store, so antivirus/proxy roots trusted by
    # Windows (e.g. Norton's HTTPS scanning) don't break requests with CERTIFICATE_VERIFY_FAILED.
    truststore.inject_into_ssl()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        cfg = load_config()
        return COMMANDS[args.command](args, cfg)
    except (ConfigError, RiotError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo descargado ya está guardado; vuelve a ejecutar el comando para continuar.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
