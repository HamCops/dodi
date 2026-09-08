"""Static ESPN fantasy football id maps.

ESPN encodes positions, lineup slots and pro teams as integers throughout the
v3 API. Nothing here is league-specific -- league scoring and roster shape are
read from the league itself, never hardcoded.
"""

POSITION_BY_ID = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    7: "P",
    9: "DT",
    10: "DE",
    11: "LB",
    12: "CB",
    13: "S",
    16: "D/ST",
}

# Lineup slot ids, used by rosterSettings.lineupSlotCounts.
SLOT_BY_ID = {
    0: "QB",
    1: "TQB",
    2: "RB",
    3: "RB/WR",
    4: "WR",
    5: "WR/TE",
    6: "TE",
    7: "OP",
    16: "D/ST",
    17: "K",
    18: "P",
    19: "HC",
    20: "BE",
    21: "IR",
    23: "FLEX",
}

# Slots that hold a real starter (excludes bench and IR).
BENCH_SLOTS = {20, 21}

# Multi-position slots, mapped to the positions eligible to fill them.
FLEX_SLOT_ELIGIBILITY = {
    3: ("RB", "WR"),
    5: ("WR", "TE"),
    7: ("QB", "RB", "WR", "TE"),
    23: ("RB", "WR", "TE"),
}

# Slots that map 1:1 to a single position.
DEDICATED_SLOT_POSITION = {
    0: "QB",
    2: "RB",
    4: "WR",
    6: "TE",
    16: "D/ST",
    17: "K",
}

PRO_TEAM_BY_ID = {
    0: "FA",
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WSH",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}

# stats[] entry discriminators.
STAT_SOURCE_ACTUAL = 0
STAT_SOURCE_PROJECTED = 1
STAT_SPLIT_SEASON_TOTAL = 0
STAT_SPLIT_WEEKLY = 1

# Reverse of POSITION_BY_ID for the positions the value math handles.
POSITION_ID_BY_NAME = {name: pid for pid, name in POSITION_BY_ID.items()}

# Roster slots that do not start (bench, IR).
NON_STARTING_SLOTS = {20, 21}

# Injury statuses that should be surfaced loudly during a draft.
SERIOUS_INJURY_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL", "PUP"}
