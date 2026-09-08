"""HTTP client for ESPN's (undocumented) v3 fantasy API.

Everything the draft tools need comes from one league endpoint with different
`view` parameters. The player pool additionally needs an `x-fantasy-filter`
header, which is where paging, status filtering and stat-window selection live.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Config

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# ESPN rejects requests without a browser-ish UA from some networks.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class ESPNError(RuntimeError):
    """Raised with an actionable message when the API refuses a request."""


class ESPNClient:
    def __init__(self, cfg: Config, timeout: float = 20.0) -> None:
        self.cfg = cfg
        cookies = {}
        if cfg.has_auth:
            cookies = {"espn_s2": cfg.espn_s2, "SWID": cfg.swid}
        self._client = httpx.Client(
            headers=_HEADERS, cookies=cookies, timeout=timeout, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    @property
    def _league_url(self) -> str:
        return f"{BASE}/seasons/{self.cfg.season}/segments/0/leagues/{self.cfg.league_id}"

    def _get(self, url: str, *, views: list[str], fantasy_filter: dict | None = None,
             params: dict | None = None) -> Any:
        headers = {}
        if fantasy_filter is not None:
            headers["x-fantasy-filter"] = json.dumps(fantasy_filter)
        query: dict[str, Any] = dict(params or {})
        # httpx encodes a list value as repeated keys, which is what ESPN wants.
        query["view"] = views

        try:
            resp = self._client.get(url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise ESPNError(f"Network error talking to ESPN: {exc}") from exc

        if resp.status_code in (401, 403):
            raise ESPNError(
                "ESPN rejected the credentials (HTTP "
                f"{resp.status_code}). For a private league, ESPN_S2 and SWID must be "
                "set and current -- they expire, so re-copy them from a logged-in "
                "browser (DevTools > Application > Cookies > fantasy.espn.com)."
            )
        if resp.status_code == 404:
            raise ESPNError(
                f"League {self.cfg.league_id} not found for season {self.cfg.season}. "
                "Check ESPN_LEAGUE_ID and ESPN_SEASON."
            )
        if resp.status_code >= 400:
            raise ESPNError(f"ESPN returned HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            return resp.json()
        except ValueError as exc:
            raise ESPNError("ESPN returned a non-JSON body (often an auth redirect).") from exc

    # --- League views -----------------------------------------------------

    def league(self, views: list[str]) -> dict:
        data = self._get(self._league_url, views=views)
        # Some season/league combinations return a single-element list.
        if isinstance(data, list):
            if not data:
                raise ESPNError("ESPN returned an empty league payload.")
            return data[0]
        return data

    def settings(self) -> dict:
        return self.league(["mSettings"])

    def teams(self) -> dict:
        return self.league(["mTeam", "mRoster"])

    def draft_detail(self) -> dict:
        """Live draft state. Never cache this."""
        return self.league(["mDraftDetail", "mTeam"])

    def pro_team_byes(self) -> dict[int, int]:
        """proTeamId -> bye week. Not on the player record; a separate view."""
        data = self._get(f"{BASE}/seasons/{self.cfg.season}",
                         views=["proTeamSchedules_wl"])
        if isinstance(data, list):
            data = data[0] if data else {}
        teams = (data.get("settings") or {}).get("proTeams") or []
        return {int(t["id"]): int(t["byeWeek"])
                for t in teams if t.get("byeWeek")}

    # --- Player pool ------------------------------------------------------

    def player_pool(self, *, limit: int = 700, offset: int = 0,
                    status: tuple[str, ...] = ("FREEAGENT", "WAIVERS", "ONTEAM")) -> list[dict]:
        """Draftable players with league-scored season projections.

        `filterStatsForTopScoringPeriodIds.additionalValue` selects which stat
        splits come back: "00{season}" is season-to-date actuals and
        "10{season}" is the full-season projection.
        """
        season = self.cfg.season
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": list(status)},
                "limit": limit,
                "offset": offset,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                "filterStatsForTopScoringPeriodIds": {
                    "value": 16,
                    "additionalValue": [
                        f"00{season}",
                        f"10{season}",
                        f"00{season - 1}",
                    ],
                },
            }
        }
        data = self._get(
            self._league_url,
            views=["kona_player_info"],
            fantasy_filter=fantasy_filter,
            params={"scoringPeriodId": 0},
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        players = data.get("players") or []
        if not players:
            raise ESPNError(
                "ESPN returned no players for this league. If the league is private, "
                "the credentials are likely stale."
            )
        return players
