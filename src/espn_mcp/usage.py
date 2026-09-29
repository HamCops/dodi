"""What a player's workload says he should be scoring.

Fantasy points come from touchdowns and long plays, which are rare and
lumpy, laid over carries and targets, which are steady. A player's points
so far mix the two; his workload is only the steady part. Over two seasons
the steady part predicted the rest of the season better:

  - Players scoring 3 or more points a game ABOVE their workload fell by 2
    to 4 points a game from then on. Players 3 or more BELOW rose by about 2.
  - Trading a player running hot for one running cold, the two level on
    points so far, gained 1.8 to 3.9 points a game afterwards.
  - Among players scoring under 7 a game (the waiver wire), the top tenth
    by workload went on to outscore the top tenth by points.

No effect was found for quarterbacks, so they are left out. Coefficients
were fit on 2024 and 2025 together; see research/README.md.
"""

from __future__ import annotations

# Expected points per game from volume alone, by scoring format:
# constant, then points per carry / target / air yard as listed in INPUTS.
INPUTS = {"RB": ("carries", "targets", "air_yards"),
          "WR": ("targets", "air_yards"),
          "TE": ("targets", "air_yards")}

EXPECTED = {
    "standard": {"RB": (-0.3037, 0.7217, 0.5658, 0.0429),
                 "WR": (0.0984, 0.8204, 0.0272),
                 "TE": (0.1177, 0.8161, 0.0333)},
    "half": {"RB": (-0.3123, 0.7227, 0.9609, 0.0360),
             "WR": (0.0908, 1.2171, 0.0198),
             "TE": (0.0992, 1.2305, 0.0259)},
    "ppr": {"RB": (-0.3209, 0.7237, 1.3560, 0.0292),
            "WR": (0.0832, 1.6138, 0.0124),
            "TE": (0.0807, 1.6449, 0.0184)},
}

# Rest-of-season points per game: constant, weight on points so far, weight
# on expected points from workload.
OUTLOOK = {
    "standard": {"RB": (0.800, 0.579, 0.309), "WR": (0.503, 0.390, 0.508),
                 "TE": (0.236, 0.323, 0.705)},
    "half": {"RB": (0.902, 0.625, 0.258), "WR": (0.639, 0.448, 0.446),
             "TE": (0.403, 0.316, 0.682)},
    "ppr": {"RB": (1.019, 0.667, 0.210), "WR": (0.781, 0.496, 0.395),
            "TE": (0.571, 0.306, 0.674)},
}

MIN_GAMES = 3          # fewer than this and points so far mean very little
MIN_WORKLOAD = 6.0     # a real role: expected points per game
GAP = 3.0              # points per game between scoring and workload


def scoring(ppr: float) -> str:
    if ppr >= 0.75:
        return "ppr"
    if ppr >= 0.25:
        return "half"
    return "standard"


def profile(totals: dict, position: str, ppr: float) -> dict | None:
    """A player's season so far against his workload.

    `totals` are sums over his games: games, points_standard, points_ppr,
    carries, targets, air_yards.
    """
    games = int(totals.get("games") or 0)
    if position not in INPUTS or games < 1:
        return None
    fmt = scoring(ppr)
    per_game = lambda key: float(totals.get(key) or 0.0) / games  # noqa: E731
    const, *weights = EXPECTED[fmt][position]
    expected = const + sum(w * per_game(k) for w, k in zip(weights, INPUTS[position]))
    expected = max(expected, 0.0)
    std, full = per_game("points_standard"), per_game("points_ppr")
    scored = {"standard": std, "half": (std + full) / 2, "ppr": full}[fmt]
    c, w_scored, w_expected = OUTLOOK[fmt][position]
    out = {
        "games": games,
        "ppg": round(scored, 2),
        "expected_ppg": round(expected, 2),
        "gap": round(scored - expected, 2),
        "outlook_ppg": round(max(c + w_scored * scored + w_expected * expected, 0.0), 2),
        "per_game": {k: round(per_game(k), 1) for k in INPUTS[position]},
    }
    if games >= MIN_GAMES and expected >= MIN_WORKLOAD:
        if out["gap"] >= GAP:
            out["view"] = "running hot"
        elif out["gap"] <= -GAP:
            out["view"] = "running cold"
    return out
