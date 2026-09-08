#!/usr/bin/env python3
"""Watch the live draft room and make your picks for you.

Scoring goes beyond "highest value left". A pick is worth making now only if
the alternative -- waiting -- actually costs something, so the score combines:

  * VORP, value over the last startable player at that position
  * whether the player fills a starting slot you have not filled
  * tier survival: how likely this tier is to be gone by your next turn,
    weighted by the size of the cliff behind it
  * a hard block on K/D-ST while any real starter slot is open

Availability is reconciled from the room's own Pick History, which is complete
and authoritative. Pick state is kept in a private directory so this never
races the socket watcher writing the shared one.

    python scripts/autodraft.py --league 123456789 --team 3
    python scripts/autodraft.py --league 123456789 --team 3 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_from_room import HISTORY_JS, parse_history  # noqa: E402

READ_POOL_JS = r"""() => {
  const bad = /autopick|on the clock/i, out = [];
  for (const bt of document.querySelectorAll('button[class*="Button--draft"]')) {
    let r = bt.parentElement;
    for (let i = 0; i < 8 && r; i++) {
      const raw = (r.innerText || '').trim();
      if (raw.length > 10 && raw.length < 160 && !bad.test(raw)) {
        out.push(raw.replace(/\n+/g, '|'));
        break;
      }
      r = r.parentElement;
    }
  }
  return out;
}"""

MY_TURN_JS = ("() => /YOUR PICK|YOU'RE ON THE CLOCK|You are on the clock/i"
              ".test(document.body.innerText)")

ROSTER_JS = """() => [...document.querySelectorAll('tr')]
  .map(r => (r.innerText || '').replace(/\\t/g, ' ').trim())
  .filter(t => /^(QB|RB|WR|TE|FLEX|D\\/ST|K|BE)\\b/.test(t))
  .map(t => t.replace(/\\n+/g, ' | '))"""

TAG_BUTTON_JS = """(want) => {
  const norm = s => (s||'').toLowerCase().replace(/[^a-z ]/g,'');
  const bad = /autopick|on the clock/i;
  document.querySelectorAll('[data-pick-target]')
          .forEach(e => e.removeAttribute('data-pick-target'));
  for (const bt of document.querySelectorAll('button[class*="Button--draft"]')) {
    let r = bt.parentElement;
    for (let i = 0; i < 8 && r; i++) {
      const raw = (r.innerText || '').trim();
      if (raw.length > 10 && raw.length < 160 && !bad.test(raw)
          && norm(raw).includes(norm(want))) {
        bt.setAttribute('data-pick-target', '1');
        return raw.replace(/\\n+/g, ' | ').slice(0, 80);
      }
      r = r.parentElement;
    }
  }
  return null;
}"""


def parse_pool(rows: list[str]) -> list[dict]:
    """Rows look like '82|Trevor Lawrence|JAX|QB|DRAFT'."""
    out = []
    for raw in rows:
        parts = [x.strip() for x in raw.split("|") if x.strip()]
        if len(parts) < 3:
            continue
        rank = parts[0] if parts[0].isdigit() else None
        name = parts[1] if rank else parts[0]
        pos = next((x.upper() for x in parts
                    if x.upper() in ("QB", "RB", "WR", "TE", "K", "D/ST")), None)
        if name and pos:
            out.append({"name": name, "position": pos,
                        "espn_rank": int(rank) if rank else 9999})
    return out


SET_POS_FILTER_JS = """(label) => {
  const sel = [...document.querySelectorAll('select')].find(
      s => [...s.options].some(o => o.text.trim() === 'All Pos.'));
  if (!sel) return false;
  const opt = [...sel.options].find(o => o.text.trim() === label);
  if (!opt) return false;
  sel.value = opt.value;
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""


def read_pool_for(page, label: str) -> list[dict]:
    """Read the rendered pool with the room's position filter applied.

    ESPN renders only a handful of rows, ordered by its own ranking, so a
    position you need can be entirely invisible -- which is how a roster ends
    up with four quarterbacks and no running back. Asking per position makes
    the best of each visible.
    """
    if not page.evaluate(SET_POS_FILTER_JS, label):
        return []
    page.wait_for_timeout(900)
    return parse_pool(page.evaluate(READ_POOL_JS))


def unfilled_from_roster(rows: list[str]) -> set[str]:
    need = set()
    for r in rows:
        m = re.match(r"^(QB|RB|WR|TE|FLEX|D/ST|K)\b", r, re.I)
        if m and "Empty" in r:
            need.add(m.group(1).upper())
    return need


def reconcile(page, srv, label: str) -> int:
    """Rebuild the board from the room's complete Pick History."""
    tabs = page.evaluate("""() => {
        const f = t => [...document.querySelectorAll('button')]
            .find(e => (e.innerText||'').trim().toLowerCase() === t);
        const h = f('pick history'); if (h) { h.click(); return true; } return false;
    }""")
    if not tabs:
        return 0
    page.wait_for_timeout(2000)
    lines = page.evaluate(HISTORY_JS)
    page.evaluate("""() => {
        const p = [...document.querySelectorAll('button')]
            .find(e => (e.innerText||'').trim().toLowerCase() === 'players');
        if (p) p.click();
    }""")
    page.wait_for_timeout(800)

    picks = sorted(parse_history(lines), key=lambda p: p["overall"])
    if not picks:
        return 0
    b = srv.board()
    b.clear_manual_picks()
    res = srv.record_picks.__wrapped__(players=[p["name"] for p in picks])
    miss = res.get("needs_attention") or []
    print(f"[{label}] room reports {len(picks)} picks; board synced"
          + (f" ({len(miss)} unmatched)" if miss else ""))
    return len(picks)


def picks_until_next_turn(b, team_id: int) -> int:
    """How many players come off the board before you choose again.

    This is what decides whether waiting is affordable. At a snake turn it is
    near zero; mid-round it can be most of a lap.
    """
    state = b.draft_state()
    upcoming = b.upcoming_picks_for(team_id, count=2, state=state)
    nxt = state.get("next_overall_pick") or 0
    if len(upcoming) >= 2:
        return max(0, upcoming[1] - upcoming[0] - 1)
    teams = b.shape().teams or 10
    return teams  # fall back to one lap
        

def roster_counts(rows: list[str]) -> dict[str, int]:
    """How many of each position you already hold, from the room's roster."""
    counts: dict[str, int] = {}
    for r in rows:
        m = re.match(r"^(QB|RB|WR|TE|FLEX|D/ST|K|BE)\b", r, re.I)
        if not m or "Empty" in r:
            continue
        # Bench and FLEX rows label the slot, not the player, and tag the real
        # position in parentheses: "BE | A. Brown | (WR) | 11".
        paren = re.search(r"\((QB|RB|WR|TE|K|D/ST)\)", r, re.I)
        slot = m.group(1).upper()
        if paren:
            pos = paren.group(1).upper()
        elif slot in ("BE", "FLEX"):
            pos = None          # unknown occupant; do not guess
        else:
            pos = slot
        if pos:
            counts[pos] = counts.get(pos, 0) + 1
    return counts


# Statuses worth acting on. In August most players cycle through
# QUESTIONABLE and it means almost nothing, so it is barely penalised; the
# ones that end a season are not.
INJURY_PENALTY = {
    "OUT": 60.0, "INJURY_RESERVE": 90.0, "SUSPENSION": 70.0,
    "DOUBTFUL": 25.0, "QUESTIONABLE": 3.0, "PUP": 45.0,
}

# How much a bench player at each position is actually worth carrying. This
# is not derivable from slot counts: a backup QB is near-worthless because
# quarterbacks are streamable off waivers all season, while RB and WR depth is
# genuinely scarce and starts the moment anyone is hurt or on bye.
BENCH_POS_VALUE = {
    "RB": 1.00, "WR": 0.85, "TE": 0.25,
    "QB": 0.10, "K": 0.02, "D/ST": 0.02,
}

NOTES_PATH = Path(__file__).resolve().parents[1] / "state" / "notes.json"
CONSENSUS_PATH = Path(__file__).resolve().parents[1] / "state" / "consensus_standard.txt"

# Optional pull toward outside consensus, off by default.
#
# Tried as a hedge against our single projection source, and measured: it made
# results worse at every weight (external placing 4.75 -> 6.25 as weight went
# 0 -> 0.5). Blending makes us draft like the ADP-following field, and since
# they pick by ADP they win those races -- we became a worse copy of them
# instead of a differentiated team. Kept as a knob, defaulted off.
CONSENSUS_WEIGHT = float(os.environ.get("ESPN_CONSENSUS_WEIGHT", "0"))


def load_consensus() -> dict[str, int]:
    try:
        if CONSENSUS_PATH.is_file():
            out, i = {}, 0
            for line in CONSENSUS_PATH.read_text().splitlines():
                n = line.strip()
                if n and not n.startswith("#"):
                    i += 1
                    out[n.lower()] = i
            return out
    except OSError:
        pass
    return {}


def load_notes() -> dict:
    """Hand-entered intel: {"Player Name": {"adjust": -40, "note": "holdout"}}.

    Projections lag the things that actually sink a draft pick -- holdouts,
    committee backfields, a coach naming someone else the starter. This is
    where that knowledge goes, and it overrides everything.
    """
    try:
        if NOTES_PATH.is_file():
            return json.loads(NOTES_PATH.read_text()) or {}
    except (OSError, ValueError):
        pass
    return {}


def roster_byes(rows: list[str]) -> dict[str, list[int]]:
    """Position -> bye weeks of the players you already hold.

    The roster panel prints the bye in the last cell, e.g.
    "QB |  | J. Daniels |  | 7".
    """
    out: dict[str, list[int]] = {}
    for r in rows:
        if "Empty" in r:
            continue
        m = re.match(r"^(QB|RB|WR|TE|FLEX|D/ST|K|BE)\b", r, re.I)
        if not m:
            continue
        paren = re.search(r"\((QB|RB|WR|TE|K|D/ST)\)", r, re.I)
        slot = m.group(1).upper()
        pos = paren.group(1).upper() if paren else (
            None if slot in ("BE", "FLEX") else slot)
        bye = re.search(r"\|\s*(\d{1,2})\s*$", r)
        if pos and bye:
            out.setdefault(pos, []).append(int(bye.group(1)))
    return out


def my_players(b, rows: list[str]) -> list[dict]:
    """Resolve the roster panel's abbreviated names against the board."""
    out = []
    for r in rows:
        if "Empty" in r:
            continue
        parts = [x.strip() for x in r.split("|") if x.strip()]
        # "RB |  | J. Taylor |  | 13" -> the name cell is the one with a dot
        name = next((x for x in parts if "." in x and len(x) > 3), None)
        if not name:
            continue
        surname = name.split(".")[-1].strip()
        hits = b.find_players(surname, limit=4)
        if hits:
            out.append(hits[0])
    return out


def build_scorer(b, need: set[str], gap: int, have: dict[str, int] | None = None,
                 byes: dict[str, list[int]] | None = None,
                 notes: dict | None = None, roster: list[dict] | None = None):
    """Score = value now, plus what waiting would cost.

    VORP assumes the player starts. Once a position's startable slots are
    full, the next one is a bench player and worth a fraction of his VORP --
    without this the scorer happily drafts six wide receivers.
    """
    have = have or {}
    byes = byes or {}
    notes = notes or {}
    note_by_name = {k.lower(): v for k, v in notes.items()}
    # Pro teams of the running backs you already start. Their backup inherits
    # the whole workload on an injury, which is the one bench player whose
    # value spikes precisely when you need it.
    roster = roster or []
    # Map consensus rank onto our own value scale: the player the field ranks
    # 10th is worth what our 10th-best player is worth. That makes the two
    # comparable without pretending consensus publishes projections.
    consensus = load_consensus()
    scale = sorted((p["vorp"] for p in b.available(limit=400)
                    if p.get("vorp") is not None), reverse=True)

    def consensus_value(name: str):
        r = consensus.get((name or "").lower())
        if not r or not scale:
            return None
        return scale[min(r - 1, len(scale) - 1)]

    all_byes = [r["bye_week"] for r in roster if r.get("bye_week")]
    # NFL team -> the value of the best running back you start there. A
    # handcuff is only worth what it replaces: the backup to a workhorse
    # inherits a startable role, the backup to your RB3 inherits a committee.
    my_rb_value: dict[str, float] = {}
    for r in roster:
        if r.get("position") != "RB":
            continue
        team = r.get("pro_team")
        v = r.get("vorp")
        if team and v is not None:
            my_rb_value[team] = max(my_rb_value.get(team, 0.0), float(v))
    shape = b.shape()
    startable = dict(shape.starters_by_position)
    for eligible, count in shape.flex_slots.items():
        for pos in eligible:
            startable[pos] = startable.get(pos, 0) + count  # flex is shared
    pool = b.available(limit=600)
    by_pos: dict[str, list[dict]] = {}
    for p in pool:
        by_pos.setdefault(p["position"], []).append(p)

    def tier_context(p: dict) -> tuple[int, float]:
        if p.get("tier") is None:
            return 99, 0.0
        """(players left in this player's tier, VORP drop to the next tier)"""
        same = [q for q in by_pos.get(p["position"], [])
                if q.get("tier") == p.get("tier")]
        nxt = [q for q in by_pos.get(p["position"], [])
               if (q.get("tier") or 0) == (p.get("tier") or 0) + 1]
        # `same` is empty when the candidate is no longer in the available
        # pool -- taking min() of that raised and killed the pick.
        if (not same or not nxt or p.get("vorp") is None
                or nxt[0].get("vorp") is None):
            return len(same), 0.0
        floor = min(q["vorp"] for q in same if q.get("vorp") is not None)
        return len(same), max(0.0, floor - nxt[0]["vorp"])

    def score(p: dict) -> float:
        pos = p["position"]
        if p.get("vorp") is not None:
            base = p["vorp"]
        else:
            # Some leagues publish no projections at all for a position --
            # D/ST here. Scoring those as -200 makes them literally undraftable
            # even when a starting slot demands one, so rank them among
            # themselves by ADP on a neutral scale instead.
            adp = p.get("espn_adp") or 300.0
            base = -min(40.0, adp / 8.0)

        # Blend toward outside consensus before anything else keys off value.
        if CONSENSUS_WEIGHT > 0 and p.get("vorp") is not None:
            cv = consensus_value(p.get("name"))
            if cv is not None:
                base = (1 - CONSENSUS_WEIGHT) * base + CONSENSUS_WEIGHT * cv

        # A starting slot has to be filled by someone. A negative-VORP player
        # you are forced to start is still worth more than a backup who can
        # never enter the lineup.
        owned = have.get(pos, 0)
        cap = startable.get(pos, 1)

        fills = pos in need or ("FLEX" in need and pos in ("RB", "WR", "TE"))
        late = pos in ("K", "D/ST")
        # Never spend a pick on K/DST while a real starter slot is open.
        penalty = 500.0 if late and (need - {"K", "D/ST"}) else 0.0

        # Every player is one of two things, and they are scored on different
        # scales, so mixing them is what produced both earlier failures --
        # flooring starters by position floated bad RBs over good WRs, and
        # not flooring them let a junk kicker outscore a poor WR.
        #
        #   fills a starting slot -> judged on VORP, the value of starting him
        #   otherwise             -> judged as insurance, by position and depth
        if fills:
            pass                      # keep VORP; the need bonus is added below
        else:
            weight = BENCH_POS_VALUE.get(pos, 0.3) * (0.6 ** max(0, owned - cap))
            base = 60.0 * weight + 0.15 * base

        # Injury status, straight from ESPN.
        base -= INJURY_PENALTY.get((p.get("injury_status") or "").upper(), 0.0)

        # A sliding ADP means the room has heard something the projection has
        # not absorbed yet. Treat a sharp slide as a warning, not noise.
        if p.get("adp_moving") == "later":
            base -= min(30.0, (p.get("adp_change_pct") or 0.0) * 8.0)

        # Bye weeks. Additive, because a multiplier flips sign on negatives.
        held = byes.get(pos, [])
        cand_bye = p.get("bye_week")
        if cand_bye:
            if held and owned >= cap and cand_bye in held:
                base -= 12.0        # a backup who is off the same week covers nothing
            # Stacking across the whole roster: a week with four idle starters
            # loses that matchup outright. Starters are scored on raw VORP,
            # where gaps run past 50, so this has to grow quadratically to
            # ever outweigh them.
            same_week = sum(1 for v in all_byes if v == cand_bye)
            if same_week >= 2:
                base -= 14.0 * (same_week - 1) ** 2

        # Handcuff bonus: a backup on the same NFL team as one of your
        # starting RBs. Only for RB, where the workload transfers wholesale
        # instead of being split, and only once the bench phase is reached.
        # Scaled by the starter's value and capped, so handcuffing a stud is
        # worth real points and handcuffing a fringe back is worth almost none.
        if owned >= cap and pos == "RB":
            starter_vorp = my_rb_value.get(p.get("pro_team"), 0.0)
            if starter_vorp > 0:
                # Only the man behind him -- not a better back on the same team.
                if (p.get("vorp") or -999) < starter_vorp:
                    base += max(0.0, min(40.0, starter_vorp * 0.20))

        # Hand-entered intel wins over everything above.
        hit = note_by_name.get((p.get("name") or "").lower())
        if hit:
            base += float(hit.get("adjust", 0))

        # Urgency: the share of this tier's cliff you forfeit by waiting. A
        # tier deeper than the gap survives to your next pick and costs
        # nothing; a thinner one means you eat the drop behind it.
        left, cliff = tier_context(p)
        exposure = 1.0 - min(1.0, left / max(1, gap))
        urgency = cliff * exposure

        need_bonus = 150.0 if fills else 0.0
        return base + need_bonus + urgency - penalty

    return score, tier_context


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--team", type=int, required=True)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--minutes", type=float, default=90.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.environ["ESPN_LEAGUE_ID"] = args.league
    os.environ["ESPN_TEAM_ID"] = str(args.team)
    # Private state: live_draft.py owns the shared file, and two writers race.
    os.environ["ESPN_STATE_DIR"] = tempfile.mkdtemp(prefix="autodraft-")
    import espn_mcp.server as srv

    srv.board().board()  # warm the pool before the clock matters

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = br.contexts[0]
        page = next((p for p in ctx.pages
                     if f"leagueId={args.league}" in p.url and "/draft" in p.url), None)
        if not page:
            print("No draft tab open for that league.")
            return 1

        reconcile(page, srv, "startup")
        print(f"autodraft armed for team {args.team}"
              + (" (dry run)" if args.dry_run else "") + ". Ctrl-C to stop.\n")

        deadline = time.time() + args.minutes * 60
        made = 0
        last_sync = 0.0
        while time.time() < deadline:
            try:
                if not page.evaluate(MY_TURN_JS):
                    # Resync between turns, never on the clock.
                    if time.time() - last_sync > 45:
                        reconcile(page, srv, "sync")
                        last_sync = time.time()
                    time.sleep(2)
                    continue

                # The room is the only reliable source of who can still be
                # drafted -- a DRAFT button exists for undrafted players only.
                # The board supplies VORP and tier shape for those names.
                b = srv.board()
                roster_rows = page.evaluate(ROSTER_JS)
                need = unfilled_from_roster(roster_rows)
                have = roster_counts(roster_rows)
                byes = roster_byes(roster_rows)
                gap = picks_until_next_turn(b, args.team)
                score, tier_context = build_scorer(
                    b, need, gap, have, byes, load_notes(),
                    roster=my_players(b, roster_rows))

                # Look at the overall board plus the best of every position
                # still missing a starter, so a needed slot is never invisible.
                # Always ask for RB/WR/TE even when their starting slots are
                # full: that is where bench value and handcuffs live, and if
                # they are absent from the candidate list the scorer cannot
                # choose them however highly it would rate them.
                wanted = ["All Pos.", "RB", "WR", "TE"] + [
                    x for x in ("QB", "D/ST", "K") if x in need
                ]
                pool, seen_names = [], set()
                for label in wanted:
                    for c in read_pool_for(page, label):
                        if c["name"] not in seen_names:
                            seen_names.add(c["name"])
                            pool.append(c)
                page.evaluate(SET_POS_FILTER_JS, "All Pos.")
                page.wait_for_timeout(600)
                if not pool:
                    time.sleep(1)
                    continue

                for c in pool:
                    m = b.find_players(c["name"], limit=1)
                    src = m[0] if m else {}
                    c["vorp"] = src.get("vorp")
                    c["tier"] = src.get("tier")
                ranked = sorted(pool, key=score, reverse=True)
                print(f"MY TURN. need={sorted(need)} have={dict(sorted(have.items()))} "
                      f"| {gap} picks until my next")
                for p in ranked[:4]:
                    l, c = tier_context(p)
                    print(f"    {p['name']:<22} {p['position']:<4} vorp {p['vorp']:>7} "
                          f"tier {p.get('tier')} left {l:<3} cliff {c:.1f} "
                          f"score {score(p):.1f}")

                if args.dry_run:
                    time.sleep(6)
                    continue

                # Search so ESPN renders the row, then click that row's button.
                for cand in ranked:
                    row = page.evaluate(TAG_BUTTON_JS, cand["name"])
                    if not row:
                        print(f"  [skip] {cand['name']} not draftable in room")
                        continue
                    page.click('button[data-pick-target="1"]', timeout=5000)
                    page.wait_for_timeout(1000)
                    for lbl in ("Confirm", "Yes", "Draft Player"):
                        loc = page.get_by_role("button", name=re.compile(f"^{lbl}$", re.I))
                        if loc.count() and loc.first.is_visible():
                            loc.first.click()
                            break
                    made += 1
                    print(f"  DRAFTED {cand['name']} ({cand['position']})  total {made}")
                    page.wait_for_timeout(3000)
                    last_sync = 0.0  # resync on the next idle tick
                    break
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f"  [warn] {type(exc).__name__}: {str(exc)[:100]}")
                time.sleep(2)

    print(f"\ndone. picks made: {made}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
