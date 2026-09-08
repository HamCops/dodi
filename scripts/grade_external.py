#!/usr/bin/env python3
"""Grade a drafted roster against an outside ranking, not our own board.

Scoring a draft with the projections that produced it is circular -- it only
shows the scorer agreed with itself. This ranks every team by an external
consensus list instead, so a good grade means outside sources think the roster
is good.

    python scripts/grade_external.py --league 1234 --team 4 --ranks consensus.txt

The ranks file is one player name per line, best first.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STARTER_SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX")


def load_ranks(path: Path) -> dict[str, int]:
    out, i = {}, 0
    for line in path.read_text().splitlines():
        name = re.sub(r"^\s*\d+[\.\)]?\s*", "", line).strip()
        name = re.sub(r",\s*(RB|WR|QB|TE|K|D/ST).*$", "", name).strip()
        if not name or name.startswith("#"):
            continue
        i += 1
        out[name.lower()] = i
    return out


def best_starters(players, shape):
    """Pick the lineup a manager would actually start, by external rank."""
    ranked = sorted(players, key=lambda p: p["_ext"])
    used, chosen = set(), []
    for pos, n in shape.starters_by_position.items():
        if pos in ("K", "D/ST"):
            continue
        c = 0
        for p in ranked:
            if c >= n or id(p) in used or p["position"] != pos:
                continue
            used.add(id(p)); chosen.append(p); c += 1
    for eligible, n in shape.flex_slots.items():
        c = 0
        for p in ranked:
            if c >= n or id(p) in used or p["position"] not in eligible:
                continue
            used.add(id(p)); chosen.append(p); c += 1
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--team", type=int, required=True)
    ap.add_argument("--ranks", required=True)
    args = ap.parse_args()

    os.environ["ESPN_LEAGUE_ID"] = args.league
    os.environ["ESPN_TEAM_ID"] = str(args.team)
    import espn_mcp.server as srv

    ranks = load_ranks(Path(args.ranks))
    print(f"external list: {len(ranks)} ranked players\n")

    b = srv.board()
    shape = b.shape()
    state = b.draft_state()
    by_id = b.board()["by_id"]
    unranked = len(ranks) + 40   # players off the list are worse than the tail

    rosters: dict[int, list] = {}
    for p in state["picks"]:
        pl = by_id.get(p["player_id"])
        if not pl:
            continue
        q = dict(pl)
        q["_ext"] = ranks.get(q["name"].lower(), unranked)
        rosters.setdefault(p["team_id"], []).append(q)

    rows = []
    for tid, players in rosters.items():
        st = best_starters(players, shape)
        if not st:
            continue
        rows.append((statistics.mean(p["_ext"] for p in st), tid, st))
    rows.sort()

    for i, (avg, tid, st) in enumerate(rows, 1):
        tag = "  <== YOU" if tid == args.team else ""
        print(f"{i:>2}. team {tid:<3} avg external rank {avg:7.1f}{tag}")

    mine = next((r for r in rows if r[1] == args.team), None)
    if mine:
        print(f"\nyour starters by the external list:")
        for p in sorted(mine[2], key=lambda x: x["_ext"]):
            r = p["_ext"]
            shown = f"#{r}" if r < unranked else "unranked"
            print(f"   {p['position']:<5} {p['name']:<24} {shown}")
        print(f"\nfinish: {rows.index(mine) + 1} of {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
