"""Sleeper's public API: the player directory, pickup trends, projections.

No key and no login. The directory is large and Sleeper asks that it be
fetched at most once a day; it is cut down to the fields used before it is
cached.
"""

from __future__ import annotations

from typing import Any, Callable

BASE = "https://api.sleeper.app"

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

PLAYER_FIELDS = ("player_id", "full_name", "position", "team", "espn_id",
                 "injury_status", "injury_body_part", "news_updated",
                 "practice_participation", "depth_chart_order")

Fetch = Callable[..., Any]


def players(fetch: Fetch) -> list[dict]:
    """Every fantasy-relevant player, slimmed."""
    raw = fetch(f"{BASE}/v1/players/nfl", timeout=60.0)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Sleeper returned no players.")
    out = []
    for pid, p in raw.items():
        if p.get("position") not in POSITIONS:
            continue
        if not (p.get("active") or p.get("espn_id")):
            continue
        rec = {k: p.get(k) for k in PLAYER_FIELDS}
        rec["player_id"] = str(pid)
        if not rec["full_name"]:
            rec["full_name"] = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        out.append(rec)
    return out


def trending(fetch: Fetch, kind: str, hours: int = 24, limit: int = 100) -> dict[str, int]:
    """Sleeper id -> how many leagues added (or dropped) him in the window."""
    if kind not in ("add", "drop"):
        raise ValueError("kind must be add or drop")
    rows = fetch(f"{BASE}/v1/players/nfl/trending/{kind}",
                 params=[("lookback_hours", hours), ("limit", limit)])
    return {str(r["player_id"]): int(r["count"]) for r in rows or []}


def scoring_key(ppr: float) -> str:
    """Which of Sleeper's three totals matches the league's reception points."""
    if ppr >= 0.75:
        return "pts_ppr"
    if ppr >= 0.25:
        return "pts_half_ppr"
    return "pts_std"


def projections(fetch: Fetch, season: int, week: int, ppr: float) -> dict[str, float]:
    """Sleeper id -> projected points for the week, in the nearest scoring."""
    key = scoring_key(ppr)
    params: list[tuple[str, Any]] = [("season_type", "regular"), ("order_by", key)]
    params += [("position[]", pos) for pos in POSITIONS]
    rows = fetch(f"{BASE}/projections/nfl/{int(season)}/{int(week)}", params=params)
    out: dict[str, float] = {}
    for r in rows or []:
        pts = (r.get("stats") or {}).get(key)
        if pts is not None:
            out[str(r["player_id"])] = round(float(pts), 2)
    return out
