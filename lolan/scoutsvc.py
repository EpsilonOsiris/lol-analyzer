"""The scouting service of the web panel: state, background jobs and the 30 s search.

`ScoutService` keeps the last report ("who is in my game") and runs the jobs that make it:
  * automatic: while the En vivo page is open and the game client says there is no game (or it is
    loading), ask the Spectator API for the main player's game every POLL_S seconds (1 request);
    when a game shows up the scout runs once and the report is pushed through the live stream;
  * manual: the "Buscar partida ahora" button, for the main player or any registered player.
A scout never runs while a sync does (they share the same `ApiSlot`): the manual button answers with
a friendly message and the automatic search tries again 30 s later.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

from . import comps
from .config import ConfigError, load_config
from .players import load_roster
from .riot import (ApiSlot, InvalidApiKeyError, Limiters, MissingApiKeyError, NotFoundError, RiotError)
from .scout import (CACHE_PRUNE_S, BudgetStop, LocalHistory, PuuidResolver, ScoutApi, ScoutRun,
                    parse_spectator)
from .settings import KeyHealth, read_settings
from .store import Store

POLL_S = 30.0
TICK_S = 1.0

MISSING_KEY_MESSAGE = ("Falta tu key de Riot. Pégala en la página Configuración y vuelve a "
                       "intentarlo; no hace falta reiniciar el panel.")
INVALID_KEY_MESSAGE = ("Tu key de Riot es inválida o expiró (las keys de desarrollo duran 24 horas). "
                       "Consigue una nueva en developer.riotgames.com y pégala en la página Configuración.")
BUSY_SYNC_MESSAGE = ("Hay una sincronización en curso. La búsqueda de rivales espera a que termine "
                     "(las dos usan el mismo cupo de peticiones de tu key).")
ALREADY_MESSAGE = "Ya se está buscando la partida."
NO_RIOT_ID_MESSAGE = "Falta tu Riot ID (formato Nombre#TAG): escríbelo en la página Configuración."


class ScoutService:
    def __init__(self, root, limiters: Limiters, slot: ApiSlot, client_factory, health: KeyHealth | None = None,
                 notify=None, clock=time.time, poll_s: float = POLL_S, resolver: PuuidResolver | None = None):
        """`client_factory(api_key, host) -> RiotClient-like` (host: the platform or the region)."""
        self.root = root
        self.db_path = root / "data" / "lol.db"
        self.limiters, self.slot = limiters, slot
        self._factory = client_factory
        self.health = health or KeyHealth()
        self._notify = notify or (lambda: None)
        self._clock = clock
        self.poll_s = poll_s
        self.resolver = resolver or PuuidResolver()
        self.history = LocalHistory(lambda: Store(self.db_path))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._live = None
        self._last_poll = float("-inf")
        self.auto_on = False
        self._reported_error = False
        # ---- visible state
        self.version = 0
        self.status = "idle"        # idle | searching | ready | nogame | busy | error
        self.message = ""
        self.target = ""
        self.report: dict | None = None
        self._running = False
        self._tied = True                   # report belongs to the main player's live game
        self._attached: int | None = None   # game_seq of the live game the report is for
        self._pending_after: int | None = None

    # ------------------------------------------------------------------ state
    def _set(self, **changes) -> None:
        with self._lock:
            before = (self.status, self.message, self.target, self.report)
            for k, v in changes.items():
                setattr(self, k, v)
            changed = before != (self.status, self.message, self.target, self.report)
            if changed:
                self.version += 1
        if changed:
            self._notify()

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message, "target": self.target,
                    "report": self.report, "auto": self.auto_on, "busy": self._running,
                    "version": self.version}

    def reconcile(self, live_status: str, game_seq: int) -> None:
        """Tie the report of the main player's game to the live game: kept through the whole game and
        its "finished" summary, dropped when the next game starts."""
        drop = False
        with self._lock:
            if self.report is None or not self._tied:
                return
            if self._attached is None:
                if live_status == "playing" and game_seq > (self._pending_after or 0):
                    self._attached = game_seq
            elif live_status == "playing" and game_seq != self._attached:
                drop = True
        if drop:
            self._set(report=None, status="idle", message="")
            with self._lock:
                self._attached = self._pending_after = None

    def targets(self) -> list[dict]:
        """People whose current game can be scouted: you, then the registered players."""
        try:
            cfg = load_config(self.root, override=True)
        except ConfigError:
            return []
        store = Store(self.db_path)
        try:
            roster = load_roster(store, cfg.riot_id)
        finally:
            store.close()
        out, seen = [], set()
        if cfg.riot_id:
            out.append({"riot_id": cfg.riot_id, "label": f"{cfg.riot_id} (tú)", "is_main": True})
            seen.add(cfg.riot_id.casefold())
        for p in roster.players:
            if p.riot_id.casefold() not in seen:
                out.append({"riot_id": p.riot_id, "label": p.riot_id, "is_main": False})
                seen.add(p.riot_id.casefold())
        return out

    # ------------------------------------------------------------------- jobs
    def join(self, timeout=None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def request(self, ref=None, live_status="waiting", game_seq=0) -> tuple[int, dict]:
        """The "Buscar partida ahora" button: (http status, body)."""
        try:
            cfg = load_config(self.root, override=True)
        except ConfigError as exc:
            return 400, {"error": str(exc)}
        ref = str(ref or "").strip()
        main = cfg.riot_id
        if not ref or ref.casefold() == main.casefold():
            target, is_main = main, True
        else:
            store = Store(self.db_path)
            try:
                player = load_roster(store, main).by_ref(ref)
            finally:
                store.close()
            if player is None:
                return 400, {"error": "Ese jugador no está registrado. Agrégalo en la página Jugadores."}
            target, is_main = player.riot_id, player.is_main
        if not target:
            return 400, {"error": NO_RIOT_ID_MESSAGE}
        if self.slot.holder == "sync":
            return 409, {"error": BUSY_SYNC_MESSAGE}
        if not self._begin():
            return 409, {"error": ALREADY_MESSAGE}
        self._set(status="searching", message=f"Buscando la partida de {target}…", target=target)
        self._spawn(target, is_main, manual=True, live_status=live_status, game_seq=game_seq)
        return 202, self.snapshot()

    def _begin(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            return True

    def _spawn(self, target, is_main, manual, live_status, game_seq) -> None:
        self._thread = threading.Thread(
            target=self._job, args=(target, is_main, manual, live_status, game_seq),
            daemon=True, name="lolan-scout")
        self._thread.start()

    def _job(self, target, is_main, manual, live_status, game_seq) -> None:
        key = ""
        try:
            cfg = load_config(self.root, override=True)
            key = cfg.api_key
            if not key:
                raise MissingApiKeyError(MISSING_KEY_MESSAGE)
            if not manual and self.health.is_invalid(key):
                return                          # the key was already refused: say nothing every 30 s
            api = ScoutApi(self._factory(key, cfg.platform), self._factory(key, cfg.region))
            puuid = self.resolver.resolve(target, key, api.account)
            raw = api.active_game(puuid)
            game = parse_spectator(raw, puuid, target) if raw else None
            self.health.mark_ok(key)
            if game is None:
                self._no_game(target, is_main, manual)
                return
            with self._lock:
                same = (self.report is not None and not manual
                        and self.report["game"]["game_id"] == game["game_id"]
                        and self.report["scouted"]["riot_id"].casefold() == target.casefold())
            if same:
                return                          # already scouted: the 30 s search just looked again
            if not self.slot.acquire("scout"):
                self._set(status="busy", message=BUSY_SYNC_MESSAGE, target=target)
                return
            try:
                report = self._scout(api, game, cfg, target, is_main)
            finally:
                self.slot.release("scout")
            with self._lock:
                self._tied = is_main
                self._attached = game_seq if (is_main and live_status == "playing") else None
                self._pending_after = game_seq
            self._reported_error = False
            self._set(report=report, status="ready", message="", target=target)
        except MissingApiKeyError:
            self._error(MISSING_KEY_MESSAGE, manual)
        except InvalidApiKeyError:
            self.health.mark_invalid(key)
            self._error(INVALID_KEY_MESSAGE, manual)
        except NotFoundError:
            self._error(f"No se encontró la cuenta {target}. Revisa el nombre y el TAG.", manual)
        except BudgetStop:
            self._error("Tu key de Riot está al límite de peticiones por ahora; vuelve a intentarlo en "
                        "un minuto.", manual)
        except (ConfigError, RiotError, ValueError) as exc:
            self._error(str(exc), manual)
        except Exception as exc:    # keep the failure visible, never kill the panel
            traceback.print_exc()
            self._error(f"Error inesperado: {type(exc).__name__}: {exc}", manual)
        finally:
            with self._lock:
                self._running = False
            if self.status == "searching":
                self._set(status="idle", message="")

    def _scout(self, api, game, cfg, target, is_main) -> dict:
        st = read_settings(self.root)
        store = Store(self.db_path)
        try:
            store.prune_scout_cache(CACHE_PRUNE_S, self._clock())
            roster = load_roster(store, cfg.riot_id)
            tracked = [p.riot_id for p in roster.players]
            clan = comps.ClanConfig.build(st.clan_prefix, st.clan_aliases, {p.puuid: p.riot_id for p in roster.players})
            run = ScoutRun(api, store, self.history, clan, viewer_riot_id=target, viewer_is_main=is_main,
                           now=self._clock, tracked_ids=tracked)
            return run.run(game)
        finally:
            store.close()

    def _no_game(self, target, is_main, manual) -> None:
        if manual:
            self._set(status="nogame", message=f"{target} no está en una partida en este momento. "
                      "Riot publica la partida unos segundos después de empezar la pantalla de carga.",
                      target=target)
            return
        with self._lock:
            gone = self.report is not None and self._tied and self._attached is None
        if gone:                                # the scouted game never started (dodge, remake, other PC)
            self._set(report=None, status="idle", message="")

    def _error(self, message: str, manual: bool) -> None:
        if manual or not self._reported_error:
            self._reported_error = True
            self._set(status="error", message=message)

    # ---------------------------------------------------------------- watcher
    def tick(self, live_status: str, game_seq: int, listeners: int, now: float | None = None) -> bool:
        """One step of the automatic search; True when it started a job."""
        self.reconcile(live_status, game_seq)
        now = time.monotonic() if now is None else now
        wanted = listeners > 0 and live_status != "playing"
        self.auto_on = wanted
        if not wanted or now - self._last_poll < self.poll_s:
            return False
        try:
            cfg = load_config(self.root, override=True)
        except ConfigError:
            return False
        if not cfg.riot_id or not cfg.api_key:
            return False
        if not self._begin():
            return False
        self._last_poll = now
        self._spawn(cfg.riot_id, True, manual=False, live_status=live_status, game_seq=game_seq)
        return True

    def start(self, live) -> None:
        """Start the background watcher that follows `live` (a LivePoller)."""
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._live = live
        self._stop.clear()
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="lolan-scout-watch")
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(2)

    def _watch(self) -> None:
        reported = False
        while not self._stop.is_set():
            try:
                live = self._live
                self.tick(live.status, getattr(live, "game_seq", 0), live.listeners)
            except Exception as exc:
                if not reported:
                    print(f"Aviso: falló la búsqueda automática de rivales: {exc!r}", file=sys.stderr)
                    reported = True
            self._stop.wait(TICK_S)
