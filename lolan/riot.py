"""Riot API client with a sliding-window rate limiter (development key limits)."""
from __future__ import annotations

import time
from collections import deque
from urllib.parse import quote

import requests

# Development key limits: 20 requests / 1 s and 100 requests / 120 s, enforced per
# routing value. Every endpoint we call lives on the regional host, so one limiter suffices.
DEFAULT_LIMITS = ((20, 1.0), (100, 120.0))
# Requests are stamped when sent but Riot counts them on arrival; latency jitter (e.g. the
# TLS handshake on the first request) could push a request into a window early -> 429.
WINDOW_MARGIN = 0.5
MAX_THROTTLE_RETRIES = 10

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


class RateLimiter:
    """Sliding-window limiter. Blocks (via the injected sleep) until a request may go out."""

    def __init__(self, limits=DEFAULT_LIMITS, clock=time.monotonic, sleep=time.sleep,
                 margin=WINDOW_MARGIN):
        self._clock = clock
        self._sleep = sleep
        self._windows = [(limit, window + margin, deque()) for limit, window in limits]

    def acquire(self) -> None:
        while True:
            now = self._clock()
            wait = 0.0
            for limit, window, stamps in self._windows:
                while stamps and now - stamps[0] >= window:
                    stamps.popleft()
                if len(stamps) >= limit:
                    wait = max(wait, stamps[0] + window - now)
            if wait <= 0:
                break
            self._sleep(wait)
        now = self._clock()
        for _limit, _window, stamps in self._windows:
            stamps.append(now)


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
        self.base_url = f"https://{region}.api.riotgames.com"
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
                    raise RiotError(
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

    def get_match(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{quote(match_id, safe='')}")

    def get_timeline(self, match_id: str) -> dict:
        return self._get(f"/lol/match/v5/matches/{quote(match_id, safe='')}/timeline")
