#!/usr/bin/env python3
"""Draft your real league offline, using the real autodraft scoring.

The live mocks were all PPR with one flex; the league that matters is Standard
with two. This runs the same scorer against the real board so its behaviour can
be checked under the parameters that count, repeatedly and without a clock.

    python scripts/simulate_autodraft.py --slot 9
    python scripts/simulate_autodraft.py --slot 9 --seeds 5
"""

from __future__ import annotations

import argparse
import collections
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def roster_state(shape, players):
    """have / need / byes, mirroring what autodraft reads from the room."""
    have = collections.Counter(p["position"] for p in players)
    byes: dict[str, list[int]] = {}
    for p in players:
        if p.get("bye_week"):
            byes.setdefault(p["position"], []).append(p["bye_week"])

    remaining = dict(have)
    need = set()
    for pos, count in shape.starters_by_position.items():
        used = min(remaining.get(pos, 0), count)
        remaining[pos] = remaining.get(pos, 0) - used
        if count - used > 0:
            need.add(pos)
    for eligible, count in shape.flex_slots.items():
        spare = sum(max(remaining.get(p, 0), 0) for p in eligible)
        if spare < count:
            need.add("FLEX")
    return dict(have), need, byes


def run(b, srv, build_scorer, slot: int, seed: int, verbose: bool, notes=None):
    notes = notes or {}
    shape = b.shape()
    teams, rounds = shape.teams, shape.roster_size
    rng = random.Random(seed)
    pool = {p["player_id"]: p for p in b.available(limit=10_000)}
    mine: list[dict] = []
    others: dict[int, list[dict]] = {t: [] for t in range(1, teams + 1)}

    for overall in range(1, teams * rounds + 1):
        rnd = (overall - 1) // teams + 1
        idx = (overall - 1) % teams
        pos_in_round = idx if rnd % 2 == 1 else teams - 1 - idx
        team = pos_in_round + 1
        avail = list(pool.values())
        if not avail:
            break

        if team == slot:
            have, need, byes = roster_state(shape, mine)
            gap = 0 if rnd % 2 == 1 and idx == teams - 1 else teams
            # Load the same intel file the live autodraft reads, so the
            # simulation reflects what would actually happen on draft night.
            score, _ = build_scorer(b, need, gap, have, byes, notes, roster=mine)
            pick = max(avail, key=score)
            mine.append(pick)
            if verbose:
                print(f"  #{overall:<4} R{rnd:<3} {pick['position']:<5} "
                      f"{pick['name']:<22} {pick['pro_team']:<4} "
                      f"bye {str(pick.get('bye_week')):<4} proj {pick['projected_points']}")
        else:
            # Opponents follow ADP with a little reaching, like a real room.
            by_adp = sorted(avail, key=lambda p: (p.get("espn_adp") is None,
                                                  p.get("espn_adp") or 9999))
            pick = rng.choice(by_adp[:6])
            others[team].append(pick)
        pool.pop(pick["player_id"], None)
    return mine, others


def lineup_total(shape, players):
    pool = sorted(players, key=lambda p: -p["projected_points"])
    used, total = set(), 0.0
    for pos, n in shape.starters_by_position.items():
        c = 0
        for p in pool:
            if c >= n:
                break
            if id(p) in used or p["position"] != pos:
                continue
            used.add(id(p)); total += p["projected_points"]; c += 1
    for eligible, n in shape.flex_slots.items():
        c = 0
        for p in pool:
            if c >= n:
                break
            if id(p) in used or p["position"] not in eligible:
                continue
            used.add(id(p)); total += p["projected_points"]; c += 1
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league")
    ap.add_argument("--team", type=int)
    ap.add_argument("--slot", type=int, default=9, help="your draft position")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.league:
        os.environ["ESPN_LEAGUE_ID"] = args.league
    if args.team:
        os.environ["ESPN_TEAM_ID"] = str(args.team)
    os.environ["ESPN_STATE_DIR"] = tempfile.mkdtemp(prefix="sim-")

    import espn_mcp.server as srv
    from autodraft import build_scorer, load_notes

    b = srv.board()
    shape = b.shape()
    info = shape.describe()
    print(f"{info['league_name']} | {info['teams']} teams | {info['scoring_format']}")
    n = load_notes()
    print(f"starters: {info['starting_lineup']}  slot {args.slot}")
    print(f"notes loaded: {len([k for k in n if not k.startswith('_')])} entries\n")

    finishes = []
    for seed in range(args.seeds):
        mine, others = run(b, srv, build_scorer, args.slot, seed,
                           args.verbose and args.seeds == 1, notes=load_notes())
        mine_total = lineup_total(shape, mine)
        scores = sorted([lineup_total(shape, r) for r in others.values() if r] + [mine_total],
                        reverse=True)
        rank = scores.index(mine_total) + 1
        counts = dict(collections.Counter(p["position"] for p in mine))
        byes = collections.Counter(p.get("bye_week") for p in mine)
        worst_bye = max(byes.values()) if byes else 0
        finishes.append((rank, mine_total, counts, worst_bye, mine))
        print(f"seed {seed}: rank {rank}/{len(scores)}  lineup {mine_total:7.1f}  "
              f"{counts}  max-same-bye {worst_bye}")

    print()
    ranks = [f[0] for f in finishes]
    print(f"finished 1st in {ranks.count(1)}/{len(ranks)} sims | average rank {sum(ranks)/len(ranks):.2f}")
    qbs = [f[2].get("QB", 0) for f in finishes]
    tes = [f[2].get("TE", 0) for f in finishes]
    print(f"QBs drafted: min {min(qbs)} max {max(qbs)} | TEs: min {min(tes)} max {max(tes)}")

    if args.seeds == 1:
        print("\nfinal roster:")
        for p in finishes[0][4]:
            print(f"   {p['position']:<5} {p['name']:<22} {p['pro_team']:<4} "
                  f"bye {str(p.get('bye_week')):<4} proj {p['projected_points']:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
