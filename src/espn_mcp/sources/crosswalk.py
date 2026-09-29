"""Matching ESPN's players to the other sources' ids.

Ids first: Sleeper's directory and FantasyCalc both carry ESPN's id for most
players. Names only as a fallback, and only when the name and position pick
out exactly one player -- a wrong match puts one player's trade value on
another, which is worse than no value at all.
"""

from __future__ import annotations

import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# ESPN's abbreviation -> Sleeper's, where they differ.
TEAM_ALIASES = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV"}


def norm_team(team: str | None) -> str | None:
    if not team:
        return None
    team = team.upper()
    return TEAM_ALIASES.get(team, team)


def norm_name(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", " ")).split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def build(espn_players: list[dict], sleeper_players: list[dict],
          market: list[dict] | None = None) -> dict[int, str]:
    """ESPN player id -> Sleeper player id."""
    by_espn: dict[str, str] = {}
    by_name: dict[tuple[str, str], list[dict]] = {}
    defenses: dict[str, str] = {}
    for s in sleeper_players:
        if s.get("position") == "DEF":
            if s.get("team"):
                defenses[s["team"]] = s["player_id"]
            continue
        if s.get("espn_id"):
            by_espn.setdefault(str(s["espn_id"]), s["player_id"])
        by_name.setdefault((norm_name(s.get("full_name")), s.get("position") or ""),
                           []).append(s)
    # FantasyCalc knows both ids for everyone it prices; fill Sleeper's gaps.
    for m in market or []:
        if m.get("espn_id") and m.get("sleeper_id"):
            by_espn.setdefault(m["espn_id"], m["sleeper_id"])

    out: dict[int, str] = {}
    for p in espn_players:
        pid = p["player_id"]
        if p.get("position") == "D/ST":
            sid = defenses.get(norm_team(p.get("pro_team")) or "")
        else:
            sid = by_espn.get(str(pid))
            if sid is None:
                sid = _by_name(p, by_name)
        if sid:
            out[pid] = sid
    return out


def _by_name(p: dict, by_name: dict[tuple[str, str], list[dict]]) -> str | None:
    found = by_name.get((norm_name(p.get("name")), p.get("position") or ""), [])
    if len(found) > 1:
        team = norm_team(p.get("pro_team"))
        found = [s for s in found if s.get("team") == team]
    return found[0]["player_id"] if len(found) == 1 else None
