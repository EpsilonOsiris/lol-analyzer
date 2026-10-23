"""Riot API client with a sliding-window rate limiter (development key limits)."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from urllib.parse import quote, urlsplit

import requests

# Development key limits: 20 requests / 1 s and 100 requests / 120 s, enforced per routing value
# (the regional host, e.g. americas, and the platform host, e.g. la1, each count on their own).
# `Limiters` keeps ONE limiter per host for the whole process.
DEFAULT_LIMITS = ((20, 1.0), (100, 120.0))
# FOR TESTS ONLY (automated tests and the local smoke test with a fake Riot server). Normal use
# never sets it. Only a loopback address is honoured, so it can never point the panel elsewhere.
TEST_URL_ENV = "LOLAN_TEST_RIOT_URL"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Requests are stamped when sent but Riot counts them on arrival; latency jitter (e.g. the
# TLS handshake on the first request) could push a request into a window early -> 429.
WINDOW_MARGIN = 0.5
MAX_THROTTLE_RETRIES = 10
APEX_TIERS = ("challenger", "grandmaster", "master")

MISSING_KEY_MESSAGE = (
    "Falta RIOT_API_KEY. Copia .env.example a .env y pega tu API key "
    "(se obtiene en developer.riotgames.com)."
)
INVALID_KEY_MESSAGE = (
    "La API key es inválida o expiró; las keys de desarrollo duran 24 h. "
    "Genera una nueva en developer.riotgames.com y actualiza .env."
)


class RiotError(Exception):
    """Generic API failure (message is user-facing, Spanish)."""


class MissingApiKeyError(RiotError):
    pass


class InvalidApiKeyError(RiotError):
    pass


class NotFoundError(RiotError):
    pass


PUUID_OTHER_APP_MESSAGE = (
    "Cambiaste de API key: Riot cifra los identificadores de jugador por app, así que los datos "
    "guardados ya no sirven con esta key. Actualízalos con: python -m lolan rekey "
    "(o con el botón «Actualizar mis datos» del panel)."
)


class PuuidFromOtherAppError(RiotError):
    """A stored puuid was issued to another Riot app (the API key changed to a different app)."""


class RiotConnectionError(RiotError):
    """Riot could not be reached at all (no internet, firewall, antivirus...)."""


def riot_base_url(region: str) -> str:
    """https://<region>.api.riotgames.com, or the test override when it is a loopback address."""
    override = os.environ.get(TEST_URL_ENV, "").strip()
    if override:
        parts = urlsplit(override)
        if parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS:
            return override.rstrip("/")
    return f"https://{region}.api.riotgames.com"


class RateLimiter:
    """Sliding-window limiter. Blocks (via the injected sleep) until a request may go out.

    Thread-safe: the sync, the rivals scouting and the page's own lookups share one limiter per host.
    """

    def __init__(self, limits=DEFAULT_LIMITS, clock=time.monotonic, sleep=time.sleep,
                 margin=WINDOW_MARGIN):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._windows = [(limit, window + margin, deque()) for limit, window in limits]

    def _wait_locked(self, now: float) -> float:
        wait = 0.0
        for limit, window, stamps in self._windows:
            while stamps and now - stamps[0] >= window:
                stamps.popleft()
            if len(stamps) >= limit:
                wait = max(wait, stamps[0] + window - now)
        return wait

    def wait_estimate(self) -> float:
        """Seconds a request made right now would have to wait (0 when it can go out)."""
        with self._lock:
            return max(0.0, self._wait_locked(self._clock()))

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_locked(now)
                if wait <= 0:
                    for _limit, _window, stamps in self._windows:
                        stamps.append(now)
                    return
            self._sleep(wait)


class Limiters:
    """One RateLimiter per routing value (host), shared by everything that calls Riot in a process."""

    def __init__(self, **limiter_kwargs):
        self._kwargs = limiter_kwargs
        self._lock = threading.Lock()
        self._by_host: dict[str, RateLimiter] = {}

    def for_host(self, host: str) -> RateLimiter:
        host = str(host or "").strip().lower()
        with self._lock:
            if host not in self._by_host:
                self._by_host[host] = RateLimiter(**self._kwargs)
            return self._by_host[host]


class ApiSlot:
    """Only one of "sync" / "scout" may use the Riot API heavily at a time (non-blocking)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.holder: str | None = None

    def acquire(self, who: str) -> bool:
        with self._lock:
            if self.holder is not None:
                return False
            self.holder = who
            return True

    def release(self, who: str) -> None:
        with self._lock:
            if self.holder == who:
                self.holder = None


class RiotClient:
    def __init__(
        self,
        api_key: str,
        region: str = "americas",
        session=None,
        sleep=time.sleep,
        clock=time.monotonic,
        limiter: RateLimiter | None = None,
        max_retries: int = 5,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise MissingApiKeyError(MISSING_KEY_MESSAGE)
        self.region = region
        self.base_url = riot_base_url(region)
        self.session = session if session is not None else requests.Session()
        self._sleep = sleep
        self.limiter = limiter or RateLimiter(clock=clock, sleep=sleep)
        self.max_retries = max_retries
        self.timeout = timeout
        self._headers = {"X-Riot-Token": api_key}

    # ------------------------------------------------------------------ core
    @staticmethod
    def _retry_after(headers) -> float:
        try:
            return max(0.0, float(headers.get("Retry-After", 1)))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _is_other_app_puuid(resp) -> bool:
        """True for the 400 Riot answers when a puuid was encrypted by another app's key:
        {"status": {"message": "Bad Request - Exception decrypting <puuid>", ...}}."""
        try:
            data = resp.json()
            message = str(((data or {}).get("status") or {}).get("message") or "")
        except (ValueError, AttributeError, TypeError):
            return False
        return "exception decrypting" in message.lower()

    def _get(self, path: str, params: dict | None = None):
        url = self.base_url + path
        failures = 0
        throttled = 0
        while True:
            self.limiter.acquire()
            try:
                resp = self.session.get(
                    url, params=params, headers=self._headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                failures += 1
                if failures >= self.max_retries:
                    raise RiotConnectionError(
                        "No se pudo conectar con la API de Riot "
                        f"({type(exc).__name__}). Revisa tu conexión."
                    ) from exc
                self._sleep(min(2 ** failures, 30))
                continue

            status = resp.status_code
            if status == 200:
                return resp.json()
            if status in (401, 403):
                raise InvalidApiKeyError(INVALID_KEY_MESSAGE)
            if status == 404:
                raise NotFoundError(f"No encontrado: {path}")
            if status == 400 and self._is_other_app_puuid(resp):
                raise PuuidFromOtherAppError(PUUID_OTHER_APP_MESSAGE)
            if status == 429:
                throttled += 1
                if throttled > MAX_THROTTLE_RETRIES:
                    raise RiotError("La API de Riot sigue limitando las peticiones (429).")
                self._sleep(self._retry_after(resp.headers))
                continue
            if status >= 500:
                failures += 1
                if failures >= self.max_retries:
                    raise RiotError(f"La API de Riot respondió con error {status} repetidamente.")
                self._sleep(min(2 ** failures, 30))
                continue
            raise RiotError(f"Respuesta inesperada de la API de Riot: {status}.")

    # ------------------------------------------------------------- endpoints
    def get_puuid(self, game_name: str, tag_line: str) -> str:
        path = (
            "/riot/account/v1/accounts/by-riot-id/"
            f"{quote(game_name, safe='')}/{quote(tag_line, safe='')}"
        )
        data = self._get(path)
        return data["puuid"]

    def get_riot_id(self, puuid: str) -> str:
        """Current "Name#TAG" of an account (follows renames, unlike get_puuid)."""
        data = self._get(f"/riot/account/v1/accounts/by-puuid/{quote(puuid, safe='')}")
        return f"{data['gameName']}#{data['tagLine']}"

    def list_match_ids(self, puuid: str, start_time: int | None = None, queue: int | None = None,
                       limit: int | None = None) -> list[str]:
        """All match ids (newest first), paginating until an empty page."""
        ids: list[str] = []
        start = 0
        while True:
            params = {"start": start, "count": 100}
            if start_time is not None:
                params["startTime"] = int(start_time)
            if queue is not None:
                params["queue"] = int(queue)
            page = self._get(f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids", params)
            if not page:
                break
            ids.extend(page)
            start += len(page)
            if limit is not None and len(ids) >= limit:
                return ids[:limit]
        return ids

    def get_platform_status(self) -> dict:
        """lol-status-v4 platform data; a cheap call to check that the key works.

        Lives on the platform host, so build the client with region=<platform> (e.g. "la1").
        """
        return self._get("/lol/status/v4/platform-data")

    # ---- spectator / league / mastery (platform host: build the client with region=<platform>)
    def get_active_game(self, puuid: str) -> dict | None:
        """spectator-v5: the game the account is playing right now, or None (404 = not in a game)."""
        try:
            return self._get(f"/lol/spectator/v5/active-games/by-summoner/{quote(puuid, safe='')}")
        except NotFoundError:
            return None

    def get_league_entries(self, puuid: str) -> list:
        """league-v4: ranked entries (RANKED_SOLO_5x5, RANKED_FLEX_SR) of the account; [] if unranked."""
        try:
            data = self._get(f"/lol/league/v4/entries/by-puuid/{quote(puuid, safe='')}")
        except NotFoundError:
            return []
        return data if isinstance(data, list) else []

    def get_top_league(self, tier: str, queue: str = "RANKED_SOLO_5x5") -> dict:
        """league-v4 apex league (tier "challenger" / "grandmaster" / "master"): {tier, queue, entries}.

        Each entry has puuid, leaguePoints, wins, losses, inactive ... (the whole league in one answer).
        """
        tier = str(tier).strip().lower()
        if tier not in APEX_TIERS:
            raise ValueError(f"Liga desconocida: {tier}")
        data = self._get(f"/lol/league/v4/{tier}leagues/by-queue/{quote(queue, safe='')}")
        return data if isinstance(data, dict) else {"tier": tier.upper(), "queue": queue, "entries": []}

    def get_league_page(self, tier: str, division: str, page: int = 1,
                        queue: str = "RANKED_SOLO_5x5") -> list:
        """league-v4 entries of one tier / division (e.g. DIAMOND, I), 205 per page; [] past the last page."""
        path = (f"/lol/league/v4/entries/{quote(queue, safe='')}/{quote(str(tier).upper(), safe='')}"
                f"/{quote(str(division).upper(), safe='')}")
        data = self._get(path, {"page": int(page)})
        return data if isinstance(data, list) else []

    def get_champion_mastery(self, puuid: str, champion_id: int) -> dict | None:
        """champion-mastery-v4 for one champion, or None when the account never played it (404)."""
        path = (f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{quote(puuid, safe='')}"
                f"/by-champion/{int(champion_id)}")
        try:
            return self._get(path)
        except NotFoundError:
            return None

    def get_match_ids(self, puuid: str, count: int = 10, type: str | None = None,
                      queue: int | None = None, start_time: int | None = None) -> list[str]:
        """ONE page of match ids (newest first); `type="ranked"` keeps solo/duo and flex games.

        start_time: epoch seconds, only games that started after it.
        """
        params: dict = {"start": 0, "count": int(count)}
        if type:
            params["type"] = type
        if queue is not None:
            params["queue"] = int(queue)
        if start_time is not None:
            params["startTime"] = int(start_time)
        page = self._get(f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids", params)
        return page if isinstance(page, list) else []

    def get_match(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{quote(match_id, safe='')}")

    def get_timeline(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{quote(match_id, safe='')}/timeline")
