"""Configuration loading (.env) and platform/region helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Platform (game server) -> regional routing value used by account-v1 / match-v5.
PLATFORM_REGIONS = {
    "la1": "americas", "la2": "americas", "na1": "americas", "br1": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}
VALID_REGIONS = {"americas", "europe", "asia", "sea"}


class ConfigError(Exception):
    """User-facing configuration problem (message is already in Spanish)."""


@dataclass
class Config:
    api_key: str
    riot_id: str
    platform: str
    region: str
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lol.db"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"


def project_root() -> Path:
    """Project folder. LOLAN_HOME overrides it (useful for tests)."""
    override = os.environ.get("LOLAN_HOME")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent


def load_config(root: Path | None = None) -> Config:
    root = Path(root) if root else project_root()
    load_dotenv(root / ".env")
    platform = (os.environ.get("PLATFORM") or "la1").strip().lower()
    region = (os.environ.get("REGION") or "").strip().lower()
    if not region:
        region = PLATFORM_REGIONS.get(platform, "americas")
    if region not in VALID_REGIONS:
        raise ConfigError(
            f"REGION '{region}' no es válida. Usa una de: {', '.join(sorted(VALID_REGIONS))}."
        )
    return Config(
        api_key=(os.environ.get("RIOT_API_KEY") or "").strip(),
        riot_id=(os.environ.get("RIOT_ID") or "").strip(),
        platform=platform,
        region=region,
        root=root,
    )
