"""Carry the stored data over to a new Riot API key / app.

Riot encrypts PUUIDs per app: after switching from, say, a Development key to a Personal key
(a different app) every stored puuid is useless (by-puuid answers 400 "Exception decrypting")
and the same player comes back with a different puuid. Match ids are the same in every app, so
the migration is:

  1. look every registered player up again by Riot ID (new puuid);
  2. re-download each stored match and its timeline (their JSON holds the old puuids) and
     replace it, which also rebuilds that match's `match_players` links;
  3. swap the old puuid for the new one in the registry and in every other table with a
     `puuid` column;
  4. verify that nobody lost matches.

Resumable: a match is pending only while none of its participants is a (new) puuid of a
registered player, so running it again continues where it stopped. The registry is switched
at the very end, so an interrupted run leaves the old identifiers in place.
"""
from __future__ import annotations

import math
import os
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import analyze as an
from .riot import InvalidApiKeyError, NotFoundError, PuuidFromOtherAppError, RiotError
from .store import Store, participant_puuids
from .sync import SECONDS_PER_REQUEST, format_duration, parse_riot_id

MAX_CONSECUTIVE_FAILURES = 5
BACKUP_MARKER = ".antes-rekey-"
BACKUP_HEADROOM = 1.2          # free space needed, as a multiple of the database size


class RekeyError(Exception):
    """The migration cannot run (message is user-facing, Spanish)."""


@dataclass
class Remap:
    riot_id: str
    old: str
    new: str


@dataclass
class RekeyResult:
    remapped: list = field(default_factory=list)      # Remap of the players that were resolved
    skipped: list = field(default_factory=list)       # (riot_id, reason): kept as they were
    total_matches: int = 0                            # stored matches
    pending: int = 0                                  # matches that needed re-downloading
    updated: int = 0
    untouched: int = 0                                # matches with no resolved registered player
    not_found: list = field(default_factory=list)     # match ids Riot no longer returns (kept)
    failed: list = field(default_factory=list)        # match ids that failed for another reason
    missing_timelines: int = 0
    registry_updated: int = 0
    backup: Path | None = None
    problems: list = field(default_factory=list)      # verification differences
    verified: bool = False

    @property
    def nothing_to_do(self) -> bool:
        return self.pending == 0 and self.registry_updated == 0

    @property
    def ok(self) -> bool:
        """Everything migrated and verified (no skipped players, no kept-as-is matches)."""
        return not (self.problems or self.not_found or self.failed or self.skipped)


def estimate_minutes(store: Store) -> int:
    """Rough time for the whole migration: ~2 requests per stored match at the key's rate."""
    seconds = store.count_matches() * 2 * SECONDS_PER_REQUEST
    return max(1, math.ceil(seconds / 60))


def key_app_changed(client, store: Store) -> bool | None:
    """Does the key `client` uses belong to another Riot app than the stored puuids?

    One by-puuid request for a registered player: True if Riot says it cannot decrypt the
    puuid, False if it accepts it, None when it cannot be told (no players, no connection...).
    """
    players = store.list_players()
    if not players:
        return None
    try:
        client.get_riot_id(players[0].puuid)
    except PuuidFromOtherAppError:
        return True
    except RiotError:
        return None
    return False


# ------------------------------------------------------------------- backup
def backup_database(store: Store, now: datetime | None = None, free_bytes: int | None = None
                    ) -> Path | None:
    """Copy the database to lol.db.antes-rekey-<YYYYMMDD-HHMM> next to it.

    Refuses (RekeyError) when the disk can't hold the copy. In-memory stores are not backed up.
    """
    if not isinstance(store.path, Path):
        return None
    src = store.path
    size = src.stat().st_size if src.exists() else 0
    free = shutil.disk_usage(src.parent).free if free_bytes is None else free_bytes
    needed = int(size * BACKUP_HEADROOM) + 16 * 1024 * 1024
    if free < needed:
        raise RekeyError(
            f"No hay espacio en disco para el respaldo de la base de datos: hacen falta unos "
            f"{needed // (1024 * 1024)} MB libres y hay {free // (1024 * 1024)} MB. Libera espacio "
            "y vuelve a intentarlo (no se cambió nada).")
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    target = src.with_name(f"{src.name}{BACKUP_MARKER}{stamp}")
    n = 2
    while target.exists():
        target = src.with_name(f"{src.name}{BACKUP_MARKER}{stamp}-{n}")
        n += 1
    tmp = target.with_name(target.name + ".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        store.conn.backup(dst)        # consistent copy even if another process is writing
    finally:
        dst.close()
    os.replace(tmp, target)
    return target


def existing_backups(store: Store) -> list[Path]:
    if not isinstance(store.path, Path):
        return []
    return sorted(store.path.parent.glob(store.path.name + BACKUP_MARKER + "*"))


# ---------------------------------------------------------------- migration
def _games_by_match(store: Store, *puuids: str) -> dict:
    """match_id -> queue of the analyzable games (as `analyze` sees them) of those puuids."""
    result = {}
    for puuid in dict.fromkeys(puuids):
        for game in an.load_games(store, puuid):
            result[game["match_id"]] = game["queue_id"]
    return result


def rekey(client, store: Store, out=print, clock=time.monotonic, backup: bool = True,
          backup_now: datetime | None = None) -> RekeyResult:
    """Migrate the store to the app of the key `client` uses. See the module docstring."""
    result = RekeyResult()
    players = store.list_players()
    if not players:
        out("No hay jugadores registrados; no hay nada que actualizar.")
        return result

    # 1. New puuid of every registered player, by Riot ID.
    out("Buscando de nuevo a los jugadores registrados con la key actual ...")
    for p in players:
        try:
            name, tag = parse_riot_id(p.riot_id)
            new = client.get_puuid(name, tag)
        except ValueError as exc:
            result.skipped.append((p.riot_id, str(exc)))
        except NotFoundError:
            result.skipped.append((p.riot_id, "ya no existe una cuenta con ese Riot ID "
                                              "(¿cambió de nombre?)"))
        else:
            result.remapped.append(Remap(p.riot_id, p.puuid, new))
    for riot_id, reason in result.skipped:
        out(f"AVISO: no se pudo actualizar a {riot_id}: {reason}. Se omite y se conservan sus "
            "datos como estaban; corrige su Riot ID en la página Jugadores y vuelve a ejecutar.")
    if not result.remapped:
        out("Ningún jugador se pudo resolver con esta key; no se cambió nada.")
        return result

    new_set = {r.new for r in result.remapped}
    old_set = {r.old for r in result.remapped if r.old != r.new}

    # 2. Which matches still hold old-app puuids.
    links = store.participants_by_match()
    ordered = store.match_ids_newest_first()
    result.total_matches = len(ordered)
    pending, done = [], 0
    for match_id in ordered:
        who = links.get(match_id, set())
        if who & new_set:
            done += 1
        elif who & old_set:
            pending.append(match_id)
        else:
            result.untouched += 1
    result.pending = len(pending)
    registry_todo = [r for r in result.remapped
                     if r.old != r.new and store.get_player(puuid=r.old) is not None]
    if not pending and not registry_todo:
        out("Tus datos ya usan los identificadores de esta key; no hay nada que actualizar.")
        return result
    if result.untouched:
        out(f"{result.untouched} partidas no incluyen a ningún jugador que se pueda actualizar; "
            "se dejan como están.")

    # Baseline to verify against at the end (old and new links together, so a resumed run
    # compares with the same numbers as a fresh one).
    snapshot: dict = {}      # new puuid -> (linked match ids, {match id: queue}) before
    for r in result.remapped:
        ids, games = snapshot.setdefault(r.new, (set(), {}))
        ids |= store.linked_match_ids(r.old, r.new)
        games.update(_games_by_match(store, r.old, r.new))

    if backup and not (done and existing_backups(store)):
        result.backup = backup_database(store, now=backup_now)
        if result.backup is not None:
            out(f"Respaldo de la base de datos: {result.backup}")
    elif backup:
        out("Continuando una actualización anterior; se conserva el respaldo que ya existe.")

    # 3. Re-download the pending matches.
    total = len(pending)
    if total:
        out(f"Partidas por actualizar: {total} de {result.total_matches}. "
            f"Tiempo estimado: ~{format_duration(total * 2 * SECONDS_PER_REQUEST)} "
            "(se puede interrumpir y reanudar).")
    started = clock()
    consecutive_failures = 0
    for i, match_id in enumerate(pending, 1):
        try:
            match = client.get_match(match_id)
            timeline = None
            try:
                timeline = client.get_timeline(match_id)
            except NotFoundError:
                result.missing_timelines += 1      # the stored timeline (if any) stays
            store.save_match(match_id, match, timeline)
            result.updated += 1
            consecutive_failures = 0
        except NotFoundError:
            result.not_found.append(match_id)
            consecutive_failures = 0
            out(f"[{i}/{total}] {match_id}: Riot ya no tiene esta partida; se conserva como estaba.")
            continue
        except InvalidApiKeyError:
            out(f"Se actualizaron {result.updated} partidas antes del error; vuelve a ejecutar "
                "rekey con una key válida para continuar.")
            raise
        except RiotError as exc:
            result.failed.append(match_id)
            consecutive_failures += 1
            out(f"[{i}/{total}] {match_id}: no se pudo descargar ({exc}). Se omite.")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RiotError(
                    "Demasiados errores seguidos al descargar; se detiene. Lo ya actualizado está "
                    "guardado; vuelve a ejecutar rekey.") from exc
            continue

        if not set(participant_puuids(match)) & new_set:
            out(f"AVISO: {match_id} no incluye a ningún jugador registrado con la key nueva.")
        elapsed = clock() - started
        eta = (elapsed / i) * (total - i)
        out(f"[{i}/{total}] {match_id} | transcurrido {format_duration(elapsed)} "
            f"| faltan ~{format_duration(eta)}")

    # 4. Registry and every other table with a puuid column.
    for r in registry_todo:
        store.replace_player_puuid(r.old, r.new)
        result.registry_updated += 1

    # 5. Verify.
    _verify(store, result, snapshot)
    _summary(result, out)
    return result


def _verify(store: Store, result: RekeyResult, snapshot: dict) -> None:
    kept = set(result.not_found) | set(result.failed)
    names = {}
    for r in result.remapped:
        names.setdefault(r.new, r.riot_id)
    for new, riot_id in names.items():
        before_ids, before_games = snapshot[new]
        expected_ids = before_ids - kept
        after_ids = store.linked_match_ids(new)
        if after_ids != expected_ids:
            result.problems.append(
                f"{riot_id}: partidas enlazadas {len(after_ids)}, se esperaban "
                f"{len(expected_ids)}.")
        expected = Counter(q for m, q in before_games.items() if m not in kept)
        after = Counter(g["queue_id"] for g in an.load_games(store, new))
        if after != expected:
            result.problems.append(
                f"{riot_id}: el análisis por cola cambió (antes {dict(sorted(expected.items()))}, "
                f"ahora {dict(sorted(after.items()))}).")
    result.verified = True


def _summary(result: RekeyResult, out) -> None:
    out(f"Listo: {result.updated} partidas actualizadas"
        + (f", {len(result.not_found)} que Riot ya no tiene (se conservan sin cambios)"
           if result.not_found else "")
        + (f", {len(result.failed)} con error (vuelve a ejecutar rekey)" if result.failed else "")
        + (f", {result.missing_timelines} sin timeline" if result.missing_timelines else "")
        + f". Jugadores actualizados: {result.registry_updated}.")
    if result.not_found:
        out("Partidas que Riot ya no devuelve: " + ", ".join(result.not_found))
    if result.problems:
        out("VERIFICACIÓN CON DIFERENCIAS:")
        for line in result.problems:
            out(f"  - {line}")
    else:
        out("Verificación: cada jugador conserva el mismo número de partidas y el análisis por "
            "cola da los mismos resultados que antes.")
