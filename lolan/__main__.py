"""Command line interface: python -m lolan {sync,rekey,analyze,game,campeon,jugadores,composiciones,rivales,lineas,muestras,web}."""
from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import Counter
import webbrowser
from datetime import datetime

import truststore

from . import analyze as an
from . import report
from .config import ConfigError, load_config
from .players import load_roster, normalize_riot_id
from .riot import Limiters, MissingApiKeyError, NotFoundError, RiotClient, RiotError
from .rekey import RekeyError
from .rekey import rekey as run_rekey
from .store import Store
from .sync import sync as run_sync
from .sync import sync_players, update_lanes

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
                 "quickplay": 490, "premade": 710}


# Folder/file name used for a queue's reports (analyze --cola).
QUEUE_SLUGS = {420: "solo", 440: "flex", 400: "normal", 430: "blind", 490: "quickplay", 710: "premade"}


def queue_slug(queue: int) -> str:
    return QUEUE_SLUGS.get(queue, f"cola-{queue}")


def _parse_line(text: str) -> str:
    from .comps import parse_line
    role = parse_line(text)
    if role is None:
        raise argparse.ArgumentTypeError("la línea debe ser auto, top, jungla, mid, adc o soporte")
    return role


def _parse_line_or_all(text: str) -> str:
    """auto / todas / top / jungla / mid / adc / soporte -> "auto", "todas" or the role."""
    if text.strip().casefold() == "todas":
        return "todas"
    from .comps import parse_line
    role = parse_line(text)
    if role is None:
        raise argparse.ArgumentTypeError("la línea debe ser auto, todas, top, jungla, mid, adc o soporte")
    return role


def _parse_queue(text: str) -> int:
    key = text.strip().casefold()
    if key in QUEUE_ALIASES:
        return QUEUE_ALIASES[key]
    try:
        return int(key)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "la cola debe ser solo, flex, normal, blind, quickplay o un número de cola (por ejemplo, 420)")


def _positive_float(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        value = 0.0
    if value <= 0:
        raise argparse.ArgumentTypeError("debe ser un número mayor que 0 (por ejemplo, 2 o 0.5)")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError("debe ser un número entero mayor que 0")
    return value


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
                        "blind (430), quickplay (490), premade (710) o un número de cola")
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
    p.add_argument("--linea", type=_parse_line_or_all, metavar="LINEA",
                   help="línea a analizar: top, jungla, mid, adc, soporte o todas (por defecto, auto: la que "
                        "más juega el jugador en esa cola)")
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
    p.add_argument("--linea", type=_parse_line_or_all, metavar="LINEA",
                   help="línea a analizar: top, jungla, mid, adc, soporte o todas (por defecto, auto: la que "
                        "más juega el jugador en esa cola)")
    _add_player_arg(p, "usa las partidas de este jugador registrado")

    p = sub.add_parser("jugadores", help="lista, agrega o quita jugadores (los de tu clan)")
    acts = p.add_subparsers(dest="accion", metavar="acción")
    acts.add_parser("listar", help="lista los jugadores registrados (es la acción por defecto)")
    q = acts.add_parser("agregar", help="registra a un jugador por su Riot ID (usa tu misma API key)")
    q.add_argument("riot_id", metavar="Nombre#TAG")
    q = acts.add_parser("quitar", help="quita a un jugador del registro (sus partidas se conservan)")
    q.add_argument("riot_id", metavar="Nombre#TAG")

    p = sub.add_parser("composiciones",
                       help="analiza qué compones con tu clan (por línea, duplas, plan de pelea) desde tu línea y recomienda picks (escribe reports/composiciones.md)")
    p.add_argument("--cola", type=_parse_queue, metavar="COLA",
                   help="solo esa cola: solo, flex, normal, blind, quickplay o un número (por defecto, todas)")
    p.add_argument("--linea", type=_parse_line, metavar="LINEA",
                   help="tu línea: top, jungla, mid, adc o soporte (por defecto, auto: la que más juegas en esa cola)")
    p.add_argument("--todos-los-adc", action="store_true",
                   help="si juegas de ADC, la recomendación incluye todos los ADC clasificados, no solo los de tu pool")

    p = sub.add_parser("rivales",
                       help="muestra quién está en tu partida actual (Spectator de Riot): campeones, rango, maestría e historial")
    _add_player_arg(p, "busca la partida de este jugador registrado")

    p = sub.add_parser("lineas",
                       help="cómo van las líneas: tu campeón contra el rival de su línea (gana la línea, "
                            "en qué niveles, picos de poder); --reconstruir las calcula de tus partidas")
    p.add_argument("campeon", nargs="?", help="campeón a consultar (por ejemplo Diana)")
    p.add_argument("--vs", metavar="Rival", help="el campeón rival de la línea (por ejemplo Akali)")
    p.add_argument("--linea", metavar="LINEA",
                   help="top, jungla, mid, adc o soporte (por defecto, la línea donde más sale el campeón)")
    p.add_argument("--persona", metavar="Nombre",
                   help="solo las partidas de este jugador registrado (por defecto, todas las muestras)")
    p.add_argument("--personas", action="store_true",
                   help="lista los miembros del clan que tienen líneas calculadas")
    p.add_argument("--parches", metavar="N|todos", default=None,
                   help="cuántos parches recientes usar (por defecto 3) o 'todos'")
    p.add_argument("--reconstruir", action="store_true",
                   help="vuelve a calcular las líneas de todas tus partidas guardadas (data/lineas/)")

    p = sub.add_parser("muestras",
                       help="descarga una muestra Diamante+ (KR y EUW) con tu API key para saber cómo van las "
                            "líneas de los campeones de tu clan; se puede interrumpir y reanudar")
    p.add_argument("--horas", type=_positive_float, metavar="H",
                   help="se detiene después de H horas (por defecto, sigue hasta Ctrl+C)")
    p.add_argument("--regiones", metavar="kr,euw",
                   help="regiones a muestrear, separadas por coma (por defecto kr,euw: así no se gasta la "
                        "cuota de tu región)")
    p.add_argument("--max-partidas", type=_positive_int, metavar="N", dest="max_partidas",
                   help="se detiene después de descargar N partidas")
    p.add_argument("--solo-pool", action="store_true", dest="solo_pool",
                   help="solo jugadores de los campeones de tu clan (sin las ligas Diamante+ en general)")
    p.add_argument("--estado", action="store_true",
                   help="muestra el estado de la descarga (data/lineas/descarga.json) y termina")

    p = sub.add_parser("arquetipos",
                       help="decide con la muestra Diamante+ los tipos de campeón dudosos (early o farm, "
                            "bruiser o split, asesino o melee); sin --decidir solo muestra la evidencia")
    p.add_argument("--decidir", action="store_true",
                   help="escribe las decisiones en lolan/data/champion_classes.json (reinicia el panel después)")
    p.add_argument("--linea", type=_parse_line, metavar="LINEA",
                   help="solo esa línea: top, jungla, mid, adc o soporte (por defecto, todas)")

    sub.add_parser("grabar-seleccion",
                   help="graba lo que muestra el cliente de League en la selección de campeones "
                        "(en data/capturas/); Ctrl+C para detenerlo")

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
    store = Store(cfg.db_path)
    try:
        update_lanes(store, print)
    finally:
        store.close()
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


def _line_note(line, scoped, games) -> str | None:
    """"Línea: Soporte (273 de 440 partidas)" when the reports leave games out or are not a laner's."""
    if len(scoped) == len(games) and an.profile_for_games(scoped).key == "lane":
        return None
    name = an.role_name(line) if line else "todas"
    return f"Línea: {name} ({len(scoped)} de {len(games)} partidas)"


def _write_player_reports(cfg, player, roster, games, queue, linea=None) -> None:
    reports_dir, csv_name = report_paths(cfg, player, roster, queue)
    line = an.resolve_line(linea, games)
    scoped = an.games_in_line(games, line)
    info = report.write_reports(scoped, reports_dir, cfg.data_dir, csv_name, all_games=games)
    o = an.group_stats(scoped)
    print(f"Partidas analizadas: {info['games']} | winrate {an.fmt_pct(o['winrate'])} | "
          f"KDA {o['kda']:.2f}")
    if (note := _line_note(line, scoped, games)):
        print(note + ". Se cambia con --linea.")
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
            _write_player_reports(cfg, player, roster, games, args.cola, args.linea)
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
    _write_player_reports(cfg, player, roster, games, args.cola, args.linea)
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
    line = an.resolve_line(args.linea, games)
    scoped = an.games_in_line(games, line)
    if not an.games_for_champion(scoped, args.nombre):
        known = sorted({g["champion"] for g in scoped})
        elsewhere = an.games_for_champion(games, args.nombre)
        if elsewhere and line:
            print(f"No hay partidas con '{args.nombre}' en la línea {an.role_name(line)}, pero sí "
                  f"{len(elsewhere)} en otras líneas: usa --linea todas.")
        else:
            print(f"No hay partidas con '{args.nombre}'. Campeones jugados: {', '.join(known) or '(ninguno)'}.")
        return 1
    print(report.render_champion(scoped, args.nombre))
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
    main_role = args.linea if args.linea not in (None, "auto") else comps.main_line(records)
    analysis = comps.analyze_all(records, main_role=main_role)
    lineup = analysis["common_lineup"]
    draft = {role: (person, None) for role, person in lineup.items() if role != main_role}
    ranking = (comps.recommend(draft, records, include_all=args.todos_los_adc)
               if main_role == "BOTTOM" else None)
    notes = comps.draft_notes(draft, records, main_role=main_role)
    plan_draft = comps.recommend_draft(draft, records, include_all=args.todos_los_adc, main_role=main_role)
    cola_text = f"cola {queue_slug(args.cola)}" if args.cola else "todas las colas"
    print(comps.render_summary(analysis, ranking, lineup, notes, cola_text, plan_draft))
    reports_dir = cfg.reports_dir / queue_slug(args.cola) if args.cola else cfg.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "composiciones.md"
    path.write_text(comps.render_markdown(analysis, ranking, lineup, cola_text, notes, plan_draft),
                    encoding="utf-8")
    print(f"\nReporte completo: {path}")
    return 0


def cmd_rivales(args, cfg) -> int:
    from . import comps
    from . import scout as sc
    from .ddragon import DDragon
    from .settings import read_settings

    if not cfg.api_key:
        raise MissingApiKeyError(
            "Falta RIOT_API_KEY en el archivo .env. Copia .env.example a .env y pega tu API key "
            "(se obtiene en developer.riotgames.com; las keys de desarrollo duran 24 h).")
    store = Store(cfg.db_path)
    try:
        roster = load_roster(store, cfg.riot_id)
    finally:
        store.close()
    if args.jugador:
        player = roster.by_ref(args.jugador)
        if player is not None:
            target, is_main = player.riot_id, player.is_main
        elif cfg.riot_id and args.jugador.strip().casefold() == cfg.riot_id.strip().casefold():
            target, is_main = cfg.riot_id, True
        else:
            _resolve_player(roster, args.jugador, cfg)       # raises the "not registered" error
            return 1
    else:
        if not cfg.riot_id:
            raise ConfigError("Falta RIOT_ID en el archivo .env (formato Nombre#TAG).")
        target, is_main = cfg.riot_id, True
    limiters = Limiters()          # one limiter per Riot host, like the panel
    api = sc.ScoutApi(RiotClient(cfg.api_key, region=cfg.platform, limiter=limiters.for_host(cfg.platform)),
                      RiotClient(cfg.api_key, region=cfg.region, limiter=limiters.for_host(cfg.region)))
    puuid = sc.PuuidResolver().resolve(target, cfg.api_key, api.account)
    raw = api.active_game(puuid)
    game = sc.parse_spectator(raw, puuid, target) if raw else None
    if game is None:
        print(f"{target} no está en una partida en este momento. Riot publica la partida unos "
              "segundos después de empezar la pantalla de carga; vuelve a intentarlo.")
        return 0
    st = read_settings(cfg.root)
    clan = comps.ClanConfig.build(st.clan_prefix, st.clan_aliases, {p.puuid: p.riot_id for p in roster.players})
    store = Store(cfg.db_path)
    try:
        report = sc.ScoutRun(api, store, sc.LocalHistory(lambda: Store(cfg.db_path)), clan,
                             viewer_riot_id=target, viewer_is_main=is_main,
                             tracked_ids=[p.riot_id for p in roster.players]).run(game)
    finally:
        store.close()
    dd = DDragon(cfg.data_dir / "ddragon")
    champs, spells, perks = sc.wanted_assets(report)
    dd.prepare((None,), (), wait=10, minimap=False, champion_keys=champs, spells=spells, perks=perks)
    print(sc.render_text(report, sc.Assets(dd)))
    return 0


def _lineas_reconstruir(cfg) -> int:
    from . import samples
    from .lanephase import SKIP_LABELS

    store = Store(cfg.db_path)
    try:
        total = store.count_matches()
        if total == 0:
            print(EMPTY_DB_MESSAGE)
            return 0
        started = time.monotonic()
        done = samples.update_from_lol(store, cfg.data_dir, cfg.root, full=True)
        elapsed = time.monotonic() - started
    finally:
        store.close()
    skipped = done["skipped"]
    print(f"Partidas guardadas: {total} | analizadas: {done['matches'] - sum(skipped.values())} "
          f"| filas de línea: {done['rows']}")
    for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  omitidas ({SKIP_LABELS.get(reason, reason)}): {n}")
    if done["sin_timeline"]:
        print(f"  sin timeline: {done['sin_timeline']}")
    print(f"Agregados: {done['index']['files']} archivos en {cfg.data_dir / 'lineas' / 'duelos'}")
    print(f"Tiempo: {elapsed:.1f} s")
    return 0


def _patch_text(patches) -> str:
    if len(patches) > 4:
        return f"{patches[0]} a {patches[-1]} ({len(patches)} parches)"
    return ", ".join(patches) or "-"


def _lineas_personas(cfg) -> int:
    from . import samples

    people = samples.LineStore(cfg.data_dir / "lineas").people()
    if not people:
        print("Ningún miembro del clan tiene líneas calculadas. Ejecuta: python -m lolan lineas --reconstruir")
        return 0
    print(f"Miembros del clan con líneas ({len(people)}), filas de línea:")
    for name, n in sorted(people.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name}: {n}")
    return 0


def cmd_lineas(args, cfg) -> int:
    from . import samples

    if args.reconstruir:
        return _lineas_reconstruir(cfg)
    if args.personas:
        return _lineas_personas(cfg)
    if not args.campeon:
        print("Indica un campeón (por ejemplo: python -m lolan lineas Diana --vs Akali --linea mid) "
              "o usa --reconstruir para calcular las líneas de tus partidas.")
        return 0
    store = samples.LineStore(cfg.data_dir / "lineas")
    rows = store.read_rows()
    if not rows:
        print("Aún no hay líneas calculadas. Ejecuta primero: python -m lolan lineas --reconstruir")
        return 0
    champ = store.champion_id(args.campeon)
    if champ is None:
        raise ValueError(f"No conozco al campeón «{args.campeon}» en las muestras (solo los que han "
                         "salido en las partidas analizadas).")
    rival = None
    if args.vs:
        rival = store.champion_id(args.vs)
        if rival is None:
            raise ValueError(f"No conozco al campeón «{args.vs}» en las muestras.")
    if args.linea:
        role = samples.parse_role(args.linea)
        if role is None:
            raise ValueError("La línea debe ser top, jungla, mid, adc o soporte.")
    else:
        counts = Counter(r["role"] for r in rows if r["champ"] == champ)
        if not counts:
            print(f"No hay líneas de {store.champion_name(champ)} en las muestras.")
            return 0
        role = counts.most_common(1)[0][0]
    person = None
    if args.persona:
        from .settings import read_settings
        person = store.find_person(args.persona, read_settings(cfg.root).clan_prefix)
        if person is None:
            people = sorted(store.people())
            raise ValueError(f"No hay muestras de «{args.persona}». Personas con muestras: "
                             f"{', '.join(people) or '(ninguna)'}.")
    patches, recent = None, samples.RECENT_PATCHES
    if args.parches:
        if args.parches.strip().casefold() == samples.ALL_PATCHES:
            patches = samples.ALL_PATCHES
        elif args.parches.strip().isdigit() and int(args.parches) > 0:
            recent = int(args.parches)
        else:
            raise ValueError("--parches debe ser un número (por ejemplo 3) o 'todos'.")
    options = dict(patches=patches, person=person, recent=recent)
    name, label = store.champion_name(champ), samples.ROLE_LABELS[role]
    who = f" · solo {person}" if person else ""
    if rival is not None:
        res = store.matchup(role, champ, rival, **options)
        print(f"{name} vs {store.champion_name(rival)} ({label}) · parches "
              f"{_patch_text(res['patches'])}{who}")
        for line in samples.text_summary(res):
            print(f"  {line}")
        return 0
    res = store.champion_overall(role, champ, **options)
    print(f"{name} ({label}) contra todos · parches {_patch_text(res['patches'])}{who}")
    for line in samples.text_summary(res):
        print(f"  {line}")
    ranked = store.matchups_of(role, champ, **options)
    if not ranked:
        print(f"  Sin cruces con al menos {samples.MIN_LIST_GAMES} partidas.")
        return 0

    def show(m):
        return (f"  {store.champion_name(m['opp'])}: la ganas {round(m['lane_wr'] * 100)}% "
                f"({m['games']} {'partida' if m['games'] == 1 else 'partidas'}, "
                f"{samples.signed(m['avg_diff14'])} oro+exp al 14)")

    print("Mejores cruces:")
    for m in ranked[:5]:
        print(show(m))
    worst = list(reversed(ranked[5:]))[:5]
    if worst:
        print("Peores cruces:")
        for m in worst:
            print(show(m))
    return 0


def cmd_muestras(args, cfg) -> int:
    from . import ddragon, sampler, samples
    from .lcu import LcuClient, lockfile_path

    store = samples.LineStore(cfg.data_dir / "lineas")
    if args.estado:
        for line in sampler.status_lines(sampler.DownloadState(store.base / "descarga.json"), store):
            print(line)
        return 0
    regions = sampler.parse_regions(args.regiones) if args.regiones else list(sampler.DEFAULT_REGIONS)
    if not cfg.api_key:
        raise MissingApiKeyError(
            "Falta RIOT_API_KEY en el archivo .env. Copia .env.example a .env y pega tu API key "
            "(se obtiene en developer.riotgames.com; las keys de desarrollo duran 24 h).")
    pool: dict = {}
    if cfg.db_path.is_file():
        db = Store(cfg.db_path)
        try:
            pool = sampler.clan_pool(db, cfg.root)
        finally:
            db.close()
    if not pool and args.solo_pool:
        raise ValueError("No hay campeones en el pool del clan (hacen falta partidas guardadas: "
                         "python -m lolan sync). Quita --solo-pool para muestrear las ligas en general.")
    limiters = Limiters()                # one limiter per Riot host: the panel's LA1 quota is not touched
    apis = {name: sampler.RiotRegionApi(sampler.Region(name), cfg.api_key, limiters) for name in regions}
    lcu = LcuClient(lockfile=lambda: lockfile_path(cfg.root))
    downloader = sampler.Downloader(
        store, apis, pool=pool, opgg=sampler.OpggLeaderboards(), dd=ddragon.DDragon(cfg.data_dir / "ddragon"),
        phase_fn=lcu.phase, solo_pool=args.solo_pool, hours=args.horas, max_matches=args.max_partidas)
    print(f"Muestra Diamante+ en {', '.join(r.upper() for r in regions)} · pool del clan: {len(pool)} campeones"
          + (" · solo el pool" if args.solo_pool else "") + ". Ctrl+C para detener; se puede reanudar.")
    print("Se pausa sola mientras el cliente de League esté en selección de campeones o en partida.")
    result = downloader.run()
    for line in sampler.run_summary(result, downloader.state, downloader.coverage()):
        print(line)
    return 0 if result.ok else 1


def cmd_arquetipos(args, cfg) -> int:
    from . import archetypes_data as ad, samples

    store = samples.LineStore(cfg.data_dir / "lineas")
    classes = ad.load_classes()
    lanes = ad.LANE_ORDER if args.linea in (None, "auto") else (args.linea,)
    report = ad.analyze(store, classes, lanes)
    written = ad.apply_decisions(classes, report) if args.decidir else []
    if written:
        ad.save_classes(classes)
    for line in ad.report_lines(report, decided=bool(written)):
        print(line)
    if written:
        print("Reinicia el panel para que use los tipos nuevos.")
    return 0


def cmd_web(args, cfg) -> int:
    from .web import create_app, start_live  # Flask is only needed for this command

    app = create_app(cfg.root)
    start_live(app)        # reads the game client on this PC (127.0.0.1:2999) while a game runs
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


def cmd_grabar_seleccion(args, cfg) -> int:
    from . import lcu

    def on_phase(phase):
        print(f"[{time.strftime('%H:%M:%S')}] " + ("Cliente de League cerrado, esperando…" if phase is None
                                                 else f"Fase: {phase}"))

    recorder = lcu.ChampSelectRecorder(out_dir=cfg.root / "data" / "capturas", root=cfg.root,
                                       on_phase=on_phase)
    print(f"Esperando el cliente de League ({lcu.lockfile_path(cfg.root)}). "
          "Graba la selección de campeones en data/capturas/. Ctrl+C para detener.")
    try:
        while True:
            time.sleep(recorder.step())
    except KeyboardInterrupt:
        print()
    finally:
        recorder.close()
    print("Grabación detenida.")
    return 0


COMMANDS = {"arquetipos": cmd_arquetipos, "lineas": cmd_lineas, "muestras": cmd_muestras, "sync": cmd_sync, "rekey": cmd_rekey, "analyze": cmd_analyze, "game": cmd_game,
            "campeon": cmd_campeon, "jugadores": cmd_jugadores, "composiciones": cmd_composiciones,
            "rivales": cmd_rivales, "web": cmd_web, "grabar-seleccion": cmd_grabar_seleccion}


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
