"""Turn raw ESPN payloads into league shape and league-scored projections.

Scoring is never hardcoded. ESPN returns `scoringSettings.scoringItems`, each
mapping a statId to a point value (with optional per-position overrides), so
custom scoring, TE premium and half-PPR all fall out of the same join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .constants import (
    BENCH_SLOTS,
    DEDICATED_SLOT_POSITION,
    FLEX_SLOT_ELIGIBILITY,
    POSITION_BY_ID,
    PRO_TEAM_BY_ID,
    SLOT_BY_ID,
    STAT_SOURCE_ACTUAL,
    STAT_SOURCE_PROJECTED,
    STAT_SPLIT_SEASON_TOTAL,
    STAT_SPLIT_WEEKLY,
)

# statId for receptions, used only to describe the league in human terms.
STAT_RECEPTIONS = 53


@dataclass(frozen=True)
class ScoringItem:
    stat_id: int
    points: float
    overrides: dict[int, float] = field(default_factory=dict)

    def points_for(self, position_id: int) -> float:
        return self.overrides.get(position_id, self.points)


@dataclass(frozen=True)
class LeagueShape:
    name: str
    teams: int
    draft_type: str
    seconds_per_pick: int | None
    draft_date_ms: int | None
    pick_order: list[int]
    # lineup slot id -> count
    lineup_slots: dict[int, int]
    scoring_items: list[ScoringItem]
    roster_size: int
    # --- in-season fields (from mStatus / acquisition / trade settings) ---
    current_week: int = 1
    final_week: int = 17
    regular_season_weeks: int | None = None
    playoff_teams: int | None = None
    is_active: bool = False
    # position -> max rostered (None = unlimited)
    position_limits: dict[str, int] = field(default_factory=dict)
    waivers: dict = field(default_factory=dict)
    trade_deadline_ms: int | None = None
    trade_veto_votes: int | None = None

    @property
    def starters_by_position(self) -> dict[str, int]:
        """Dedicated (non-flex) starting slots per position, per team."""
        out: dict[str, int] = {}
        for slot_id, count in self.lineup_slots.items():
            pos = DEDICATED_SLOT_POSITION.get(slot_id)
            if pos and count:
                out[pos] = out.get(pos, 0) + count
        return out

    @property
    def flex_slots(self) -> dict[tuple[str, ...], int]:
        """Multi-position slots per team, keyed by the eligible positions."""
        out: dict[tuple[str, ...], int] = {}
        for slot_id, count in self.lineup_slots.items():
            eligible = FLEX_SLOT_ELIGIBILITY.get(slot_id)
            if eligible and count:
                out[eligible] = out.get(eligible, 0) + count
        return out

    @property
    def ppr(self) -> float:
        for item in self.scoring_items:
            if item.stat_id == STAT_RECEPTIONS:
                return item.points
        return 0.0

    @property
    def draft_datetime(self) -> datetime | None:
        if not self.draft_date_ms:
            return None
        return datetime.fromtimestamp(self.draft_date_ms / 1000, timezone.utc)

    def draft_timing(self) -> dict:
        """When the draft starts, in the machine's local zone and in UTC.

        ESPN reports this as epoch milliseconds, which reads as the next day in
        UTC for any US evening draft -- worth rendering locally so it matches
        what the league page shows.
        """
        dt = self.draft_datetime
        if dt is None:
            return {"draft_scheduled": False}
        local = dt.astimezone()
        delta = dt - datetime.now(timezone.utc)
        hours = delta.total_seconds() / 3600
        return {
            "draft_scheduled": True,
            "draft_time_local": local.strftime("%a %b %d, %Y at %I:%M %p %Z").replace(" 0", " "),
            "draft_time_utc": dt.isoformat(),
            "draft_has_started": hours <= 0,
            "hours_until_draft": round(hours, 1) if hours > 0 else 0,
            "days_until_draft": round(hours / 24, 1) if hours > 0 else 0,
        }

    def describe(self) -> dict:
        ppr = self.ppr
        fmt = "PPR" if ppr >= 1 else "Half-PPR" if ppr > 0 else "Standard"
        return {
            "league_name": self.name,
            "teams": self.teams,
            "draft_type": self.draft_type,
            "seconds_per_pick": self.seconds_per_pick,
            **self.draft_timing(),
            "scoring_format": fmt,
            "points_per_reception": ppr,
            "roster_size": self.roster_size,
            "starting_lineup": {
                SLOT_BY_ID.get(slot, str(slot)): count
                for slot, count in sorted(self.lineup_slots.items())
                if count and slot not in BENCH_SLOTS
            },
            "bench_spots": self.lineup_slots.get(20, 0),
            "ir_spots": self.lineup_slots.get(21, 0),
            **self.season_describe(),
        }

    def season_describe(self) -> dict:
        """The in-season facts: week, waiver rules, trade deadline, roster limits."""
        out: dict = {
            "season_active": self.is_active,
            "current_week": self.current_week,
            "final_week": self.final_week,
            "regular_season_weeks": self.regular_season_weeks,
            "playoff_teams": self.playoff_teams,
            "position_limits": self.position_limits,
            "waivers": self.waivers,
        }
        if self.trade_deadline_ms:
            dt = datetime.fromtimestamp(self.trade_deadline_ms / 1000, timezone.utc)
            out["trade_deadline_local"] = (
                dt.astimezone().strftime("%a %b %d, %Y at %I:%M %p %Z").replace(" 0", " ")
            )
            out["trade_deadline_passed"] = dt <= datetime.now(timezone.utc)
        if self.trade_veto_votes is not None:
            out["trade_veto_votes_required"] = self.trade_veto_votes
        return out


def parse_settings(payload: dict) -> LeagueShape:
    settings = payload.get("settings") or {}
    roster = settings.get("rosterSettings") or {}
    draft = settings.get("draftSettings") or {}
    scoring = settings.get("scoringSettings") or {}

    lineup_slots = {int(k): int(v) for k, v in (roster.get("lineupSlotCounts") or {}).items()}

    items: list[ScoringItem] = []
    for raw in scoring.get("scoringItems") or []:
        overrides = {
            int(k): float(v) for k, v in (raw.get("pointsOverrides") or {}).items()
        }
        items.append(
            ScoringItem(
                stat_id=int(raw["statId"]),
                points=float(raw.get("points") or 0.0),
                overrides=overrides,
            )
        )

    teams = int(settings.get("size") or len(payload.get("teams") or []) or 0)

    # --- in-season -------------------------------------------------------
    status = payload.get("status") or {}
    current_week = int(
        status.get("latestScoringPeriod")
        or status.get("currentMatchupPeriod")
        or payload.get("scoringPeriodId")
        or 1
    )
    final_week = int(status.get("finalScoringPeriod") or 17)
    sched = settings.get("scheduleSettings") or {}
    acq = settings.get("acquisitionSettings") or {}
    trade = settings.get("tradeSettings") or {}
    limits: dict[str, int] = {}
    for pid, cap in (roster.get("positionLimits") or {}).items():
        pos = POSITION_BY_ID.get(int(pid))
        if pos and int(cap) >= 0:
            limits[pos] = int(cap)
    waivers: dict = {}
    if acq:
        kind = str(acq.get("acquisitionType") or "")
        waivers = {
            "type": kind,
            "uses_faab": bool(acq.get("isUsingAcquisitionBudget")),
            "faab_budget": acq.get("acquisitionBudget") if acq.get("isUsingAcquisitionBudget") else None,
            "minimum_bid": acq.get("minimumBid") if acq.get("isUsingAcquisitionBudget") else None,
            "waiver_hours": acq.get("waiverHours"),
            "process_days": acq.get("waiverProcessDays"),
            "process_hour": acq.get("waiverProcessHour"),
            "season_acquisition_limit": (
                None if (acq.get("acquisitionLimit") in (None, -1)) else acq.get("acquisitionLimit")
            ),
            "per_matchup_acquisition_limit": (
                None if (acq.get("matchupAcquisitionLimit") in (None, -1, -1.0))
                else acq.get("matchupAcquisitionLimit")
            ),
        }

    return LeagueShape(
        name=settings.get("name") or "Unnamed league",
        teams=teams,
        draft_type=str(draft.get("type") or "UNKNOWN"),
        seconds_per_pick=draft.get("timePerSelection"),
        draft_date_ms=int(draft["date"]) if draft.get("date") else None,
        pick_order=[int(t) for t in (draft.get("pickOrder") or [])],
        lineup_slots=lineup_slots,
        scoring_items=items,
        roster_size=sum(lineup_slots.values()),
        current_week=current_week,
        final_week=final_week,
        regular_season_weeks=sched.get("matchupPeriodCount"),
        playoff_teams=sched.get("playoffTeamCount"),
        is_active=bool(status.get("isActive")),
        position_limits=limits,
        waivers=waivers,
        trade_deadline_ms=int(trade["deadlineDate"]) if trade.get("deadlineDate") else None,
        trade_veto_votes=trade.get("vetoVotesRequired"),
    )


def _season_projection_entry(player: dict, season: int) -> dict | None:
    for entry in player.get("stats") or []:
        if (
            entry.get("statSourceId") == STAT_SOURCE_PROJECTED
            and entry.get("statSplitTypeId") == STAT_SPLIT_SEASON_TOTAL
            and int(entry.get("seasonId") or 0) == season
        ):
            return entry
    return None


def _stat_total(entry: dict, position_id: int, shape: LeagueShape) -> float:
    applied = entry.get("appliedTotal")
    if applied is not None:
        return round(float(applied), 2)
    return round(score_stat_line(entry.get("stats") or {}, position_id, shape.scoring_items), 2)


def season_actual_points(player: dict, season: int, shape: LeagueShape) -> float:
    """League-scored points actually scored so far this season."""
    pos_id = int(player.get("defaultPositionId") or 0)
    for entry in player.get("stats") or []:
        if (
            entry.get("statSourceId") == STAT_SOURCE_ACTUAL
            and entry.get("statSplitTypeId") == STAT_SPLIT_SEASON_TOTAL
            and int(entry.get("seasonId") or 0) == season
        ):
            return _stat_total(entry, pos_id, shape)
    return 0.0


def weekly_projections(player: dict, season: int, shape: LeagueShape) -> dict[int, float]:
    """week -> league-scored projection, for whichever weeks ESPN returned.

    ESPN only returns the projection for the week named by the request's
    `scoringPeriodId`, so this is normally a single entry.
    """
    pos_id = int(player.get("defaultPositionId") or 0)
    out: dict[int, float] = {}
    for entry in player.get("stats") or []:
        if (
            entry.get("statSourceId") == STAT_SOURCE_PROJECTED
            and entry.get("statSplitTypeId") == STAT_SPLIT_WEEKLY
            and int(entry.get("seasonId") or 0) == season
            and entry.get("scoringPeriodId")
        ):
            out[int(entry["scoringPeriodId"])] = _stat_total(entry, pos_id, shape)
    return out


def weekly_actual_points(player: dict, season: int, shape: LeagueShape) -> dict[int, float]:
    """week -> league-scored points actually scored, for the weeks ESPN returned.

    Like `weekly_projections`, normally just the week named by the request's
    `scoringPeriodId`. Empty before that week's first kickoff.
    """
    pos_id = int(player.get("defaultPositionId") or 0)
    out: dict[int, float] = {}
    for entry in player.get("stats") or []:
        if (
            entry.get("statSourceId") == STAT_SOURCE_ACTUAL
            and entry.get("statSplitTypeId") == STAT_SPLIT_WEEKLY
            and int(entry.get("seasonId") or 0) == season
            and entry.get("scoringPeriodId")
        ):
            out[int(entry["scoringPeriodId"])] = _stat_total(entry, pos_id, shape)
    return out


def score_stat_line(stats: dict, position_id: int, items: list[ScoringItem]) -> float:
    """Apply this league's scoring rules to a raw {statId: value} line."""
    total = 0.0
    for item in items:
        value = stats.get(str(item.stat_id), stats.get(item.stat_id))
        if value:
            total += float(value) * item.points_for(position_id)
    return total


def projected_points(player: dict, season: int, shape: LeagueShape) -> float:
    """League-scored full-season projection.

    ESPN pre-applies league scoring in `appliedTotal`, which handles stat
    categories we may not model. Fall back to computing from the raw stat line
    when it is absent (some seasons return projections without appliedTotal).
    """
    entry = _season_projection_entry(player, season)
    if not entry:
        return 0.0
    applied = entry.get("appliedTotal")
    if applied is not None:
        return round(float(applied), 2)
    raw = entry.get("stats") or {}
    pos_id = int(player.get("defaultPositionId") or 0)
    return round(score_stat_line(raw, pos_id, shape.scoring_items), 2)


def normalize_player(entry: dict, season: int, shape: LeagueShape) -> dict:
    """Flatten one kona_player_info entry into a compact draft-facing record."""
    player = entry.get("player") or entry
    pos_id = int(player.get("defaultPositionId") or 0)
    ownership = player.get("ownership") or {}

    draft_ranks = {}
    for rank_type, rank in (player.get("draftRanksByRankType") or {}).items():
        if isinstance(rank, dict) and rank.get("rank") is not None:
            draft_ranks[rank_type] = rank["rank"]

    # ESPN reports ADP drift as a percent change. A rising ADP *number* means
    # the player is being taken later, so the sign is the opposite of "hype" --
    # name the direction explicitly rather than leaving a bare signed float.
    adp_change = ownership.get("averageDraftPositionPercentChange")
    adp_change = float(adp_change) if adp_change else 0.0
    adp_moving = None
    if abs(adp_change) >= 0.05:
        adp_moving = "later" if adp_change > 0 else "earlier"

    # Roster status lives on the pool entry, not the player.
    status = entry.get("status") if entry is not player else None
    on_team = int(entry.get("onTeamId") or 0) if entry is not player else 0
    waiver_clears = entry.get("waiverProcessDate") if entry is not player else None

    return {
        "player_id": int(player.get("id")),
        "name": player.get("fullName"),
        "position": POSITION_BY_ID.get(pos_id, str(pos_id)),
        "position_id": pos_id,
        "pro_team": PRO_TEAM_BY_ID.get(int(player.get("proTeamId") or 0), "FA"),
        "pro_team_id": int(player.get("proTeamId") or 0),
        "bye_week": None,  # filled in by the board from proTeam bye data when present
        "projected_points": projected_points(player, season, shape),
        "season_points": season_actual_points(player, season, shape),
        "week_projections": weekly_projections(player, season, shape),
        "week_points": weekly_actual_points(player, season, shape),
        "roster_status": status,          # FREEAGENT / WAIVERS / ONTEAM
        "on_team_id": on_team or None,
        "waiver_clears_ms": waiver_clears,
        "espn_adp": round(float(ownership.get("averageDraftPosition") or 0.0), 1) or None,
        "adp_change_pct": round(adp_change, 3),
        "adp_moving": adp_moving,
        "auction_value": round(float(ownership.get("auctionValueAverage") or 0.0), 1) or None,
        "percent_owned": round(float(ownership.get("percentOwned") or 0.0), 1),
        "percent_started": round(float(ownership.get("percentStarted") or 0.0), 1),
        # ESPN's "+/-": change in rostered percentage over the last week.
        "percent_owned_change": round(float(ownership.get("percentChange") or 0.0), 2),
        "ownership_as_of_ms": ownership.get("date"),
        "espn_draft_rank": draft_ranks.get("PPR") if shape.ppr else draft_ranks.get("STANDARD"),
        "injury_status": player.get("injuryStatus"),
        "eligible_slots": [
            SLOT_BY_ID.get(s, str(s)) for s in (player.get("eligibleSlots") or [])
        ],
    }
