#!/usr/bin/env python3
"""Run a full mock draft against the real league board, offline.

Uses this league's actual settings, projections and ADP, but synthesizes the
picks -- so it exercises the entire ingestion path (pick parsing, pool
shrinking, roster needs, tier depletion, on-the-clock tracking, end of draft)
without needing ESPN's draft room.

It asserts invariants after every single pick, so a regression anywhere in the
live path surfaces as a failure at the pick where it happens.

    python scripts/simulate_draft.py            # quiet, invariants only
    python scripts/simulate_draft.py --verbose  # print every pick
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402
from espn_mcp.espn import ESPNClient  # noqa: E402


class SimClient:
    """Serves the league's real payloads, with a draft that we drive."""

    def __init__(self, real: ESPNClient) -> None:
        self._settings = real.settings()
        self._teams = real.teams()
        self._pool = real.player_pool()
        # Start from ESPN's real published schedule: every slot, playerId -1.
        detail = real.draft_detail().get("draftDetail") or {}
        self.slots = {
            int(p["overallPickNumber"]): {
                "overallPickNumber": int(p["overallPickNumber"]),
                "roundId": int(p["roundId"]),
                "roundPickNumber": int(p["roundPickNumber"]),
                "teamId": int(p["teamId"]),
                "playerId": -1,
                "autoDraftTypeId": 0,
            }
            for p in (detail.get("picks") or [])
            if p.get("overallPickNumber") and p.get("teamId")
        }

    def settings(self) -> dict:
        return self._settings

    def teams(self) -> dict:
        return self._teams

    def player_pool(self, **_: object) -> list[dict]:
        return self._pool

    def draft_detail(self) -> dict:
        return {
            "draftDetail": {
                "drafted": all(s["playerId"] > 0 for s in self.slots.values()),
                "inProgress": True,
                "picks": list(self.slots.values()),
            }
        }

    def make_pick(self, overall: int, player_id: int) -> None:
        self.slots[overall]["playerId"] = player_id


def choose_opponent_pick(available: list[dict], rng: random.Random) -> dict:
    """Approximate a human: mostly follows ADP, with some reaching."""
    by_adp = sorted(
        available,
        key=lambda p: (p.get("espn_adp") is None, p.get("espn_adp") or 9999),
    )
    window = by_adp[: max(1, min(6, len(by_adp)))]
    return rng.choice(window)


def choose_my_pick(available: list[dict], needs: dict, picks_left: int) -> dict:
    """Best value, nudged toward positions we still have to start.

    Mirrors how the board is meant to be read: kickers and defenses are last
    resorts, taken only when the remaining picks are needed to fill them.
    """
    unfilled = dict(needs.get("unfilled_starting_slots") or {})
    late_needed = {p for p in ("K", "D/ST") if unfilled.get(p)}
    # Only reach for a K/DST when there are barely enough picks left to fill
    # every remaining starting slot.
    must_fill_late = picks_left <= sum(unfilled.values())

    def score(p: dict) -> float:
        pos = p["position"]
        if pos in ("K", "D/ST"):
            if not (must_fill_late and pos in late_needed):
                return -1e9
            # Within the late positions, VORP is meaningful; ADP breaks ties
            # for defenses, which carry no projection at all.
            return (p.get("vorp") or 0.0) - (p.get("espn_adp") or 300) / 1000
        return (p.get("vorp") or -1e6) + (25.0 if pos in unfilled else 0.0)

    return max(available, key=score)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    cfg = load_config()
    print(f"loading real league {cfg.league_id} ...")
    real = ESPNClient(cfg)
    sim = SimClient(real)
    b = DraftBoard(cfg, client=sim)

    shape = b.shape()
    total = shape.teams * shape.roster_size
    me = cfg.team_id
    rng = random.Random(args.seed)

    print(f"{shape.describe()['league_name']}: {shape.teams} teams x "
          f"{shape.roster_size} rounds = {total} picks, my team {me}\n")

    seen: set[int] = set()
    prev_avail = len(b.available(limit=10_000))
    slowest = 0.0
    my_picks: list[dict] = []

    for overall in range(1, total + 1):
        state = b.draft_state()

        assert state["picks_made"] == overall - 1, (
            f"pick {overall}: picks_made={state['picks_made']}, expected {overall - 1}")
        assert state["next_overall_pick"] == overall, (
            f"pick {overall}: next_overall_pick={state['next_overall_pick']}")
        assert not state["complete"], f"pick {overall}: draft reported complete early"

        on_clock = state["on_the_clock_team_id"]
        assert on_clock == sim.slots[overall]["teamId"], (
            f"pick {overall}: on the clock {on_clock}, schedule says "
            f"{sim.slots[overall]['teamId']}")

        taken = b.drafted_ids(state)
        available = b.available(limit=10_000, taken=taken)
        assert len(available) == prev_avail, (
            f"pick {overall}: pool is {len(available)}, expected {prev_avail}")
        assert not (taken & {p["player_id"] for p in available}), (
            f"pick {overall}: a drafted player is still listed available")

        if on_clock == me:
            # Time the call the real draft depends on.
            t0 = time.perf_counter()
            import espn_mcp.server as srv

            srv._board = b
            ctx = srv.get_draft_context.__wrapped__(top_per_position=5)
            srv._board = None
            slowest = max(slowest, time.perf_counter() - t0)

            assert "error" not in ctx, f"pick {overall}: get_draft_context errored: {ctx}"
            assert ctx["on_the_clock_is_me"] is True, f"pick {overall}: not flagged as my turn"
            assert ctx["next_overall_pick"] == overall
            picks_left = len(ctx.get("my_next_picks") or []) or 1
            remaining_rounds = shape.roster_size - len(my_picks)
            pick = choose_my_pick(available, ctx, remaining_rounds)
            my_picks.append(pick)
        else:
            pick = choose_opponent_pick(available, rng)

        assert pick["player_id"] not in seen, f"pick {overall}: {pick['name']} drafted twice"
        seen.add(pick["player_id"])
        sim.make_pick(overall, pick["player_id"])
        prev_avail -= 1

        if args.verbose or on_clock == me:
            tag = " <== ME" if on_clock == me else ""
            vorp = pick["vorp"] if pick["vorp"] is not None else "n/a"
            print(f"  {overall:>3} R{sim.slots[overall]['roundId']:<2} team {on_clock:<3} "
                  f"{pick['name']:<24} {pick['position']:<5} vorp {str(vorp):>7}{tag}")

    final = b.draft_state()
    assert final["picks_made"] == total, f"final picks_made={final['picks_made']}"
    assert final["complete"], "draft did not report complete"
    assert final["next_overall_pick"] is None, "next pick set after the draft ended"
    assert len(seen) == total, f"{len(seen)} unique players for {total} picks"

    counts: dict[int, int] = {}
    for slot in sim.slots.values():
        counts[slot["teamId"]] = counts.get(slot["teamId"], 0) + 1
    assert set(counts.values()) == {shape.roster_size}, f"uneven rosters: {counts}"

    import espn_mcp.server as srv

    srv._board = b
    roster = srv.get_roster.__wrapped__(team_id=me)
    srv._board = None
    assert not roster.get("unfilled_starting_slots"), (
        f"finished with unfilled starters: {roster['unfilled_starting_slots']}")

    print(f"\nmy roster ({len(roster['players'])} players):")
    for p in roster["players"]:
        print(f"    {p['pos']:<5} {p['name']:<24} proj {p['proj']}")
    print(f"  positions: {roster['current_roster_counts']}")

    print(f"\nOK -- {total} picks, every invariant held.")
    print(f"slowest get_draft_context: {slowest * 1000:.0f} ms (excludes network)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
