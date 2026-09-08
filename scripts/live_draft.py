#!/usr/bin/env python3
"""Watch a live ESPN draft room and publish picks as they happen.

ESPN's league API does not expose picks until a draft ends (verified against
two real drafts), so the only live source is the draft room itself. This opens
the room in a real browser and listens to what it receives.

It sniffs the network rather than scraping the DOM: websocket frames and XHR
responses carry the pick events, and unlike CSS selectors they do not break
when ESPN restyles the page.

New picks are appended to the same state file the MCP server reads, so the
board, tiers, VORP and recommendations all update with no further plumbing.

    python scripts/live_draft.py --discover    # dump traffic, find the feed
    python scripts/live_draft.py               # watch and publish picks

Requires ESPN_S2 and SWID in .env: the draft room needs a logged-in session
even when the league itself reads publicly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402

# The draft room needs memberId (your SWID) or it 404s, and it is a separate
# SPA that checks for a full Disney OneID session in browser storage -- the
# espn_s2/SWID cookies alone get "Log in Required". Hence the persistent
# profile: log in once by hand, reuse that session thereafter.
DRAFT_URL = ("https://fantasy.espn.com/football/draft"
             "?leagueId={league}&seasonId={season}&teamId={team}&memberId={swid}")

PROFILE_DIR = Path(__file__).resolve().parents[1] / "state" / "browser-profile"

# Brave is Chromium under the hood, so Playwright can drive it directly given
# the binary. Preferred when present: it is the browser the user actually uses,
# so the login flow looks familiar and extensions/settings behave as expected.
# Point at the real binary, not the /usr/bin/brave-origin wrapper: that
# wrapper does `exec < /dev/null`, which severs the pipe Playwright drives the
# browser over.
BROWSER_CANDIDATES = (
    "/opt/brave-origin-bin/brave",
    "/opt/brave.com/brave/brave",
    "/usr/lib/brave-bin/brave",
    "/usr/bin/brave",
    "/usr/bin/brave-browser",
)


def find_browser(explicit: str | None = None) -> str | None:
    """Path to a Chromium-family browser, or None for Playwright's bundled one."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    for c in BROWSER_CANDIDATES:
        if Path(c).exists():
            return c
    return None


# Brave shows a first-run/onboarding flow and starts background services that
# hang an automated launch. These skip all of it.
BRAVE_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-brave-update",
    "--disable-features=BraveRewards,BraveWallet,BraveVPN,BraveAIChat",
    "--disable-sync",
]


def is_brave(path: str | None) -> bool:
    return bool(path) and "brave" in path.lower()


def launch_profile(pw, headless: bool, browser_path: str | None):
    # Brave Origin's headless mode hangs (reproducible with a bare
    # `brave --headless=new --dump-dom`, so it is not a Playwright problem).
    # Headed works fine, so run Brave visible rather than not at all.
    if is_brave(browser_path):
        headless = False
    kwargs = {"headless": headless}
    if browser_path:
        kwargs["executable_path"] = browser_path
        kwargs["args"] = BRAVE_ARGS
        kwargs["ignore_default_args"] = ["--disable-component-update"]
    return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)

# Pick events carry an overall pick number and a player id together. Matching
# on that pairing finds them wherever they live, without knowing the schema.
PICK_KEYS = ("overallPickNumber", "playerId")


def find_picks(node, found: list[dict]) -> None:
    """Walk arbitrary JSON and collect anything shaped like a draft pick."""
    if isinstance(node, dict):
        if all(k in node for k in PICK_KEYS):
            try:
                overall = int(node["overallPickNumber"])
                pid = int(node["playerId"])
            except (TypeError, ValueError):
                overall = pid = 0
            if overall > 0 and pid not in (0, -1):
                found.append({
                    "overall": overall,
                    "player_id": pid,
                    "team_id": int(node.get("teamId") or node.get("toTeamId") or 0),
                })
        for v in node.values():
            find_picks(v, found)
    elif isinstance(node, list):
        for v in node:
            find_picks(v, found)


# ESPN's draft socket speaks a plain space-delimited text protocol, not JSON:
#
#   SELECTED <teamId> <playerId> <lineupSlotId> <memberGUID>   a pick
#   SELECTING <teamId> <millis>                                on the clock
#   CLOCK <n> <millis>                                         countdown
#   AUTOSUGGEST <playerId>                                     ESPN's hint
#   STATE <n> / PING / PONG                                    housekeeping
#
# Picks carry no overall pick number, so ordering comes from arrival order --
# which is exactly the order the draft happens in.
SELECTED_RE = re.compile(r"^SELECTED\s+(\d+)\s+(-?\d+)\s+(-?\d+)")
SELECTING_RE = re.compile(r"^SELECTING\s+(\d+)\s+(\d+)")


def parse_draft_frame(text: str) -> dict | None:
    """Decode one draft-socket frame. Returns a pick, a clock event, or None."""
    text = (text or "").strip()
    m = SELECTED_RE.match(text)
    if m:
        return {"kind": "pick", "team_id": int(m.group(1)),
                "player_id": int(m.group(2)), "slot_id": int(m.group(3))}
    m = SELECTING_RE.match(text)
    if m:
        return {"kind": "on_clock", "team_id": int(m.group(1)),
                "millis": int(m.group(2))}
    return None


def as_text(frame) -> str:
    """Websocket frames arrive as str or bytes; never silently drop the bytes."""
    if isinstance(frame, bytes):
        return frame.decode("utf-8", "replace")
    return frame if isinstance(frame, str) else ""


def maybe_json(text: str):
    text = (text or "").strip()
    if not text or text[0] not in "[{":
        # Some socket transports prefix frames with a numeric opcode.
        m = re.search(r"[\[{].*", text, re.S)
        if not m:
            return None
        text = m.group(0)
    try:
        return json.loads(text)
    except ValueError:
        return None


class PickSink:
    """Turns raw pick events into recorded picks, in order, without duplicates."""

    def __init__(self, b: DraftBoard, quiet: bool = False) -> None:
        self.b = b
        self.quiet = quiet
        self.by_id = b.board()["by_id"]
        state = b.draft_state()
        self.seen_overall = {p["overall"] for p in state["picks"]}
        self.seen_players = {p["player_id"] for p in state["picks"]}
        self.pending: dict[int, dict] = {}

    def offer(self, picks: list[dict]) -> int:
        added = 0
        for p in picks:
            # Socket picks arrive without an overall number; they are simply
            # the next pick, in arrival order.
            if not p.get("overall"):
                if p["player_id"] in self.seen_players:
                    continue
                nxt = len(self.seen_overall) + 1
                p = dict(p, overall=nxt)
            if p["overall"] in self.seen_overall or p["overall"] in self.pending:
                continue
            self.pending[p["overall"]] = p
        # Only commit a contiguous run, so out-of-order frames cannot scramble
        # the pick numbering the board depends on.
        while True:
            nxt = len(self.seen_overall) + 1
            p = self.pending.pop(nxt, None)
            if not p:
                break
            self.b.record_manual_picks([p["player_id"]],
                                       team_ids=[p.get("team_id")] if p.get("team_id") else None)
            self.seen_overall.add(nxt)
            self.seen_players.add(p["player_id"])
            added += 1
            player = self.by_id.get(p["player_id"])
            name = player["name"] if player else f"id:{p['player_id']}"
            pos = player["position"] if player else "?"
            mine = "  <== YOU" if p.get("team_id") == self.b.cfg.team_id else ""
            if not self.quiet:
                print(f"  #{nxt:>3} {name:<26} {pos:<5}{mine}", flush=True)
        return added


def discover_leagues(ctx) -> dict[str, str]:
    """Find the user's leagues and team ids from their own logged-in session.

    Reads the fantasy hub rather than an API: the "my leagues" endpoints
    return nothing useful, but the page itself links to every team with both
    ids in the query string.
    """
    page = ctx.new_page()
    try:
        page.goto("https://www.espn.com/fantasy/", wait_until="domcontentloaded",
                  timeout=45_000)
        page.wait_for_timeout(6000)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    finally:
        page.close()

    found: dict[str, str] = {}
    for h in hrefs:
        lg = re.search(r"leagueId=(\d+)", h)
        tm = re.search(r"teamId=(\d+)", h)
        if lg and tm:
            found.setdefault(lg.group(1), tm.group(1))
    return found


def pick_league(ctx, cfg) -> tuple[str, str] | None:
    """Choose which league to watch: a live draft first, else the soonest."""
    leagues = discover_leagues(ctx)
    if not leagues:
        print("No leagues found in your ESPN session.")
        return None

    import urllib.request

    best = None
    print("leagues in your account:")
    for lid, tid in leagues.items():
        name, when, live, done = lid, None, False, False
        try:
            url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
                   f"seasons/{cfg.season}/segments/0/leagues/{lid}"
                   f"?view=mSettings&view=mDraftDetail")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            d = d[0] if isinstance(d, list) and d else d
            st = (d.get("settings") or {})
            name = st.get("name") or lid
            when = ((st.get("draftSettings") or {}).get("date"))
            detail = d.get("draftDetail") or {}
            live = bool(detail.get("inProgress"))
            done = bool(detail.get("drafted"))
        except Exception:
            pass
        flag = "  <- DRAFT LIVE" if live else ("  (already drafted)" if done else "")
        print(f"  league {lid}  team {tid}  {name[:40]}{flag}")
        # A live draft wins outright; then the soonest draft still to come; a
        # finished draft is last, since watching it can never yield a pick.
        tier = 0 if live else (2 if done else 1)
        rank = (tier, when or float("inf"))
        if best is None or rank < best[0]:
            best = (rank, lid, tid, name)

    _, lid, tid, name = best
    print(f"\nusing: {name} (league {lid}, team {tid})")
    return lid, tid


def do_login(url: str, browser_path: str | None) -> int:
    """Open a visible browser so the user can log in once, then persist it."""
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening {browser_path or 'bundled Chromium'}.")
    print("Log in to ESPN, then return here.")
    print("The session is saved to state/browser-profile and reused later.\n")
    with sync_playwright() as pw:
        ctx = launch_profile(pw, headless=False, browser_path=browser_path)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        input("Press Enter here once you are logged in and can see the page... ")
        body = page.inner_text("body")[:200]
        ok = "Log in Required" not in body and "Log In" not in body[:60]
        print("session looks logged in" if ok else "still shows a login prompt")
        ctx.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="dump websocket/XHR traffic instead of publishing picks")
    ap.add_argument("--league", help="league id; auto-discovered with --attach")
    ap.add_argument("--team", type=int, help="your team id; auto-discovered too")
    ap.add_argument("--minutes", type=float, default=120.0)
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--browser", help="path to a Chromium-family browser "
                                      "(defaults to Brave if installed)")
    ap.add_argument("--chromium", action="store_true",
                    help="use Playwright's bundled Chromium instead of Brave; "
                         "the only way to run headless, since Brave's headless "
                         "mode hangs")
    ap.add_argument("--attach", metavar="PORT", nargs="?", const=9222, type=int,
                    help="attach to your already-running browser over CDP "
                         "instead of launching a fresh one. Uses your real "
                         "session, so no separate login is needed. Requires "
                         "Brave started with --remote-debugging-port=PORT.")
    ap.add_argument("--login", action="store_true",
                    help="open a visible browser to log in to ESPN once; the "
                         "session is saved and reused by later runs")
    args = ap.parse_args()

    if args.league:
        os.environ["ESPN_LEAGUE_ID"] = args.league
    if args.team:
        os.environ["ESPN_TEAM_ID"] = str(args.team)
    # A league id is only required up front when we cannot ask the browser.
    if args.attach and not args.league:
        os.environ.setdefault("ESPN_LEAGUE_ID", "0")
    cfg = load_config()

    if not cfg.has_auth:
        # Warn rather than refuse: the plumbing is worth exercising even
        # unauthenticated, and it is the page itself that decides what it will
        # serve. An unauthenticated run should show zero pick traffic.
        print("WARNING: ESPN_S2/SWID not set. The draft room needs a logged-in "
              "session, so expect no pick traffic. Continuing anyway.\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed:  pip install playwright && playwright install chromium")
        return 1

    url = DRAFT_URL.format(league=cfg.league_id, season=cfg.season,
                           team=cfg.team_id or 1, swid=cfg.swid or "")

    browser_path = None if args.chromium else find_browser(args.browser)
    if args.browser and not browser_path:
        print(f"No browser at {args.browser}")
        return 1

    if args.login:
        return do_login(url, browser_path)

    b = None if (args.attach and not args.league) else DraftBoard(cfg)
    sink = None if (args.discover or b is None) else PickSink(b)
    print(f"opening draft room for league {cfg.league_id if b else '(auto)'} "
          f"using {browser_path or 'bundled Chromium'} ...")
    if is_brave(browser_path):
        print("Brave runs visible (its headless mode hangs). "
              "Use --chromium for a headless run.")
    if not args.attach and not PROFILE_DIR.exists():
        print("No saved browser session. Run with --login first, "
              "or use --attach to drive your own browser.")
        return 1

    seen_frames = 0
    with sync_playwright() as pw:
        if args.attach:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.attach}")
            except Exception as exc:
                print(f"Could not attach on port {args.attach}: "
                      f"{type(exc).__name__}: {str(exc)[:120]}")
                print(f"Start Brave with --remote-debugging-port={args.attach} "
                      "(see README), or drop --attach to use a separate profile.")
                return 1
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            opened_page = False

            if not args.league:
                chosen = pick_league(ctx, cfg)
                if not chosen:
                    return 1
                lid, tid = chosen
                os.environ["ESPN_LEAGUE_ID"] = lid
                os.environ["ESPN_TEAM_ID"] = tid
                cfg = load_config()
                b = DraftBoard(cfg)
                sink = None if args.discover else PickSink(b)
                url = DRAFT_URL.format(league=cfg.league_id, season=cfg.season,
                                       team=cfg.team_id or 1, swid=cfg.swid or "")
            # Reuse a draft-room tab if one is already open, so the user's own
            # session and any in-progress room are picked up as-is.
            # Reuse a draft tab only if it is THIS league's room -- a stale tab
            # from another league would silently watch the wrong draft.
            want = f"leagueId={cfg.league_id}"
            page = next((pg for pg in ctx.pages
                         if "/football/draft" in pg.url and want in pg.url), None)
            if page:
                print(f"attached to your open draft tab for league {cfg.league_id}")
            else:
                page = ctx.new_page()
                opened_page = True
        else:
            ctx = launch_profile(pw, headless=not args.headed, browser_path=browser_path)
            browser = ctx
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Append as frames arrive. Writing only at exit means a Ctrl-C -- the
        # normal way to stop a watcher -- throws away the whole capture.
        log_path = Path(__file__).resolve().parents[1] / "state" / "discover.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", buffering=1) if args.discover else None
        raw_log = []

        def handle_payload(source: str, payload: str) -> None:
            nonlocal seen_frames
            found: list[dict] = []

            # The draft socket's text protocol is the live feed; JSON payloads
            # are the pre-draft snapshots.
            ev = parse_draft_frame(payload)
            if ev and ev["kind"] == "pick":
                found = [{"overall": 0, "player_id": ev["player_id"],
                          "team_id": ev["team_id"]}]
            elif ev and ev["kind"] == "on_clock" and not args.discover:
                mine = " <== YOU" if ev["team_id"] == (b.cfg.team_id if b else None) else ""
                print(f"  on the clock: team {ev['team_id']} "
                      f"({ev['millis'] // 1000}s){mine}", flush=True)
                return

            if not found:
                data = maybe_json(payload)
                if data is not None:
                    find_picks(data, found)

            if args.discover:
                # Log everything, not just what the matcher recognises -- an
                # unfamiliar schema is exactly what discovery needs to reveal,
                # and a silent run would teach nothing.
                raw_log.append((source, payload[:1500]))
                if log_fh:
                    log_fh.write(f"[{source}]\n{payload[:4000]}\n\n")
                if found:
                    seen_frames += 1
                    print(f"\n[MATCH {source}] {len(found)} picks: "
                          f"{json.dumps(found[:3])}", flush=True)
                elif payload.strip():
                    print(f"  [frame {source}] {payload[:160]}", flush=True)
                return

            if found:
                seen_frames += 1
                if sink:
                    sink.offer(found)

        def on_ws(ws):
            print(f"  websocket: {ws.url[:110]}")
            ws.on("framereceived", lambda f: handle_payload("ws", as_text(f)))
            ws.on("framesent", lambda f: handle_payload("ws-sent", as_text(f)))

        page.on("websocket", on_ws)

        # page.on("websocket") only fires for sockets opened AFTER attaching.
        # When joining a draft already in progress the socket is long since
        # open, so hook the CDP Network domain, which reports frames on
        # existing connections too.
        try:
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.on("Network.webSocketFrameReceived",
                   lambda e: handle_payload(
                       "cdp-ws", (e.get("response") or {}).get("payloadData", "")))
            cdp.on("Network.webSocketFrameSent",
                   lambda e: handle_payload(
                       "cdp-ws-sent", (e.get("response") or {}).get("payloadData", "")))
            cdp.on("Network.webSocketCreated",
                   lambda e: print(f"  cdp socket: {e.get('url','')[:100]}", flush=True))
            print("CDP network capture enabled (sees already-open sockets)")
        except Exception as exc:
            print(f"CDP capture unavailable: {type(exc).__name__}: {str(exc)[:80]}")

        def on_response(resp):
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct:
                return
            try:
                handle_payload(f"xhr {resp.url.split('?')[0][-60:]}", resp.text())
            except Exception:
                pass

        page.on("response", on_response)

        if "/football/draft" not in page.url:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print("draft room open. watching...\n")

        deadline = time.time() + args.minutes * 60
        try:
            while time.time() < deadline:
                page.wait_for_timeout(2000)
                if not args.discover and sink and b and len(sink.seen_overall) >= (
                        b.shape().teams * b.shape().roster_size):
                    print("\ndraft complete.")
                    break
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            if log_fh:
                log_fh.close()
                print(f"capture appended to {log_path}")

        if args.attach:
            # Never close the user's browser; only clean up a tab we opened.
            if opened_page:
                try:
                    page.close()
                except Exception:
                    pass
        else:
            browser.close()

    print(f"\npick-bearing payloads seen: {seen_frames}")
    if sink:
        print(f"picks recorded: {len(sink.seen_overall)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
