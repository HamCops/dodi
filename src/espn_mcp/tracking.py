"""A record of what was known before each game, to be scored after it.

Whether a second projection, a trade value or an injury report would have
made a better lineup can only be judged against what those sources said
before kickoff. Afterwards they have all seen the result. So the numbers are
written down while they are still predictions: one row per rostered player
per snapshot, only for players whose game has not started.
"""

from __future__ import annotations

import sqlite3
import time

from .board import DraftBoard
from .season import ROS_KEY, WEEK_KEY

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    season INTEGER NOT NULL, week INTEGER NOT NULL, taken_at REAL NOT NULL,
    player_id INTEGER NOT NULL, name TEXT, position TEXT, team_id INTEGER,
    slot TEXT, kickoff_ms INTEGER, week_proj REAL, alt_week_proj REAL,
    ros_per_game REAL, market_value INTEGER, injury_status TEXT, injury_alt TEXT,
    PRIMARY KEY (season, week, taken_at, player_id)
);
CREATE TABLE IF NOT EXISTS actuals (
    season INTEGER NOT NULL, week INTEGER NOT NULL, player_id INTEGER NOT NULL,
    points REAL NOT NULL, PRIMARY KEY (season, week, player_id)
);
"""


def _db(b: DraftBoard) -> sqlite3.Connection:
    root = b.cfg.state_root
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / f"tracking-{b.cfg.league_id}-{b.cfg.season}.db", timeout=10)
    db.executescript(SCHEMA)
    return db


def record_snapshot(b: DraftBoard, now: float | None = None) -> dict:
    """Write down every rostered player still to play, and any final scores."""
    now = time.time() if now is None else now
    week = b.week()
    rows, finals = [], []
    for tid in b.league_rosters(week):
        for p in b.team_players(tid, week):
            for wk, pts in (p.get("week_points") or {}).items():
                if int(wk) < week:
                    finals.append((b.cfg.season, int(wk), p["player_id"], float(pts)))
            kickoff = p.get("kickoff_ms")
            if not kickoff or kickoff <= now * 1000:
                continue
            alt = p.get("injury_alt") or {}
            rows.append((b.cfg.season, week, now, p["player_id"], p["name"], p["position"],
                         tid, p.get("slot"), kickoff, p.get(WEEK_KEY),
                         p.get("alt_week_proj"), p.get(ROS_KEY), p.get("market_value"),
                         p.get("injury_status"), alt.get("status")))
    with _db(b) as db:
        db.executemany("INSERT OR REPLACE INTO snapshots VALUES "
                       "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO actuals VALUES (?,?,?,?)", finals)
    return {"week": week, "players_recorded": len(rows), "final_scores": len(finals)}
