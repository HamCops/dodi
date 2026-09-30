"""Watch for news between Dodi's scheduled runs, cheaply.

Runs after every scheduler tick (five minutes). No model is called here: it
reads ESPN and the cached outside data, compares with what it saw last time,
and only when something that matters has changed does it wake Dodi, through
the same hook as the game-time runs.

What counts as news:
  - a player of mine changes injury status (ACTIVE -> QUESTIONABLE -> OUT ...)
  - a starter of mine becomes OUT, DOUBTFUL, IR or SUSPENSION
  - an unrostered player starts trending hard across Sleeper leagues
    (TRENDING_ADDS in a day) and was not trending last time
  - a new stash move appears that was not there last time (a dead spot or
    a clear bench upgrade), e.g. after a waiver run frees a claim
  - a trade offer arrives, or one of mine is answered

Waking the agent costs money and pings the manager, so:
  - at most one wake per WAKE_GAP, unless a starter is newly out
  - never inside the window where the game-time agent run is about to fire
  - quiet 23:00-08:00 Eastern except for a starter newly out before a game
The snapshot is saved every run, so an event is reported once, not every tick.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

OUT_STATUSES = {"OUT", "DOUBTFUL", "INJURY_RESERVE", "SUSPENSION"}
TRENDING_ADDS = 500_000
WAKE_GAP = 3 * 3600
QUIET = (23, 8)


def snapshot(b) -> dict:
    """What the watcher compares from one run to the next. Pure reads."""
    from .season import ROS_KEY, WEEK_KEY, optimal_lineup
    from .server import _pending_proposals, _stash_repl
    from .stash import stash_moves

    week = b.week()
    shape = b.shape()
    me = b.cfg.team_id
    mine = b.team_players(me, week)
    team = b.league_rosters(week).get(me) or {}
    starters = {p["player_id"] for _, p in optimal_lineup(mine, shape, WEEK_KEY)["starters"] if p}
    ros_starters = {p["player_id"] for _, p in optimal_lineup(mine, shape, ROS_KEY)["starters"] if p}
    avail = b.season_available(week)
    moves = stash_moves(mine, avail, starters | ros_starters, _stash_repl(b, shape, week),
                        set(team.get("pending_add_ids") or []),
                        set(team.get("pending_drop_ids") or []))
    return {
        "week": week,
        "injury": {str(p["player_id"]): (p.get("injury_status") or "ACTIVE") for p in mine},
        "names": {str(p["player_id"]): p["name"] for p in mine},
        "starters": sorted(starters),
        "trending": sorted(p["name"] for p in avail if (p.get("adds_24h") or 0) >= TRENDING_ADDS),
        "stash": sorted(f"{m['add']} for {m['drop']}" for m in moves),
        "trades": sorted(str(t.get("id")) for t in _pending_proposals(b, me)),
    }


def diff(old: dict, new: dict) -> list[dict]:
    """Events between two snapshots. Each: {kind, text, urgent}."""
    if not old or old.get("week") != new.get("week"):
        return []            # first look, or a new week: the roster run covers it
    events = []
    starters = set(new["starters"])
    for pid, status in new["injury"].items():
        before = old["injury"].get(pid)
        if before is None or before == status:
            continue
        name = new["names"].get(pid, pid)
        urgent = status in OUT_STATUSES and int(pid) in starters
        events.append({"kind": "injury", "urgent": urgent,
                       "text": f"{name}: {before} -> {status}"
                               + (" (starter)" if int(pid) in starters else "")})
    for name in sorted(set(new["trending"]) - set(old["trending"])):
        events.append({"kind": "trending", "urgent": False,
                       "text": f"{name} is trending on waivers"})
    for move in sorted(set(new["stash"]) - set(old["stash"])):
        events.append({"kind": "stash", "urgent": False, "text": f"New stash move: {move}"})
    for tid in sorted(set(new["trades"]) - set(old["trades"])):
        events.append({"kind": "trade", "urgent": False, "text": "New trade offer"})
    for tid in sorted(set(old["trades"]) - set(new["trades"])):
        events.append({"kind": "trade", "urgent": False, "text": "A trade offer was answered"})
    return events


def should_wake(events: list[dict], state: dict, now: float, tz: str,
                agent_due_soon: bool) -> bool:
    if not events:
        return False
    urgent = any(e["urgent"] for e in events)
    if agent_due_soon:
        return False                       # the game-time run will see it anyway
    hour = datetime.fromtimestamp(now, ZoneInfo(tz)).hour
    quiet = hour >= QUIET[0] or hour < QUIET[1]
    if quiet and not urgent:
        return False
    if not urgent and now - state.get("last_wake", 0) < WAKE_GAP:
        return False
    return True


def _agent_due_soon(b, now: float) -> bool:
    """True if a game-time agent run fires in the next 40 minutes."""
    from .gametime import AGENT_EXTRA, MIN, Schedule
    sched = Schedule(b.cfg.state_root / f"gametime-{b.cfg.league_id}-{b.cfg.season}.json")
    lead = b.cfg.approval_lead_minutes
    for s in sched.slots:
        if s.get("agent_done"):
            continue
        run_at = s["kickoff_ms"] - (lead + AGENT_EXTRA) * MIN
        if 0 <= run_at - now * 1000 <= 40 * MIN:
            return True
    return False


def run(now: float | None = None, *, board_fn: Callable | None = None,
        wake: Callable[[list[dict]], bool] | None = None) -> list[str]:
    """One watcher pass. Returns log lines."""
    now = time.time() if now is None else now
    if board_fn is None:
        from .server import board as board_fn
    b = board_fn()
    path = Path(b.cfg.state_root) / f"watch-{b.cfg.league_id}-{b.cfg.season}.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    new = snapshot(b)
    events = diff(state.get("snapshot") or {}, new)
    lines = [f"watch: {e['text']}" for e in events]
    if events:
        state["last_events"] = events
        state["last_events_at"] = now
    if should_wake(events, state, now, b.cfg.timezone, _agent_due_soon(b, now)):
        if (wake or _wake)(events):
            state["last_wake"] = now
            lines.append(f"watch: woke Dodi ({len(events)} event(s))")
        else:
            lines.append("watch: wake failed; will report again on the next change")
    state["snapshot"] = new
    state["checked_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)
    return lines


def _wake(events: list[dict]) -> bool:
    """Start the watch agent run through the game-time hook: `<hook> watch <events>`."""
    import subprocess
    import sys
    from .config import load_config
    hook = load_config().gametime_hook
    if not hook:
        return False
    summary = "; ".join(e["text"] for e in events)[:900]
    try:
        subprocess.run([hook, "watch", summary], check=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"watch hook failed: {exc}", file=sys.stderr)
        return False
    return True
