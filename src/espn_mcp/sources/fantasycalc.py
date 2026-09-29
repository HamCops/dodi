"""FantasyCalc: what players are worth in trades, from trades actually made.

The values come from real leagues' completed trades, so they measure what
managers will pay, not what a player will score. The gap between the two is
the point: a player the market prices above his projection is one to sell.
"""

from __future__ import annotations

from typing import Any, Callable

URL = "https://api.fantasycalc.com/values/current"

Fetch = Callable[..., Any]


def league_params(teams: int, ppr: float, qb_slots: int = 1) -> list[tuple[str, Any]]:
    # FantasyCalc models 0, 0.5 and 1 point per reception.
    nearest = min((0, 0.5, 1), key=lambda v: abs(v - ppr))
    return [("isDynasty", "false"), ("numQbs", 2 if qb_slots >= 2 else 1),
            ("numTeams", int(teams)), ("ppr", nearest)]


def values(fetch: Fetch, teams: int, ppr: float, qb_slots: int = 1) -> list[dict]:
    rows = fetch(URL, params=league_params(teams, ppr, qb_slots))
    if not isinstance(rows, list) or not rows:
        raise ValueError("FantasyCalc returned no values.")
    out = []
    for r in rows:
        p = r.get("player") or {}
        if r.get("value") is None or not p.get("name"):
            continue
        out.append({
            "name": p["name"],
            "position": p.get("position"),
            "team": p.get("maybeTeam"),
            "espn_id": str(p["espnId"]) if p.get("espnId") else None,
            "sleeper_id": str(p["sleeperId"]) if p.get("sleeperId") else None,
            "value": int(r["value"]),
            "overall_rank": r.get("overallRank"),
            "position_rank": r.get("positionRank"),
            "trend_30d": r.get("trend30Day"),
        })
    return out
