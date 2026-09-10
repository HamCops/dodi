"""In-season value math: rest-of-season points, optimal lineups, and the
before/after arithmetic behind start/sit, waiver and trade decisions.

Pure functions over plain dicts, like value.py, so it is all unit-testable
offline. The design rule carries over from the draft tools: the server does
the deterministic arithmetic (what does my lineup project to with and without
this player?) and leaves the judgement (is that worth a waiver claim?) to the
model reasoning over the numbers.

Two projection horizons matter in season and they answer different questions:

- `week_proj`: ESPN's projection for one week, which already bakes in the NFL
  opponent, injury designation and bye. This is what wins *this* matchup.
- `ros_per_game`: rest-of-season points per remaining game. This is what a
  roster spot is worth, and the basis for trade and waiver value.
"""

from __future__ import annotations

from .constants import (
    DEDICATED_SLOT_POSITION,
    FLEX_SLOT_ELIGIBILITY,
    NON_STARTING_SLOTS,
    SLOT_BY_ID,
)
from .scoring import LeagueShape

ROS_KEY = "ros_per_game"
WEEK_KEY = "week_proj"


# --------------------------------------------------------------------------
# Rest-of-season
# --------------------------------------------------------------------------


def attach_ros(players: list[dict], current_week: int, final_week: int,
               week: int | None = None) -> None:
    """Tag every player with rest-of-season fields, in place.

    ESPN publishes a full-season projection and season-to-date actuals, not a
    rest-of-season number, so ROS is the difference. Per-game divides that by
    the games the player has left, skipping a bye that is still ahead -- a
    player on a week-9 bye has one fewer game to score in than one who already
    had theirs, and that is what a lineup slot is buying.
    """
    week = week or current_week
    for p in players:
        season = float(p.get("projected_points") or 0.0)
        actual = float(p.get("season_points") or 0.0)
        ros = max(season - actual, 0.0)
        bye = p.get("bye_week")
        games = sum(1 for w in range(current_week, final_week + 1) if w != bye)
        p["ros_points"] = round(ros, 2)
        p["games_remaining"] = games
        p[ROS_KEY] = round(ros / games, 2) if games else 0.0
        weekly = p.get("week_projections") or {}
        if bye == week:
            p[WEEK_KEY] = 0.0
            p["on_bye"] = True
        else:
            p[WEEK_KEY] = weekly.get(week)
            p["on_bye"] = False


# --------------------------------------------------------------------------
# Lineups
# --------------------------------------------------------------------------


def starting_slots(shape: LeagueShape) -> list[tuple[str, tuple[str, ...]]]:
    """Starting slots in fill order: dedicated first, then flex, tightest first.

    Filling dedicated slots with the best players at each position and then
    flex from whoever is left is optimal whenever every flex slot's eligible
    set contains the dedicated positions it draws from (the normal case).
    """
    dedicated: list[tuple[str, tuple[str, ...]]] = []
    flex: list[tuple[str, tuple[str, ...]]] = []
    for slot_id, count in sorted(shape.lineup_slots.items()):
        if not count or slot_id in NON_STARTING_SLOTS:
            continue
        name = SLOT_BY_ID.get(slot_id, str(slot_id))
        pos = DEDICATED_SLOT_POSITION.get(slot_id)
        if pos:
            dedicated.extend([(name, (pos,))] * count)
            continue
        eligible = FLEX_SLOT_ELIGIBILITY.get(slot_id)
        if eligible:
            flex.extend([(name, eligible)] * count)
    flex.sort(key=lambda s: len(s[1]))
    return dedicated + flex


def _val(p: dict | None, key: str) -> float:
    if p is None:
        return 0.0
    v = p.get(key)
    return float(v) if v is not None else 0.0


def optimal_lineup(players: list[dict], shape: LeagueShape, key: str) -> dict:
    """Best starting lineup by `key`, greedy over slots.

    Returns starters as (slot, player) with None for a slot nobody can fill,
    the bench, and the projected total.
    """
    pool = sorted(players, key=lambda p: -_val(p, key))
    starters: list[tuple[str, dict | None]] = []
    for slot, eligible in starting_slots(shape):
        pick = next((p for p in pool if p.get("position") in eligible), None)
        if pick is not None:
            pool.remove(pick)
        starters.append((slot, pick))
    total = round(sum(_val(p, key) for _, p in starters), 2)
    return {"starters": starters, "bench": pool, "total": total}


def lineup_total(players: list[dict], shape: LeagueShape, key: str) -> float:
    return optimal_lineup(players, shape, key)["total"]


def current_starters(players: list[dict]) -> list[dict]:
    """Players ESPN has in a starting slot right now (needs `slot_id`)."""
    return [p for p in players
            if p.get("slot_id") is not None and p["slot_id"] not in NON_STARTING_SLOTS]


def lineup_changes(players: list[dict], shape: LeagueShape, key: str) -> dict:
    """What moving from the set lineup to the optimal one is worth."""
    now = current_starters(players)
    now_ids = {p["player_id"] for p in now}
    best = optimal_lineup(players, shape, key)
    best_ids = {p["player_id"] for _, p in best["starters"] if p}
    start = [p for _, p in best["starters"] if p and p["player_id"] not in now_ids]
    sit = [p for p in now if p["player_id"] not in best_ids]
    now_total = round(sum(_val(p, key) for p in now), 2)
    return {
        "current_total": now_total,
        "optimal_total": best["total"],
        "gain": round(best["total"] - now_total, 2),
        "start": start,
        "sit": sit,
        "optimal": best,
    }


# --------------------------------------------------------------------------
# Roster legality
# --------------------------------------------------------------------------


def position_counts(players: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in players:
        out[p["position"]] = out.get(p["position"], 0) + 1
    return dict(sorted(out.items()))


def roster_violations(players: list[dict], shape: LeagueShape) -> list[str]:
    out = []
    over = len(players) - shape.roster_size
    if over > 0:
        out.append(f"{over} over the roster limit of {shape.roster_size} -- must drop {over}")
    for pos, n in position_counts(players).items():
        cap = shape.position_limits.get(pos)
        if cap is not None and n > cap:
            out.append(f"{n} {pos} exceeds the league limit of {cap}")
    return out


# --------------------------------------------------------------------------
# Roster strength
# --------------------------------------------------------------------------


def roster_profile(players: list[dict], shape: LeagueShape) -> dict:
    """Starter strength by position, plus who on the bench would start elsewhere.

    Starter strength is the average `ros_per_game` of the players the optimal
    lineup starts at each position (flex starters count toward their own
    position). It is comparable across teams, which is what a trade-partner
    search needs.
    """
    best = optimal_lineup(players, shape, ROS_KEY)
    by_pos: dict[str, list[dict]] = {}
    for _, p in best["starters"]:
        if p:
            by_pos.setdefault(p["position"], []).append(p)
    bench_by_pos: dict[str, list[dict]] = {}
    for p in best["bench"]:
        bench_by_pos.setdefault(p["position"], []).append(p)

    positions: dict[str, dict] = {}
    for pos in sorted(set(by_pos) | set(bench_by_pos)):
        starters = by_pos.get(pos, [])
        bench = sorted(bench_by_pos.get(pos, []), key=lambda p: -_val(p, ROS_KEY))
        positions[pos] = {
            "starters": starters,
            "starter_avg": round(
                sum(_val(p, ROS_KEY) for p in starters) / len(starters), 2
            ) if starters else None,
            "weakest_starter": min(starters, key=lambda p: _val(p, ROS_KEY)) if starters else None,
            "bench": bench,
        }
    return {
        "starters_ros_per_game": best["total"],
        "starters_this_week": lineup_total(players, shape, WEEK_KEY),
        "positions": positions,
        "bench_ros_vorp": round(
            sum(max(_val(p, "vorp"), 0.0) for p in best["bench"]), 2
        ),
    }


def league_position_averages(profiles: dict[int, dict]) -> dict[str, float]:
    """League-wide mean starter strength per position, over every team."""
    sums: dict[str, list[float]] = {}
    for prof in profiles.values():
        for pos, block in prof["positions"].items():
            if block["starter_avg"] is not None:
                sums.setdefault(pos, []).append(block["starter_avg"])
    return {pos: round(sum(v) / len(v), 2) for pos, v in sums.items() if v}


# --------------------------------------------------------------------------
# Deltas: what a move is worth
# --------------------------------------------------------------------------


def _side_summary(players: list[dict], shape: LeagueShape) -> dict:
    prof = roster_profile(players, shape)
    return {
        "starters_ros_per_game": prof["starters_ros_per_game"],
        "starters_this_week": prof["starters_this_week"],
        "bench_ros_vorp": prof["bench_ros_vorp"],
        "roster_size": len(players),
        "position_counts": position_counts(players),
    }


def _delta(before: dict, after: dict) -> dict:
    return {
        k: round(after[k] - before[k], 2)
        for k in ("starters_ros_per_game", "starters_this_week", "bench_ros_vorp")
    }


def evaluate_swap(roster: list[dict], out_players: list[dict], in_players: list[dict],
                  shape: LeagueShape) -> dict:
    """Before/after for one roster: what leaves, what arrives, what it does."""
    out_ids = {p["player_id"] for p in out_players}
    after = [p for p in roster if p["player_id"] not in out_ids] + list(in_players)
    before_s = _side_summary(roster, shape)
    after_s = _side_summary(after, shape)
    violations = roster_violations(after, shape)
    return {
        "before": before_s,
        "after": after_s,
        "delta": _delta(before_s, after_s),
        "roster_after": after,
        "must_drop": max(len(after) - shape.roster_size, 0),
        "violations": violations,
    }


def evaluate_trade(my_roster: list[dict], their_roster: list[dict],
                   give: list[dict], receive: list[dict], shape: LeagueShape) -> dict:
    mine = evaluate_swap(my_roster, give, receive, shape)
    theirs = evaluate_swap(their_roster, receive, give, shape)
    return {"me": mine, "them": theirs}


def waiver_gain(roster: list[dict], candidate: dict, shape: LeagueShape) -> dict:
    """What adding one free agent does to the lineup, before any drop.

    Lineup gains ignore the roster limit on purpose: the drop is chosen
    afterwards from players who do not start, so it never changes the lineup
    totals unless the roster is so thin that a starter has to go.
    """
    with_c = roster + [candidate]
    base_ros = lineup_total(roster, shape, ROS_KEY)
    base_week = lineup_total(roster, shape, WEEK_KEY)
    best = optimal_lineup(with_c, shape, ROS_KEY)
    starts = any(p is candidate for _, p in best["starters"])
    return {
        "lineup_gain_ros_per_game": round(best["total"] - base_ros, 2),
        "lineup_gain_this_week": round(
            lineup_total(with_c, shape, WEEK_KEY) - base_week, 2
        ),
        "would_start_ros": starts,
    }


def drop_candidates(roster: list[dict], shape: LeagueShape, limit: int = 4,
                    protect_position: str | None = None) -> list[dict]:
    """Rostered players that neither lineup horizon starts, least valuable first.

    Kickers and defenses only appear when the incoming player is one too
    (`protect_position`), since dropping your only kicker for a fourth WR
    leaves a starting slot empty.
    """
    ros_ids = {p["player_id"] for _, p in optimal_lineup(roster, shape, ROS_KEY)["starters"] if p}
    week_ids = {p["player_id"] for _, p in optimal_lineup(roster, shape, WEEK_KEY)["starters"] if p}
    bench = [p for p in roster if p["player_id"] not in ros_ids | week_ids]
    late = {"K", "D/ST"}
    bench = [p for p in bench if p["position"] not in late or p["position"] == protect_position]
    bench.sort(key=lambda p: (_val(p, "vorp"), _val(p, ROS_KEY)))
    return bench[:limit]


# --------------------------------------------------------------------------
# Transaction log
# --------------------------------------------------------------------------

# ESPN transaction types, grouped into what a manager would call them.
TRANSACTION_KIND = {
    "ROSTER": "lineup",
    "FUTURE_ROSTER": "lineup",
    "FREEAGENT": "add_drop",
    "WAIVER": "waiver",
    "TRADE_PROPOSAL": "trade",
    "TRADE_ACCEPT": "trade",
    "TRADE_DECLINE": "trade",
    "TRADE_VETO": "trade",
    "TRADE_UPHOLD": "trade",
    "DRAFT": "draft",
}


def _slot_label(slot_id) -> str | None:
    if slot_id is None or int(slot_id) < 0:
        return None
    return SLOT_BY_ID.get(int(slot_id), str(slot_id))


def describe_transaction(t: dict, name_of, team_of) -> dict:
    """One ESPN transaction as a readable record.

    `name_of(player_id)` and `team_of(team_id)` resolve ids to names; either
    may return None, in which case the id is shown instead.
    """
    kind = TRANSACTION_KIND.get(t.get("type"), "other")
    items, parts = [], []
    for i in t.get("items") or []:
        pid = int(i.get("playerId") or 0)
        name = name_of(pid) or f"player {pid}"
        action = i.get("type")
        row: dict = {"action": action, "player_id": pid, "player": name}
        if action == "LINEUP":
            row["from"] = _slot_label(i.get("fromLineupSlotId"))
            row["to"] = _slot_label(i.get("toLineupSlotId"))
            parts.append(f"{name} {row['from']} -> {row['to']}")
        elif action in ("ADD", "DRAFT"):
            row["to"] = _slot_label(i.get("toLineupSlotId"))
            parts.append(f"+{name}")
        elif action == "DROP":
            row["from"] = _slot_label(i.get("fromLineupSlotId"))
            parts.append(f"-{name}")
        elif action == "TRADE":
            src, dst = i.get("fromTeamId"), i.get("toTeamId")
            row["from_team"] = team_of(src) or src
            row["to_team"] = team_of(dst) or dst
            parts.append(f"{name}: {row['from_team']} -> {row['to_team']}")
        else:
            parts.append(f"{action} {name}")
        items.append(row)
    tid = t.get("teamId")
    out = {
        "id": t.get("id"),
        "kind": kind,
        "type": t.get("type"),
        "status": t.get("status"),
        "team_id": tid,
        "team": team_of(tid) or tid,
        "week": t.get("scoringPeriodId"),
        "date_ms": t.get("proposedDate") or t.get("processDate"),
        "summary": "; ".join(parts),
        "items": items,
    }
    if kind == "waiver" and t.get("bidAmount"):
        out["bid"] = t["bidAmount"]
    return out
