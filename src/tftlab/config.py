from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    riot_api_key: str | None
    platform: str = "na1"
    region: str = "americas"
    db_path: Path = Path("data/tftlab.sqlite3")
    database_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            riot_api_key=os.getenv("RIOT_API_KEY") or None,
            platform=os.getenv("TFT_PLATFORM", "na1").lower(),
            region=os.getenv("TFT_REGION", "americas").lower(),
            db_path=Path(os.getenv("TFT_DB_PATH", "data/tftlab.sqlite3")),
            database_url=os.getenv("DATABASE_URL") or None,
        )
