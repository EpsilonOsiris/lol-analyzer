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


def sync(client, store, riot_id: str, max_matches: int | None = None, since: date | None = None,
         with_timeline: bool = True, out=print, clock=time.monotonic) -> SyncResult:
    name, tag = parse_riot_id(riot_id)
    out(f"Buscando la cuenta {name}#{tag} ...")
    puuid = client.get_puuid(name, tag)
    store.save_account(puuid, f"{name}#{tag}")

    out("Listando las partidas de la cuenta ...")
    ids = client.list_match_ids(puuid, start_time=since_to_epoch(since))
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
