"""MCP server exposing an ESPN fantasy football league: draft and in-season.

Design rule: tools return facts, not opinions. The one exception is the value
math (VORP, tiers, replacement level), which is deterministic arithmetic that a
language model should not be doing in its head. Pick recommendations are left
to the model reasoning over these tools.
"""

from __future__ import annotations

import contextvars
import functools
from typing import Any

from mcp.server import MCPServer

from . import __version__
from .autopolicy import auto_ok
from .board import DraftBoard
from .stash import (POSITION_CAP, STASH_FLOOR, dead_spots, replacement_per_game, stash_moves,
                    stash_score)
from .constants import SLOT_BY_ID
from .config import Config, load_config
from .espn import ESPNError
from .factcheck import known_numbers, unsupported_numbers
from .market import acceptable, trade_view, worth_offering
from .notify import deadline, push_proposal
from .proposals import ProposalError, ProposalStore, clean_params, public
from .scoring import LeagueShape
from .sources.signals import market_view
from .season import (
    ROS_KEY,
    SLOT_ID_BY_NAME,
    START_KEY,
    WEEK_KEY,
    close_calls,
    current_starters,
    describe_transaction,
    drop_candidates,
    evaluate_swap,
    evaluate_trade,
    league_position_averages,
    lineup_changes,
    optimal_lineup,
    plan_lineup,
    plan_move,
    roster_profile,
    waiver_gain,
    with_start_proj,
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
        "slot (e.g. IR to BE). ROSTER MOVES: add_player (free agent or waiver claim, with "
        "the drop), drop_player. TRADES: propose_trade sends an offer, get_pending_trades "
        "lists offers waiting on either side, respond_to_trade accepts, declines or "
        "withdraws one. Every writing tool previews by default and only touches ESPN "
        "with apply=true; confirm with the user before applying. APPROVAL: when a "
        "roster move or trade is refused because it needs approval, queue it with "
        "request_approval; the manager approves it from a notification and it is "
        "sent then. get_proposals shows what is waiting and what was decided. "
        "ACCURACY: copy every number, name and status from a tool result; never "
        "recall or compute one. Before delivering a report, call check_report with "
        "its text and deliver it only once ok is true. Rankings are by VORP (value over replacement), which already "
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
            result = fn(*args, **kwargs)
        except ESPNError as exc:
            return {"error": str(exc), "recoverable": True}
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return {"error": f"{type(exc).__name__}: {exc}", "recoverable": False}
        _remember(result)
        return result

    return wrapper


# Every number the tools have handed out lately. A report is checked against
# these: a number in it that no tool returned was not read, it was recalled.
SEEN_FOR = 45 * 60
_seen: dict[float, float] = {}


def _remember(result) -> None:
    import time
    now = time.time()
    try:
        for value in known_numbers(result):
            _seen[value] = now
        if len(_seen) > 200_000:
            for value in [v for v, at in _seen.items() if now - at > SEEN_FOR]:
                del _seen[value]
    except Exception:  # noqa: BLE001 - bookkeeping must never fail a tool
        pass


def _seen_lately() -> list[float]:
    import time
    now = time.time()
    return [v for v, at in _seen.items() if now - at <= SEEN_FOR]


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
    out.update(_outside(p))
    return out


def _outside(p: dict) -> dict:
    """What the sources beyond ESPN say about a player, when they say anything."""
    out: dict[str, Any] = {}
    if p.get("market_value") is not None:
        out["market"] = {"value": p["market_value"], "pos_rank": p.get("market_pos_rank"),
                         "model_pos_rank": p.get("model_pos_rank")}
        if p.get("market_trend_30d"):
            out["market"]["trend_30d"] = p["market_trend_30d"]
        if (view := market_view(p)):
            out["market"]["view"] = view
    if p.get("usage"):
        out["usage"] = {k: v for k, v in p["usage"].items() if k != "per_game"}
    for key in ("adj_week_proj", "game", "adds_24h", "drops_24h", "alt_week_proj",
                "practice", "injury_alt"):
        if p.get(key) is not None:
            out[key] = p[key]
    return out


def _usage_view(give: list[dict], receive: list[dict]) -> dict | None:
    """A trade by workload: whose scoring is likely to hold up."""
    rows = [p for p in give + receive if p.get("usage")]
    if not rows:
        return None
    side = lambda ps: [  # noqa: E731
        {"name": p["name"], "ppg": p["usage"]["ppg"],
         "outlook_ppg": p["usage"]["outlook_ppg"], "view": p["usage"].get("view")}
        for p in ps if p.get("usage")]
    total = lambda ps, key: round(sum(p["usage"][key] for p in ps if p.get("usage")), 2)  # noqa: E731
    out = {
        "give": side(give), "receive": side(receive),
        "ppg_so_far": {"give": total(give, "ppg"), "receive": total(receive, "ppg")},
        "outlook_ppg": {"give": total(give, "outlook_ppg"),
                        "receive": total(receive, "outlook_ppg")},
    }
    out["outlook_change"] = round(out["outlook_ppg"]["receive"] - out["outlook_ppg"]["give"], 2)
    hot = [p["name"] for p in receive if (p.get("usage") or {}).get("view") == "running hot"]
    cold = [p["name"] for p in give if (p.get("usage") or {}).get("view") == "running cold"]
    if hot:
        out["warning"] = (f"Buying high: {', '.join(hot)} is scoring well above his "
                          "workload. Players like that fell 2 to 4 points a game.")
    elif cold:
        out["warning"] = (f"Selling low: {', '.join(cold)} is scoring well below his "
                          "workload. Players like that rose about 2 points a game.")
    return out


def _streaming(mine: list[dict], available: list[dict], now_ms: int) -> dict:
    """Defense and kicker for this week: what I have against what is free."""
    from .season import is_locked

    score = lambda p: p.get("adj_week_proj") if p.get("adj_week_proj") is not None \
        else (p.get(WEEK_KEY) or 0.0)  # noqa: E731

    def row(p: dict) -> dict:
        out = {"name": p["name"], "team": p["pro_team"], "opp": p.get("nfl_opponent"),
               "week_proj": p.get(WEEK_KEY), "adj_week_proj": p.get("adj_week_proj")}
        game = p.get("game") or {}
        for k in ("opponent_implied_total", "implied_total", "indoor", "wind_mph"):
            if game.get(k) is not None:
                out[k] = game[k]
        if p.get("roster_status"):
            out["status"] = p["roster_status"]
        return out

    out = {}
    for pos in ("D/ST", "K"):
        held = sorted((p for p in mine if p["position"] == pos), key=lambda p: -score(p))
        free = sorted((p for p in available if p["position"] == pos
                       and not is_locked(p, now_ms) and score(p) > 0),
                      key=lambda p: -score(p))[:3]
        best = held[0] if held else None
        entry = {"mine": [row(p) for p in held], "hold_count": len(held),
                 "best_available": [row(p) for p in free]}
        if free:
            entry["upgrade"] = round(score(free[0]) - (score(best) if best else 0.0), 2)
        out[pos] = entry
    return out


def _sources(b: DraftBoard) -> dict | None:
    return b.signals.status() if b.signals is not None else None


_OUTSIDE_NOTE = (
    "market is the player's trade value from real trades (FantasyCalc): pos_rank is "
    "the market's rank at the position, model_pos_rank is this league's rest-of-season "
    "projection rank, and view is 'sell' when the market rates him well above his "
    "projection and 'buy' when well below. adds_24h/drops_24h are pickups and cuts "
    "across Sleeper leagues in the last day. alt_week_proj is Sleeper's projection "
    "for the week; a large gap from week_proj means the two disagree, not that either "
    "is right. injury_alt is Sleeper's injury designation where it differs from "
    "ESPN's, with its age and the game it is about: a second report to check, not a "
    "correction. When about is 'next game' this week's game has already kicked off "
    "and the report says nothing about it. game is the player's NFL game: "
    "implied_total is the points the betting market expects his team to score, and "
    "wind_mph/rain_pct/temp_f are the forecast at kickoff for outdoor games. "
    "adj_week_proj is week_proj moved by the implied total; set_lineup uses it. "
    "Wind and rain are shown for judgement only: over two seasons they did not "
    "reliably improve on ESPN's projection, which already accounts for them. "
    "usage is the player's season against his workload: ppg is what he scores, "
    "expected_ppg is what his carries and targets say he should, gap is the "
    "difference, outlook_ppg is the rest-of-season estimate from both. view is "
    "'running hot' (scoring 3+ a game above his workload) or 'running cold' (3+ "
    "below). Over two seasons hot players fell 2 to 4 points a game afterwards "
    "and cold ones rose about 2: sell hot, buy cold."
)


def _record(t: dict) -> str:
    rec = f"{t['wins']}-{t['losses']}"
    if t.get("ties"):
        rec += f"-{t['ties']}"
    return rec


def _team_brief(t: dict) -> dict:
    return {"team_id": t["team_id"], "name": t["name"], "record": _record(t),
            "points_for": t["points_for"]}


def player_ref(player_id: int) -> str:
    """How a stored proposal names a player: by id, which cannot be ambiguous
    and does not change when a roster does."""
    return f"id:{int(player_id)}"


def _resolve(names: list[str], players: list[dict], label: str) -> tuple[list[dict], list[dict]]:
    """Match names, or `id:<player id>` references, against a roster.
    Returns (matched, problems)."""
    import re
    found, problems = [], []
    for name in names:
        ref = re.fullmatch(r"id:(-?\d+)", str(name).strip())
        if ref:
            hit = next((p for p in players if p["player_id"] == int(ref.group(1))), None)
            if hit is None:
                problems.append({"name": name, "problem": f"not on {label}"})
            else:
                found.append(hit)
            continue
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
    # The manager's own start/sit decisions outrank the projection.
    # ...unless the player he chose to start can no longer play: a decision
    # made on Thursday must not start a man ruled out on Sunday.
    by_id = {p["player_id"]: p for p in players}
    pins, dropped = [], []
    for x in _pins(b, week):
        chosen = by_id.get(x["start"])
        if chosen is None or x["sit"] not in by_id or _cannot_play(chosen):
            dropped.append(x)
        else:
            pins.append(x)
    start_ids = {x["start"] for x in pins}
    sit_ids = {x["sit"] for x in pins}
    scored = [{**p, "_start": (p.get(START_KEY) or 0.0)
               + (1000 if p["player_id"] in start_ids else 0)
               - (1000 if p["player_id"] in sit_ids else 0)}
              for p in with_start_proj(players)]
    plan = plan_lineup(scored, shape, "_start", now_ms=_now_ms())
    before = round(sum(p.get(WEEK_KEY) or 0.0 for p in current_starters(players)), 2)
    after = round(sum(p.get(WEEK_KEY) or 0.0 for _, p in plan["starters"]), 2)
    out: dict[str, Any] = {
        "week": week,
        "applied": False,
        "moves": [_move_row(m) for m in plan["moves"]],
        "set_total": before,
        "optimal_total": after,
        "gain": round(after - before, 2),
        "starters_after": [
            {**_slim_season(p), "slot": SLOT_BY_ID.get(sid, str(sid))}
            for sid, p in plan["starters"]
        ],
        "locked": [p["name"] for p in plan["locked"]],
        "unfillable_slots": plan["unfilled"],
        "questionable": [f"{p['name']} is {p['injury_status']}" for _, p in plan["starters"]
                         if p.get("injury_status") == "QUESTIONABLE"],
    }
    if any(p.get("adj_week_proj") is not None for p in players):
        out["set_by"] = (
            "adj_week_proj: ESPN's projection nudged by the betting line (a team "
            "expected to score more than the week's average moves its players up, "
            "by at most a point or so). Totals here are ESPN's own numbers, so they "
            "match the app.")
    if dropped:
        out["decisions_set_aside"] = [
            f"start {x['start_name']} over {x['sit_name']}: {x['start_name']} "
            "is out or no longer on the roster" for x in dropped]
    if pins:
        out["manager_decisions"] = [f"start {x['start_name']} over {x['sit_name']}"
                                    for x in pins]
    # Judge close calls on the lineup as it will stand after these moves.
    final_slot = {m["player_id"]: m["to_slot_id"] for m in plan["moves"]}
    standing = [{**p, "slot_id": final_slot.get(p["player_id"], p.get("slot_id"))}
                for p in with_start_proj(players)]
    calls = [c for c in close_calls(standing, shape, START_KEY, now_ms=_now_ms())
             if c["start"]["player_id"] not in sit_ids
             and c["sit"]["player_id"] not in start_ids]
    if calls:
        out["close_calls"] = [_close_call_row(c) for c in calls]
        out["close_calls_note"] = (
            "A benched player within half a point of a starter this week who is "
            "clearly the better player over the season or by trade value. The "
            "projection cannot separate them, and over two seasons neither could "
            "reputation: these came out even. It is the manager's preference. Queue with "
            "request_approval('start_player', {'player': ..., 'over': ...}, reasoning).")
    if not plan["moves"]:
        out["note"] = "The set lineup is already the best one; nothing to do."
        return out
    if not apply:
        out["note"] = "Preview only. Call again with apply=true to submit these swaps."
        return out
    result = b.client.set_lineup(me, week, plan["moves"])
    synced = _after_write(b, week, _expect_slots(me, plan["moves"]))
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    if not synced:
        out["note"] = _LAG_NOTE
    return out


def _cannot_play(p: dict) -> bool:
    from .season import NOT_PLAYING
    return ((p.get("injury_status") or "").upper() in NOT_PLAYING
            or bool(p.get("on_bye")) or p.get("nfl_opponent") == "BYE"
            or not (p.get(WEEK_KEY) or 0) > 0)


def _close_call_row(c: dict) -> dict:
    return {"start": c["start"]["name"], "over": c["sit"]["name"], "slot": c["slot"],
            "week_proj": [c["start"].get(WEEK_KEY), c["sit"].get(WEEK_KEY)],
            "start_proj": [c["start"].get(START_KEY), c["sit"].get(START_KEY)],
            "reasons": c["reasons"]}


def _pins_path(b: DraftBoard):
    return b.cfg.state_root / f"pins-{b.cfg.league_id}-{b.cfg.season}.json"


def _pins(b: DraftBoard, week: int) -> list[dict]:
    """The manager's approved start/sit decisions for a week."""
    import json
    try:
        return json.loads(_pins_path(b).read_text()).get(str(week), [])
    except (OSError, ValueError):
        return []


def _add_pin(b: DraftBoard, week: int, start: dict, sit: dict) -> None:
    import json
    path = _pins_path(b)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    ids = (start["player_id"], sit["player_id"])
    # A new decision about either player replaces the old one.
    rows = [x for x in data.get(str(week), [])
            if x["start"] not in ids and x["sit"] not in ids]
    rows.append({"start": start["player_id"], "sit": sit["player_id"],
                 "start_name": start["name"], "sit_name": sit["name"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(week): rows}))  # only the current week matters


@handle_errors
def start_player(player: str, over: str, apply: bool = False) -> dict:
    """Start one player in place of another, and keep it that way.

    Not a tool: reached through request_approval('start_player', ...), so it
    is the manager's decision. Once applied, set_lineup leaves the pair
    alone for the rest of the week.
    """
    ctx = _my_context()
    if isinstance(ctx, dict):
        return ctx
    b, shape, me, week, mine = ctx
    found, problems = _resolve([player, over], mine, "your roster")
    if problems:
        return {"error": problems[0]["problem"], **problems[0]}
    up, down = found
    if up["player_id"] == down["player_id"]:
        return {"error": "Those are the same player."}
    for p in (up, down):
        if is_locked_now(p):
            return {"error": f"{p['name']} is locked: his game has started."}
    slot = SLOT_BY_ID.get(down["slot_id"], str(down["slot_id"]))
    if up["slot_id"] not in (20, 21) and down["slot_id"] == 20:
        # The lineup got there on its own since this was asked. The decision
        # still stands, and is kept so that it cannot drift back.
        out = {"week": week, "applied": bool(apply), "start": _slim_season(up),
               "sit": _slim_season(down), "moves": [],
               "slot": SLOT_BY_ID.get(up["slot_id"], str(up["slot_id"])),
               "week_proj_change": 0.0,
               "kickoff_ms": min((k for k in (up.get("kickoff_ms"), down.get("kickoff_ms"))
                                  if k), default=None),
               "note": f"{up['name']} is already starting and {down['name']} is on the "
                       "bench."}
        if apply:
            _add_pin(b, week, up, down)
            out["note"] += " Kept that way for the rest of the week."
        return out
    if down["slot_id"] in (20, 21):
        return {"error": f"{down['name']} is not starting."}
    if up["slot_id"] not in (20,):
        return {"error": f"{up['name']} is not on the bench (he is in "
                         f"{SLOT_BY_ID.get(up['slot_id'], up['slot_id'])})."}
    if slot not in (up.get("eligible_slots") or []):
        return {"error": f"{up['name']} ({up['position']}) cannot play {slot}."}
    moves = [
        {"player_id": up["player_id"], "name": up["name"], "from_slot_id": up["slot_id"],
         "to_slot_id": down["slot_id"], "from_slot": "BE", "to_slot": slot},
        {"player_id": down["player_id"], "name": down["name"],
         "from_slot_id": down["slot_id"], "to_slot_id": 20, "from_slot": slot,
         "to_slot": "BE"},
    ]
    out: dict[str, Any] = {
        "week": week, "applied": False, "slot": slot,
        "start": _slim_season(up), "sit": _slim_season(down),
        "week_proj_change": round((up.get(WEEK_KEY) or 0) - (down.get(WEEK_KEY) or 0), 2),
        "kickoff_ms": min(k for k in (up.get("kickoff_ms"), down.get("kickoff_ms")) if k)
        if (up.get("kickoff_ms") or down.get("kickoff_ms")) else None,
        "moves": [_move_row(m) for m in moves],
    }
    if not apply:
        out["note"] = "Preview only."
        return out
    result = b.client.set_lineup(me, week, moves)
    _add_pin(b, week, up, down)
    synced = _after_write(b, week, _expect_slots(me, moves))
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    out["note"] = (_LAG_NOTE if not synced else
                   f"{up['name']} starts at {slot}; the lineup optimizer will leave "
                   "this alone for the rest of the week.")
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
    synced = _after_write(b, week, _expect_slots(me, plan["moves"]))
    out: dict[str, Any] = {
        "week": week,
        "applied": True,
        "moves": [_move_row(m) for m in plan["moves"]],
        "espn_status": result.get("status"),
        "transaction_id": result.get("id"),
    }
    if plan.get("displaced"):
        out["displaced"] = plan["displaced"]["name"]
    if not synced:
        out["note"] = _LAG_NOTE
    return out


def _my_context() -> tuple[DraftBoard, LeagueShape, int, int, list[dict]] | dict:
    """(board, shape, my team id, week, my players), or an error dict."""
    b = board()
    shape = b.shape()
    me = _require_team(b)
    if not me:
        return {"error": "ESPN_TEAM_ID is not set."}
    week = b.week()
    return b, shape, me, week, b.team_players(me, week)


def _swap_preview(res: dict, shape: LeagueShape, protect: str | None = None,
                  incoming: list[dict] = ()) -> dict:
    out = {
        "before": res["before"],
        "after": res["after"],
        "delta": res["delta"],
        "must_drop": res["must_drop"],
        "violations": res["violations"],
    }
    if res["must_drop"]:
        # Never suggest cutting the player being added.
        new_ids = {p["player_id"] for p in incoming}
        out["suggested_drops"] = [
            _slim_season(p) for p in
            drop_candidates(res["roster_after"], shape, res["must_drop"] + 3, protect)
            if p["player_id"] not in new_ids][:res["must_drop"] + 2]
    return out


# ESPN's reads lag its writes by a moment: a roster fetched right after a
# transaction can still show the old state. Re-read until the change shows.
_RETRY_DELAYS = (0.5, 1.0, 1.5, 2.0)


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def _after_write(b: DraftBoard, week: int, expect=None) -> bool:
    """Refresh the roster cache after a write, waiting for ESPN to reflect it.

    `expect(teams)` says whether the refreshed rosters show the change. Until
    it does, re-read with a short back-off; if ESPN never catches up within
    a few seconds, drop the cache so the next read fetches fresh, and return
    False so the caller can say so.
    """
    for delay in (0.0, *_RETRY_DELAYS):
        if delay:
            _sleep(delay)
        teams = b.league_rosters(week, refresh=True)
        if expect is None or expect(teams):
            return True
    b.invalidate_rosters(week)
    return False


def _expect_slots(team_id: int, moves: list[dict]):
    """Every moved player sits in his target slot."""
    def check(teams: dict) -> bool:
        entries = {e["player_id"]: e["slot_id"] for e in (teams.get(team_id) or {}).get("entries", [])}
        return all(entries.get(m["player_id"]) == m["to_slot_id"] for m in moves)
    return check


def _expect_roster(team_id: int, present=(), absent=()):
    """Added players are on the roster and dropped players are gone."""
    def check(teams: dict) -> bool:
        ids = {e["player_id"] for e in (teams.get(team_id) or {}).get("entries", [])}
        return all(pid in ids for pid in present) and not any(pid in ids for pid in absent)
    return check


# Set only while an approved proposal is being replayed. It is the one way
# past the approval gate, and nothing a tool caller passes can set it.
_approved_call: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "approved_call", default=False)


def _approval_gate(apply: bool) -> dict | None:
    """Refuse a direct write when the league is run on approvals."""
    if not apply or _approved_call.get() or not board().cfg.require_approval:
        return None
    return {
        "error": "This move needs the manager's approval and was not sent.",
        "applied": False,
        "recoverable": True,
        "hint": "Queue it with request_approval(action, params, reasoning).",
    }


_LAG_NOTE = "ESPN accepted the change but its reads have not caught up yet; re-read in a moment."


def _stash_repl(b: DraftBoard, shape: LeagueShape, week: int) -> dict:
    """Replacement level per position, in points a game, for stash_score."""
    board_ = b.season_board(week)
    # ROS totals cover the weeks left, one of which is usually a bye.
    games_left = max(shape.final_week - shape.current_week, 1)
    return replacement_per_game(board_["replacement_points"], games_left)


def _stash_view(b: DraftBoard, shape: LeagueShape, week: int, me: int,
                mine: list[dict], target: dict, drops: list[dict]) -> dict:
    """How an add looks as a stash: his upside, and whether the drop is dead."""
    repl = _stash_repl(b, shape, week)
    pending_drop = set((b.league_rosters(week).get(me) or {}).get("pending_drop_ids") or [])
    dead_ids = {p["player_id"] for p in dead_spots(mine, pending_drop)}
    pos = target["position"]
    held = sum(1 for p in mine if p["position"] == pos and p["player_id"] not in pending_drop
               and p.get("slot_id") != 21)
    like_for_like = bool(drops) and all(p["position"] == pos for p in drops)
    return {
        "add_stash_score": stash_score(target, repl),
        "drop_stash_score": round(sum(stash_score(p, repl) for p in drops), 2),
        "drop_is_dead_spot": bool(drops) and all(p["player_id"] in dead_ids for p in drops),
        "over_position_cap": (pos in POSITION_CAP and not like_for_like
                              and held + 1 > POSITION_CAP[pos]),
        "floor": STASH_FLOOR,
    }


@mcp.tool()
@handle_errors
def add_player(add: str, drop: str | None = None, apply: bool = False,
               bid: int | None = None) -> dict:
    """Pick up a free agent, or claim a player on waivers, dropping someone.

    A FREEAGENT add executes immediately; a WAIVERS claim is queued and
    processed at the league's next waiver run (get_waiver_targets shows
    `waivers_clear`). The drop goes in the same transaction so the roster
    never sits over the limit. With apply=false (the default) nothing is
    sent: the before/after lineup value, roster legality and suggested
    drops come back for review.

    Args:
        add: name of the unrostered player, or a unique part of it.
        drop: name of your player to cut. Required when the roster is full.
        apply: submit to ESPN. False previews.
        bid: FAAB bid for a waiver claim, in leagues that use a budget.
    """
    if (gate := _approval_gate(apply)):
        return gate
    ctx = _my_context()
    if isinstance(ctx, dict):
        return ctx
    b, shape, me, week, mine = ctx
    pool = b.season_available(week)
    found, problems = _resolve([add], pool, "the free-agent pool")
    if problems:
        return {"error": problems[0]["problem"], **problems[0],
                "hint": "Only unrostered players can be added; check get_waiver_targets."}
    target = found[0]
    drops: list[dict] = []
    if drop:
        drops, more = _resolve([drop], mine, "your roster")
        if more:
            return {"error": more[0]["problem"], **more[0]}
        if is_locked_now(drops[0]):
            return {"error": f"{drops[0]['name']} is locked: his game has started."}
    res = evaluate_swap(mine, drops, [target], shape)
    waiver = target.get("roster_status") == "WAIVERS"
    out: dict[str, Any] = {
        "week": week,
        "applied": False,
        "add": _slim_season(target),
        "drop": [_slim_season(p) for p in drops],
        "transaction": "waiver claim" if waiver else "free-agent add",
        **_swap_preview(res, shape, target["position"], incoming=[target]),
        "stash": _stash_view(b, shape, week, me, mine, target, drops),
    }
    if waiver and target.get("waiver_clears_ms"):
        out["waivers_clear"] = _local_time(target["waiver_clears_ms"], "%a %b %d %I:%M %p")
        out["waivers_clear_ms"] = target["waiver_clears_ms"]
    elif target.get("kickoff_ms") and not is_locked_now(target):
        # A free agent is only any use this week if he is added before his game.
        out["add_kickoff_ms"] = target["kickoff_ms"]
    if shape.waivers.get("uses_faab"):
        out["bid"] = bid or 0
    if res["must_drop"]:
        out["error"] = (f"Roster would be {res['must_drop']} over the limit; "
                        "name a player to drop.")
        return out
    if res["violations"]:
        out["error"] = "; ".join(res["violations"])
        return out
    if not apply:
        out["note"] = "Preview only. Call again with apply=true to submit."
        return out
    body = b.client.add_drop_transaction(me, week, [target["player_id"]],
                                         [p["player_id"] for p in drops],
                                         waiver=waiver, bid=bid)
    result = b.client.post_transaction(body)
    if waiver:
        # Nothing moves until the waiver run; just drop the cache.
        b.invalidate_rosters(week)
        synced = True
    else:
        synced = _after_write(b, week, _expect_roster(
            me, present=[target["player_id"]], absent=[p["player_id"] for p in drops]))
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    if waiver:
        out["note"] = "Claim queued; ESPN processes it at the next waiver run."
    elif not synced:
        out["note"] = _LAG_NOTE
    return out


@mcp.tool()
@handle_errors
def drop_player(player: str, apply: bool = False) -> dict:
    """Drop one of your players to free a roster spot.

    Previews what the lineup loses (usually nothing, if he was not
    starting) and applies with apply=true. Prefer add_player with `drop`
    when the spot is for a specific pickup: one transaction, no window
    where the spot is empty.

    Args:
        player: name, or a unique part of it.
        apply: submit to ESPN. False previews.
    """
    if (gate := _approval_gate(apply)):
        return gate
    ctx = _my_context()
    if isinstance(ctx, dict):
        return ctx
    b, shape, me, week, mine = ctx
    found, problems = _resolve([player], mine, "your roster")
    if problems:
        return {"error": problems[0]["problem"], **problems[0]}
    p = found[0]
    if is_locked_now(p):
        return {"error": f"{p['name']} is locked: his game has started."}
    res = evaluate_swap(mine, [p], [], shape)
    out: dict[str, Any] = {
        "week": week,
        "applied": False,
        "drop": _slim_season(p),
        **_swap_preview(res, shape),
        "starts_now": p.get("slot_id") not in (20, 21),
    }
    if not apply:
        out["note"] = "Preview only. Call again with apply=true to submit."
        return out
    result = b.client.post_transaction(
        b.client.add_drop_transaction(me, week, [], [p["player_id"]]))
    synced = _after_write(b, week, _expect_roster(me, absent=[p["player_id"]]))
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    if not synced:
        out["note"] = _LAG_NOTE
    return out


@mcp.tool()
@handle_errors
def propose_trade(give: list[str], receive: list[str],
                  partner_team_id: int | None = None, apply: bool = False) -> dict:
    """Send a trade offer to another team, or preview it first.

    The preview is analyze_trade: both sides before and after. With
    apply=true the offer is posted to ESPN and the other manager is
    notified; it then waits on them (see get_pending_trades; withdraw it
    with respond_to_trade). Sending an offer is a message to a real person,
    so confirm with the user before applying.

    Args:
        give: names of players you send.
        receive: names of players you get.
        partner_team_id: the other team. Inferred from `receive` if omitted.
        apply: post the offer. False previews.
    """
    if (gate := _approval_gate(apply)):
        return gate
    ev = analyze_trade(give, receive, partner_team_id)
    if "error" in ev:
        return ev
    ev["applied"] = False
    if ev.get("trade_deadline_passed"):
        ev["error"] = "The trade deadline has passed."
        return ev
    for side in ("me", "them"):
        if ev[side]["violations"]:
            ev["error"] = f"{side}: " + "; ".join(ev[side]["violations"])
            return ev
    if not apply:
        ev["note"] = "Preview only. Call again with apply=true to send the offer."
        return ev
    b = board()
    me = _require_team(b)
    week = b.week()
    body = b.client.trade_proposal_transaction(
        me, week, ev["partner"]["team_id"],
        [p["id"] for p in ev["give"]], [p["id"] for p in ev["receive"]])
    result = b.client.post_transaction(body)
    ev["applied"] = True
    ev["espn_status"] = result.get("status")
    ev["transaction_id"] = result.get("id")
    ev["note"] = "Offer sent. It waits on the other manager; get_pending_trades tracks it."
    return ev


PENDING_TRADE_STATUSES = {"PENDING", "PROPOSED"}


def open_trade_proposals(transactions: list[dict], me: int, now_ms: int) -> list[dict]:
    """Trade offers involving `me` that can still be answered.

    ESPN never updates the original proposal: it stays PENDING for good, and
    what became of it is a separate record pointing back through
    relatedTransactionId (a decline, an accept, a cancellation). So a
    proposal is open only if nothing refers to it and it has not run past
    its expiration date.
    """
    answered = {t.get("relatedTransactionId") for t in transactions
                if t.get("relatedTransactionId")}
    out = []
    for t in transactions:
        if t.get("type") != "TRADE_PROPOSAL" or t.get("status") not in PENDING_TRADE_STATUSES:
            continue
        if t.get("relatedTransactionId") or t.get("id") in answered:
            continue
        if t.get("expirationDate") and int(t["expirationDate"]) <= now_ms:
            continue
        teams = {int(i.get("fromTeamId") or 0) for i in t.get("items") or []}
        teams |= {int(i.get("toTeamId") or 0) for i in t.get("items") or []}
        if me in teams:
            out.append(t)
    return out


def _pending_proposals(b: DraftBoard, me: int) -> list[dict]:
    import time
    # Wall clock, not _now_ms: that one is the lineup-lock clock.
    return open_trade_proposals(b.client.transactions(), me, int(time.time() * 1000))


def _describe_proposal(b: DraftBoard, t: dict, me: int, week: int) -> dict:
    shape = b.shape()
    teams = b.league_rosters(week)
    items = [i for i in t.get("items") or [] if i.get("type") == "TRADE"]
    partner = next((int(i["toTeamId"]) for i in items if int(i["fromTeamId"]) == me), None)
    if partner is None:
        partner = next((int(i["fromTeamId"]) for i in items if int(i["toTeamId"]) == me), None)
    mine = b.team_players(me, week)
    theirs = b.team_players(partner, week) if partner else []
    by_id = {p["player_id"]: p for p in mine + theirs}
    give_ids = [int(i["playerId"]) for i in items if int(i["fromTeamId"]) == me]
    recv_ids = [int(i["playerId"]) for i in items if int(i["toTeamId"]) == me]
    give = [by_id[i] for i in give_ids if i in by_id]
    receive = [by_id[i] for i in recv_ids if i in by_id]
    out: dict[str, Any] = {
        "trade_id": t.get("id"),
        "status": t.get("status"),
        "proposed_by": "me" if int(t.get("teamId") or 0) == me else "them",
        "partner": _team_brief(teams[partner]) if partner in teams else {"team_id": partner},
        "proposed": _local_time(t.get("proposedDate"), "%a %b %d %I:%M %p"),
        "give": [_slim_season(p) for p in give],
        "receive": [_slim_season(p) for p in receive],
    }
    if len(give) == len(give_ids) and len(receive) == len(recv_ids) and theirs:
        ev = evaluate_trade(mine, theirs, give, receive, shape)
        out["evaluation"] = {
            "my_starters_ros_per_game_change": ev["me"]["delta"]["starters_ros_per_game"],
            "their_starters_ros_per_game_change": ev["them"]["delta"]["starters_ros_per_game"],
            "my_this_week_change": ev["me"]["delta"]["starters_this_week"],
            "my_must_drop": ev["me"]["must_drop"],
            "my_violations": ev["me"]["violations"],
        }
        if (view := trade_view(give, receive)):
            out["evaluation"]["market"] = view
    else:
        out["note"] = "Some players in this offer are no longer on either roster."
    return out


@mcp.tool()
@handle_errors
def get_pending_trades() -> dict:
    """Trade offers waiting on you, and offers you sent that are waiting on them.

    Each with its id (for respond_to_trade), who proposed it, both sides
    from your point of view, and the same lineup evaluation analyze_trade
    gives.
    """
    ctx = _my_context()
    if isinstance(ctx, dict):
        return ctx
    b, shape, me, week, _ = ctx
    rows = [_describe_proposal(b, t, me, week) for t in _pending_proposals(b, me)]
    return {
        "week": week,
        "waiting_on_me": [r for r in rows if r["proposed_by"] == "them"],
        "waiting_on_them": [r for r in rows if r["proposed_by"] == "me"],
    }


@mcp.tool()
@handle_errors
def respond_to_trade(trade_id: str, action: str, apply: bool = False) -> dict:
    """Accept or decline an offer made to you, or withdraw one you sent.

    Args:
        trade_id: from get_pending_trades.
        action: "accept", "decline" or "withdraw" (your own offer).
        apply: submit to ESPN. False previews the offer and the action.
    """
    if (gate := _approval_gate(apply)):
        return gate
    ctx = _my_context()
    if isinstance(ctx, dict):
        return ctx
    b, shape, me, week, _ = ctx
    action = action.strip().lower()
    if action not in ("accept", "decline", "withdraw", "cancel"):
        return {"error": "action must be accept, decline or withdraw."}
    t = next((t for t in _pending_proposals(b, me) if str(t.get("id")) == str(trade_id)), None)
    if t is None:
        return {"error": f"No pending trade {trade_id!r} involving your team.",
                "hint": "get_pending_trades lists the current ones."}
    desc = _describe_proposal(b, t, me, week)
    own = desc["proposed_by"] == "me"
    if action == "accept" and own:
        return {"error": "This is your own offer; it waits on the other manager.", **desc}
    if action in ("withdraw", "cancel") and not own:
        return {"error": "This offer was made to you; decline it instead.", **desc}
    if action == "accept" and desc.get("evaluation", {}).get("my_violations"):
        return {"error": "Accepting would break your roster limits: "
                         + "; ".join(desc["evaluation"]["my_violations"]), **desc}
    out: dict[str, Any] = {"action": action, "applied": False, **desc}
    if not apply:
        out["note"] = f"Preview only. Call again with apply=true to {action}."
        return out
    result = b.client.post_transaction(
        b.client.trade_response_transaction(me, week, t, accept=(action == "accept")))
    b.invalidate_rosters(week)  # an accepted trade may apply now or after review
    out["applied"] = True
    out["espn_status"] = result.get("status")
    out["transaction_id"] = result.get("id")
    if action == "accept":
        out["note"] = ("Accepted. ESPN applies it now, or after the league's review "
                       "period if one is set.")
    return out


def is_locked_now(p: dict) -> bool:
    from .season import is_locked
    return is_locked(p, _now_ms())


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
        "trending_pickups": sorted((e for e in scored if e.get("adds_24h")),
                                   key=lambda e: -e["adds_24h"])[:min(limit, 8)],
        "streaming": _streaming(my, avail, _now_ms()),
        "workload_targets": sorted(
            (e for e in scored if (e.get("usage") or {}).get("games", 0) >= 2),
            key=lambda e: -e["usage"]["expected_ppg"])[:min(limit, 8)],
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
    # Bench upside: dead spots (a D/ST or K who cannot start) and weak bench
    # players, each paired with the best stash for it. These do not raise
    # today's lineup, which is why lineup_gain never finds them.
    repl = _stash_repl(b, shape, week)
    starters = {p["player_id"] for key in (ROS_KEY, WEEK_KEY)
                for _, p in optimal_lineup(my, shape, key)["starters"] if p}
    out["stash_moves"] = stash_moves(
        my, avail, starters, repl, set(mine_t.get("pending_add_ids") or []),
        set(mine_t.get("pending_drop_ids") or []), limit=3)
    out["dead_spots"] = [p["name"] for p in dead_spots(
        my, set(mine_t.get("pending_drop_ids") or []))]
    out["stash_note"] = (
        "stash_moves turn dead or weak roster spots into upside. stash_score is "
        "points a game above replacement at the position, from workload "
        "(expected_ppg), ESPN's rest-of-season projection and Sleeper pickup "
        "trends. dead_spots are D/ST and K beyond the one that starts: they can "
        "never score for you, so any stash above the floor is better. Queue "
        "these; players already claimed or dropped by a pending claim are left out.")
    if b.signals is None:
        del out["trending_pickups"]
        del out["workload_targets"]
        del out["streaming"]
    else:
        out["outside_note"] = (
            "streaming is defense and kicker by this week's matchup: my starter "
            "against the best unrostered, by adj_week_proj. A defense is moved by "
            "the points its opponent is expected to score (ESPN under-rates this: "
            "against offenses expected to score under 17, defenses scored 10.1 where "
            "ESPN said 7.2). A kicker is moved up indoors, where kickers scored a "
            "point more than ESPN projected. upgrade is what swapping is worth this "
            "week; it is one week's edge, so do not drop a useful player for it, and "
            "note hold_count: more than one defense or kicker on the roster is a "
            "bench spot a running back or receiver could have. "
            "workload_targets are unrostered players ranked by what their carries "
            "and targets say they should score (usage.expected_ppg), whatever they "
            "have scored. On the waiver wire over two seasons, the top players by "
            "workload went on to outscore the top players by points. A target with "
            "expected_ppg well above ppg has the role and not yet the results. "
            "trending_pickups are unrostered players being added across Sleeper "
            "leagues right now, most added first: news is moving there before it "
            "reaches this league's projections. Weigh them against lineup gain, "
            "not instead of it. " + _OUTSIDE_NOTE)
        out["data_sources"] = _sources(b)
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
        "market": trade_view(mine, theirs_in),
        "usage": _usage_view(mine, theirs_in),
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
    if out["usage"] is None:
        del out["usage"]
    if out["market"] is None:
        del out["market"]
    else:
        out["likely_accepted"] = acceptable(
            ev["them"]["delta"]["starters_ros_per_game"], out["market"])
        out["market_note"] = _OUTSIDE_NOTE
    worth = worth_offering(ev["me"]["delta"]["starters_ros_per_game"],
                           ev["me"]["delta"]["starters_this_week"],
                           out.get("market"), out.get("usage"))
    out["worth_offering"] = worth["ok"]
    if worth["reasons"]:
        out["not_worth_because"] = worth["reasons"]
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
        # The same search, judged the way the other manager will judge it:
        # my lineup by projection, his side by what the market says he got.
        # Anyone of mine can go back, not only players who would start for him.
        sellable = None
        for _, theirs_p in offers[:6]:
            for mine_p in my_roster:
                if not skill(mine_p) or mine_p.get("market_value") is None:
                    continue
                view = trade_view([mine_p], [theirs_p])
                if view is None or view["their_market_gain_pct"] < 0:
                    continue
                ev = evaluate_trade(my_roster, roster, [mine_p], [theirs_p], shape)
                if ev["me"]["violations"] or ev["them"]["violations"]:
                    continue
                mine_d = ev["me"]["delta"]["starters_ros_per_game"]
                their_d = ev["them"]["delta"]["starters_ros_per_game"]
                if mine_d <= 0 or not acceptable(their_d, view):
                    continue
                # Accepted is not enough: it has to be a trade I would make.
                if not worth_offering(mine_d, ev["me"]["delta"]["starters_this_week"],
                                      view, _usage_view([mine_p], [theirs_p]))["ok"]:
                    continue
                if sellable is None or mine_d > sellable["my_gain"]:
                    sellable = {"give": mine_p["name"], "receive": theirs_p["name"],
                                "my_gain": mine_d, "their_gain": their_d,
                                "their_market_gain": view["their_market_gain"],
                                "their_market_gain_pct": view["their_market_gain_pct"]}
        t = teams[tid]
        partners.append({
            **_team_brief(t),
            "best_1_for_1": best,
            "best_by_market": sellable,
            "mutual": bool(best and best["my_gain"] > 0 and best["their_gain"] > 0),
            "their_needs": their_needs,
            "they_could_send": [{"my_lineup_gain": round(g, 2), **_slim_season(p)}
                                for g, p in offers[:per_team]],
            "i_could_send": [{"their_lineup_gain": round(g, 2), **_slim_season(p)}
                             for g, p in asks[:per_team]],
            "trade_block": [p["name"] for p in roster if p.get("on_trade_block")],
        })
    partners.sort(key=lambda t: (-bool(t["best_by_market"]), -t["mutual"],
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
    if b.signals is not None:
        hot = lambda p: (p.get("usage") or {}).get("view") == "running hot"  # noqa: E731
        cold = lambda p: (p.get("usage") or {}).get("view") == "running cold"  # noqa: E731
        out["sell_high"] = [_slim_season(p) for p in sorted(
            (p for p in my_roster if hot(p)), key=lambda p: -p["usage"]["gap"])]
        out["buy_low"] = [
            {"owner": teams[tid]["name"], "team_id": tid,
             "my_lineup_gain": round(waiver_gain(my_roster, p, shape)
                                     ["lineup_gain_ros_per_game"], 2),
             **_slim_season(p)}
            for tid, p in sorted(((tid, p) for tid, roster in rosters.items() if tid != me
                                  for p in roster if cold(p)),
                                 key=lambda t: t[1]["usage"]["gap"])[:8]]
        out["workload_note"] = (
            "sell_high are my players scoring 3+ points a game above their workload; "
            "buy_low are other teams' players scoring 3+ below theirs. Hot players "
            "fell 2 to 4 points a game afterwards and cold ones rose about 2, in both "
            "seasons tested. The other manager sees the points, not the workload: "
            "offer a sell_high player for a buy_low one at similar points so far. "
            "my_lineup_gain is by ESPN's projection, which may not see the rebound.")
        out["sell_candidates"] = [
            _slim_season(p) for p in sorted(
                (p for p in my_roster if market_view(p) == "sell"),
                key=lambda p: -p["market_gap"])]
        out["market_note"] = (
            "best_by_market is the 1-for-1 that raises my lineup the most among "
            "those the other manager should accept: he comes out ahead on market "
            "value and his lineup is not clearly worse. Offer these first. "
            "sell_candidates are my players the market rates above their "
            "projection. " + _OUTSIDE_NOTE)
        out["data_sources"] = _sources(b)
    if (n := _week_note(shape)):
        out["season_note"] = n
    return out


# --------------------------------------------------------------------------
# Approval queue
# --------------------------------------------------------------------------

_store: ProposalStore | None = None


def proposal_store() -> ProposalStore:
    global _store
    if _store is None:
        cfg = board().cfg
        _store = ProposalStore(
            cfg.state_root / f"proposals-{cfg.league_id}-{cfg.season}.db")
    return _store


def _writers() -> dict:
    return {"add_player": add_player, "drop_player": drop_player,
            "propose_trade": propose_trade, "respond_to_trade": respond_to_trade,
            "start_player": start_player}


def _names(players: list[dict]) -> str:
    return ", ".join(f"{p['name']} ({p['pos']})" for p in players) or "nobody"


def _signed(value) -> str:
    return f"{value:+.2f}" if isinstance(value, (int, float)) else "n/a"


def _describe_move(action: str, params: dict, preview: dict) -> tuple[str, str]:
    """Title and one-paragraph summary of a previewed move, from its numbers."""
    if action == "add_player":
        d = preview["delta"]
        kind = "Waiver claim" if preview["transaction"] == "waiver claim" else "Add"
        body = f"{kind}: {_names([preview['add']])}"
        if preview["drop"]:
            body += f", dropping {_names(preview['drop'])}"
        body += (f". Starters {_signed(d['starters_ros_per_game'])} pts/game rest of "
                 f"season, {_signed(d['starters_this_week'])} this week.")
        if preview.get("waivers_clear"):
            body += f" Waivers clear {preview['waivers_clear']}."
        return f"Dodi: add {preview['add']['name']}?", body
    if action == "drop_player":
        d = preview["delta"]
        return (f"Dodi: drop {preview['drop']['name']}?",
                f"Drop {_names([preview['drop']])}. Starters "
                f"{_signed(d['starters_ros_per_game'])} pts/game rest of season.")
    if action == "propose_trade":
        body = (f"Give {_names(preview['give'])} for {_names(preview['receive'])}. "
                f"My starters {_signed(preview['me']['delta']['starters_ros_per_game'])} "
                f"pts/game rest of season, theirs "
                f"{_signed(preview['them']['delta']['starters_ros_per_game'])}.")
        if (m := preview.get("market")):
            body += (f" Market value: I give {m['value_given']}, get "
                     f"{m['value_received']} ({m['verdict'].split(':')[0]}).")
        return f"Dodi: offer trade to {preview['partner']['name']}?", body
    if action == "start_player":
        up, down = preview["start"], preview["sit"]
        body = (f"Start {up['name']} at {preview['slot']} over {down['name']}. This "
                f"week's projection: {up.get('week_proj')} vs {down.get('week_proj')} "
                f"({_signed(preview['week_proj_change'])}). Rest of season per game: "
                f"{up.get('ros_pg')} vs {down.get('ros_pg')}.")
        mu, md = up.get("market"), down.get("market")
        if mu or md:
            body += (f" Trade value: {(mu or {}).get('value', 'unpriced')} vs "
                     f"{(md or {}).get('value', 'unpriced')}.")
        return f"Dodi: start {up['name']} over {down['name']}?", body
    verb = preview["action"].capitalize()
    body = (f"{verb} trade with {preview['partner'].get('name', 'team')}: give "
            f"{_names(preview['give'])} for {_names(preview['receive'])}.")
    ev = preview.get("evaluation")
    if ev:
        body += (f" My starters {_signed(ev['my_starters_ros_per_game_change'])} "
                 "pts/game rest of season.")
    return f"Dodi: {preview['action']} trade?", body


def _by_id(action: str, params: dict, preview: dict) -> dict:
    """The same move with every player named by id, taken from its preview.

    What the manager approves is the summary built from this preview. Storing
    ids means the move replayed on approval is that move and no other, even
    if a name that was unique when it was queued no longer is.
    """
    if action == "add_player":
        out = {"add": player_ref(preview["add"]["id"])}
        if preview["drop"]:
            out["drop"] = player_ref(preview["drop"][0]["id"])
        return out
    if action == "drop_player":
        return {"player": player_ref(preview["drop"]["id"])}
    if action == "propose_trade":
        return {"give": [player_ref(p["id"]) for p in preview["give"]],
                "receive": [player_ref(p["id"]) for p in preview["receive"]],
                "partner_team_id": int(preview["partner"]["team_id"])}
    if action == "start_player":
        return {"player": player_ref(preview["start"]["id"]),
                "over": player_ref(preview["sit"]["id"])}
    return params     # respond_to_trade names a trade, by its own id


def _expiry(action: str, preview: dict) -> float | None:
    """When the move stops being possible, if sooner than the default."""
    if action == "add_player" and preview.get("waivers_clear_ms"):
        # A claim has to be in before the waiver run.
        return preview["waivers_clear_ms"] / 1000 - 600
    if action == "start_player" and preview.get("kickoff_ms"):
        return preview["kickoff_ms"] / 1000 - 300
    if action == "add_player" and preview.get("add_kickoff_ms"):
        import time
        by_kickoff = preview["add_kickoff_ms"] / 1000 - 300
        # Only when his game is the nearer deadline; never shorten past "now".
        if time.time() < by_kickoff < time.time() + 24 * 3600:
            return by_kickoff
    return None


def queue_close_calls(limit: int = 2, calls: list[dict] | None = None) -> list[dict]:
    """Put this week's close calls to the manager. Returns what was queued.

    `calls` are the close_calls of a set_lineup result already in hand.
    """
    if calls is None:
        calls = set_lineup(apply=False).get("close_calls") or []
    queued = []
    for c in calls[:limit]:
        r = request_approval("start_player", {"player": c["start"], "over": c["over"]},
                             "Too close for the projection to call, and he is the "
                             "better player: " + "; ".join(c["reasons"]) + ". By "
                             "history calls this close are a coin flip; it is your "
                             "preference.")
        if r.get("queued"):
            queued.append(r["proposal"])
    return queued


def _auto_apply(proposal: dict, why: str) -> dict:
    """Make a move the policy allows without asking, and tell the manager.

    Goes through the same record as an approved proposal (approved, then
    applied or failed), so get_proposals and the weekly review see it. The
    push after it says what was done and why; a failure says so loudly.
    """
    from .approve import apply_proposal
    store = proposal_store()
    store.decide(proposal["id"], "approved")
    done = apply_proposal(store.get(proposal["id"]) or proposal, auto=True, why=why)
    out = {"queued": False, "auto_applied": done["status"] == "applied",
           "status": done["status"], "proposal": public(done), "policy": why}
    if done["status"] != "applied":
        out["error"] = (done.get("result") or {}).get("error") or "ESPN did not apply it."
    return out


@mcp.tool()
@handle_errors
def request_approval(action: str, params: dict, reasoning: str) -> dict:
    """Queue a roster move or trade for the manager to approve.

    The move is previewed first; if the preview fails, nothing is queued and
    the reason comes back. Otherwise it is stored and pushed to the manager's
    phone with approve and reject buttons, and sent to ESPN only on approve.
    The same move is not queued twice, and one the manager rejected is not
    offered again for three days.

    Args:
        action: add_player, drop_player, propose_trade, respond_to_trade, or
            start_player (a close start/sit call from set_lineup).
        params: that tool's arguments, without `apply`. E.g.
            {"add": "Player A", "drop": "Player B"},
            {"player": "Benched Star", "over": "Current Starter"},
            {"give": ["A"], "receive": ["B"], "partner_team_id": 6},
            {"trade_id": "...", "action": "accept"}.
        reasoning: why this move, in two or three sentences. The manager
            reads this on his phone when deciding. Every number in it must
            be one from the preview of the move (call the tool with
            apply=false first and copy from its result); a number that is
            not is refused. Do not compute new numbers. Names as the tools
            spell them.
    """
    try:
        params = clean_params(action, params or {})
    except ProposalError as exc:
        return {"queued": False, "error": str(exc)}
    store = proposal_store()
    preview = _writers()[action](**params, apply=False)
    if "error" in preview:
        return {"queued": False, "error": preview["error"], "preview": preview}
    unsupported = unsupported_numbers(reasoning or "", preview)
    if unsupported:
        return {
            "queued": False,
            "error": ("The reasoning quotes numbers that are not in the preview of this "
                      "move: " + ", ".join(unsupported) + ". The manager decides on what "
                      "you write, so every number must be copied from the preview. "
                      "Correct them or leave them out, and call request_approval again."),
            "unsupported_numbers": unsupported,
            "preview": preview,
        }
    params = _by_id(action, params, preview)
    blocking = store.find_blocking(action, params)
    if blocking:
        return {"queued": False, "already": blocking["status"],
                "proposal": public(blocking),
                "note": ("Rejected recently; do not ask again." if blocking["status"] == "rejected"
                         else "Already waiting on the manager.")}
    title, summary = _describe_move(action, params, preview)
    auto, why = (auto_ok(action, params, preview) if board().cfg.auto_apply
                 else (False, ""))
    if board().cfg.auto_apply and not auto:
        # He is being asked; tell him why this one was not made for him.
        summary = f"{summary} Asked because: {why}"
    try:
        proposal = store.create(action, params, title=title, summary=summary,
                                reasoning=(reasoning or "").strip(),
                                expires_at=_expiry(action, preview))
    except ProposalError as exc:
        return {"queued": False, "error": str(exc)}
    if auto:
        return _auto_apply(proposal, why)
    sent = push_proposal(board().cfg, proposal)
    by = deadline(board().cfg, proposal)
    out = {"queued": True, "proposal": public(proposal), "notified": sent["sent"],
           "decide_by": by["text"]}
    if by["short_notice"]:
        out["short_notice"] = (
            f"Only {by['minutes']} minutes to decide; the manager asked for "
            f"{board().cfg.approval_lead_minutes}. Sent as urgent. Queue earlier next time.")
    if not sent["sent"]:
        out["note"] = f"Queued, but the notification failed: {sent['reason']}"
    return out


@mcp.tool()
@handle_errors
def get_proposals(status: str | None = None, limit: int = 10) -> dict:
    """Moves queued for approval, newest first, and what became of them.

    Args:
        status: pending, approved, applied, failed, rejected or expired.
        limit: how many to return.
    """
    rows = proposal_store().list(status, limit)
    return {"count": len(rows), "proposals": [public(p) for p in rows]}


@mcp.tool()
def check_report(text: str) -> dict:
    """Check a report before it is delivered: is every number in it one that
    a tool actually returned?

    Call this with the full text of any report for the manager, and deliver
    the report only when `ok` is true. A number listed as unsupported was
    not returned by any tool in the last 45 minutes: it was misremembered,
    belongs to something else, or was computed by you. Look it up again with
    the tool it should have come from, or take it out.

    Numbers are all that can be checked this way. For names and statuses:
    spell players and teams as the tools do, and when a tool returned an
    error, give its own words, not a summary of them.

    Args:
        text: the report, as it will be delivered.
    """
    missing = unsupported_numbers(text or "", _seen_lately())
    if not missing:
        return {"ok": True, "note": "Every number in the report was returned by a tool."}
    return {"ok": False, "unsupported_numbers": missing,
            "instruction": ("These were not returned by any tool in this run. For each: "
                            "call the tool again and copy the value, or remove it. Do not "
                            "compute sums, differences or percentages yourself. Then call "
                            "check_report again with the corrected text.")}


def execute_proposal(proposal: dict) -> dict:
    """Replay an approved proposal against ESPN. Used by the approval service."""
    token = _approved_call.set(True)
    try:
        return _writers()[proposal["action"]](**proposal["params"], apply=True)
    finally:
        _approved_call.reset(token)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
