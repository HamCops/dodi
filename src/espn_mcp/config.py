"""Runtime configuration, loaded from the environment (or a local .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader so we don't take a dependency for five variables."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


@dataclass(frozen=True)
class Config:
    league_id: str
    season: int
    team_id: int | None
    espn_s2: str | None
    swid: str | None
    pool_ttl: int
    state_dir: str | None

    @property
    def has_auth(self) -> bool:
        return bool(self.espn_s2 and self.swid)


def load_config() -> Config:
    _load_dotenv()
    league_id = os.environ.get("ESPN_LEAGUE_ID", "").strip()
    if not league_id:
        raise RuntimeError(
            "ESPN_LEAGUE_ID is not set. Copy .env.example to .env and fill it in."
        )

    team_id = os.environ.get("ESPN_TEAM_ID", "").strip()
    swid = os.environ.get("SWID", "").strip() or None
    if swid and not swid.startswith("{"):
        # ESPN stores SWID wrapped in braces; tolerate a paste that dropped them.
        swid = "{" + swid.strip("{}") + "}"

    return Config(
        league_id=league_id,
        season=int(os.environ.get("ESPN_SEASON", "2026")),
        team_id=int(team_id) if team_id else None,
        espn_s2=os.environ.get("ESPN_S2", "").strip() or None,
        swid=swid,
        pool_ttl=int(os.environ.get("ESPN_POOL_TTL", "900")),
        state_dir=os.environ.get("ESPN_STATE_DIR") or None,
    )
