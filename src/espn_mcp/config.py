"""Runtime configuration, loaded from the environment (or a local .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# The one place credentials are read from. Deliberately not the current
# directory: whatever launches the server picks the cwd, and a stray .env
# there must not be able to swap the league or the session cookies.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader so we don't take a dependency for five variables.

    Real environment variables win over the file.
    """
    path = ENV_FILE if path is None else path
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mask(secret: str | None) -> str | None:
    """A secret as it may appear in logs or reprs: presence, never content."""
    if not secret:
        return None
    return "***"


@dataclass(frozen=True, repr=False)
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

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in output; used to scrub error text."""
        return tuple(v for v in (self.espn_s2, self.swid) if v)

    def __repr__(self) -> str:
        return (f"Config(league_id={self.league_id!r}, season={self.season!r}, "
                f"team_id={self.team_id!r}, espn_s2={mask(self.espn_s2)!r}, "
                f"swid={mask(self.swid)!r}, pool_ttl={self.pool_ttl!r}, "
                f"state_dir={self.state_dir!r})")


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
