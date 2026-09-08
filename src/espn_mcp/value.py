"""Draft value math: replacement level, VORP and tier breaks.

Kept as pure functions over plain dicts so it can be unit tested without any
network access. This is the part that must not be left to the model -- ranking
by raw projected points ignores positional scarcity, which is the entire basis
of a good draft board.
"""

from __future__ import annotations

from .scoring import LeagueShape

VALUE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")

# Kicker and defense VORP is real arithmetic but is not comparable to a skill
# position's. Kicker projections are tightly clustered and barely predictive
# year to year, so a "+26 VORP" kicker is not worth a mid-round pick the way a
# +26 VORP running back is. Left unhandled, the top kicker outranks real
# starters and a value-following drafter takes six of them. These positions
# keep their within-position VORP but are ordered below every skill player.
LATE_ROUND_POSITIONS = frozenset({"K", "D/ST"})


def replacement_ranks(players: list[dict], shape: LeagueShape) -> dict[str, int]:
    """How many players at each position are startable league-wide.

    Dedicated slots are simply teams x slots. Flex slots are allocated
    empirically: pool every flex-eligible player who is not already a dedicated
    starter, take the best N by projection, and count what positions they
    actually are. That adapts to the league's scoring instead of assuming a
    fixed RB/WR split.
    """
    teams = shape.teams
    ranks = {pos: teams * count for pos, count in shape.starters_by_position.items()}

    by_pos: dict[str, list[dict]] = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p)
    for pos_players in by_pos.values():
        pos_players.sort(key=lambda p: p["projected_points"], reverse=True)

    for eligible, slot_count in shape.flex_slots.items():
        remaining: list[dict] = []
        for pos in eligible:
            already = ranks.get(pos, 0)
            remaining.extend(by_pos.get(pos, [])[already:])
        remaining.sort(key=lambda p: p["projected_points"], reverse=True)
        for p in remaining[: teams * slot_count]:
            ranks[p["position"]] = ranks.get(p["position"], 0) + 1

    for pos in VALUE_POSITIONS:
        ranks.setdefault(pos, teams)
    return ranks


def replacement_points(players: list[dict], ranks: dict[str, int]) -> dict[str, float]:
    """Projection of the first player *below* the startable cutoff."""
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p["projected_points"])

    out: dict[str, float] = {}
    for pos, points in by_pos.items():
        points.sort(reverse=True)
        cutoff = ranks.get(pos, len(points))
        if not points:
            out[pos] = 0.0
        elif cutoff < len(points):
            out[pos] = points[cutoff]
        else:
            out[pos] = points[-1]
    return out


def natural_breaks(values: list[float], k: int, iters: int = 100) -> list[int]:
    """1-D k-means (Jenks natural breaks) over a descending series.

    Returns a cluster index per value, 0 being the highest group. Because the
    input is sorted and the centroids start sorted, clusters stay contiguous.

    A plain gap threshold does not work here: elite players are genuinely far
    apart, so any global cutoff makes each of them their own tier and dumps
    everyone else into one blob. Clustering adapts to the local scale instead.
    """
    n = len(values)
    if n == 0:
        return []
    if k <= 1 or n <= k:
        return list(range(n))

    lo, hi = values[-1], values[0]
    if hi == lo:
        return [0] * n

    centroids = [hi - (hi - lo) * i / (k - 1) for i in range(k)]
    assignment = [0] * n
    for _ in range(iters):
        changed = False
        for idx, v in enumerate(values):
            best = min(range(k), key=lambda c: abs(v - centroids[c]))
            if assignment[idx] != best:
                assignment[idx] = best
                changed = True
        sums = [0.0] * k
        counts = [0] * k
        for idx, v in enumerate(values):
            sums[assignment[idx]] += v
            counts[assignment[idx]] += 1
        for c in range(k):
            if counts[c]:
                centroids[c] = sums[c] / counts[c]
        if not changed:
            break
    return assignment


def assign_tiers(players: list[dict], ranks: dict[str, int], teams: int,
                 key: str = "vorp", tiers: int = 6) -> None:
    """Tag each player with a within-position tier, in place.

    Tiers are only meaningful over the draftable range, so each position is
    clustered down to its replacement rank plus one round of bench depth.
    Everything past that lands in a single trailing tier -- deep waiver-wire
    players don't need tiering, and including them distorts the clusters.
    """
    by_pos: dict[str, list[dict]] = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p)

    for pos, pos_players in by_pos.items():
        pos_players.sort(key=lambda p: p[key], reverse=True)
        depth = ranks.get(pos, len(pos_players)) + max(teams, 1)
        head, tail = pos_players[:depth], pos_players[depth:]

        # Keep tiers averaging at least ~3 players. In a shallow position a
        # full k would split genuine clusters just to hit the target count.
        k = min(tiers, max(1, -(-len(head) // 3)))
        clusters = natural_breaks([p[key] for p in head], k)
        # k-means can leave a centroid empty; renumber so tiers stay contiguous.
        renumber: dict[int, int] = {}
        for c in clusters:
            if c not in renumber:
                renumber[c] = len(renumber) + 1
        for p, c in zip(head, clusters):
            p["tier"] = renumber[c]

        last = max(renumber.values(), default=0) + 1
        for p in tail:
            p["tier"] = last


def unprojected_positions(players: list[dict]) -> set[str]:
    """Positions ESPN publishes no season projections for.

    D/ST is the standing example: every defense comes back with 0.0, so VORP
    for them would be a uniform 0 -- worse than useless, since it would sort
    them above genuinely negative-value players. Those positions fall back to
    ADP ordering and are marked so callers don't read meaning into the number.
    """
    best: dict[str, float] = {}
    for p in players:
        pos = p["position"]
        best[pos] = max(best.get(pos, 0.0), p["projected_points"])
    return {pos for pos, top in best.items() if top <= 0.0}


def _adp_sort_key(p: dict) -> tuple:
    """Best-first by ADP, then ESPN draft rank, then ownership."""
    adp = p.get("espn_adp")
    rank = p.get("espn_draft_rank")
    return (
        adp is None and rank is None,
        adp if adp is not None else (rank if rank is not None else 9999),
        -(p.get("percent_owned") or 0.0),
    )


def build_value_board(players: list[dict], shape: LeagueShape) -> dict:
    """Attach VORP + tier to every player and return the board with metadata."""
    ranks = replacement_ranks(players, shape)
    baselines = replacement_points(players, ranks)
    no_projections = unprojected_positions(players)

    valued: list[dict] = []
    unvalued: list[dict] = []
    for p in players:
        pos = p["position"]
        p["late_round_position"] = pos in LATE_ROUND_POSITIONS
        if pos in no_projections:
            p["value_basis"] = "espn_adp"
            p["replacement_points"] = None
            p["vorp"] = None
            p["tier"] = None
            unvalued.append(p)
        else:
            baseline = baselines.get(pos, 0.0)
            p["value_basis"] = "vorp"
            p["replacement_points"] = round(baseline, 2)
            p["vorp"] = round(p["projected_points"] - baseline, 2)
            valued.append(p)

    assign_tiers(valued, ranks, shape.teams)

    # Skill players first by VORP; kickers and defenses after them, however
    # good their raw VORP looks; then positions with no projections at all.
    skill = [p for p in valued if not p["late_round_position"]]
    late = [p for p in valued if p["late_round_position"]]
    skill.sort(key=lambda p: p["vorp"], reverse=True)
    late.sort(key=lambda p: p["vorp"], reverse=True)
    unvalued.sort(key=_adp_sort_key)
    ordered = skill + late + unvalued
    for i, p in enumerate(ordered, start=1):
        p["overall_value_rank"] = i

    by_pos_counter: dict[str, int] = {}
    for p in ordered:
        by_pos_counter[p["position"]] = by_pos_counter.get(p["position"], 0) + 1
        p["position_value_rank"] = by_pos_counter[p["position"]]

    return {
        "players": ordered,
        "replacement_ranks": ranks,
        "replacement_points": {
            k: (None if k in no_projections else round(v, 2))
            for k, v in baselines.items()
        },
        "positions_without_projections": sorted(no_projections),
    }


def value_vs_adp(player: dict) -> float | None:
    """Positive means the player is going later than their value warrants.

    Only meaningful where the value rank came from projections; for positions
    ranked by ADP the comparison would be circular.
    """
    if player.get("value_basis") != "vorp":
        return None
    adp = player.get("espn_adp")
    rank = player.get("overall_value_rank")
    if not adp or not rank:
        return None
    return round(adp - rank, 1)
