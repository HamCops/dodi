"""Bench upside: who is worth a roster spot that is doing nothing.

The lineup-gain view (season.waiver_gain) only values a free agent who would
start. That leaves dead spots dead: a third defense is worth nothing, but no
free agent "beats" him on lineup gain either, because neither starts. So the
roster sat on three defenses.

This module values the spot, not the lineup. A spot is dead when its player
can never start for me: a D/ST or K beyond the one that starts. Any spot can
also hold a stash, a player the lineup does not need today but might soon:

  workload    usage.expected_ppg: what his carries and targets say he should
              score. On two seasons of waiver data the top players by workload
              outscored the top players by points. The best stash signal.
  trending    adds_24h across Sleeper leagues: news the projections have not
              caught up with (a starter hurt, a depth chart change).
  projection  ros_per_game, ESPN's rest-of-season points a game.

stash_score is points a game ABOVE REPLACEMENT at his position (what the waiver
wire hands anyone for free), so positions compare fairly. Raw points would rank
every waiver QB first: a 16-point QB is worth nothing when 16 is what any free
QB scores. It is a ranking, not a forecast.
"""

from __future__ import annotations

from .season import ROS_KEY, WEEK_KEY

SKILL = ("QB", "RB", "WR", "TE")
STREAM = ("D/ST", "K")

# A trending player gets up to this many points a game added to his score,
# reached at TREND_FULL adds in a day across Sleeper leagues.
TREND_BONUS = 2.0
TREND_FULL = 500_000
# A stash must beat the player he replaces by this much to be worth a move.
STASH_MARGIN = 1.0
# Nobody below this (points a game over replacement) is worth claiming at all.
STASH_FLOOR = 0.5
# Most of a position worth holding. A stash may not push a position past its
# cap unless it replaces a man at the same position: a fourth TE is not upside,
# it is a wasted spot, however hard he is trending.
POSITION_CAP = {"QB": 2, "TE": 2}

Repl = dict  # position -> replacement points a game


def replacement_per_game(replacement_points: dict[str, float], games_left: float) -> Repl:
    """The board's replacement level, rest-of-season totals, as points a game."""
    g = max(float(games_left), 1.0)
    return {pos: pts / g for pos, pts in replacement_points.items()}


def stash_score(p: dict, repl: Repl) -> float:
    """Upside of holding a player over what the wire gives free. Skill only."""
    pos = p.get("position")
    if pos not in SKILL:
        return 0.0
    ros = float(p.get(ROS_KEY) or 0.0)
    usage = p.get("usage") or {}
    expected = usage.get("expected_ppg")
    if expected is not None and (usage.get("games") or 0) >= 2:
        # Workload counts as much as the projection; it is the better signal.
        base = (ros + float(expected)) / 2
    else:
        base = ros
    adds = float(p.get("adds_24h") or 0.0)
    return round(base - repl.get(pos, 0.0) + TREND_BONUS * min(adds / TREND_FULL, 1.0), 2)


def _score(p: dict) -> float:
    s = p.get("adj_week_proj")
    return float(s if s is not None else (p.get(WEEK_KEY) or 0.0))


def dead_spots(roster: list[dict], pending_drop_ids=frozenset()) -> list[dict]:
    """Players holding a spot with no way into the lineup, most useless first.

    A D/ST or K beyond the best one at the position (by this week's matchup).
    Players already being dropped by a pending claim are left out: their spot
    is spoken for.
    """
    out = []
    for pos in STREAM:
        held = sorted((p for p in roster if p["position"] == pos
                       and p["player_id"] not in pending_drop_ids
                       and p.get("slot_id") != 21), key=_score, reverse=True)
        out.extend(held[1:])
    return sorted(out, key=_score)


def weakest_stash(roster: list[dict], starters: set, repl: Repl,
                  pending_drop_ids=frozenset()) -> list[dict]:
    """Bench skill players by stash_score, lowest first: the next to go.

    Never a starter, never the last QB, never a man a pending claim drops.
    """
    qbs = [p for p in roster if p["position"] == "QB" and p["player_id"] not in pending_drop_ids]
    out = [p for p in roster if p["position"] in SKILL
           and p["player_id"] not in starters
           and p["player_id"] not in pending_drop_ids
           and p.get("slot_id") != 21
           and not (p["position"] == "QB" and len(qbs) <= 1)]
    return sorted(out, key=lambda p: stash_score(p, repl))


def stash_moves(roster: list[dict], available: list[dict], starters: set, repl: Repl,
                pending_add_ids=frozenset(), pending_drop_ids=frozenset(),
                limit: int = 3) -> list[dict]:
    """Add/drop pairs that turn a dead or weak spot into upside.

    Dead spots first (anyone above the floor beats a defense that cannot
    start), then the weakest bench stash, but only when the new man beats him
    by STASH_MARGIN. Each move drops a different player and adds a different
    one. No position goes past POSITION_CAP except like for like.
    """
    held: dict[str, int] = {}
    for p in roster:
        if p["player_id"] not in pending_drop_ids and p.get("slot_id") != 21:
            held[p["position"]] = held.get(p["position"], 0) + 1

    def fits(pick: dict, drop: dict) -> bool:
        pos = pick["position"]
        if pos == drop["position"] or pos not in POSITION_CAP:
            return True
        return held.get(pos, 0) + 1 <= POSITION_CAP[pos]

    candidates = sorted((p for p in available if p.get("position") in SKILL
                         and p["player_id"] not in pending_add_ids
                         and stash_score(p, repl) >= STASH_FLOOR),
                        key=lambda p: stash_score(p, repl), reverse=True)
    moves: list[dict] = []
    used_add: set = set()
    used_drop: set = set()
    dead = dead_spots(roster, pending_drop_ids)
    dead_ids = {p["player_id"] for p in dead}
    weak = weakest_stash(roster, starters, repl, pending_drop_ids)
    for drop in dead + weak:
        if len(moves) >= limit:
            break
        if drop["player_id"] in used_drop:
            continue
        is_dead = drop["player_id"] in dead_ids
        drop_score = 0.0 if is_dead else stash_score(drop, repl)
        floor = STASH_FLOOR if is_dead else drop_score + STASH_MARGIN
        pick = next((c for c in candidates if c["player_id"] not in used_add
                     and stash_score(c, repl) >= floor and fits(c, drop)), None)
        if pick is None:
            continue
        used_add.add(pick["player_id"])
        used_drop.add(drop["player_id"])
        held[pick["position"]] = held.get(pick["position"], 0) + 1
        held[drop["position"]] = held.get(drop["position"], 0) - 1
        moves.append({
            "add": pick["name"], "add_id": pick["player_id"], "add_pos": pick["position"],
            "add_stash_score": stash_score(pick, repl),
            "drop": drop["name"], "drop_id": drop["player_id"], "drop_pos": drop["position"],
            "drop_stash_score": drop_score,
            "why": ("dead spot: a D/ST or K who cannot start" if is_dead
                    else "more upside than the weakest bench player"),
            "status": pick.get("roster_status"),
        })
    return moves
