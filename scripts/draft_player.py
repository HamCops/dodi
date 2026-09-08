#!/usr/bin/env python3
"""Make a pick in the live ESPN draft room by clicking it in your browser.

ESPN exposes no write API, so a pick is made the way a person makes it: find
the player's row in the draft room and press its DRAFT button. Attaches to
your already-running browser over CDP, so it uses your real session.

    python scripts/draft_player.py "Tyler Warren"
    python scripts/draft_player.py "Tyler Warren" --dry-run

Refuses to click unless the room says it is your turn, so a mistimed call
cannot spend someone else's pick.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.config import load_config  # noqa: E402


def find_draft_tab(ctx, league_id: str):
    want = f"leagueId={league_id}"
    return next((p for p in ctx.pages if "/football/draft" in p.url and want in p.url), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("player", help="player name, full or partial")
    ap.add_argument("--league")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--dry-run", action="store_true",
                    help="locate the button and report, but do not click")
    ap.add_argument("--force", action="store_true",
                    help="click even if the room does not say it is your turn")
    args = ap.parse_args()

    cfg = load_config()
    league = args.league or cfg.league_id

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = browser.contexts[0]
        page = find_draft_tab(ctx, league)
        if not page:
            print(f"No draft tab open for league {league}.")
            return 1

        state = page.evaluate("""() => {
            const t = document.body.innerText;
            return {
              onclock: (t.match(/ON THE CLOCK[^\\n]{0,40}/i) || [''])[0],
              yours: /YOUR PICK|YOU'RE ON THE CLOCK|YOU ARE ON THE CLOCK/i.test(t),
            };
        }""")
        print(f"room: {state['onclock'] or '(no clock text)'} | your turn: {state['yours']}")
        if not state["yours"] and not args.force:
            print("Not your pick -- refusing to click. Use --force to override.")
            return 2

        # ESPN renders the pool with fixedDataTable: the DRAFT button lives in
        # its own cell and the player name only appears six levels up at the
        # cell group, so the ancestor walk has to go that far.
        # ESPN virtualises the player list, so a player who is not currently
        # rendered has no DRAFT button at all. Type the name into the room's
        # own search box first -- and type it, since a programmatic value set
        # does not fire their React handler.
        try:
            box = page.locator("input[placeholder*='layer' i]").first
            box.click()
            box.fill("")
            box.press_sequentially(args.player, delay=60)
            page.wait_for_timeout(2200)
        except Exception as exc:
            print(f"search box unavailable ({type(exc).__name__}); "
                  "trying the visible list")

        # Locate the row whose text contains the player, then that row's own
        # DRAFT button. Anchoring to the row prevents drafting a neighbour.
        # Match the tightest ancestor that names the player. Walking too far up
        # reaches the "your autopick would be: X" banner, which also contains a
        # DRAFT button -- clicking that drafts the wrong player entirely.
        hit = page.evaluate("""(name) => {
            const norm = s => (s||'').toLowerCase().replace(/[^a-z ]/g,'');
            const want = norm(name);
            const bad = /autopick|on the clock|queue/i;
            const btns = [...document.querySelectorAll('button[class*="Button--draft"]')];
            let best = null;
            for (const b of btns) {
                let row = b.closest('tr') || b.parentElement;
                for (let i = 0; i < 8 && row; i++) {
                    const raw = (row.innerText || '').trim();
                    if (raw.length > 10 && raw.length < 160 &&
                        norm(raw).includes(want) && !bad.test(raw)) {
                        if (!best || raw.length < best.len) {
                            best = {el: b, len: raw.length,
                                    row: raw.replace(/\\n+/g, ' | ').slice(0, 90)};
                        }
                        break;
                    }
                    row = row.parentElement;
                }
            }
            if (!best) return {found: false};
            best.el.setAttribute('data-pick-target', '1');
            return {found: true, row: best.row};
        }""", args.player)

        if not hit.get("found"):
            print(f"Could not find a DRAFT button for {args.player!r}. "
                  "Search for the player in the room first so the row is visible.")
            return 1

        print(f"matched row: {hit['row']}")
        if args.dry_run:
            print("dry run -- not clicking")
            return 0

        page.click('button[data-pick-target="1"]')
        page.wait_for_timeout(1200)
        # ESPN sometimes asks to confirm.
        # Never include a bare "DRAFT" here: if ESPN drafts without a confirm
        # dialog, the first visible DRAFT button belongs to another player.
        for label in ("Confirm", "Yes", "Draft Player"):
            try:
                btn = page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    print(f"confirmed via '{label}'")
                    break
            except Exception:
                pass
        page.wait_for_timeout(800)
        print(f"clicked DRAFT for {args.player}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
