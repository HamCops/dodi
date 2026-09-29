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
    # Approval queue. With require_approval, roster moves and trades are only
    # sent to ESPN through an approved proposal; lineup changes stay direct.
    require_approval: bool = False
    # With auto_apply, a requested move that passes autopolicy.auto_ok is
    # applied at once and the manager is told afterwards; the rest still
    # wait on his approval.
    auto_apply: bool = False
    # Minutes after which an undecided proposal is pushed again (once), and
    # at most how many reminders go out in one hour across all proposals.
    remind_after_minutes: int = 60
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    approve_base_url: str | None = None
    approve_port: int = 6075
    # Read Sleeper and FantasyCalc for trade values, pickup trends and a
    # second projection. Off unless asked for: it calls third parties.
    external_sources: bool = False
    # The manager's clock: how long he wants to decide in, where he is, and
    # what to call when it is time for the agent to look (see gametime.py).
    approval_lead_minutes: int = 30
    timezone: str = "America/New_York"
    gametime_hook: str | None = None

    @property
    def state_root(self) -> Path:
        return (Path(self.state_dir) if self.state_dir
                else Path(__file__).resolve().parents[2] / "state")

    @property
    def has_auth(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in output; used to scrub error text."""
        return tuple(v for v in (self.espn_s2, self.swid, self.ntfy_token) if v)

    def __repr__(self) -> str:
        return (f"Config(league_id={self.league_id!r}, season={self.season!r}, "
                f"team_id={self.team_id!r}, espn_s2={mask(self.espn_s2)!r}, "
                f"swid={mask(self.swid)!r}, pool_ttl={self.pool_ttl!r}, "
                f"state_dir={self.state_dir!r})")


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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
        require_approval=_flag(os.environ.get("ESPN_REQUIRE_APPROVAL")),
        auto_apply=_flag(os.environ.get("ESPN_AUTO_APPLY")),
        remind_after_minutes=int(os.environ.get("REMIND_AFTER_MINUTES", "60")),
        ntfy_url=os.environ.get("NTFY_URL", "").strip() or None,
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip() or None,
        ntfy_token=os.environ.get("NTFY_TOKEN", "").strip() or None,
        approve_base_url=os.environ.get("APPROVE_BASE_URL", "").strip() or None,
        approve_port=int(os.environ.get("APPROVE_PORT", "6075")),
        external_sources=_flag(os.environ.get("ESPN_EXTERNAL_SOURCES")),
        approval_lead_minutes=int(os.environ.get("APPROVAL_LEAD_MINUTES", "30")),
        timezone=os.environ.get("ESPN_TIMEZONE", "").strip() or "America/New_York",
        gametime_hook=os.environ.get("GAMETIME_HOOK", "").strip() or None,
    )
