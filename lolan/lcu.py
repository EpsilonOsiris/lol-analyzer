"""League client (LCU API) champion-select recorder: python -m lolan grabar-seleccion.

While the League client is open it writes `lockfile` in its install folder (`LeagueClient:<pid>:
<port>:<password>:<protocol>`) and serves a local HTTPS API on that port, with HTTP basic auth
(user `riot`, the password from the lockfile). Among other things it says which phase the player is
in (lobby, queue, champion select, game...) and, during champion select, the whole draft: bans,
picks, hovers, summoner spells, timers. Nothing here uses Riot's web API or your key, and nothing
leaves the PC.

Why a recorder: the field names of those endpoints are not documented by Riot, so instead of
guessing them this module saves what the client really answers during your next champion select
(JSON Lines in data/capturas/seleccion-YYYYMMDD-HHMMSS.jsonl, newest 30 kept), to build and test a
draft page against real data.

This module has four parts:
  * lockfile helpers: where the lockfile is and how it is read (parsed defensively).
  * LcuClient: the HTTPS client (loopback only; same TLS handling as the game client, see live.py).
  * ChampSelectRecorder: a state machine that follows the gameflow phase and writes the capture,
    driven by a daemon thread (or step() by step() from tests and from the CLI).
  * observer hook: the recorder's loop is the panel's single poller of the client; an observer
    (see draft_live.DraftFeed) gets (phase, client, champ-select session) on every poll, so the
    Partida page needs no second loop. LcuFollower is the same loop without the recording, for
    when the recorder is switched off.

Capture format, one JSON object per line, all with "t" (unix seconds):
  {"kind":"phase","phase":...}                 every phase change seen while a capture is open
  {"kind":"gameflow_session","session":...}    raw /lol-gameflow/v1/session (at the start; once more
                                               when the game starts, it carries the gameId)
  {"kind":"lobby","lobby":...}                 raw /lol-lobby/v2/lobby (null when the client has none)
  {"kind":"champ_select","session":...}        raw /lol-champ-select/v1/session, only when it changed
                                               apart from the ticking clock fields of its timer
  {"kind":"end","result":"game"|"dodge"|...}   last line: "game" (it went on to GameStart/InProgress),
                                               "dodge" (back to the lobby / queue), "closed" (the client
                                               closed), "stopped" (the recorder was stopped), "other"

Privacy: the capture keeps the raw data only on this PC, to build the draft page. In ranked solo/duo
the client anonymizes teammates and enemies during champion select, and no code may ever use a
capture (or anything else) to work out who an anonymous player is.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

from .live import CA_FILE, LOOPBACK_HOSTS, build_loopback_sessions, get_loopback

LOCKFILE_ENV = "LOLAN_LCU_LOCKFILE"       # full path of the lockfile (tests); wins over everything
DEFAULT_LOCKFILE = Path(r"C:\Riot Games\League of Legends\lockfile")
DISABLE_ENV = "LOLAN_GRABAR_SELECCION"    # "off": the panel does not start the recorder
LCU_USER = "riot"
HOST = "127.0.0.1"

TIMEOUT_S = 1.5
FAST_S = 1.0          # the client answers
IDLE_S = 5.0          # no lockfile, or the client does not answer
KEEP_FILES = 30       # newest capture files kept
DOWN_AFTER = 3        # unanswered polls in a row before an open capture counts as "client closed"
FILE_PREFIX, FILE_SUFFIX = "seleccion-", ".jsonl"

PHASE_PATH = "/lol-gameflow/v1/gameflow-phase"
GAMEFLOW_PATH = "/lol-gameflow/v1/session"
CHAMP_SELECT_PATH = "/lol-champ-select/v1/session"
LOBBY_PATH = "/lol-lobby/v2/lobby"

CHAMP_SELECT = "ChampSelect"
GAME_PHASES = {"GameStart", "InProgress", "Reconnect"}          # champion select ended in a game
DODGE_PHASES = {"None", "Lobby", "Matchmaking", "ReadyCheck"}   # ...or went back to the lobby


def recorder_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() != "off"


# ------------------------------------------------------------------ lockfile
class Lockfile:
    __slots__ = ("pid", "port", "password", "protocol")

    def __init__(self, pid: int, port: int, password: str, protocol: str):
        self.pid, self.port, self.password, self.protocol = pid, port, password, protocol

    def __repr__(self) -> str:       # never shows the password
        return f"Lockfile(pid={self.pid}, port={self.port}, protocol={self.protocol!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Lockfile) and all(getattr(self, k) == getattr(other, k) for k in self.__slots__)


def parse_lockfile(text) -> Lockfile | None:
    """`name:pid:port:password:protocol`, or None when it is not that (empty, cut, odd values)."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(":")
    if len(parts) != 5:
        return None
    _name, pid, port, password, protocol = parts
    try:
        pid_n, port_n = int(pid), int(port)
    except ValueError:
        return None
    protocol = protocol.strip().lower()
    if not (0 < port_n < 65536) or not password or protocol not in ("https", "http"):
        return None
    return Lockfile(pid_n, port_n, password, protocol)


def lockfile_path(root=None) -> Path:
    """LOLAN_LCU_LOCKFILE, else LOL_DIR (.env) + `lockfile`, else the default install folder."""
    override = os.environ.get(LOCKFILE_ENV, "").strip()
    if override:
        return Path(override)
    if root is not None:
        from .settings import read_settings

        folder = read_settings(root).lol_dir
        if folder:
            return Path(folder) / "lockfile"
    return DEFAULT_LOCKFILE


def read_lockfile(path) -> Lockfile | None:
    """The parsed lockfile; None when it does not exist (client closed) or can't be read."""
    try:
        return parse_lockfile(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# -------------------------------------------------------------------- client
class LcuClient:
    """GETs from the League client. Never raises: returns (kind, data).

    kind is "ok" (data = parsed JSON), "none" (404 or no body: not in that phase) or "down" (no
    lockfile, nobody answers, wrong credentials, certificate refused; `problem` says "tls" or
    "auth" when it is one of those). TLS and its fallbacks are exactly the game client's (live.py).
    """

    def __init__(self, lockfile=None, ca_file=CA_FILE, timeout=TIMEOUT_S, host=HOST,
                 system_fallback=True, unverified_fallback=True):
        """`lockfile`: a path, or a callable returning one (default: lockfile_path())."""
        if host not in LOOPBACK_HOSTS:
            raise ValueError("El cliente de League solo puede leerse en una dirección local.")
        self.host = host
        self.timeout = timeout
        self._lockfile = lockfile or lockfile_path
        self._ca_file, self._system, self._unverified = ca_file, system_fallback, unverified_fallback
        self._sessions: list = []
        self._key: tuple | None = None        # (port, protocol) the sessions were built for
        self.problem: str | None = None
        self.tls_mode: str | None = None

    def credentials(self) -> Lockfile | None:
        path = self._lockfile() if callable(self._lockfile) else self._lockfile
        return read_lockfile(path)

    def base_url(self, lock: Lockfile) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{lock.protocol}://{host}:{lock.port}"

    def get(self, path: str):
        lock = self.credentials()
        if lock is None:
            self.problem = None
            return "down", None
        try:
            key = (lock.port, lock.protocol)
            if key != self._key:                      # the client was restarted: new port
                self.close()
                self._sessions = build_loopback_sessions(self.base_url(lock), self._ca_file,
                                                         self._system, self._unverified)
                self._key = key
            resp, mode = get_loopback(self._sessions, self.base_url(lock) + path, self.timeout,
                                      auth=(LCU_USER, lock.password))
        except requests.exceptions.SSLError:
            self.problem = "tls"
            return "down", None
        except (requests.RequestException, OSError, ValueError):
            self.problem = None
            return "down", None
        self.tls_mode = mode if mode != "plain" else None
        self.problem = None
        if resp.status_code in (401, 403):
            self.problem = "auth"                    # stale lockfile or another client
            return "down", None
        if resp.status_code != 200:
            return "none", None
        try:
            return "ok", resp.json()
        except ValueError:
            return "none", None

    def _value(self, path):
        kind, data = self.get(path)
        return data if kind == "ok" else None

    def phase(self) -> str | None:
        """The gameflow phase ("None", "Lobby", "ChampSelect", ...), or None when the client is closed."""
        kind, data = self.get(PHASE_PATH)
        return data if kind == "ok" and isinstance(data, str) else None

    def gameflow_session(self):
        return self._value(GAMEFLOW_PATH)

    def champ_select(self):
        return self._value(CHAMP_SELECT_PATH)

    def lobby(self):
        return self._value(LOBBY_PATH)

    def close(self) -> None:
        for _mode, session in self._sessions:
            session.close()
        self._sessions, self._key = [], None


# ------------------------------------------------------------------ recorder
def _is_clock_key(key: str) -> bool:
    low = str(key).lower()
    return "timeleft" in low or low.startswith("internalnow")


def stable_form(session):
    """The champion-select session without the ticking clock fields of its timer, to compare snapshots."""
    if not isinstance(session, dict) or not isinstance(session.get("timer"), dict):
        return session
    timer = {k: v for k, v in session["timer"].items() if not _is_clock_key(k)}
    return {**session, "timer": timer}


class ChampSelectRecorder:
    """Follows the gameflow phase and writes one capture file per champion select.

    `step()` does one poll and returns how long to wait before the next one; the daemon thread
    (start/stop) calls it in a loop. Nothing here raises: problems are one short warning per kind.
    """

    def __init__(self, client=None, out_dir=None, clock=time.time, keep=KEEP_FILES, say=print,
                 warn=None, on_phase=None, fast_s=FAST_S, idle_s=IDLE_S, root=None, observer=None):
        """`observer(phase, client, session)`: called after every poll; `session` is the raw
        champion-select session during ChampSelect (None otherwise, or when it could not be read)."""
        self._client = client
        self._observer = observer
        self._root = root
        self.out_dir = Path(out_dir) if out_dir else Path("data") / "capturas"
        self._clock = clock
        self.keep = keep
        self._say = say
        self._warn_out = warn or (lambda text: print(text, file=sys.stderr))
        self._on_phase = on_phase
        self.fast_s, self.idle_s = fast_s, idle_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase: str | None = None
        self._misses = 0
        self._fh = None
        self._path: Path | None = None
        self._last_session = None
        self._warned: set = set()
        self.files: list[Path] = []          # captures saved by this recorder

    # ---- output
    def warn(self, kind: str, text: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            self._warn_out(f"Aviso: {text}")

    def _write(self, kind: str, **fields) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps({"t": self._clock(), "kind": kind, **fields}, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, TypeError, ValueError) as exc:
            self.warn("archivo", f"no se pudo escribir la grabación de la selección: {exc!r}")
            self._drop_file()

    def _drop_file(self) -> None:
        fh, self._fh = self._fh, None
        try:
            if fh is not None:
                fh.close()
        except OSError:
            pass

    def _open(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / f"{FILE_PREFIX}{stamp}{FILE_SUFFIX}"
            n = 1
            while path.exists():
                n += 1
                path = self.out_dir / f"{FILE_PREFIX}{stamp}-{n}{FILE_SUFFIX}"
            self._fh = open(path, "w", encoding="utf-8")
        except OSError as exc:
            self.warn("archivo", f"no se pudo crear la grabación de la selección: {exc!r}")
            self._fh = None
            return
        self._path = path
        self._last_session = None
        self._rotate()

    def _rotate(self) -> None:
        try:
            old = sorted(self.out_dir.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"))
            for path in old[:-self.keep] if self.keep > 0 else old:
                if path != self._path:
                    path.unlink()
        except OSError as exc:
            self.warn("rotar", f"no se pudieron borrar grabaciones viejas: {exc!r}")

    def _finish(self, result: str, phase: str | None = None) -> None:
        """Close the open capture with its end line."""
        if self._path is None:
            return
        if self._fh is not None:
            extra = {"phase": phase} if phase else {}
            self._write("end", result=result, **extra)
        self._drop_file()
        path, self._path = self._path, None
        self._last_session = None
        if path.exists():
            self.files.append(path)
            self._say(f"Selección de campeones grabada: {self._display(path)}")

    def _display(self, path: Path) -> str:
        return f"data/capturas/{path.name}" if self.out_dir.name == "capturas" else str(path)

    # ---- one poll
    def client(self):
        if self._client is None:
            self._client = LcuClient(lockfile=lambda: lockfile_path(self._root))
        return self._client

    def step(self) -> float:
        try:
            return self._step()
        except Exception as exc:        # a surprise in the client's data must never kill the thread
            self.warn("lectura", f"falló la grabación de la selección de campeones: {exc!r}")
            return self.idle_s

    def _step(self) -> float:
        client = self.client()
        phase = client.phase()
        problem = getattr(client, "problem", None)
        if problem == "tls":
            self.warn("tls", "el cliente de League no acepta el certificado; no se grabará la selección.")
        elif problem == "auth":
            self.warn("auth", "el cliente de League rechazó la clave del lockfile; "
                              "ciérralo y ábrelo de nuevo para grabar la selección.")
        if phase is None:
            self._misses += 1
            if self._phase is None:
                self._observe(None, client, None)
                return self.idle_s
            if self._misses < DOWN_AFTER:
                return self.fast_s              # one slow answer must not close a champion select
            self._changed(None)
            self._finish("closed")
            self._phase = None
            self._observe(None, client, None)
            return self.idle_s
        self._misses = 0
        if phase != self._phase:
            self._changed(phase)
            if phase == CHAMP_SELECT:
                if self._path is None:
                    self._start_capture(phase, client)
            elif self._path is not None:
                self._leave(phase, client)
            self._phase = phase
        session = None
        if phase == CHAMP_SELECT and (self._path is not None or self._observer is not None):
            session = client.champ_select()          # one read serves the capture and the observer
        if session is not None and self._path is not None:
            self._snapshot(session)
        self._observe(phase, client, session)
        return self.fast_s

    def _observe(self, phase, client, session) -> None:
        if self._observer is not None:
            try:
                self._observer(phase, client, session)
            except Exception as exc:            # an observer's bug must not stop the recording
                self.warn("observador", f"falló el seguimiento de la selección de campeones: {exc!r}")

    def _changed(self, phase) -> None:
        if self._on_phase is not None:
            try:
                self._on_phase(phase)
            except Exception:
                pass

    def _start_capture(self, phase, client) -> None:
        self._open()
        self._write("phase", phase=phase)
        self._write("gameflow_session", session=client.gameflow_session())
        self._write("lobby", lobby=client.lobby())

    def _snapshot(self, session) -> None:
        form = stable_form(session)
        if form == self._last_session:
            return
        self._last_session = form
        self._write("champ_select", session=session)      # the raw session, clock fields included

    def _leave(self, phase, client) -> None:
        self._write("phase", phase=phase)
        if phase in GAME_PHASES:
            self._write("gameflow_session", session=client.gameflow_session())
            self._finish("game", phase)
        elif phase in DODGE_PHASES:
            self._finish("dodge", phase)
        else:
            self._finish("other", phase)

    # ---- thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lolan-lcu")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
        self.close()

    def close(self) -> None:
        """Finish a capture that is still open (the recorder is stopping)."""
        if self._path is not None:
            self._finish("stopped", self._phase)
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.step())


# ------------------------------------------------------------------ follower
class LcuFollower:
    """The recorder's loop without the recording: polls the phase (and the champion-select session
    during ChampSelect) and feeds `observer(phase, client, session)`. Used when the recorder is off."""

    def __init__(self, observer, client=None, root=None, fast_s=FAST_S, idle_s=IDLE_S):
        self._observer = observer
        self._client = client
        self._root = root
        self.fast_s, self.idle_s = fast_s, idle_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def client(self):
        if self._client is None:
            self._client = LcuClient(lockfile=lambda: lockfile_path(self._root))
        return self._client

    def step(self) -> float:
        try:
            client = self.client()
            phase = client.phase()
            session = client.champ_select() if phase == CHAMP_SELECT else None
            self._observer(phase, client, session)
            return self.idle_s if phase is None else self.fast_s
        except Exception:                      # nothing the client says may kill the thread
            return self.idle_s

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lolan-lcu-follow")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.step())
