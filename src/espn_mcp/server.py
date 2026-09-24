"""MCP server exposing an ESPN fantasy football league: draft and in-season.

Design rule: tools return facts, not opinions. The one exception is the value
math (VORP, tiers, replacement level), which is deterministic arithmetic that a
language model should not be doing in its head. Pick recommendations are left
to the model reasoning over these tools.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server import MCPServer

from . import __version__
from .board import DraftBoard
from .constants import SLOT_BY_ID
from .config import Config, load_config
from .espn import ESPNError
from .scoring import LeagueShape
from .season import (
    ROS_KEY,
    SLOT_ID_BY_NAME,
    WEEK_KEY,
    current_starters,
    describe_transaction,
    drop_candidates,
    evaluate_trade,
    league_position_averages,
    lineup_changes,
    optimal_lineup,
    plan_lineup,
    plan_move,
    roster_profile,
    waiver_gain,
)

mcp = MCPServer(
    "espn-fantasy-draft",
    version=__version__,
    instructions=(
        "Tools for an ESPN fantasy football league, for the draft and the season. "
        "Call get_league_settings once to learn the format. DRAFT: get_draft_context "
        "whenever a pick decision is needed -- it bundles state, roster needs and "
        "best-available in one call. IN SEASON: get_matchup for this week's game "
        "and start/sit, get_waiver_targets for who to add and drop, analyze_trade "
        "to evaluate a specific offer, find_trade_partners to see which teams have "
        "what you need, get_transactions for history (lineup moves, adds, drops, "
        "trades, with timestamps; rosters only show the present). CHANGING THE LINEUP: "
        "set_lineup previews the swaps to the best lineup by this week's projection "
        "and applies them with apply=true; move_player puts one named player in one "
        "slot (e.g. IR to BE). Both are real ESPN roster changes. Rankings are by VORP (value over replacement), which already "
        "accounts for positional scarcity in this league's specific lineup; do not "
        "re-rank by raw projected points. In season, VORP is over rest-of-season "
        "points; week_proj is ESPN's single-week projection and already reflects "
        "the NFL opponent, injury status and bye."
    ),
)

_board: DraftBoard | None = None


def board() -> DraftBoard:
    global _board
    if _board is None:
        cfg: Config = load_config()
        _board = DraftBoard(cfg)
    return _board


def handle_errors(fn):
    """Surface actionable errors as data -- a raised exception mid-draft is useless."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ESPNError as exc:
            return {"error": str(exc), "recoverable": True}
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return {"error": f"{type(exc).__name__}: {exc}", "recoverable": False}

    return wrapper


def _slim(p: dict) -> dict:
    """Compact player record. Draft clocks are short; payloads should be too."""
    out = {
        "id": p["player_id"],
        "name": p["name"],
        "pos": p["position"],
        "team": p["pro_team"],
        "proj": p["projected_points"],
        "vorp": p["vorp"],
        "tier": p.get("tier"),
        "adp": p.get("espn_adp"),
        "value_vs_adp": p.get("value_vs_adp"),
        "injury": p.get("injury_status"),
    }
    # ADP is a lagging average. Direction of travel is what says whether it
    # still holds, so carry it whenever the player is actually moving.
    if p.get("adp_moving"):
        out["adp_moving"] = p["adp_moving"]
        out["adp_change_pct"] = p.get("adp_change_pct")
    if p.get("percent_owned_change"):
        out["rostered_change"] = p["percent_owned_change"]
    if p.get("value_basis") != "vorp":
        # ESPN publishes no projections here, so ordering is ADP-based.
        out["ranked_by"] = p.get("value_basis")
    if p.get("late_round_position"):
        # Guard against comparing a kicker's VORP to a running back's.
        out["late_round_position"] = True
    return out


def _best_tier(pool: list[dict]) -> int | None:
    tiers = [p["tier"] for p in pool if p.get("tier") is not None]
    return min(tiers) if tiers else None


def _position_summary(pool: list[dict], top_n: int) -> dict:
    """Best available at a position, plus how much of the current tier is left."""
    best_tier = _best_tier(pool)
    summary: dict[str, Any] = {
        "top": [_slim(p) for p in pool[:top_n]],
        "available_above_replacement": sum(1 for p in pool if (p.get("vorp") or 0) > 0),
    }
    if best_tier is None:
        summary["ranked_by"] = "espn_adp"
        summary["note"] = "ESPN publishes no season projections for this position."
    else:
        summary["current_best_tier"] = best_tier
        summary["players_left_in_best_tier"] = sum(
            1 for p in pool if p.get("tier") == best_tier
        )
        # The real wait-or-not signal: a thin top tier only matters if the drop
        # to the next one is steep, so report what is behind it.
        nxt = [p for p in pool if p.get("tier") == best_tier + 1]
        if nxt:
            in_tier = [p for p in pool if p.get("tier") == best_tier]
            summary["next_tier"] = {
                "tier": best_tier + 1,
                "players": len(nxt),
                "best": nxt[0]["name"],
                "vorp_drop_from_current_tier": round(
                    min(p["vorp"] for p in in_tier) - nxt[0]["vorp"], 2
                ),
            }
    return summary


def _roster_needs(shape: LeagueShape, roster_positions: list[str]) -> dict:
    """Unfilled starting slots, given what a team already owns."""
    have: dict[str, int] = {}
    for pos in roster_positions:
        have[pos] = have.get(pos, 0) + 1

    remaining = dict(have)
    needs: dict[str, int] = {}
    for pos, count in shape.starters_by_position.items():
        used = min(remaining.get(pos, 0), count)
        remaining[pos] = remaining.get(pos, 0) - used
        if count - used > 0:
            needs[pos] = count - used

    flex_open = 0
    for eligible, count in shape.flex_slots.items():
        spare = sum(max(remaining.get(pos, 0), 0) for pos in eligible)
        flex_open += max(count - min(spare, count), 0)

    return {
        "current_roster_counts": have,
        "unfilled_starting_slots": needs,
        "open_flex_slots": flex_open,
        "roster_spots_filled": len(roster_positions),
        "roster_size": shape.roster_size,
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
@handle_errors
def get_league_settings() -> dict:
    """League format: team count, scoring rules, starting lineup, draft type.

    Call this once at the start of a session -- every other tool's numbers are
    relative to this league's shape.
    """
    b = board()
    shape = b.shape()
    out = shape.describe()

    state = b.draft_state()
    order = state["pick_order"]
    out["pick_order_team_ids"] = order
    out["pick_order_source"] = state["pick_order_source"]
    out["my_team_id"] = b.cfg.team_id
    if b.cfg.team_id:
        out["my_draft_slot"] = b.my_slot(b.cfg.team_id, state)

    # Many leagues randomize the order shortly before the draft. Until that
    # happens ESPN publishes a placeholder order, which is usually just teams
    # in id order -- treating it as final would plan the whole draft around a
    # slot that is about to change.
    if not state["picks_made"] and order and order == sorted(order):
        out["draft_slot_is_provisional"] = True
        out["draft_slot_warning"] = (
            "The published pick order is teams in id order, which usually means "
            "it has not been randomized yet. Do not rely on the draft slot until "
            "the order is set (many leagues randomize shortly before the draft). "
            "Call refresh_draft_order once it has been drawn."
        )
    else:
        out["draft_slot_is_provisional"] = False

    out["teams_in_league"] = [
        {"team_id": t["team_id"], "name": t["name"], "abbrev": t["abbrev"]}
        for t in b.teams().values()
    ]
    return out


@mcp.tool()
@handle_errors
def get_draft_state() -> dict:
    """Live draft state: picks made so far, who is on the clock, your next picks.

    Never cached. Safe to poll every few seconds during a live draft.
    """
    b = board()
    state = b.draft_state()
    by_id = b.board()["by_id"]

    recent = []
    for pick in state["picks"][-15:]:
        p = by_id.get(pick["player_id"])
        recent.append(
            {
                "overall": pick["overall"],
                "round": pick["round"],
                "team_id": pick["team_id"],
                "player": p["name"] if p else f"id:{pick['player_id']}",
                "pos": p["position"] if p else "?",
                "auto": pick["auto"],
            }
        )

    out = {
        "in_progress": state["in_progress"],
        "complete": state["complete"],
        "picks_made": state["picks_made"],
        "next_overall_pick": state["next_overall_pick"],
        "on_the_clock_team_id": state["on_the_clock_team_id"],
        "recent_picks": recent,
    }

    if b.cfg.team_id:
        upcoming = b.upcoming_picks_for(b.cfg.team_id, count=4, state=state)
        out["my_next_picks"] = upcoming
        out["on_the_clock_is_me"] = state["on_the_clock_team_id"] == b.cfg.team_id
        if upcoming and state["next_overall_pick"]:
            out["picks_until_my_turn"] = upcoming[0] - state["next_overall_pick"]
    return out


@mcp.tool()
@handle_errors
def get_available_players(position: str | None = None, limit: int = 25,
                          sort_by: str = "vorp") -> dict:
    """Undrafted players, ranked.

    Args:
        position: QB, RB, WR, TE, K or D/ST. Omit for all positions.
        limit: how many to return.
        sort_by: "vorp" (value over replacement, the default and usually right),
            "projected_points" (ignores scarcity), or "espn_adp" (what your
            leaguemates are likely to do).
    """
    players = board().available(position=position, limit=limit, sort_by=sort_by)
    return {"count": len(players), "players": [_slim(p) for p in players]}


@mcp.tool()
@handle_errors
def get_value_board() -> dict:
    """Replacement levels and tier structure for the league.

    Explains *why* the rankings look the way they do: how many players at each
    position are startable league-wide, and what the last startable player at
    each position projects for.
    """
    b = board()
    data = b.board()
    shape = b.shape()
    available = b.available(limit=10_000)

    by_pos: dict[str, Any] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
        pool = [p for p in available if p["position"] == pos]
        if not pool:
            continue
        best_tier = _best_tier(pool)
        in_tier = [p for p in pool if p.get("tier") == best_tier] if best_tier else pool
        entry = {
            "startable_league_wide": data["replacement_ranks"].get(pos),
            "replacement_points": data["replacement_points"].get(pos),
            "available_above_replacement": sum(
                1 for p in pool if (p.get("vorp") or 0) > 0
            ),
            "current_best_tier": best_tier,
            "players_left_in_that_tier": len(in_tier) if best_tier else None,
            "tier_members": [p["name"] for p in in_tier[:8]],
        }
        if best_tier is None:
            entry["ranked_by"] = "espn_adp"
        by_pos[pos] = entry

    return {
        "starting_lineup": shape.describe()["starting_lineup"],
        "teams": shape.teams,
        "positions": by_pos,
        "positions_without_projections": data.get("positions_without_projections", []),
        "note": (
            "startable_league_wide = teams x starting slots, with flex slots "
            "allocated to whichever positions actually project into them. "
            "Positions listed in positions_without_projections have no ESPN "
            "season projection and are ordered by ADP instead of VORP."
        ),
    }


@mcp.tool()
@handle_errors
def get_roster(team_id: int | None = None) -> dict:
    """A team's current roster and which starting slots are still unfilled.

    Defaults to your team (ESPN_TEAM_ID).
    """
    b = board()
    tid = team_id or b.cfg.team_id
    if not tid:
        return {"error": "No team_id given and ESPN_TEAM_ID is not set."}

    if b.shape().is_active:
        return _season_roster(b, int(tid))

    teams = b.teams(refresh=True)
    team = teams.get(int(tid))
    if not team:
        return {"error": f"Team {tid} not found. Known: {sorted(teams)}"}

    by_id = b.board()["by_id"]
    # A team's roster during a live draft is most reliably read from the picks.
    drafted = [
        pick["player_id"]
        for pick in b.draft_state()["picks"]
        if pick["team_id"] == int(tid)
    ]
    player_ids = drafted or team["roster_player_ids"]

    players = [by_id[pid] for pid in player_ids if pid in by_id]
    needs = _roster_needs(b.shape(), [p["position"] for p in players])

    return {
        "team_id": int(tid),
        "team_name": team["name"],
        "players": [_slim(p) for p in players],
        **needs,
    }


def _season_roster(b: DraftBoard, tid: int) -> dict:
    """In-season roster view: lineup slots, this week, rest of season, strength."""
    shape = b.shape()
    week = b.week()
    teams = b.league_rosters(week)
    team = teams.get(tid)
    if not team:
        return {"error": f"Team {tid} not found. Known: {sorted(teams)}"}
    players = b.team_players(tid, week)
    prof = roster_profile(players, shape)
    starters = current_starters(players)
    return {
        **_team_brief(team),
        "week": week,
        "waiver_priority": team.get("waiver_rank"),
        "starters": [_slim_season(p) for p in starters],
        "bench": [_slim_season(p) for p in players if p not in starters],
        "starters_this_week": prof["starters_this_week"],
        "starters_ros_per_game": prof["starters_ros_per_game"],
        "starter_avg_ros_per_game_by_position": {
            pos: blk["starter_avg"] for pos, blk in prof["positions"].items()
            if blk["starter_avg"] is not None
        },
        "position_counts": {pos: len(blk["starters"]) + len(blk["bench"])
                            for pos, blk in prof["positions"].items()},
        "position_limits": shape.position_limits,
        "trade_block": [p["name"] for p in players if p.get("on_trade_block")],
    }


@mcp.tool()
@handle_errors
def get_player(name: str | None = None, player_id: int | None = None) -> dict:
    """Full detail on one player: projection, VORP, tier, ADP, injury status.

    Search by name (partial match) or exact player_id.
    """
    b = board()
    if player_id:
        p = b.player(int(player_id))
        if not p:
            return {"error": f"No player with id {player_id} in the pool."}
        matches = [p]
    elif name:
        matches = b.find_players(name)
        if not matches:
            return {"error": f"No player matching {name!r}."}
    else:
        return {"error": "Pass either name or player_id."}

    taken = b.drafted_ids()
    out = []
    for p in matches:
        rec = dict(p)
        rec["drafted"] = p["player_id"] in taken
        out.append(rec)
    return {"matches": out}


@mcp.tool()
@handle_errors
def get_draft_context(top_per_position: int = 5) -> dict:
    """One-call snapshot for when you are on the clock.

    Bundles draft state, your roster needs, the best available at each position
    with tier depth, recent-pick position runs, and the biggest ADP fallers --
    everything needed to make a pick inside a 60-90 second clock.
    """
    b = board()
    shape = b.shape()
    # One draft-state fetch, reused everywhere below -- this runs on a live clock.
    state = b.draft_state()
    by_id = b.board()["by_id"]
    available = b.available(limit=10_000, taken=b.drafted_ids(state))

    positions: dict[str, Any] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
        pool = [p for p in available if p["position"] == pos]
        if pool:
            positions[pos] = _position_summary(pool, top_per_position)

    # Position run detection over the last full round of picks.
    window = state["picks"][-shape.teams :] if shape.teams else state["picks"][-10:]
    runs: dict[str, int] = {}
    for pick in window:
        p = by_id.get(pick["player_id"])
        if p:
            runs[p["position"]] = runs.get(p["position"], 0) + 1

    # Players the room is undervaluing relative to this league's scoring.
    # ADP is an average over past drafts, so it lags: a player whose ADP is
    # trending earlier may not actually last to your pick, and one trending
    # later may be sliding for a reason the projection has not caught yet.
    fallers = []
    for p in available:
        gap = p.get("value_vs_adp")
        if gap is None or gap < 12:
            continue
        entry = _slim(p)
        if p.get("adp_moving") == "earlier":
            entry["caution"] = "ADP is trending earlier -- may not last to your pick"
        elif p.get("adp_moving") == "later":
            entry["caution"] = "ADP is trending later -- check for news the projection misses"
        fallers.append(entry)
        if len(fallers) >= 8:
            break

    board_data = b.board()
    out: dict[str, Any] = {
        "picks_made": state["picks_made"],
        "next_overall_pick": state["next_overall_pick"],
        "adp_as_of": board_data.get("adp_as_of_local"),
        "adp_age_hours": board_data.get("adp_age_hours"),
        "on_the_clock_team_id": state["on_the_clock_team_id"],
        "position_runs_last_round": dict(sorted(runs.items(), key=lambda kv: -kv[1])),
        "best_available": positions,
        "falling_below_adp": fallers,
    }

    if b.cfg.team_id:
        upcoming = b.upcoming_picks_for(b.cfg.team_id, count=3, state=state)
        out["on_the_clock_is_me"] = state["on_the_clock_team_id"] == b.cfg.team_id
        out["my_next_picks"] = upcoming
        if upcoming and state["next_overall_pick"]:
            out["picks_until_my_turn"] = upcoming[0] - state["next_overall_pick"]
        if len(upcoming) >= 2:
            out["picks_between_this_and_next"] = upcoming[1] - upcoming[0] - 1
        mine = [
            by_id[pick["player_id"]]
            for pick in state["picks"]
            if pick["team_id"] == b.cfg.team_id and pick["player_id"] in by_id
        ]
        out["my_roster"] = [_slim(p) for p in mine]
        out.update(_roster_needs(shape, [p["position"] for p in mine]))

    return out


@mcp.tool()
@handle_errors
def record_pick(player: str | None = None, player_id: int | None = None,
                team_id: int | None = None) -> dict:
    """Manually record a pick when ESPN is not reporting the draft.

    Use this if picks stop appearing in get_draft_state during a live draft, or
    for a draft held outside ESPN's draft room. Recorded picks merge with
    anything ESPN does report, so the available pool stays correct either way.

    Args:
        player: Player name, full or partial ("Gibbs", "jahmyr gibbs").
        player_id: Exact id, if you have it instead of a name.
        team_id: Who made the pick. Defaults to whoever is on the clock.

    Ambiguous names are not guessed -- the candidates come back instead.
    """
    b = board()
    if player_id is None:
        if not player:
            return {"error": "Pass a player name or a player_id."}
        matches = b.find_players(player, limit=6)
        if not matches:
            return {"error": f"No player matching {player!r}."}

        # Resolve the name before filtering by who is already drafted, so a
        # repeat of an exact name reports "already drafted" rather than
        # collapsing into a confusing ambiguity error.
        exact = [m for m in matches if m["name"].lower() == player.lower().strip()]
        if exact:
            player_id = exact[0]["player_id"]
        else:
            taken = b.drafted_ids()
            undrafted = [m for m in matches if m["player_id"] not in taken]
            if not undrafted:
                return {"error": f"{matches[0]['name']} is already drafted."}
            if len(undrafted) > 1:
                return {
                    "error": "Name is ambiguous -- nothing recorded.",
                    "candidates": [_slim(m) for m in undrafted],
                }
            player_id = undrafted[0]["player_id"]

    chosen = b.player(int(player_id))
    if not chosen:
        return {"error": f"No player with id {player_id} in the pool."}
    if int(player_id) in b.drafted_ids():
        return {"error": f"{chosen['name']} is already drafted."}

    pick = b.record_manual_pick(int(player_id), team_id)
    state = b.draft_state()
    out = {
        "recorded": f"#{pick['overall']} R{pick['round']} team {pick['team_id']}: "
                    f"{chosen['name']} ({chosen['position']})",
        "pick": pick,
        "picks_made": state["picks_made"],
        "on_the_clock_team_id": state["on_the_clock_team_id"],
    }
    if b.cfg.team_id:
        upcoming = b.upcoming_picks_for(b.cfg.team_id, count=2, state=state)
        out["my_next_picks"] = upcoming
        if upcoming and state["next_overall_pick"]:
            out["picks_until_my_turn"] = upcoming[0] - state["next_overall_pick"]
    return out


@mcp.tool()
@handle_errors
def record_picks(players: list[str]) -> dict:
    """Record several picks at once, in draft order, by name.

    The fast path during a live draft: ESPN does not publish picks until the
    draft ends, so these ARE the draft. Recorded picks persist, so you only
    ever send the picks made since your last call -- never the whole board.

    Re-sending the entire pick history is fine and is the intended way to sync
    -- players already recorded are counted and ignored, and only the new names
    are appended, in the order given. Unrecognised or ambiguous names are
    reported without blocking the rest.
    """
    b = board()
    # Resolve every name against the cached board first, then record in one
    # shot: recording individually re-reads draft state per pick.
    taken = set(b.drafted_ids())
    resolved, skipped = [], []
    already = 0
    for name in players:
        matches = b.find_players(name, limit=6)
        if not matches:
            skipped.append({"name": name, "reason": "no match"})
            continue
        exact = [m for m in matches if m["name"].lower() == name.lower().strip()]
        fresh = [m for m in matches if m["player_id"] not in taken]
        if exact:
            pick = exact[0]
        elif not fresh:
            # Already have it. Re-sending the whole board is the normal way to
            # sync, so this is expected, not a problem worth reporting.
            already += 1
            continue
        elif len(fresh) > 1:
            skipped.append({"name": name, "reason": "ambiguous",
                            "candidates": [m["name"] for m in fresh[:4]]})
            continue
        else:
            pick = fresh[0]
        if pick["player_id"] in taken:
            already += 1
            continue
        taken.add(pick["player_id"])
        resolved.append(pick)

    made = b.record_manual_picks([p["player_id"] for p in resolved]) if resolved else []
    recorded = [f"#{m['overall']} {p['name']} ({p['position']})"
                for m, p in zip(made, resolved)]

    state = b.draft_state()
    out = {
        "newly_recorded": len(recorded),
        "already_had": already,
        "picks_made": state["picks_made"],
        "on_the_clock_team_id": state["on_the_clock_team_id"],
    }
    if recorded:
        out["new_picks"] = recorded[-12:]
    if skipped:
        out["needs_attention"] = skipped
    if b.cfg.team_id:
        upcoming = b.upcoming_picks_for(b.cfg.team_id, count=2, state=state)
        out["my_next_picks"] = upcoming
        if upcoming and state["next_overall_pick"]:
            out["picks_until_my_turn"] = upcoming[0] - state["next_overall_pick"]
    return out


@mcp.tool()
@handle_errors
def next_pick(candidates: int = 5) -> dict:
    """The single call to make when you are on the clock.

    Returns only what a pick decision needs: the best available players that
    fill an actual roster hole, the tier cliff behind each, and how long until
    your next turn. Deliberately small -- a draft clock is 60 seconds.
    """
    b = board()
    shape = b.shape()
    state = b.draft_state()
    taken = b.drafted_ids(state)
    pool = b.available(limit=400, taken=taken)

    mine = [
        b.board()["by_id"][p["player_id"]]
        for p in state["picks"]
        if p["team_id"] == b.cfg.team_id and p["player_id"] in b.board()["by_id"]
    ]
    needs = _roster_needs(shape, [p["position"] for p in mine])
    unfilled = set(needs["unfilled_starting_slots"])
    flex_open = needs["open_flex_slots"] > 0
    flex_positions = {pos for eligible in shape.flex_slots for pos in eligible}

    def fills_a_hole(p: dict) -> bool:
        if not unfilled and not flex_open:
            return True  # starters are set; everything is bench depth
        if p["position"] in unfilled:
            return True
        return flex_open and p["position"] in flex_positions

    ranked = [p for p in pool if fills_a_hole(p)] or pool
    top = []
    for p in ranked[:candidates]:
        entry = _slim(p)
        same_tier = [
            q for q in pool
            if q["position"] == p["position"] and q.get("tier") == p.get("tier")
        ]
        entry["left_in_tier"] = len(same_tier)
        below = [
            q for q in pool
            if q["position"] == p["position"] and (q.get("tier") or 0) == (p.get("tier") or 0) + 1
        ]
        if below and p.get("vorp") is not None and below[0].get("vorp") is not None:
            entry["cliff_after_tier"] = round(
                min(q["vorp"] for q in same_tier if q["vorp"] is not None) - below[0]["vorp"], 1
            )
        top.append(entry)

    out = {
        "on_the_clock_pick": state["next_overall_pick"],
        "is_my_turn": state["on_the_clock_team_id"] == b.cfg.team_id,
        "unfilled_starting_slots": needs["unfilled_starting_slots"],
        "open_flex_slots": needs["open_flex_slots"],
        "candidates": top,
    }
    if b.cfg.team_id:
        upcoming = b.upcoming_picks_for(b.cfg.team_id, count=2, state=state)
        out["my_next_picks"] = upcoming
        if len(upcoming) >= 2:
            out["picks_until_i_pick_again"] = upcoming[1] - upcoming[0] - 1
    return out


@mcp.tool()
@handle_errors
def reset_draft() -> dict:
    """Clear all manually recorded picks. Use before a new draft."""
    return {"cleared": board().clear_manual_picks()}


@mcp.tool()
@handle_errors
def undo_pick() -> dict:
    """Remove the most recently manually recorded pick."""
    pick = board().undo_manual_pick()
    return {"removed": pick} if pick else {"removed": None, "note": "No manual picks."}


@mcp.tool()
@handle_errors
def refresh_draft_order() -> dict:
    """Re-read the draft order and your slot.

    Call this after a randomized draft order is drawn (many leagues randomize
    shortly before the draft starts). League settings are otherwise cached, so
    a session opened before the draw would keep the old order.
    """
    b = board()
    b.shape(refresh=True)
    state = b.draft_state()
    order = state["pick_order"]
    out = {
        "pick_order_team_ids": order,
        "pick_order_source": state["pick_order_source"],
        "looks_unrandomized": bool(order) and order == sorted(order),
    }
    if b.cfg.team_id:
        out["my_team_id"] = b.cfg.team_id
        out["my_draft_slot"] = b.my_slot(b.cfg.team_id, state)
        out["my_next_picks"] = b.upcoming_picks_for(b.cfg.team_id, count=5, state=state)
    return out


@mcp.tool()
@handle_errors
def refresh_board() -> dict:
    """Force a re-fetch of the player pool, projections and value math.

    The pool is cached for ESPN_POOL_TTL seconds. Call this if projections
    changed (injury news, depth chart move) or the cache looks stale.
    """
    data = board().board(refresh=True)
    return {
        "players_loaded": len(data["players"]),
        "replacement_points": data["replacement_points"],
    }


# --------------------------------------------------------------------------
# In-season tools
# --------------------------------------------------------------------------


def _local_time(ms: int | None, fmt: str = "%a %I:%M %p") -> str | None:
    if not ms:
        return None
    from datetime import datetime, timezone
    return (datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone()
            .strftime(fmt).replace(" 0", " "))


def _slim_season(p: dict) -> dict:
    """Compact in-season record: this week and the rest of the season."""
    out = {
        "id": p["player_id"],
        "name": p["name"],
        "pos": p["position"],
        "team": p["pro_team"],
        "opp": p.get("nfl_opponent"),
        "week_proj": p.get(WEEK_KEY),
        "ros_pg": p.get(ROS_KEY),
        "ros": p.get("ros_points"),
        "vorp": p.get("vorp"),
        "injury": p.get("injury_status"),
    }
    if p.get("slot"):
        out["slot"] = p["slot"]
    if p.get("on_bye"):
        out["bye"] = True
    elif p.get("bye_week"):
        out["bye_week"] = p["bye_week"]
    if p.get("opp_rank_vs_pos"):
        out["opp_rank_vs_pos"] = p["opp_rank_vs_pos"]
    if p.get("kickoff_ms"):
        out["kickoff"] = _local_time(p["kickoff_ms"])
    if p.get("on_trade_block"):
        out["on_trade_block"] = True
    if p.get("value_basis") != "vorp":
        out["ranked_by"] = "week_proj"  # no ROS projection (D/ST)
    return out


def _record(t: dict) -> str:
    rec = f"{t['wins']}-{t['losses']}"
    if t.get("ties"):
        rec += f"-{t['ties']}"
    return rec


def _team_brief(t: dict) -> dict:
    return {"team_id": t["team_id"], "name": t["name"], "record": _record(t),
            "points_for": t["points_for"]}


def _resolve(names: list[str], players: list[dict], label: str) -> tuple[list[dict], list[dict]]:
    """Match names against a roster. Returns (matched, problems)."""
    found, problems = [], []
    for name in names:
        q = name.lower().strip()
        hits = [p for p in players if q in (p["name"] or "").lower()]
        exact = [p for p in hits if p["name"].lower() == q]
        if exact:
            hits = exact
        if not hits:
            problems.append({"name": name, "problem": f"not on {label}"})
        elif len(hits) > 1:
            problems.append({"name": name, "problem": "ambiguous",
                             "candidates": [p["name"] for p in hits[:5]]})
        else:
            found.append(hits[0])
    return found, problems


def _require_team(b: DraftBoard) -> int | None:
    return b.cfg.team_id


def _week_note(shape: LeagueShape) -> str | None:
    if not shape.is_active:
        return "ESPN reports the season as not active yet; projections are preseason."
    return None


@mcp.tool()
@handle_errors
def get_matchup(week: int | None = None) -> dict:
    """This week's head-to-head: both lineups, start/sit, and where the game is won.

    Compares your set lineup to the optimal one by ESPN's weekly projection
    (which already reflects the NFL opponent, injury designation and bye),
    lists the exact start/sit swaps and what they are worth, and shows the
    opponent's projected lineup with any holes (bye, OUT, empty slot).

    Args:
        week: defaults to the current week. Pass next week to plan ahead.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = week or b.week()

    game = next((m for m in b.matchups(week)
                 if me in (m["home_team_id"], m["away_team_id"])), None)
    teams = b.league_rosters(week)
    if not game:
        return {"week": week, "error": f"No matchup found for team {me} in week {week}.",
                "note": "Bye week, or the week is outside the schedule."}
    i_am_home = game["home_team_id"] == me
    opp_id = game["away_team_id"] if i_am_home else game["home_team_id"]

    my = b.team_players(me, week)
    theirs = b.team_players(opp_id, week)
    mine = lineup_changes(my, shape, WEEK_KEY)
    opp = lineup_changes(theirs, shape, WEEK_KEY)

    def holes(starters: list[dict]) -> list[dict]:
        out = []
        for p in starters:
            why = None
            if p.get("on_bye"):
                why = "bye"
            elif p.get("injury_status") in ("OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL"):
                why = p["injury_status"].lower()
            elif not p.get(WEEK_KEY):
                why = "no projection"
            if why:
                out.append({"name": p["name"], "pos": p["position"], "slot": p.get("slot"), "why": why})
        return out

    def flags(starters: list[dict]) -> list[str]:
        return [f"{p['name']} is {p['injury_status']}" for p in starters
                if p.get("injury_status") in ("QUESTIONABLE",)]

    my_now = current_starters(my)
    opp_now = current_starters(theirs)
    empty = [slot for slot, p in mine["optimal"]["starters"] if p is None]

    my_win_prob = game["home_win_prob"] if i_am_home else (
        round(1 - game["home_win_prob"], 3) if game["home_win_prob"] is not None else None)

    out: dict[str, Any] = {
        "week": week,
        "me": {**_team_brief(teams[me]), "home": i_am_home},
        "opponent": _team_brief(teams[opp_id]),
        "espn": {
            "my_projection": game["home_espn_proj"] if i_am_home else game["away_espn_proj"],
            "their_projection": game["away_espn_proj"] if i_am_home else game["home_espn_proj"],
            "my_win_probability": my_win_prob,
            "my_points": game["home_points"] if i_am_home else game["away_points"],
            "their_points": game["away_points"] if i_am_home else game["home_points"],
            "status": game["winner"],
        },
        "my_lineup": {
            "set_total": mine["current_total"],
            "optimal_total": mine["optimal_total"],
            "gain_from_optimal": mine["gain"],
            "start": [_slim_season(p) for p in mine["start"]],
            "sit": [_slim_season(p) for p in mine["sit"]],
            "starters_now": [_slim_season(p) for p in my_now],
            "bench": [_slim_season(p) for p in my if p not in my_now],
            "holes": holes(my_now),
            "questionable": flags(my_now),
            "unfillable_slots": empty,
        },
        "opponent_lineup": {
            "set_total": opp["current_total"],
            "optimal_total": opp["optimal_total"],
            "starters_now": [_slim_season(p) for p in opp_now],
            "best_bench": [_slim_season(p) for p in
                           sorted((p for p in theirs if p not in opp_now),
                                  key=lambda p: -(p.get(WEEK_KEY) or 0))[:4]],
            "holes": holes(opp_now),
            "questionable": flags(opp_now),
        },
        "margin_if_both_optimal": round(mine["optimal_total"] - opp["optimal_total"], 1),
        "note": (
            "week_proj is ESPN's projection for this week and already reflects the "
            "NFL opponent, injury status and bye. opp_rank_vs_pos is ESPN's OPRK: "
            "1 = defense that allows the most to that position (best matchup), 32 = "
            "stingiest; absent until games have been played."
        ),
    }
    if (n := _week_note(shape)):
        out["season_note"] = n
    return out


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _move_row(m: dict) -> dict:
    return {"player": m["name"], "from": m["from_slot"], "to": m["to_slot"]}


@mcp.tool()
@handle_errors
def set_lineup(week: int | None = None, apply: bool = False) -> dict:
    """Set the best lineup for a week, or preview the swaps it would make.

    The best lineup is by ESPN's projection for that week (which reflects
    the NFL opponent, injury designation and bye), the same one get_matchup
    scores. Players whose game has kicked off are locked and kept where they
    are; players on IR stay on IR (use move_player to activate one, which
    needs a free bench spot). With apply=false (the default) nothing changes
    on ESPN: the swaps and what they are worth come back for review. With
    apply=true the swaps are submitted as one ESPN transaction.

    Args:
        week: defaults to the current week. Pass next week to set it early.
        apply: submit the swaps to ESPN. False previews them.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = week or b.week()
    players = b.team_players(me, week)
    if not players:
        return {"error": f"No roster found for team {me} in week {week}."}
    plan = plan_lineup(players, shape, WEEK_KEY, now_ms=_now_ms())
    out: dict[str, Any] = {
        "week": week,
        "applied": False,
        "moves": [_move_row(m) for m in plan["moves"]],
        "set_total": plan["total_before"],
        "optimal_total": plan["total_after"],
        "gain": plan["gain"],
        "starters_after": [
            {**_slim_season(p), "slot": SLOT_BY_ID.get(sid, str(sid))}
            for sid, p in plan["starters"]
        ],
        "locked": [p["name"] for p in plan["locked"]],
        "unfillable_slots": plan["unfilled"],
        "questionable": [f"{p['name']} is {p['injury_status']}" for _, p in plan["starters"]
                         if p.get("injury_status") == "QUESTIONABLE"],
    }
    if not plan["moves"]:
        out["note"] = "The set lineup is already the best one; nothing to do."
        return out
    if not apply:
        out["note"] = "Preview only. Call again with apply=true to submit these swaps."
        return out
    result = b.client.set_lineup(me, week, plan["moves"])
    b.league_rosters(week, refresh=True)
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    return out


@mcp.tool()
@handle_errors
def move_player(player: str, to_slot: str, week: int | None = None) -> dict:
    """Move one of your players into a lineup slot on ESPN.

    Slots: QB, RB, WR, TE, FLEX, K, D/ST, BE (bench), IR. Moving into a full
    starting slot swaps out its lowest-projected starter, who goes to the
    mover's old slot when eligible and the bench otherwise. Moving a player
    out of IR needs a free bench spot (drop someone first). A player whose
    game has started cannot be moved. This is a real ESPN roster change.

    Args:
        player: name, or a unique part of it.
        to_slot: the slot to put him in.
        week: defaults to the current week.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = week or b.week()
    players = b.team_players(me, week)
    found, problems = _resolve([player], players, "your roster")
    if problems:
        return {"error": problems[0]["problem"], **problems[0]}
    mover = found[0]
    slot_name = to_slot.strip().upper().replace("BENCH", "BE")
    slot_name = {"DST": "D/ST", "DEF": "D/ST"}.get(slot_name, slot_name)
    sid = SLOT_ID_BY_NAME.get(slot_name)
    if sid is None:
        return {"error": f"Unknown slot {to_slot!r}. Use one of: "
                         f"{', '.join(SLOT_BY_ID[s] for s in sorted(shape.lineup_slots) if shape.lineup_slots[s])}."}
    plan = plan_move(players, shape, mover, sid, WEEK_KEY, now_ms=_now_ms())
    if "error" in plan:
        return {"error": plan["error"]}
    result = b.client.set_lineup(me, week, plan["moves"])
    b.league_rosters(week, refresh=True)
    out: dict[str, Any] = {
        "week": week,
        "applied": True,
        "moves": [_move_row(m) for m in plan["moves"]],
        "espn_status": result.get("status"),
        "transaction_id": result.get("id"),
    }
    if plan.get("displaced"):
        out["displaced"] = plan["displaced"]["name"]
    return out


@mcp.tool()
@handle_errors
def get_waiver_targets(position: str | None = None, limit: int = 12,
                       week: int | None = None) -> dict:
    """Who to add, who to drop, and what each swap is worth.

    Every unrostered player is scored by what adding him does to your optimal
    lineup: rest-of-season points per game (the lasting value of the roster
    spot) and this week's projection (a streamer). Ranked by the former;
    `streamers_this_week` re-ranks by the latter. Drop candidates are players
    neither horizon starts, least valuable first.

    Args:
        position: QB, RB, WR, TE, K or D/ST. Omit for all.
        limit: how many targets to return.
        week: defaults to the current week.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = week or b.week()
    pos = position.upper() if position else None

    my = b.team_players(me, week)
    teams = b.league_rosters(week)
    avail = b.season_available(week, pos)

    scored = []
    for c in avail:
        g = waiver_gain(my, c, shape)
        entry = _slim_season(c)
        entry.update(g)
        entry["status"] = c.get("roster_status")
        if c.get("waiver_clears_ms"):
            entry["waivers_clear"] = _local_time(c["waiver_clears_ms"], "%a %b %d %I:%M %p")
        entry["rostered_pct"] = c.get("percent_owned")
        if c.get("percent_owned_change"):
            entry["rostered_change_7d"] = c["percent_owned_change"]
        scored.append(entry)

    late = {"K", "D/ST"}
    # Ties on lineup gain (usually: nobody cracks a set lineup) fall through to
    # depth value, where a kicker's VORP must not outrank a running back's --
    # the same guard the draft board applies.
    by_ros = sorted(scored, key=lambda e: (-(e["lineup_gain_ros_per_game"] or 0),
                                           e["pos"] in late,
                                           -(e["vorp"] or 0), -(e["ros_pg"] or 0)))
    by_week = sorted(scored, key=lambda e: (-(e["lineup_gain_this_week"] or 0),
                                            -(e["week_proj"] or 0)))
    # Depth adds that never crack the lineup still matter: best by ROS VORP.
    depth = sorted((e for e in scored if e["vorp"] is not None
                    and (pos or e["pos"] not in late)),
                   key=lambda e: -e["vorp"])

    mine_t = teams[me]
    out: dict[str, Any] = {
        "week": week,
        "my_waiver_priority": mine_t.get("waiver_rank"),
        "waiver_rules": shape.waivers,
        "targets": by_ros[:limit],
        "streamers_this_week": [e for e in by_week[:limit] if (e["lineup_gain_this_week"] or 0) > 0],
        "best_depth_by_ros_vorp": depth[:min(limit, 8)],
        "drop_candidates": [_slim_season(p) for p in drop_candidates(my, shape, 5, pos)],
        "note": (
            "lineup_gain_* is the change in your optimal lineup total if the player "
            "is added (before any drop). 0 means he sits behind what you have. "
            "status WAIVERS means a claim, processed at waivers_clear; FREEAGENT "
            "is an immediate add. rostered_change_7d is the league-wide add trend."
        ),
    }
    if shape.waivers.get("uses_faab"):
        spent = mine_t.get("faab_spent") or 0
        out["faab_remaining"] = (shape.waivers.get("faab_budget") or 0) - spent
    if (n := _week_note(shape)):
        out["season_note"] = n
    return out


@mcp.tool()
@handle_errors
def analyze_trade(give: list[str], receive: list[str],
                  partner_team_id: int | None = None) -> dict:
    """Evaluate a trade from both sides: lineup strength before and after.

    For each team: optimal-lineup rest-of-season points per game (the lasting
    value), this week's optimal total, bench value, roster legality (size and
    position limits), and suggested drops if the trade leaves a roster over
    the limit. A trade that raises both teams' starting lineups is the kind
    that gets accepted.

    Args:
        give: names of players you send (must be on your roster).
        receive: names of players you get.
        partner_team_id: the other team. Inferred from `receive` if omitted.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = b.week()
    teams = b.league_rosters(week)

    my = b.team_players(me, week)
    mine, problems = _resolve(give, my, "your roster")

    if partner_team_id is None:
        # Resolve `receive` against every other roster at once, so a partial
        # name is matched league-wide (exact name first), then read the owner.
        everyone = []
        for tid in teams:
            if tid != me:
                for p in b.team_players(tid, week):
                    everyone.append({**p, "_team": tid})
        found, more = _resolve(receive, everyone, "any other roster")
        if more:
            return {"error": "Could not resolve every player.", "problems": problems + more}
        owners = {p["_team"] for p in found}
        if len(owners) != 1:
            return {"error": "The players in `receive` are on different teams; a trade "
                             "has one partner. Pass partner_team_id.",
                    "owners": {p["name"]: teams[p["_team"]]["name"] for p in found}}
        partner_team_id = owners.pop()
    if int(partner_team_id) not in teams:
        return {"error": f"Team {partner_team_id} not found. Known: {sorted(teams)}"}
    theirs = b.team_players(int(partner_team_id), week)
    theirs_in, more = _resolve(receive, theirs, teams[int(partner_team_id)]["name"])
    problems += more
    if problems:
        return {"error": "Could not resolve every player.", "problems": problems}

    ev = evaluate_trade(my, theirs, mine, theirs_in, shape)

    def side(res: dict, roster_after: list[dict], protect: str | None) -> dict:
        out = {
            "before": res["before"],
            "after": res["after"],
            "delta": res["delta"],
            "must_drop": res["must_drop"],
            "violations": res["violations"],
            "lineup_after": [
                {"slot": slot, **_slim_season(p)} if p else {"slot": slot, "empty": True}
                for slot, p in optimal_lineup(roster_after, shape, ROS_KEY)["starters"]
            ],
        }
        if res["must_drop"]:
            out["suggested_drops"] = [_slim_season(p) for p in
                                      drop_candidates(roster_after, shape, res["must_drop"] + 2, protect)]
        return out

    deadline = shape.season_describe()
    out: dict[str, Any] = {
        "week": week,
        "partner": _team_brief(teams[int(partner_team_id)]),
        "give": [_slim_season(p) for p in mine],
        "receive": [_slim_season(p) for p in theirs_in],
        "me": side(ev["me"], ev["me"]["roster_after"], None),
        "them": side(ev["them"], ev["them"]["roster_after"], None),
        "summary": {
            "my_starters_ros_per_game_change": ev["me"]["delta"]["starters_ros_per_game"],
            "their_starters_ros_per_game_change": ev["them"]["delta"]["starters_ros_per_game"],
            "my_this_week_change": ev["me"]["delta"]["starters_this_week"],
            "their_this_week_change": ev["them"]["delta"]["starters_this_week"],
            "ros_vorp_given": round(sum(p.get("vorp") or 0 for p in mine), 1),
            "ros_vorp_received": round(sum(p.get("vorp") or 0 for p in theirs_in), 1),
        },
        "note": (
            "starters_ros_per_game is each team's optimal lineup total in rest-of-"
            "season points per game -- the number that decides whether a trade "
            "helps. bench_ros_vorp is depth value. Position counts after the trade "
            "are checked against the league's roster limits."
        ),
    }
    if deadline.get("trade_deadline_local"):
        out["trade_deadline"] = deadline["trade_deadline_local"]
        out["trade_deadline_passed"] = deadline["trade_deadline_passed"]
    if shape.trade_veto_votes:
        out["veto_votes_required"] = shape.trade_veto_votes
    return out


@mcp.tool()
@handle_errors
def get_transactions(team_id: int | None = None, week: int | None = None,
                     kind: str | None = None, limit: int = 40,
                     include_draft: bool = False) -> dict:
    """The league's transaction log, newest first: who changed what, and when.

    Every lineup move (player, from slot, to slot), add, drop, waiver claim
    and trade this season, with the team and a timestamp. This is the only
    way to see history -- rosters show the current state only. Use it to
    answer "has my opponent touched his lineup this week", "who picked up X
    and when", or "did he swap someone in and back out".

    team_id: one team only (omit for the whole league).
    week: one scoring period only.
    kind: "lineup", "add_drop", "waiver", "trade" or "draft".
    include_draft: draft picks are excluded unless asked for; they swamp
    everything else.
    """
    b = board()
    raw = b.client.transactions()
    teams = b.league_rosters(week or b.week())
    names: dict[int, str] = {}
    for t in teams.values():
        for e in t["entries"]:
            if e.get("player"):
                names[e["player_id"]] = e["player"]["name"]

    def name_of(pid: int) -> str | None:
        if pid in names:
            return names[pid]
        p = b.player(pid)
        return p["name"] if p else None

    def team_of(tid) -> str | None:
        t = teams.get(int(tid)) if tid is not None else None
        return t["name"] if t else None

    rows = []
    for t in raw:
        rec = describe_transaction(t, name_of, team_of)
        if rec["kind"] == "draft" and not include_draft and kind != "draft":
            continue
        if team_id is not None and rec["team_id"] != int(team_id):
            continue
        if week is not None and rec["week"] != int(week):
            continue
        if kind and rec["kind"] != kind:
            continue
        rows.append(rec)
    rows.sort(key=lambda r: r["date_ms"] or 0, reverse=True)
    total = len(rows)
    rows = rows[:max(1, int(limit))]
    for r in rows:
        r["when"] = _local_time(r["date_ms"], "%a %b %d %I:%M %p")
    out = {
        "transactions": rows,
        "shown": len(rows),
        "total": total,
        "filters": {"team_id": team_id, "week": week, "kind": kind,
                    "include_draft": include_draft},
    }
    if team_id is not None and team_of(team_id):
        out["team"] = team_of(team_id)
    if b.cfg.team_id:
        out["my_team_id"] = b.cfg.team_id
    return out


@mcp.tool()
@handle_errors
def find_trade_partners(position: str | None = None, per_team: int = 3) -> dict:
    """Which teams have what you need, and need what you have.

    Profiles every roster by starter strength per position (rest-of-season
    points per game, versus the league average) and finds mutual fits: their
    players who would raise your optimal lineup, and your players who would
    raise theirs. Also surfaces each team's ESPN trade block and record --
    a team out of contention sells differently from one in first.

    Args:
        position: focus on one position you want to acquire. Omit for all.
        per_team: candidates to list on each side per team.
    """
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = b.week()
    teams = b.league_rosters(week)
    pos = position.upper() if position else None

    rosters = {tid: b.team_players(tid, week) for tid in teams}
    profiles = {tid: roster_profile(r, shape) for tid, r in rosters.items()}
    avgs = league_position_averages(profiles)

    def team_needs(tid: int) -> dict[str, float]:
        prof = profiles[tid]
        out = {}
        for p, block in prof["positions"].items():
            if p in ("K", "D/ST") or block["starter_avg"] is None or p not in avgs:
                continue
            gap = round(avgs[p] - block["starter_avg"], 2)
            if gap > 0:
                out[p] = gap
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    my_roster = rosters[me]
    my_needs = team_needs(me)
    if pos:
        my_needs = {pos: my_needs.get(pos, 0.0)}
    skill = lambda p: p["position"] not in ("K", "D/ST")  # noqa: E731

    partners = []
    for tid, roster in rosters.items():
        if tid == me:
            continue
        their_needs = team_needs(tid)
        # Their players at my need positions that would raise my lineup.
        offers = []
        for p in roster:
            if not skill(p) or (pos and p["position"] != pos):
                continue
            if not pos and p["position"] not in my_needs:
                continue
            gain = waiver_gain(my_roster, p, shape)["lineup_gain_ros_per_game"]
            if gain > 0:
                offers.append((gain, p))
        offers.sort(key=lambda t: -t[0])
        if not offers:
            continue
        # What I could send: anyone of mine at a position they need, plus any
        # bench player of mine who would start for them.
        asks = []
        for p in my_roster:
            if not skill(p):
                continue
            gain = waiver_gain(roster, p, shape)["lineup_gain_ros_per_game"]
            if gain > 0 and (p["position"] in their_needs or p["slot_id"] in (20, 21)):
                asks.append((gain, p))
        asks.sort(key=lambda t: -t[0])

        # The number that matters is the net of a concrete swap, not two
        # one-sided gains: try every 1-for-1 among the top candidates and keep
        # the one that helps both sides the most.
        best = None
        for _, theirs_p in offers[:6]:
            for _, mine_p in asks[:6]:
                ev = evaluate_trade(my_roster, roster, [mine_p], [theirs_p], shape)
                mine_d = ev["me"]["delta"]["starters_ros_per_game"]
                their_d = ev["them"]["delta"]["starters_ros_per_game"]
                score = min(mine_d, their_d)
                if best is None or score > best["mutual_gain"]:
                    best = {"give": mine_p["name"], "receive": theirs_p["name"],
                            "my_gain": mine_d, "their_gain": their_d,
                            "mutual_gain": round(score, 2)}
        t = teams[tid]
        partners.append({
            **_team_brief(t),
            "best_1_for_1": best,
            "mutual": bool(best and best["my_gain"] > 0 and best["their_gain"] > 0),
            "their_needs": their_needs,
            "they_could_send": [{"my_lineup_gain": round(g, 2), **_slim_season(p)}
                                for g, p in offers[:per_team]],
            "i_could_send": [{"their_lineup_gain": round(g, 2), **_slim_season(p)}
                             for g, p in asks[:per_team]],
            "trade_block": [p["name"] for p in roster if p.get("on_trade_block")],
        })
    partners.sort(key=lambda t: (-t["mutual"],
                                 -(t["best_1_for_1"] or {}).get("mutual_gain", -99),
                                 -t["they_could_send"][0]["my_lineup_gain"]))

    my_prof = profiles[me]
    out: dict[str, Any] = {
        "week": week,
        "league_starter_avg_ros_per_game": avgs,
        "my_starters_by_position": {
            p: {"starter_avg": blk["starter_avg"], "vs_league": round(blk["starter_avg"] - avgs[p], 2)}
            for p, blk in my_prof["positions"].items()
            if blk["starter_avg"] is not None and p in avgs
        },
        "my_needs": my_needs,
        "my_surplus": [_slim_season(p) for blk in my_prof["positions"].values()
                       for p in blk["bench"] if (p.get("vorp") or 0) > 0
                       and p["position"] not in ("K", "D/ST")],
        "partners": partners,
        "note": (
            "my_lineup_gain / their_lineup_gain are one-sided: the change in a "
            "team's optimal lineup (rest-of-season points per game) from adding "
            "that player, ignoring what goes back. best_1_for_1 nets both sides "
            "for a concrete swap; mutual=true means both lineups improve. Build "
            "2-for-1s and check roster limits with analyze_trade."
        ),
    }
    if (n := _week_note(shape)):
        out["season_note"] = n
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
