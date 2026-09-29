"""nflverse: what every player actually did, game by game.

Public CSV files published to GitHub releases, refreshed after each day of
games. Used for workload (carries, targets, air yards), which ESPN's
fantasy API does not give.
"""

from __future__ import annotations

import csv
import io
from typing import Callable

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"

FetchText = Callable[..., str]


def season_totals(fetch_text: FetchText, season: int) -> dict:
    """gsis id -> what he has done this regular season, summed over his games."""
    text = fetch_text(f"{RELEASES}/stats_player/stats_player_week_{int(season)}.csv",
                      timeout=90.0)
    players: dict[str, dict] = {}
    through = 0

    def num(row, key):
        try:
            return float(row.get(key) or 0.0)
        except ValueError:
            return 0.0

    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season_type") != "REG" or row.get("position") not in ("RB", "WR", "TE"):
            continue
        week = int(num(row, "week"))
        through = max(through, week)
        p = players.setdefault(row["player_id"], {
            "games": 0, "points_standard": 0.0, "points_ppr": 0.0, "carries": 0.0,
            "targets": 0.0, "air_yards": 0.0})
        p["games"] += 1
        p["points_standard"] += num(row, "fantasy_points")
        p["points_ppr"] += num(row, "fantasy_points_ppr")
        p["carries"] += num(row, "carries")
        p["targets"] += num(row, "targets")
        p["air_yards"] += num(row, "receiving_air_yards")
    if not players:
        raise ValueError(f"nflverse has no {season} games yet.")
    for p in players.values():
        for k, v in p.items():
            p[k] = round(v, 2)
    return {"through_week": through, "players": players}


def espn_ids(fetch_text: FetchText) -> dict[str, str]:
    """ESPN player id -> gsis id."""
    text = fetch_text(f"{RELEASES}/players/players.csv", timeout=120.0)
    out: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        espn, gsis = (row.get("espn_id") or "").strip(), (row.get("gsis_id") or "").strip()
        if espn and gsis:
            out[espn.split(".")[0]] = gsis
    if not out:
        raise ValueError("nflverse returned no player ids.")
    return out
