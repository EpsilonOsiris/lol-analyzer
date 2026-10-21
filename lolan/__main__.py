"""Command line interface: python -m lolan {sync,rekey,analyze,game,campeon,jugadores,web}."""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from datetime import datetime

import truststore

from . import analyze as an
from . import report
from .config import ConfigError, load_config
from .players import load_roster, normalize_riot_id
from .riot import MissingApiKeyError, NotFoundError, RiotClient, RiotError
from .rekey import RekeyError
from .rekey import rekey as run_rekey
from .store import Store
from .sync import sync as run_sync
from .sync import sync_players

EMPTY_DB_MESSAGE = (
    "No hay partidas en la base de datos. Ejecuta primero: python -m lolan sync "
    "(necesitas RIOT_API_KEY y RIOT_ID en .env)."
)
NO_MAIN_MESSAGE = (
    "No se pudo determinar tu jugador principal: el RIOT_ID de .env ({riot_id}) aún no está en la "
    "base de datos. Ejecuta primero: python -m lolan sync"
)


def _parse_date(text: str):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("la fecha debe tener el formato AAAA-MM-DD (por ejemplo, 2024-01-31)")


QUEUE_ALIASES = {"solo": 420, "soloq": 420, "flex": 440, "normal": 400, "blind": 430,
                 "quickplay": 490}


# Folder/file name used for a queue's reports (analyze --cola).
QUEUE_SLUGS = {420: "solo", 440: "flex", 400: "normal", 430: "blind", 490: "quickplay"}


def queue_slug(queue: int) -> str:
    return QUEUE_SLUGS.get(queue, f"cola-{queue}")


def _parse_queue(text: str) -> int:
    key = text.strip().casefold()
    if key in QUEUE_ALIASES:
        return QUEUE_ALIASES[key]
    try:
        return int(key)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "la cola debe ser solo, flex, normal, blind, quickplay o un número de cola (por ejemplo, 420)")


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("el puerto debe ser un número entre 1 y 65535")
    return port


def _add_player_arg(parser, help: str) -> None:
    parser.add_argument("--jugador", metavar="Nombre#TAG",
                        help=f"{help} (por defecto, tú: el RIOT_ID de .env)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lolan",
        description="Descarga tus partidas de League of Legends desde la API de Riot y analiza "
                    "qué haces bien y mal en cada partida y en general. También las de tu clan.",
    )
    sub = parser.add_subparsers(dest="command", metavar="comando")

    p = sub.add_parser("sync", help="descarga las partidas nuevas (se puede interrumpir y reanudar)")
    p.add_argument("--max", type=int, metavar="N", dest="max_matches",
                   help="descarga como máximo N partidas nuevas (por jugador)")
    p.add_argument("--since", type=_parse_date, metavar="FECHA",
                   help="solo partidas desde esta fecha (AAAA-MM-DD)")
    p.add_argument("--no-timeline", action="store_true",
                   help="no descarga el timeline (más rápido, pero sin métricas por minuto)")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="solo partidas de esa cola: solo (420), flex (440), normal (400), "
                        "blind (430), quickplay (490) o un número de cola")
    who = p.add_mutually_exclusive_group()
    _add_player_arg(who, "descarga las partidas de este jugador registrado")
    who.add_argument("--clan", action="store_true",
                     help="descarga las partidas de todos los jugadores registrados, uno tras otro")

    sub.add_parser("rekey", help="actualiza tus datos después de cambiar de API key "
                                 "(Riot cifra los identificadores por app; se puede interrumpir y reanudar)")

    p = sub.add_parser("analyze", help="reconstruye data/games.csv y los reportes en reports/")
    p.add_argument("--todas-las-colas", action="store_true",
                   help="incluye colas que no son de la Grieta del Invocador")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="analiza solo esa cola: solo, flex, normal, blind, quickplay o un número")
    who = p.add_mutually_exclusive_group()
    _add_player_arg(who, "analiza a este jugador registrado")
    who.add_argument("--clan", action="store_true",
                     help="genera los reportes de todos los jugadores registrados")

    p = sub.add_parser("game", help="muestra el reporte de una partida")
    p.add_argument("match_id", help="ID de la partida (por ejemplo LA1_123456) o 'ultima'")
    p.add_argument("--todas-las-colas", action="store_true")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA")
    _add_player_arg(p, "busca la partida entre las de este jugador registrado")

    p = sub.add_parser("campeon", help="muestra qué decide tus partidas con un campeón")
    p.add_argument("nombre", help="nombre del campeón (por ejemplo Ahri)")
    p.add_argument("--todas-las-colas", action="store_true")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA")
    _add_player_arg(p, "usa las partidas de este jugador registrado")

    p = sub.add_parser("jugadores", help="lista, agrega o quita jugadores (los de tu clan)")
    acts = p.add_subparsers(dest="accion", metavar="acción")
    acts.add_parser("listar", help="lista los jugadores registrados (es la acción por defecto)")
    q = acts.add_parser("agregar", help="registra a un jugador por su Riot ID (usa tu misma API key)")
    q.add_argument("riot_id", metavar="Nombre#TAG")
    q = acts.add_parser("quitar", help="quita a un jugador del registro (sus partidas se conservan)")
    q.add_argument("riot_id", metavar="Nombre#TAG")

    p = sub.add_parser("composiciones",
                       help="analiza qué compones con tu clan (por línea, duplas, plan de pelea) y recomienda picks (escribe reports/composiciones.md)")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="solo esa cola: solo, flex, normal, blind, quickplay o un número (por defecto, todas)")
    p.add_argument("--todos-los-adc", action="store_true",
                   help="la recomendación incluye todos los ADC clasificados, no solo los de tu pool")

    p = sub.add_parser("web", help="abre el panel web local (solo escucha en 127.0.0.1)")
    p.add_argument("--puerto", type=_port, metavar="N", default=8765,
                   help="puerto del panel (por defecto 8765)")
    p.add_argument("--no-abrir", action="store_true", help="no abre el navegador automáticamente")
    return parser


# ----------------------------------------------------------------- helpers
def _client(cfg) -> RiotClient:
    if not cfg.api_key:
        raise MissingApiKeyError(
            "Falta RIOT_API_KEY en el archivo .env. Copia .env.example a .env y pega tu API key "
            "(se obtiene en developer.riotgames.com; las keys de desarrollo duran 24 h)."
        )
    return RiotClient(cfg.api_key, region=cfg.region)


def _player_flag(player) -> str:
    """Command-line way of pointing at a player (nothing for the main one)."""
    return "" if player.is_main else f' --jugador "{player.riot_id}"'


def _resolve_player(roster, ref, cfg):
    """The player a command works on: `ref` (Riot ID) or, by default, the main player."""
    if ref is None:
        if roster.main is None:
            raise ValueError(NO_MAIN_MESSAGE.format(riot_id=cfg.riot_id or "sin configurar"))
        return roster.main
    player = roster.by_ref(ref)
    if player is None:
        known = ", ".join(p.riot_id for p in roster.players) or "(ninguno)"
        raise ValueError(
            f"No hay ningún jugador registrado como «{ref}». Registrados: {known}. "
            f'Para agregarlo: python -m lolan jugadores agregar "{ref}"')
    return player


def _load(cfg, args):
    """(games, player, roster) for --jugador (default: the main player); None after printing why not."""
    store = Store(cfg.db_path)
    try:
        if store.count_matches() == 0:
            print(EMPTY_DB_MESSAGE)
            return None
        roster = load_roster(store, cfg.riot_id)
        player = _resolve_player(roster, args.jugador, cfg)
        if store.count_matches(player.puuid) == 0:
            print(f"No hay partidas guardadas de {player.riot_id}. Ejecuta primero: "
                  f"python -m lolan sync{_player_flag(player)}")
            return None
        games = an.load_games(store, player.puuid, include_all_queues=args.todas_las_colas,
                              queue=args.cola)
        return games, player, roster
    finally:
        store.close()


def report_paths(cfg, player, roster, queue: int | None):
    """(reports_dir, csv_name) for a player and an optional queue.

    The main player keeps the original layout (reports/, reports/<cola>/, data/games[-<cola>].csv);
    everyone else goes under reports/jugadores/<slug>/ and data/games-<slug>[-<cola>].csv.
    """
    qslug = queue_slug(queue) if queue is not None else None
    if player.is_main:
        reports_dir = cfg.reports_dir / qslug if qslug else cfg.reports_dir
        return reports_dir, (f"games-{qslug}.csv" if qslug else "games.csv")
    slug = roster.slug(player)
    base = cfg.reports_dir / "jugadores" / slug
    return (base / qslug if qslug else base), (f"games-{slug}-{qslug}.csv" if qslug else f"games-{slug}.csv")


# ---------------------------------------------------------------- commands
def cmd_sync(args, cfg) -> int:
    client = _client(cfg)
    options = dict(max_matches=args.max_matches, since=args.since,
                   with_timeline=not args.no_timeline, queue=args.cola)
    store = Store(cfg.db_path)
    try:
        roster = load_roster(store, cfg.riot_id)
        if args.clan:
            # You first (even if not registered yet), then everyone else registered.
            riot_ids = [cfg.riot_id] if cfg.riot_id else []
            riot_ids += [p.riot_id for p in roster.players
                         if p.riot_id.casefold() not in {r.casefold() for r in riot_ids}]
            if not riot_ids:
                raise ConfigError("No hay jugadores que sincronizar: configura RIOT_ID en .env o "
                                  'agrega jugadores con: python -m lolan jugadores agregar "Nombre#TAG"')
            outcomes = sync_players(client, store, riot_ids, main_riot_id=cfg.riot_id, **options)
            print("Para analizar lo descargado ejecuta: python -m lolan analyze --clan")
            return 1 if any(o.error for o in outcomes) else 0

        if args.jugador:
            player = roster.by_ref(args.jugador)
            if player is not None:
                riot_id = player.riot_id
            elif cfg.riot_id and args.jugador.strip().casefold() == cfg.riot_id.strip().casefold():
                riot_id = cfg.riot_id          # you, before your first sync
            else:
                _resolve_player(roster, args.jugador, cfg)   # raises the "not registered" error
        else:
            if not cfg.riot_id:
                raise ConfigError("Falta RIOT_ID en el archivo .env (formato Nombre#TAG).")
            riot_id = cfg.riot_id
        is_main = bool(cfg.riot_id) and riot_id.strip().casefold() == cfg.riot_id.strip().casefold()
        run_sync(client, store, riot_id, main=is_main, **options)
    finally:
        store.close()
    suffix = "" if is_main else f' --jugador "{riot_id}"'
    print(f"Para analizar lo descargado ejecuta: python -m lolan analyze{suffix}")
    return 0


def cmd_rekey(args, cfg) -> int:
    client = _client(cfg)
    store = Store(cfg.db_path)
    try:
        result = run_rekey(client, store)
    finally:
        store.close()
    return 0 if result.ok else 1


def _write_player_reports(cfg, player, roster, games, queue) -> None:
    reports_dir, csv_name = report_paths(cfg, player, roster, queue)
    info = report.write_reports(games, reports_dir, cfg.data_dir, csv_name)
    o = an.group_stats(games)
    print(f"Partidas analizadas: {info['games']} | winrate {an.fmt_pct(o['winrate'])} | "
          f"KDA {o['kda']:.2f}")
    print(f"CSV: {info['csv']}")
    print(f"Resumen: {reports_dir / 'resumen.md'}")
    print(f"Campeones: {info['champions']} archivos en {reports_dir / 'campeones'}")
    print(f"Partidas: {info['cards']} reportes en {reports_dir / 'partidas'}")


def _analyze_clan(args, cfg) -> int:
    store = Store(cfg.db_path)
    try:
        if store.count_matches() == 0:
            print(EMPTY_DB_MESSAGE)
            return 0
        roster = load_roster(store, cfg.riot_id)
        if not roster.players:
            print("No hay jugadores registrados. Agrega a tu clan con: "
                  'python -m lolan jugadores agregar "Nombre#TAG"')
            return 0
        for i, player in enumerate(roster.players, 1):
            print(f"=== {player.riot_id}{' (tú)' if player.is_main else ''} "
                  f"({i}/{len(roster.players)}) ===")
            if store.count_matches(player.puuid) == 0:
                print("Sin partidas guardadas. Descárgalas con: "
                      f"python -m lolan sync{_player_flag(player)}")
                continue
            games = an.load_games(store, player.puuid, include_all_queues=args.todas_las_colas,
                                  queue=args.cola)
            if not games:
                print("Hay partidas guardadas pero ninguna es analizable con estos filtros.")
                continue
            _write_player_reports(cfg, player, roster, games, args.cola)
    finally:
        store.close()
    return 0


def cmd_analyze(args, cfg) -> int:
    if args.clan:
        return _analyze_clan(args, cfg)
    loaded = _load(cfg, args)
    if loaded is None:
        return 0
    games, player, roster = loaded
    if not games:
        print("Hay partidas guardadas pero ninguna es analizable (remakes o colas que no son de la "
              "Grieta del Invocador). Usa --todas-las-colas para incluir otras colas.")
        return 0
    # Each queue gets its own folder and CSV so analyzing one doesn't overwrite another.
    _write_player_reports(cfg, player, roster, games, args.cola)
    return 0


def cmd_game(args, cfg) -> int:
    loaded = _load(cfg, args)
    if loaded is None:
        return 0
    games = loaded[0]
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
    loaded = _load(cfg, args)
    if loaded is None:
        return 0
    games = loaded[0]
    if not an.games_for_champion(games, args.nombre):
        known = sorted({g["champion"] for g in games})
        print(f"No hay partidas con '{args.nombre}'. Campeones jugados: {', '.join(known) or '(ninguno)'}.")
        return 1
    print(report.render_champion(games, args.nombre))
    return 0


def cmd_jugadores(args, cfg) -> int:
    action = args.accion or "listar"
    if action == "agregar":
        return _jugadores_agregar(args, cfg)
    store = Store(cfg.db_path)
    try:
        roster = load_roster(store, cfg.riot_id)
        if action == "quitar":
            player = roster.by_ref(args.riot_id)
            if player is None:
                known = ", ".join(p.riot_id for p in roster.players) or "(ninguno)"
                raise ValueError(f"No hay ningún jugador registrado como «{args.riot_id}». "
                                 f"Registrados: {known}.")
            if player.is_main:
                raise ValueError("No se puede quitar al jugador principal (el RIOT_ID de .env).")
            store.remove_player(player.puuid)
            print(f"Jugador quitado: {player.riot_id}. Sus partidas siguen guardadas "
                  "(otros jugadores pueden compartirlas).")
            return 0
        if not roster.players:
            print("No hay jugadores registrados. Se registran al sincronizar o con: "
                  'python -m lolan jugadores agregar "Nombre#TAG"')
            return 0
        counts = store.match_counts()
        width = max(len(p.riot_id) for p in roster.players)
        print(f"Jugadores registrados ({len(roster.players)}):")
        for p in roster.players:
            print(f"  {p.riot_id.ljust(width)}  {counts.get(p.puuid, 0):>5} partidas guardadas"
                  + ("  (tú)" if p.is_main else ""))
        if cfg.riot_id and roster.main is None:
            print(f"Tu RIOT_ID ({cfg.riot_id}) aún no está registrado; se registra en el primer sync.")
    finally:
        store.close()
    return 0


def _jugadores_agregar(args, cfg) -> int:
    name, tag = normalize_riot_id(args.riot_id)
    client = _client(cfg)
    try:
        puuid = client.get_puuid(name, tag)
    except NotFoundError:
        raise NotFoundError(f"No se encontró la cuenta {name}#{tag}. Revisa el nombre y el TAG.") from None
    store = Store(cfg.db_path)
    try:
        known = store.get_player(puuid=puuid)
        existed = known is not None
        riot_id = f"{name}#{tag}"
        if known and known.riot_id.casefold() == riot_id.casefold():
            riot_id = known.riot_id            # same name typed in another case: keep how it was saved
        is_main = bool(cfg.riot_id) and riot_id.casefold() == cfg.riot_id.strip().casefold()
        player = store.add_player(puuid, riot_id, is_main=is_main)
        stored = store.count_matches(puuid)
    finally:
        store.close()
    print(f"{'Ya estaba registrado' if existed else 'Jugador agregado'}: {player.riot_id}. "
          f"Partidas suyas ya guardadas (compartidas con otros jugadores): {stored}.")
    print(f'Para descargar sus partidas: python -m lolan sync --jugador "{player.riot_id}"')
    return 0


def cmd_composiciones(args, cfg) -> int:
    from . import comps
    from .settings import read_settings

    store = Store(cfg.db_path)
    try:
        if store.count_matches() == 0:
            print(EMPTY_DB_MESSAGE)
            return 0
        roster = load_roster(store, cfg.riot_id)
        if roster.main is None:
            raise ValueError(NO_MAIN_MESSAGE.format(riot_id=cfg.riot_id or "sin configurar"))
        main = roster.main
        st = read_settings(cfg.root)
        clan = comps.ClanConfig.build(st.clan_prefix, st.clan_aliases,
                                      {p.puuid: p.riot_id for p in roster.players})
        records = comps.resolve_people(
            comps.build_records(store.iter_matches(main.puuid), main.puuid, queue=args.cola), clan)
    finally:
        store.close()
    if not records:
        print("No hay partidas analizables" + (f" de la cola {queue_slug(args.cola)}" if args.cola else "")
              + ". Ejecuta primero: python -m lolan sync")
        return 0
    analysis = comps.analyze_all(records)
    lineup = analysis["common_lineup"]
    draft = {role: (person, None) for role, person in lineup.items() if role != "BOTTOM"}
    ranking = comps.recommend(draft, records, include_all=args.todos_los_adc)
    notes = comps.draft_notes(draft, records)
    plan_draft = comps.recommend_draft(draft, records, include_all=args.todos_los_adc)
    cola_text = f"cola {queue_slug(args.cola)}" if args.cola else "todas las colas"
    print(comps.render_summary(analysis, ranking, lineup, notes, cola_text, plan_draft))
    reports_dir = cfg.reports_dir / queue_slug(args.cola) if args.cola else cfg.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "composiciones.md"
    path.write_text(comps.render_markdown(analysis, ranking, lineup, cola_text, notes, plan_draft),
                    encoding="utf-8")
    print(f"\nReporte completo: {path}")
    return 0


def cmd_web(args, cfg) -> int:
    from .web import create_app  # Flask is only needed for this command

    app = create_app(cfg.root)
    url = f"http://127.0.0.1:{args.puerto}/"
    print(f"Panel web en {url} (solo accesible desde este equipo). Ctrl+C para detenerlo.")
    if not args.no_abrir:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        app.run(host="127.0.0.1", port=args.puerto, threaded=True, debug=False, use_reloader=False)
    except OSError as exc:
        print(f"Error: no se pudo abrir el puerto {args.puerto} ({exc.strerror or exc}). "
              "Prueba con otro: python -m lolan web --puerto 8800", file=sys.stderr)
        return 1
    print("Panel detenido.")
    return 0


COMMANDS = {"sync": cmd_sync, "rekey": cmd_rekey, "analyze": cmd_analyze, "game": cmd_game, "campeon": cmd_campeon,
            "jugadores": cmd_jugadores, "composiciones": cmd_composiciones, "web": cmd_web}


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
    except (ConfigError, RiotError, RekeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo descargado ya está guardado; vuelve a ejecutar el comando para continuar.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
