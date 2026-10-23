"""User settings kept in .env: friendly server names, atomic writes and API key helpers."""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .config import PLATFORM_REGIONS

DEFAULT_CLAN_PREFIX = "Epsilon"      # CLAN_PREFIX default; comps.py imports it from here

# (platform, label shown in the settings page); LAN first because it is the default.
PLATFORM_CHOICES = [
    ("la1", "LAN (Latinoamérica Norte)"), ("la2", "LAS (Latinoamérica Sur)"),
    ("na1", "NA (Norteamérica)"), ("br1", "BR (Brasil)"),
    ("euw1", "EUW (Europa Oeste)"), ("eun1", "EUNE (Europa Norte y Este)"),
    ("kr", "KR (Corea)"), ("jp1", "JP (Japón)"), ("oc1", "OCE (Oceanía)"),
    ("tr1", "TR (Turquía)"), ("ru", "RU (Rusia)"), ("me1", "ME (Medio Oriente)"),
    ("ph2", "PH (Filipinas)"), ("sg2", "SG (Singapur)"), ("th2", "TH (Tailandia)"),
    ("tw2", "TW (Taiwán)"), ("vn2", "VN (Vietnam)"),
]
DEFAULT_PLATFORM = "la1"
# Development keys last 24 h; warn a bit earlier so the user renews before it fails.
KEY_MAX_AGE_SECONDS = 23 * 3600

_KEY_RE = re.compile(r"[A-Za-z0-9_\-]+")
_PLAIN_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-/:@+]*")
_LINE_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_write_lock = threading.Lock()


@dataclass
class Settings:
    riot_id: str = ""
    platform: str = DEFAULT_PLATFORM
    riot_key: str = ""
    gemini_key: str = ""
    riot_key_saved_at: float | None = None
    riot_key_permanent: bool = False
    clan_prefix: str = DEFAULT_CLAN_PREFIX  # game names starting with this belong to the clan
    clan_aliases: str = ""          # "Cuenta=Persona;Otra=Persona": accounts of the same person
    lol_dir: str = ""               # League of Legends install folder (where `lockfile` lives); "" = default
    raw: dict | None = None         # values as read, keyed by .env name

    @property
    def ready(self) -> bool:
        """Enough to start using the panel."""
        return bool(self.riot_key and self.riot_id)


def read_settings(root) -> Settings:
    """Current settings; never raises. The file wins over process variables (see load_config)."""
    root = Path(root)
    try:
        file_values = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
    except (OSError, UnicodeDecodeError):
        file_values = {}

    def get(name):
        return (file_values.get(name, os.environ.get(name)) or "").strip()

    try:
        saved_at = float(get("RIOT_API_KEY_SAVED_AT"))
    except ValueError:
        saved_at = None
    names = ("RIOT_ID", "PLATFORM", "REGION", "RIOT_API_KEY", "GEMINI_API_KEY",
             "RIOT_API_KEY_SAVED_AT", "RIOT_API_KEY_PERMANENT", "CLAN_PREFIX", "CLAN_ALIASES", "LOL_DIR")
    return Settings(
        riot_id=get("RIOT_ID"),
        platform=get("PLATFORM").lower() or DEFAULT_PLATFORM,
        riot_key=get("RIOT_API_KEY"),
        gemini_key=get("GEMINI_API_KEY"),
        riot_key_saved_at=saved_at,
        riot_key_permanent=get("RIOT_API_KEY_PERMANENT").lower() in ("1", "true", "si", "sí"),
        clan_prefix=get("CLAN_PREFIX") or DEFAULT_CLAN_PREFIX,
        clan_aliases=get("CLAN_ALIASES"),
        lol_dir=get("LOL_DIR"),
        raw={n: get(n) for n in names},
    )


def mask_key(key: str) -> str:
    """What the page may show of a saved key: only its last 4 characters."""
    if not key:
        return ""
    return "••••" + (key[-4:] if len(key) >= 8 else "")


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.fullmatch(key))


def platform_region(platform: str) -> str | None:
    return PLATFORM_REGIONS.get(platform)


# --------------------------------------------------------------- .env writing
def _format_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("El valor no puede tener saltos de línea.")
    if _PLAIN_VALUE_RE.fullmatch(value):
        return value
    # Single quotes: python-dotenv doesn't expand ${...} or treat '#' as a comment inside them.
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def update_env_file(path, updates: dict[str, str]) -> None:
    """Set `updates` in the .env file, keeping every other line (comments, unknown keys) as is.

    Written to a temporary file in the same folder and swapped in with os.replace, so a crash
    never leaves a half-written .env.
    """
    path = Path(path)
    new_lines = {k: f"{k}={_format_value(str(v))}" for k, v in updates.items()}
    with _write_lock:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except FileNotFoundError:
            text = ""
        eol = "\r\n" if "\r\n" in text else ("\n" if "\n" in text else os.linesep)
        lines = text.splitlines()
        done = set()
        for i, line in enumerate(lines):
            m = _LINE_KEY_RE.match(line)
            if m and m.group(1) in new_lines:
                lines[i] = new_lines[m.group(1)]
                done.add(m.group(1))
        lines += [new_lines[k] for k in new_lines if k not in done]
        data = (eol.join(lines) + eol).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:      # antivirus or another program holding the file
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ------------------------------------------------------------------ key health
class KeyHealth:
    """Remembers (in memory only) the key Riot last rejected, so pages can warn without a request."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bad: str | None = None

    def mark_invalid(self, key: str) -> None:
        with self._lock:
            self._bad = key or None

    def mark_ok(self, key: str) -> None:
        with self._lock:
            if self._bad == key:
                self._bad = None

    def is_invalid(self, key: str) -> bool:
        with self._lock:
            return bool(key) and self._bad == key


def key_warning(st: Settings, health: KeyHealth, now: float | None = None) -> str | None:
    """None when the Riot key looks fine; "rechazada" if Riot refused it, "vieja" if it is old."""
    if not st.riot_key:
        return None
    if health.is_invalid(st.riot_key):
        return "rechazada"
    if st.riot_key_permanent or st.riot_key_saved_at is None:
        return None
    if (time.time() if now is None else now) - st.riot_key_saved_at > KEY_MAX_AGE_SECONDS:
        return "vieja"
    return None


def age_text(saved_at: float | None, now: float | None = None) -> str:
    """"hace 5 horas"-style text for when the key was saved ("" if unknown)."""
    if saved_at is None:
        return ""
    hours = int(((time.time() if now is None else now) - saved_at) // 3600)
    if hours < 1:
        return "hace menos de una hora"
    if hours < 48:
        return f"hace {hours} hora" + ("" if hours == 1 else "s")
    return f"hace {hours // 24} días"
