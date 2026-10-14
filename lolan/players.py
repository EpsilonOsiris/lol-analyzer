"""Who is who: the registry of players, the main player (RIOT_ID in .env) and URL/file slugs."""
from __future__ import annotations

import re
import unicodedata

from .store import Player, Store

# Slugs that would clash with a report/CSV name or that Windows refuses as file names.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10)),
             "solo", "flex", "normal", "blind", "quickplay", "jugadores", "games", "campeones",
             "partidas", "resumen"}


def normalize_riot_id(text) -> tuple[str, str]:
    """Split "Nombre#TAG" into (name, tag); ValueError with a Spanish message if malformed."""
    text = str(text or "").strip()
    if "#" in text:
        name, tag = text.rsplit("#", 1)
        name, tag = name.strip(), tag.strip()
        if name and tag:
            return name, tag
    raise ValueError("El Riot ID debe tener el formato Nombre#TAG (por ejemplo, Jugador#LAN).")


def slugify(text: str) -> str:
    """Lowercase, accent-free, filesystem- and URL-safe version of a name."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _slugs(players: list[Player]) -> dict[str, str]:
    """puuid -> unique slug: the game name, plus the tag (and as a last resort the puuid) if needed."""
    def base(p, level):
        name, sep, tag = p.riot_id.rpartition("#")
        if not sep:
            name, tag = p.riot_id, ""
        parts = [slugify(name) or "jugador"]
        if level >= 1 and slugify(tag):
            parts.append(slugify(tag))
        if level >= 2:
            parts.append(slugify(p.puuid)[:8])
        slug = "-".join(parts)
        return f"j-{slug}" if slug in _RESERVED or re.fullmatch(r"cola-\d+", slug) else slug

    result: dict[str, str] = {}
    pending = list(players)
    for level in (0, 1, 2):
        counts: dict[str, int] = {}
        for p in pending:
            counts[base(p, level)] = counts.get(base(p, level), 0) + 1
        still = []
        for p in pending:
            slug = base(p, level)
            if counts[slug] == 1 or level == 2:
                result[p.puuid] = slug
            else:
                still.append(p)
        pending = still
        if not pending:
            break
    return result


class Roster:
    """The registered players, main first, with their slugs."""

    def __init__(self, players: list[Player], main: Player | None):
        self.main = main
        self.players = players
        self._slugs = _slugs(players)

    def __len__(self) -> int:
        return len(self.players)

    def slug(self, player: Player) -> str:
        return self._slugs[player.puuid]

    @property
    def main_slug(self) -> str | None:
        return self.slug(self.main) if self.main else None

    def by_slug(self, slug: str | None) -> Player | None:
        if not slug:
            return None
        slug = slug.strip().lower()
        return next((p for p in self.players if self._slugs[p.puuid] == slug), None)

    def by_puuid(self, puuid: str) -> Player | None:
        return next((p for p in self.players if p.puuid == puuid), None)

    def by_ref(self, ref: str | None) -> Player | None:
        """A player referenced by Riot ID (case-insensitive) or, failing that, by slug."""
        ref = (ref or "").strip()
        if not ref:
            return None
        low = ref.casefold()
        found = next((p for p in self.players if p.riot_id.casefold() == low), None)
        return found or self.by_slug(ref)


def load_roster(store: Store, main_riot_id: str = "") -> Roster:
    """Registered players; `main_riot_id` (RIOT_ID from .env) says who "you" are.

    The main player is the registered one with that Riot ID; when the .env value is not
    registered yet (e.g. you just changed it) the player flagged as main in the database is used.
    """
    rows = store.list_players()
    wanted = (main_riot_id or "").strip().casefold()
    main = next((p for p in rows if wanted and p.riot_id.casefold() == wanted), None)
    if main is None:
        main = next((p for p in rows if p.is_main), None)
    players = [p._replace(is_main=(main is not None and p.puuid == main.puuid)) for p in rows]
    players.sort(key=lambda p: (not p.is_main, p.riot_id.lower(), p.puuid))
    if main is not None:
        main = next(p for p in players if p.puuid == main.puuid)
    return Roster(players, main)
