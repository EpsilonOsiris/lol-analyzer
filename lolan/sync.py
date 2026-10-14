"""Download all of a player's matches into the local store (resumable)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

from .riot import InvalidApiKeyError, NotFoundError, RiotError

SECONDS_PER_REQUEST = 1.2  # 100 requests / 120 s on a development key
MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class SyncResult:
    puuid: str
    total_ids: int
    already_stored: int
    downloaded: int
    missing_timelines: int
    failed: int


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    if not riot_id or "#" not in riot_id:
        raise ValueError("RIOT_ID debe tener el formato Nombre#TAG (por ejemplo, Jugador#LAN).")
    name, tag = riot_id.rsplit("#", 1)
    name, tag = name.strip(), tag.strip()
    if not name or not tag or (name, tag) == ("Nombre", "TAG"):
        raise ValueError("Configura RIOT_ID en .env con tu Riot ID real (formato Nombre#TAG).")
    return name, tag


def format_duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def since_to_epoch(since: date | None) -> int | None:
    if since is None:
        return None
    return int(datetime(since.year, since.month, since.day).timestamp())


@dataclass
class PlayerOutcome:
    """Result of syncing one player in a multi-player run (`error` set when it was skipped)."""
    riot_id: str
    result: SyncResult | None = None
    error: str | None = None


def sync(client, store, riot_id: str, max_matches: int | None = None, since: date | None = None,
         with_timeline: bool = True, queue: int | None = None, out=print,
         clock=time.monotonic, main: bool = False) -> SyncResult:
    name, tag = parse_riot_id(riot_id)
    out(f"Buscando la cuenta {name}#{tag} ...")
    known = store.get_player(riot_id=f"{name}#{tag}")
    if known is not None and not main:
        # Registered clan member: follow the account by puuid so a rename doesn't break sync.
        # (The main player is whoever .env's RIOT_ID names, so that one is looked up by name.)
        try:
            current = client.get_riot_id(known.puuid)
        except NotFoundError:
            raise NotFoundError(f"No se encontró la cuenta {name}#{tag} en Riot (puede haberse "
                                "eliminado).") from None
        if current.casefold() != f"{name}#{tag}".casefold():
            out(f"{name}#{tag} ahora se llama {current}; se actualiza el registro.")
        puuid, (name, tag) = known.puuid, parse_riot_id(current)
    else:
        try:
            puuid = client.get_puuid(name, tag)
        except NotFoundError:
            raise NotFoundError(
                f"No se encontró la cuenta {name}#{tag}. Revisa el nombre y el TAG "
                "(la cuenta pudo haber cambiado de nombre).") from None
    # Re-registering every time keeps the Riot ID current when someone renames their account.
    store.add_player(puuid, f"{name}#{tag}", is_main=main)

    out("Listando las partidas de la cuenta ...")
    ids = client.list_match_ids(puuid, start_time=since_to_epoch(since), queue=queue)
    stored = store.existing_match_ids()
    new_ids = [m for m in ids if m not in stored]
    already = len(ids) - len(new_ids)
    if max_matches is not None:
        new_ids = new_ids[:max_matches]

    total = len(new_ids)
    out(f"Partidas encontradas: {len(ids)}. Ya guardadas: {already}. Por descargar: {total}.")
    result = SyncResult(puuid, len(ids), already, 0, 0, 0)
    if total == 0:
        out("No hay partidas nuevas por descargar.")
        return result

    per_match_estimate = SECONDS_PER_REQUEST * (2 if with_timeline else 1)
    out(f"Tiempo estimado: ~{format_duration(total * per_match_estimate)} "
        "(la descarga se puede interrumpir y reanudar).")

    started = clock()
    consecutive_failures = 0
    for i, match_id in enumerate(new_ids, 1):
        try:
            match = client.get_match(match_id)
            timeline = None
            if with_timeline:
                try:
                    timeline = client.get_timeline(match_id)
                except NotFoundError:
                    result.missing_timelines += 1
            store.save_match(match_id, match, timeline)
            result.downloaded += 1
            consecutive_failures = 0
        except InvalidApiKeyError:
            out(f"Se guardaron {result.downloaded} partidas antes del error; vuelve a ejecutar "
                "sync para continuar.")
            raise
        except RiotError as exc:
            result.failed += 1
            consecutive_failures += 1
            out(f"[{i}/{total}] {match_id}: no se pudo descargar ({exc}). Se omite.")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RiotError(
                    "Demasiados errores seguidos al descargar; se detiene. "
                    "Lo descargado ya está guardado; vuelve a ejecutar sync."
                ) from exc
            continue

        elapsed = clock() - started
        remaining = total - i
        eta = (elapsed / i) * remaining
        champ = _champion_of(match, puuid)
        out(f"[{i}/{total}] {match_id} {champ} | transcurrido {format_duration(elapsed)} "
            f"| faltan ~{format_duration(eta)}")

    out(f"Listo: {result.downloaded} partidas descargadas"
        + (f", {result.missing_timelines} sin timeline" if result.missing_timelines else "")
        + (f", {result.failed} con error" if result.failed else "") + ".")
    return result


def _champion_of(match: dict, puuid: str) -> str:
    for p in (match.get("info") or {}).get("participants") or []:
        if p.get("puuid") == puuid:
            return str(p.get("championName", "?"))
    return "?"


def summary_lines(outcomes: list[PlayerOutcome]) -> list[str]:
    lines = ["Resumen por jugador:"]
    for o in outcomes:
        if o.error:
            lines.append(f"  - {o.riot_id}: NO SINCRONIZADO ({o.error})")
            continue
        r = o.result
        lines.append(
            f"  - {o.riot_id}: {r.downloaded} descargadas, {r.already_stored} ya guardadas"
            + (f", {r.missing_timelines} sin timeline" if r.missing_timelines else "")
            + (f", {r.failed} con error" if r.failed else ""))
    return lines


def sync_players(client, store, riot_ids: list[str], main_riot_id: str = "", out=print,
                 **kwargs) -> list[PlayerOutcome]:
    """Sync several players one after another with the same client (one shared rate limiter).

    A player whose account can't be found (e.g. renamed) is skipped and the rest continue; an
    invalid/expired key, or any other API failure that is not about one account, aborts the run
    (what was already downloaded stays saved). `kwargs` go to `sync` for every player.
    """
    outcomes: list[PlayerOutcome] = []
    main = (main_riot_id or "").strip().casefold()
    total = len(riot_ids)
    try:
        for i, riot_id in enumerate(riot_ids, 1):
            out(f"=== {riot_id} ({i}/{total}) ===")
            try:
                result = sync(client, store, riot_id, out=out,
                              main=bool(main) and riot_id.strip().casefold() == main, **kwargs)
            except NotFoundError as exc:
                out(f"No se pudo sincronizar a {riot_id}: {exc}")
                outcomes.append(PlayerOutcome(riot_id, error=str(exc)))
            else:
                outcomes.append(PlayerOutcome(riot_id, result))
    except RiotError:
        if outcomes:
            for line in summary_lines(outcomes):
                out(line)
        raise
    if total > 1:
        for line in summary_lines(outcomes):
            out(line)
    return outcomes
