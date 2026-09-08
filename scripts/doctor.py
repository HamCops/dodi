#!/usr/bin/env python3
"""Verify credentials and league access before draft day.

Run this first. If it prints a league summary and a top-10 board, the MCP
server will work.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402
from espn_mcp.espn import ESPNError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", help="league id to check, overriding .env")
    ap.add_argument("--team", type=int, help="your team id in that league")
    args = ap.parse_args()
    if args.league:
        os.environ["ESPN_LEAGUE_ID"] = args.league
    if args.team:
        os.environ["ESPN_TEAM_ID"] = str(args.team)

    try:
        cfg = load_config()
    except RuntimeError as exc:
        print(f"config: {exc}")
        return 1

    print(f"league {cfg.league_id}  season {cfg.season}  auth={'yes' if cfg.has_auth else 'no'}")
    b = DraftBoard(cfg)

    try:
        shape = b.shape()
    except ESPNError as exc:
        print(f"\nFAILED reading league settings:\n  {exc}")
        return 1

    info = shape.describe()
    print(f"\n{info['league_name']}: {info['teams']} teams, {info['scoring_format']}, "
          f"{info['draft_type']} draft")
    if info.get("draft_scheduled"):
        when = info["draft_time_local"]
        if info["draft_has_started"]:
            print(f"  draft: {when} -- started")
        else:
            print(f"  draft: {when}  ({info['days_until_draft']} days away, "
                  f"{info['seconds_per_pick']}s per pick)")
    else:
        print("  draft: not scheduled yet")
    print(f"  starters: {info['starting_lineup']}")
    print(f"  bench {info['bench_spots']}, roster size {info['roster_size']}")
    if cfg.team_id:
        state = b.draft_state()
        order = state["pick_order"]
        print(f"  your draft slot: {b.my_slot(cfg.team_id, state)} of {info['teams']} "
              f"(order from {state['pick_order_source']})")
        if not state["picks_made"] and order and order == sorted(order):
            print("  NOTE: pick order is still teams in id order, so it has probably "
                  "not been\n        randomized yet. The slot above is provisional.")

    if shape.draft_type.upper() != "SNAKE":
        print(f"  WARNING: draft type is {shape.draft_type}; value math assumes snake.")

    try:
        data = b.board()
    except ESPNError as exc:
        print(f"\nFAILED loading player pool:\n  {exc}")
        return 1

    print(f"\nplayer pool: {len(data['players'])} players")
    print(f"replacement points: {data['replacement_points']}")
    print(f"startable league-wide: {data['replacement_ranks']}")

    print("\ntop 10 by VORP:")
    for p in data["players"][:10]:
        adp = p.get("espn_adp") or "-"
        print(f"  {p['overall_value_rank']:>3}. {p['name']:<24} {p['position']:<5} "
              f"proj {p['projected_points']:>6}  vorp {p['vorp']:>6}  tier {p.get('tier')}  adp {adp}")

    try:
        state = b.draft_state()
        print(f"\ndraft: in_progress={state['in_progress']} complete={state['complete']} "
              f"picks_made={state['picks_made']}")
    except ESPNError as exc:
        print(f"\ndraft state unavailable: {exc}")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
