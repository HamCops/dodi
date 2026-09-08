"""MCP server exposing an ESPN fantasy football league for draft assistance.

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
from .config import Config, load_config
from .espn import ESPNError
from .scoring import LeagueShape

mcp = MCPServer(
    "espn-fantasy-draft",
    version=__version__,
    instructions=(
        "Tools for drafting in an ESPN fantasy football snake draft. Call "
        "get_league_settings once to learn the format, then get_draft_context "
        "whenever a pick decision is needed -- it bundles state, roster needs "
        "and best-available in one call. Rankings are by VORP (value over "
        "replacement), which already accounts for positional scarcity in this "
        "league's specific lineup; do not re-rank by raw projected points."
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
