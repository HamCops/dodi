"""HTTP client for ESPN's (undocumented) v3 fantasy API.

Everything the draft tools need comes from one league endpoint with different
`view` parameters. The player pool additionally needs an `x-fantasy-filter`
header, which is where paging, status filtering and stat-window selection live.
"""

from __future__ import annotations

import json
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Config

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# The only domain the login cookies may ever be sent to, and the only one a
# request may start at: anything else is refused before it is made, so no
# future call site can point the session elsewhere. Redirects are followed,
# so the cookies are also marked Secure: an https-to-http hop, even inside
# espn.com, goes without them.
COOKIE_DOMAIN = ".espn.com"
ALLOWED_HOST_SUFFIX = ".espn.com"


def host_allowed(url: str) -> bool:
    """True for https URLs on espn.com or a subdomain of it."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "espn.com" or host.endswith(ALLOWED_HOST_SUFFIX))

# ESPN rejects requests without a browser-ish UA from some networks.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _secure_cookie(name: str, value: str) -> Cookie:
    """A session cookie scoped to ESPN and flagged Secure (https only)."""
    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=COOKIE_DOMAIN, domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )


class ESPNError(RuntimeError):
    """Raised with an actionable message when the API refuses a request."""


class ESPNClient:
    def __init__(self, cfg: Config, timeout: float = 20.0) -> None:
        self.cfg = cfg
        # Scope the session cookies to ESPN. A plain dict makes httpx attach
        # them to every host, so a redirect off espn.com would leak the login.
        cookies = httpx.Cookies()
        if cfg.has_auth:
            for name, value in (("espn_s2", cfg.espn_s2), ("SWID", cfg.swid)):
                cookies.jar.set_cookie(_secure_cookie(name, value))
        self._client = httpx.Client(
            headers=_HEADERS, cookies=cookies, timeout=timeout, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def _scrub(self, text: str) -> str:
        """Strip the session cookies out of anything that becomes an error message."""
        for secret in self.cfg.secrets:
            text = text.replace(secret, "***")
        return text

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

        if not host_allowed(url):
            raise ESPNError(f"Refusing to call a non-ESPN URL: {url}")
        try:
            resp = self._client.get(url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            # Scrub the cause too: a formatted traceback prints it verbatim.
            exc.args = tuple(self._scrub(a) if isinstance(a, str) else a for a in exc.args)
            raise ESPNError(self._scrub(f"Network error talking to ESPN: {exc}")) from exc

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
            raise ESPNError(f"ESPN returned HTTP {resp.status_code}: {self._scrub(resp.text)[:300]}")

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
        # mStatus carries the current scoring period, which every in-season
        # tool keys off; it is cheap, so always fetch it with settings.
        return self.league(["mSettings", "mStatus"])

    def teams(self) -> dict:
        return self.league(["mTeam", "mRoster"])

    # --- In-season views --------------------------------------------------

    def rosters(self, week: int) -> dict:
        """Every team's roster with lineup slots as set for `week`.

        Includes mTeam for records, waiver priority, FAAB spent and trade
        blocks. Never cache for long: waivers and trades change it.
        """
        data = self._get(self._league_url, views=["mTeam", "mRoster"],
                         params={"scoringPeriodId": week})
        if isinstance(data, list):
            data = data[0] if data else {}
        return data

    def matchups(self, week: int) -> list[dict]:
        """The league schedule with ESPN's live projections and win probability."""
        data = self._get(self._league_url, views=["mMatchupScore", "mScoreboard"],
                         params={"scoringPeriodId": week})
        if isinstance(data, list):
            data = data[0] if data else {}
        return data.get("schedule") or []

    def pro_schedule(self) -> dict[int, dict]:
        """proTeamId -> {bye, abbrev, games: {week: {opponent_id, home, kickoff_ms}}}."""
        data = self._get(f"{BASE}/seasons/{self.cfg.season}",
                         views=["proTeamSchedules_wl"])
        if isinstance(data, list):
            data = data[0] if data else {}
        out: dict[int, dict] = {}
        for t in (data.get("settings") or {}).get("proTeams") or []:
            tid = int(t["id"])
            games: dict[int, dict] = {}
            for week, entries in (t.get("proGamesByScoringPeriod") or {}).items():
                for g in entries or []:
                    home = int(g.get("homeProTeamId") or 0)
                    away = int(g.get("awayProTeamId") or 0)
                    games[int(week)] = {
                        "opponent_id": away if home == tid else home,
                        "home": home == tid,
                        "kickoff_ms": g.get("date"),
                    }
            out[tid] = {"abbrev": t.get("abbrev"), "bye": t.get("byeWeek"), "games": games}
        return out

    def positional_ratings(self, week: int) -> dict[int, dict[int, dict]]:
        """positionId -> proTeamId -> {average, rank}: points a defense allows.

        ESPN's OPRK. Empty until games have been played.
        """
        data = self._get(self._league_url, views=["mPositionalRatings"],
                         params={"scoringPeriodId": week})
        if isinstance(data, list):
            data = data[0] if data else {}
        ratings = ((data.get("positionAgainstOpponent") or {})
                   .get("positionalRatings") or {})
        out: dict[int, dict[int, dict]] = {}
        for pos_id, block in ratings.items():
            by_opp = (block or {}).get("ratingsByOpponent") or {}
            if not by_opp:
                continue
            out[int(pos_id)] = {
                int(tid): {"average": r.get("average"), "rank": r.get("rank")}
                for tid, r in by_opp.items()
            }
        return out

    def draft_detail(self) -> dict:
        """Live draft state. Never cache this."""
        return self.league(["mDraftDetail", "mTeam"])

    def transactions(self) -> list[dict]:
        """Every transaction this season: draft picks, adds, drops, waivers,
        trades and lineup moves, each with a team, timestamp and week."""
        data = self.league(["mTransactions2"])
        return data.get("transactions") or []

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
                    status: tuple[str, ...] = ("FREEAGENT", "WAIVERS", "ONTEAM"),
                    week: int | None = None) -> list[dict]:
        """Players with league-scored season projections.

        `filterStatsForTopScoringPeriodIds.additionalValue` selects which stat
        splits come back: "00{season}" is season-to-date actuals and
        "10{season}" is the full-season projection. Passing `week` also
        requests "11{season}{week}", that week's projection -- ESPN only
        returns it when the request's scoringPeriodId is that same week, so
        each week is its own call.
        """
        season = self.cfg.season
        splits = [f"00{season}", f"10{season}", f"00{season - 1}"]
        if week:
            splits.append(f"11{season}{week}")
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": list(status)},
                "limit": limit,
                "offset": offset,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                "filterStatsForTopScoringPeriodIds": {
                    "value": 17,
                    "additionalValue": splits,
                },
            }
        }
        data = self._get(
            self._league_url,
            views=["kona_player_info"],
            fantasy_filter=fantasy_filter,
            params={"scoringPeriodId": week or 0},
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
