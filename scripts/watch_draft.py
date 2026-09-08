#!/usr/bin/env python3
"""Poll a live ESPN draft and report each pick as it lands.

This is the instrument for the one thing offline tests cannot prove: that
ESPN's read API reflects picks from the draft room in real time, and how
quickly. Run it against a mock draft, then draft normally in the browser.

    ESPN_LEAGUE_ID=123456789 python scripts/watch_draft.py
    ESPN_LEAGUE_ID=123456789 python scripts/watch_draft.py --interval 2
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402
from espn_mcp.espn import ESPNError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between polls")
    ap.add_argument("--max-minutes", type=float, default=90.0)
    ap.add_argument("--league", help="league id to watch, overriding .env")
    ap.add_argument("--team", type=int, help="your team id in that league")
    args = ap.parse_args()

    if args.league:
        os.environ["ESPN_LEAGUE_ID"] = args.league
    if args.team:
        os.environ["ESPN_TEAM_ID"] = str(args.team)
    cfg = load_config()
    b = DraftBoard(cfg)
    shape = b.shape()
    me = cfg.team_id

    print(f"watching league {cfg.league_id} -- {shape.describe()['league_name']}")
    print(f"{shape.teams} teams x {shape.roster_size} rounds, my team {me}")
    print(f"polling every {args.interval}s. Ctrl-C to stop.\n")

    board = b.board()
    by_id = board["by_id"]
    print(f"player pool loaded: {len(board['players'])} players\n")

    seen: set[int] = set()
    deadline = time.time() + args.max_minutes * 60
    polls = 0
    latencies: list[float] = []
    last_change = time.time()

    while time.time() < deadline:
        try:
            state = b.draft_state()
        except ESPNError as exc:
            print(f"  [error] {exc}")
            time.sleep(args.interval)
            continue

        polls += 1
        fresh = [p for p in state["picks"] if p["overall"] not in seen]

        if fresh:
            detected = time.time()
            latencies.append(detected - last_change)
            last_change = detected
            for pick in sorted(fresh, key=lambda p: p["overall"]):
                seen.add(pick["overall"])
                player = by_id.get(pick["player_id"])
                name = player["name"] if player else f"UNKNOWN id={pick['player_id']}"
                pos = player["position"] if player else "?"
                mine = "  <== MY PICK" if pick["team_id"] == me else ""
                flag = "" if player else "   !! not found in pool"
                print(f"  #{pick['overall']:>3} R{pick['round']:<2} team {pick['team_id']:<3} "
                      f"{name:<26} {pos:<5}{mine}{flag}")

            taken = b.drafted_ids(state)
            avail = len(b.available(limit=10_000, taken=taken))
            expected = len(board["players"]) - len(taken)
            ok = "ok" if avail == expected else f"MISMATCH expected {expected}"
            print(f"       pool now {avail} available ({ok})")

            on_clock = state["on_the_clock_team_id"]
            if on_clock == me and state["next_overall_pick"]:
                nxt = b.available(limit=3, taken=taken)
                names = ", ".join(f"{p['name']} ({p['position']} {p['vorp']})" for p in nxt)
                print(f"       >>> YOU ARE ON THE CLOCK at #{state['next_overall_pick']}")
                print(f"       >>> best available: {names}")
            print()

        if state["complete"]:
            print("draft complete.")
            break

        time.sleep(args.interval)

    print(f"\n{len(seen)} picks seen over {polls} polls")
    if latencies:
        print(f"detection gap: min {min(latencies):.1f}s, max {max(latencies):.1f}s")
    unknown = [o for o in seen if not by_id.get(
        next(p["player_id"] for p in b.draft_state()["picks"] if p["overall"] == o))]
    print(f"picks whose player was missing from the pool: {len(unknown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
