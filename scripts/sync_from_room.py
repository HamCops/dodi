#!/usr/bin/env python3
"""Seed the board from the draft room's own Pick History panel.

The socket feed only streams picks made after you connect, so starting late
silently loses everything before it -- and because picks are numbered by
arrival, the gap is invisible. The room, however, renders the complete history
in its "Pick History" tab. Reading that reconciles the board exactly, whenever
you joined.

    python scripts/sync_from_room.py --league 123456789 --team 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}


def as_position(cell: str) -> str | None:
    """ESPN writes dual-eligibility inline, e.g. Travis Hunter is 'WRCB'."""
    c = (cell or "").strip().upper()
    if c in POSITIONS:
        return c
    for pos in ("D/ST", "QB", "RB", "WR", "TE", "K"):
        if c.startswith(pos) and len(c) <= 6:
            return pos
    return None

# The history panel is virtualised too, so a single read only returns the
# rows currently on screen. Scroll it end to end and accumulate.
HISTORY_JS = """async () => {
  const el = document.querySelector('[class*="pick-history"]');
  if (!el) return [];
  const seen = [];
  const push = () => {
    for (const s of (el.innerText || '').split('\\n')) {
      const t = s.trim();
      if (t) seen.push(t);
    }
  };
  el.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  push();
  let last = -1;
  for (let i = 0; i < 60 && el.scrollTop !== last; i++) {
    last = el.scrollTop;
    el.scrollTop = el.scrollTop + Math.max(200, el.clientHeight - 60);
    await new Promise(r => setTimeout(r, 220));
    push();
  }
  return seen;
}"""


# Round filters and column headers are interleaved with the rows, and after
# scrolling they can land right where a player name is expected.
CHROME = re.compile(r"^(All Rounds|Round \d+|PICK|PLAYER|TEAM|RK|"
                    r"\d{4} PTS|PROJ PTS)$", re.I)


def parse_history(lines: list[str]) -> list[dict]:
    """Cells arrive flat: #, name, NFL team, pos, manager, pts, proj, rank."""
    lines = [l for l in lines if not CHROME.match(l)]
    picks, i = [], 0
    seen_overall: set[int] = set()
    while i < len(lines) - 3:
        if re.fullmatch(r"\d{1,3}", lines[i]):
            overall = int(lines[i])
            name = lines[i + 1]
            # Position sits two or three cells on, depending on injury tags.
            pos = next((as_position(lines[j])
                        for j in range(i + 2, min(i + 6, len(lines)))
                        if as_position(lines[j])), None)
            if pos and re.search(r"[A-Za-z]", name) and len(name) > 2:
                # Scrolling re-reads overlapping rows, so key on the pick
                # number rather than assuming a strictly increasing stream.
                if overall not in seen_overall:
                    seen_overall.add(overall)
                    picks.append({"overall": overall, "name": name, "pos": pos})
                i += 4
                continue
        i += 1
    return picks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--team", type=int, required=True)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--keep", action="store_true",
                    help="merge into existing picks instead of replacing them")
    args = ap.parse_args()

    os.environ["ESPN_LEAGUE_ID"] = args.league
    os.environ["ESPN_TEAM_ID"] = str(args.team)
    import espn_mcp.server as srv

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = br.contexts[0]
        page = next((p for p in ctx.pages
                     if f"leagueId={args.league}" in p.url and "/draft" in p.url), None)
        if not page:
            print("No draft tab open for that league.")
            return 1

        tab = page.get_by_role("button", name="Pick History")
        if tab.count():
            tab.first.click()
            page.wait_for_timeout(2500)
        lines = page.evaluate(HISTORY_JS)
        # Put the room back on the Players tab so the user is where they were.
        back = page.get_by_role("button", name="Players")
        if back.count():
            back.first.click()

    picks = sorted(parse_history(lines), key=lambda p: p["overall"])
    if not picks:
        print("No picks parsed. Is the draft room open on this league?")
        return 1

    b = srv.board()
    if not args.keep:
        b.clear_manual_picks()
    res = srv.record_picks.__wrapped__(players=[p["name"] for p in picks])

    print(f"parsed {len(picks)} picks from the room")
    print(f"recorded {res['newly_recorded']} (already had {res['already_had']})")
    if res.get("needs_attention"):
        print("unmatched:", [s["name"] for s in res["needs_attention"]][:8])
    print(f"board now shows {b.draft_state()['picks_made']} picks made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
