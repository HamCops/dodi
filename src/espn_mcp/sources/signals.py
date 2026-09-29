"""Puts what the outside sources know onto ESPN's player records.

Fields added, each only when its source has the player:

  market_value, market_rank, market_pos_rank, market_trend_30d
      what he is worth in trades (FantasyCalc), and how that has moved
  model_pos_rank, market_gap
      his rank at the position by this league's rest-of-season points, and
      model_pos_rank minus market_pos_rank. Negative: the market rates him
      below what he is projected to score (cheap to buy). Positive: the
      market rates him above it (sell).
  adds_24h, drops_24h
      leagues on Sleeper that added or dropped him in the last day
  alt_week_proj
      Sleeper's projection for the week, to check ESPN's against
  practice, depth_chart_order
      practice participation and depth chart spot
  game
      his team's game this week: implied_total (points the betting market
      expects his team to score), favored_by, over_under, indoor, and the
      forecast at kickoff for outdoor games (wind_mph, gusts_mph, rain_pct,
      temp_f)
  adj_week_proj
      ESPN's projection for the week, moved by the betting line: for QB, RB,
      WR and TE by his own team's implied total; for a defense by the
      opponent's; for a kicker by whether the game is indoors. See
      VEGAS_PER_POINT, DEFENSE_PER_OPPONENT_POINT and KICKER_INDOORS.
  usage
      his season so far against his workload (nflverse): points per game,
      what his carries and targets say he should be scoring, the gap, and
      "running hot" or "running cold" when the gap is wide. See usage.py.
  injury_alt
      Sleeper's injury designation where it differs from ESPN's: what it
      says, the body part, when it was last updated, and which game it is
      about. A designation says nothing about a game that has already
      kicked off; once it has, it is about the next one. It is a second
      report to check, not a correction: either source can be the stale one.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..config import Config
from ..scoring import LeagueShape
from .. import usage as usage_model
from . import crosswalk, fantasycalc, games, nflverse, sleeper
from .cache import DiskCache
from .http import fetch_json, fetch_text

HOUR = 3600.0

TTL = {"players": 24 * HOUR, "trending": 1 * HOUR, "projections": 3 * HOUR,
       "market": 6 * HOUR, "games": 1 * HOUR, "forecast": 3 * HOUR,
       "place": 90 * 24 * HOUR, "usage": 6 * HOUR, "ids": 7 * 24 * HOUR}

# Fantasy points per point of implied team total above the week's average,
# on top of ESPN's projection. Fit on the 2024 and 2025 seasons together
# (5,800 player-weeks, standard scoring); fit on either season and scored on
# the other, it improved on ESPN alone both ways, by a little: about 0.03
# points per close start/sit decision. It is a nudge, not a second opinion.
#
# Tested the same way and left out because they did not hold up: wind
# (helped one season, hurt the other), snap/target/carry trends, recent
# scoring against projection, and preseason rank as a tiebreak. ESPN's
# projection already carries them. See research/README.md.
VEGAS_PER_POINT = {"QB": 0.131, "RB": 0.142, "WR": 0.030, "TE": 0.064}

# Defenses: points per point the OPPONENT is expected to score above the
# week's average. ESPN's projection leans the right way and not far enough:
# against offenses expected to score under 17 it said 7.2 and defenses
# scored 10.1; against offenses expected to score 26 or more it said 3.3 and
# they scored 2.3. The slope was -0.52 in 2024 and -0.49 in 2025 alongside
# ESPN's number; this is the part ESPN leaves out.
DEFENSE_PER_OPPONENT_POINT = -0.36

# Kickers: points for kicking indoors. ESPN projected indoor and outdoor
# kickers the same (7.9) and indoor kickers scored a point more, in both
# seasons (+1.06, +1.02). The kicker's own team total added nothing.
KICKER_INDOORS = 1.07

# Ranks this far apart are a real disagreement, not noise.
MARKET_GAP_MIN = 6

# Sleeper's injury designations, in ESPN's words.
INJURY_AS_ESPN = {"QUESTIONABLE": "QUESTIONABLE", "DOUBTFUL": "DOUBTFUL", "OUT": "OUT",
                  "IR": "INJURY_RESERVE", "PUP": "OUT", "SUS": "SUSPENSION"}

FIELDS = ("usage", "game", "adj_week_proj", "market_value", "market_rank", "market_pos_rank", "market_trend_30d",
          "model_pos_rank", "market_gap", "adds_24h", "drops_24h", "alt_week_proj",
          "practice", "depth_chart_order", "injury_alt")


def market_view(p: dict) -> str | None:
    """sell, buy or None: which way the market and the projection disagree."""
    gap = p.get("market_gap")
    if gap is None or abs(gap) < MARKET_GAP_MIN:
        return None
    return "sell" if gap > 0 else "buy"


class Signals:
    def __init__(self, cache: DiskCache, fetch: Callable[..., Any] = fetch_json,
                 season: int = 2026, clock: Callable[[], float] = time.time,
                 fetch_text: Callable[..., str] = fetch_text) -> None:
        self.cache = cache
        self.fetch = fetch
        self.fetch_text = fetch_text
        self.season = season
        self.clock = clock

    # --- sources, each cached and allowed to be missing ---------------------

    def _players(self) -> list[dict]:
        # v2: carries when the injury report was last updated.
        return self.cache.get("sleeper-players-v2", TTL["players"],
                              lambda: sleeper.players(self.fetch)) or []

    def _trending(self, kind: str) -> dict[str, int]:
        return self.cache.get(f"sleeper-trending-{kind}", TTL["trending"],
                              lambda: sleeper.trending(self.fetch, kind)) or {}

    def _projections(self, week: int, ppr: float) -> dict[str, float]:
        key = f"sleeper-proj-{self.season}-w{week}-{sleeper.scoring_key(ppr)}"
        return self.cache.get(key, TTL["projections"],
                              lambda: sleeper.projections(self.fetch, self.season, week,
                                                          ppr)) or {}

    def _usage(self) -> tuple[dict, dict]:
        """(what each player has done this season, ESPN id -> nflverse id)."""
        totals = self.cache.get(f"nflverse-usage-{self.season}", TTL["usage"],
                                lambda: nflverse.season_totals(self.fetch_text,
                                                               self.season)) or {}
        ids = self.cache.get("nflverse-ids", TTL["ids"],
                             lambda: nflverse.espn_ids(self.fetch_text)) or {} \
            if totals else {}
        return totals, ids

    def _games(self, week: int) -> dict[str, dict]:
        key = f"games-{self.season}-w{week}"
        return self.cache.get(key, TTL["games"],
                              lambda: games.week_games(self.fetch, self.season, week)) or {}

    def _forecast(self, game: dict) -> dict | None:
        """The weather at kickoff, for an outdoor game in the next two weeks."""
        if game.get("indoor") or not game.get("city") or not game.get("kickoff"):
            return None
        place = "-".join(str(game.get(k) or "").lower().replace(" ", "_")
                         for k in ("city", "state", "country"))
        spot = self.cache.get(f"place-{place}", TTL["place"], lambda: games.locate(
            self.fetch, game["city"], game.get("state"), game.get("country")))
        if not spot:
            return None
        key = f"forecast-{place}-{game['kickoff'][:13].replace(':', '')}"
        return self.cache.get(key, TTL["forecast"], lambda: games.forecast(
            self.fetch, spot["lat"], spot["lon"], game["kickoff"]))

    def _market(self, shape: LeagueShape) -> list[dict]:
        qbs = shape.starters_by_position.get("QB", 1)
        key = f"fantasycalc-{shape.teams}t-{shape.ppr}ppr-{qbs}qb"
        return self.cache.get(key, TTL["market"],
                              lambda: fantasycalc.values(self.fetch, shape.teams,
                                                         shape.ppr, qbs)) or []

    def status(self) -> dict[str, dict]:
        """Which sources answered, and how old their data is."""
        names = (("sleeper-players", "sleeper_players"),
                 ("sleeper-trending-add", "sleeper_adds"),
                 ("sleeper-trending-drop", "sleeper_drops"),
                 ("sleeper-proj", "sleeper_projections"),
                 ("games-", "espn_betting_lines"),
                 ("nflverse-usage", "nflverse_usage"),
                 ("nflverse-ids", "nflverse_player_ids"),
                 ("forecast-", "weather_forecast"),
                 ("place-", "venue_location"),
                 ("fantasycalc", "fantasycalc_values"))
        out: dict[str, dict] = {}
        for key, st in self.cache.status.items():
            out[next((n for prefix, n in names if key.startswith(prefix)), key)] = st
        return out

    # --- the join ------------------------------------------------------------

    def attach(self, players: list[dict], shape: LeagueShape, week: int,
               rank_pool: list[dict] | None = None) -> None:
        """Add the outside fields to `players`, in place.

        `rank_pool` is the full player pool that model_pos_rank is a rank
        within; it defaults to `players`. Pass it when attaching to one or
        two records, so their rank is against everyone.
        """
        if not players:
            return
        directory = self._players()
        market = self._market(shape)
        ids = crosswalk.build(players, directory, market)
        by_sleeper = {s["player_id"]: s for s in directory}
        adds, drops = self._trending("add"), self._trending("drop")
        now_ms = self.clock() * 1000
        proj = self._projections(week, shape.ppr)
        market_by_espn = {m["espn_id"]: m for m in market if m.get("espn_id")}
        market_by_sleeper = {m["sleeper_id"]: m for m in market if m.get("sleeper_id")}
        model_rank = _position_ranks(rank_pool if rank_pool is not None else players)
        this_week = self._games(week)
        done, gsis = self._usage()
        totals = [g["implied_total"] for g in this_week.values() if "implied_total" in g]
        average_total = sum(totals) / len(totals) if totals else None
        indoors = (sum(1 for g in this_week.values() if g.get("indoor")) / len(this_week)
                   if this_week else 0.0)
        forecasts: dict[str, dict | None] = {}

        for p in players:
            for f in (*FIELDS, "sleeper_id"):
                p.pop(f, None)
            totals = (done.get("players") or {}).get(gsis.get(str(p["player_id"])) or "")
            if totals:
                seen = usage_model.profile(totals, p["position"], shape.ppr)
                if seen:
                    seen["through_week"] = done.get("through_week")
                    p["usage"] = seen
            game = this_week.get(p.get("pro_team") or "")
            if game and now_ms < _kickoff_ms(game):
                view = {k: game[k] for k in ("opponent", "home", "implied_total",
                                             "favored_by", "over_under", "indoor")
                        if k in game}
                if p.get("pro_team") not in forecasts:
                    forecasts[p["pro_team"]] = self._forecast(game)
                view.update(forecasts[p["pro_team"]] or {})
                facing = (this_week.get(game.get("opponent") or "") or {}).get("implied_total")
                if facing is not None:
                    view["opponent_implied_total"] = facing
                p["game"] = view
                move = _line_adjustment(p["position"], game, facing, average_total, indoors)
                if move is not None and p.get("week_proj"):
                    p["adj_week_proj"] = round(max(p["week_proj"] + move, 0.0), 2)
            sid = ids.get(p["player_id"])
            m = market_by_espn.get(str(p["player_id"])) or market_by_sleeper.get(sid or "")
            if m and m.get("position") in (None, p["position"]):
                p["market_value"] = m["value"]
                p["market_rank"] = m["overall_rank"]
                p["market_pos_rank"] = m["position_rank"]
                if m.get("trend_30d"):
                    p["market_trend_30d"] = m["trend_30d"]
                rank = model_rank.get(p["player_id"])
                if rank and m["position_rank"]:
                    p["model_pos_rank"] = rank
                    p["market_gap"] = rank - m["position_rank"]
            if not sid:
                continue
            p["sleeper_id"] = sid
            if sid in adds:
                p["adds_24h"] = adds[sid]
            if sid in drops:
                p["drops_24h"] = drops[sid]
            if sid in proj:
                p["alt_week_proj"] = proj[sid]
            s = by_sleeper.get(sid) or {}
            if s.get("practice_participation"):
                p["practice"] = s["practice_participation"]
            if s.get("depth_chart_order"):
                p["depth_chart_order"] = s["depth_chart_order"]
            theirs = INJURY_AS_ESPN.get((s.get("injury_status") or "").upper())
            if theirs and theirs != (p.get("injury_status") or "ACTIVE").upper():
                p["injury_alt"] = _injury_report(theirs, s, p, now_ms)


def _line_adjustment(position: str, game: dict, facing: float | None,
                     average_total: float | None, indoors: float) -> float | None:
    """How far to move ESPN's projection for the week, in points."""
    if position == "K":
        return KICKER_INDOORS * ((1.0 if game.get("indoor") else 0.0) - indoors)
    if average_total is None:
        return None
    if position == "D/ST":
        return None if facing is None else DEFENSE_PER_OPPONENT_POINT * (facing - average_total)
    per_point = VEGAS_PER_POINT.get(position)
    if per_point is None or "implied_total" not in game:
        return None
    return per_point * (game["implied_total"] - average_total)


def _kickoff_ms(game: dict) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00")).timestamp() * 1000
    except (KeyError, ValueError, AttributeError):
        return float("inf")


def _injury_report(status: str, s: dict, p: dict, now_ms: float) -> dict:
    """Sleeper's designation, with what is needed to judge it.

    Designations are issued for the game ahead. After this week's kickoff the
    report cannot be about this week's game, however it reads.
    """
    out: dict[str, Any] = {"status": status, "source": "sleeper"}
    if s.get("injury_body_part"):
        out["body_part"] = s["injury_body_part"]
    updated = s.get("news_updated")
    if updated:
        out["updated_hours_ago"] = round(max(now_ms - updated, 0) / 3_600_000, 1)
    kickoff = p.get("kickoff_ms")
    if kickoff and now_ms >= kickoff:
        out["about"] = "next game"
    elif kickoff and updated and updated < kickoff - 7 * 24 * 3_600_000:
        out["about"] = "unclear: the report is over a week old"
    else:
        out["about"] = "this week's game"
    return out


def _position_ranks(pool: list[dict]) -> dict[int, int]:
    """Player id -> rank at his position by rest-of-season points."""
    by_pos: dict[str, list[dict]] = {}
    for p in pool:
        if p.get("ros_points") is not None:
            by_pos.setdefault(p["position"], []).append(p)
    out: dict[int, int] = {}
    for group in by_pos.values():
        group.sort(key=lambda p: -p["ros_points"])
        for i, p in enumerate(group, 1):
            out[p["player_id"]] = i
    return out


def build_signals(cfg: Config) -> Signals | None:
    if not cfg.external_sources:
        return None
    return Signals(DiskCache(cfg.state_root / "cache"), season=cfg.season)
