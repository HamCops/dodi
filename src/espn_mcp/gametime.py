"""Run the pre-game work by the kickoff clock, not the wall clock.

Decisions are best made late: inactives are announced 90 minutes before a
game, and news keeps arriving until then. But a decision the manager has to
approve is worthless if it reaches him with no time to answer. So each group
of games on the roster gets two runs, counted back from its kickoff:

    lead + 35 min   the agent looks for moves that need approval
                    (a replacement for an inactive starter, a trade);
                    every group of games gets one, because each has its
                    own inactives
    lead + 15 min   the lineup is set, and close calls are sent

where `lead` is the time the manager wants to decide in (30 minutes unless
configured). Requests expire 5 minutes before kickoff, so what is sent at
lead + 15 leaves him lead + 10.

Once a week there is a third run, tied to no kickoff. When a week ends,
every player on the roster has played and is locked: nobody can be dropped,
so no pickup can be made. ESPN unlocks them when it rolls over to the new
week, some time on Tuesday. The first tick to see that, in waking hours,
starts the agent on roster moves (`<hook> roster ...`), so claims reach the
manager as early as they can be made and he has until waivers run to
answer. A run at a fixed hour would either miss the rollover or wait on it.

`tick` is meant to be called every five minutes. It keeps the week's
kickoffs on disk and only talks to ESPN when that list is stale or a run is
due, so an idle tick costs nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

MIN = 60_000

# Kickoffs this close together are one decision (4:05 and 4:25 on a Sunday).
SLOT_WIDTH = 30 * MIN
AGENT_EXTRA = 35       # minutes before the approval lead that the agent runs
LINEUP_EXTRA = 15      # minutes before the approval lead that the lineup is set
EXPIRES_BEFORE = 5     # requests close this long before kickoff
TICK = 5               # how often tick() is expected to run, in minutes

REFRESH_IDLE = 6 * 3600     # re-read the schedule this often when nothing is near
REFRESH_NEAR = 30 * 60      # and this often inside three hours of a kickoff
REFRESH_WAITING = 20 * 60   # and this often when the week is over, watching for the next

# Roster moves are not urgent to the minute. Do not wake the manager for them.
WAKING_HOURS = (9, 21)


def slots_for(players: list[dict], now_ms: float) -> list[dict]:
    """The roster's upcoming kickoffs, grouped into decisions."""
    upcoming = sorted((p for p in players
                       if p.get("kickoff_ms") and p["kickoff_ms"] > now_ms
                       and p.get("slot_id") != 21),          # not players on IR
                      key=lambda p: p["kickoff_ms"])
    slots: list[dict] = []
    for p in upcoming:
        if slots and p["kickoff_ms"] - slots[-1]["kickoff_ms"] <= SLOT_WIDTH:
            slots[-1]["players"].append(p["name"])
        else:
            slots.append({"kickoff_ms": int(p["kickoff_ms"]), "players": [p["name"]]})
    return slots


def due(slot: dict, now_ms: float, lead: int) -> list[str]:
    """Which runs a slot is owed right now."""
    minutes = (slot["kickoff_ms"] - now_ms) / MIN
    out = []
    # The agent only in its own window: run late, it would ask for approval
    # with no time left to give it.
    if not slot.get("agent_done") \
            and lead + LINEUP_EXTRA < minutes <= lead + AGENT_EXTRA:
        out.append("agent")
    # The lineup whenever it is still owed: set late beats not set.
    if not slot.get("lineup_done") and EXPIRES_BEFORE < minutes <= lead + LINEUP_EXTRA:
        out.append("lineup")
    return out


class Schedule:
    """The week's kickoff slots and what has been done for each, on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    @property
    def slots(self) -> list[dict]:
        return self.data.get("slots") or []

    def stale(self, now: float) -> bool:
        if not self.data:
            return True
        age = now - self.data.get("fetched_at", 0)
        nearest = min((s["kickoff_ms"] / 1000 - now for s in self.slots
                       if s["kickoff_ms"] / 1000 > now), default=None)
        if nearest is None:
            return age > REFRESH_WAITING      # week over: the rollover is next
        near = nearest < 3 * 3600
        return age > (REFRESH_NEAR if near else REFRESH_IDLE)

    def roster_run_due(self, now: float, tz: str) -> bool:
        """The week has rolled over, the roster is unlocked, and nobody has
        yet been asked about roster moves for it."""
        if not self.slots or self.data.get("roster_run_week") == self.data.get("week"):
            return False
        if not any(s["kickoff_ms"] / 1000 > now for s in self.slots):
            return False
        hour = datetime.fromtimestamp(now, ZoneInfo(tz)).hour
        return WAKING_HOURS[0] <= hour < WAKING_HOURS[1]

    def replace(self, week: int, slots: list[dict], now: float) -> None:
        """Take a fresh list of slots, keeping what was already done."""
        same_week = self.data.get("week") == week
        done = {s["kickoff_ms"]: s for s in self.slots} if same_week else {}
        asked = self.data.get("roster_run_week")
        for s in slots:
            # A kickoff moved by a few minutes is still the same game.
            old = next((o for k, o in done.items() if abs(k - s["kickoff_ms"]) <= SLOT_WIDTH),
                       None)
            if old:
                s["agent_done"] = old.get("agent_done", False)
                s["lineup_done"] = old.get("lineup_done", False)
        self.data = {"week": week, "fetched_at": now, "slots": slots,
                     "roster_run_week": asked}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)


def tick(now: float | None = None, *, board_fn: Callable | None = None,
         run_lineup: Callable[[], int] | None = None,
         run_agent: Callable[[dict, str], bool] | None = None) -> list[str]:
    """One pass. Returns what it did, for the log."""
    now = time.time() if now is None else now
    if board_fn is None:
        from .server import board as board_fn
    b = board_fn()
    cfg = b.cfg
    sched = Schedule(cfg.state_root / f"gametime-{cfg.league_id}-{cfg.season}.json")
    did: list[str] = []
    failed: list[str] = []

    if sched.stale(now):
        if not cfg.team_id:
            return ["ESPN_TEAM_ID is not set; nothing to schedule."]
        _refresh(b, sched, now)

    if sched.roster_run_due(now, cfg.timezone):
        week = sched.data["week"]
        first = min(s["kickoff_ms"] for s in sched.slots)
        if not (run_agent or cfg.gametime_hook):
            sched.data["roster_run_week"] = week
            did.append(f"roster run for week {week}: no hook configured, skipped")
        elif (run_agent or _run_hook)({"kickoff_ms": first}, cfg.gametime_hook or "",
                                      "roster"):
            sched.data["roster_run_week"] = week
            did.append(f"roster run for week {week}: started (players unlocked)")
            # Set the week's lineup now too, so it is never left as last
            # week's until the first kickoff window. The per-kickoff runs
            # still reset it after inactives.
            if (run_lineup or _run_lineup)() == 0:
                did.append(f"lineup set for week {week} (players unlocked)")
            else:
                failed.append(f"lineup for week {week} on unlock failed; the kickoff "
                              "runs will set it")
        else:
            failed.append(f"roster run for week {week}: hook failed, will retry")
        sched.save()

    for slot in sched.slots:
        for job in due(slot, now * 1000, cfg.approval_lead_minutes):
            when = datetime.fromtimestamp(slot["kickoff_ms"] / 1000, ZoneInfo(cfg.timezone))
            label = when.strftime("%a %-I:%M %p")
            # A run that failed stays owed: the next tick tries again, for
            # as long as its window is open.
            if job == "agent":
                if not (run_agent or cfg.gametime_hook):
                    slot["agent_done"] = True
                    did.append(f"agent run for {label} kickoff: no hook configured, skipped")
                elif (run_agent or _run_hook)(slot, cfg.gametime_hook or ""):
                    slot["agent_done"] = True
                    did.append(f"agent run for {label} kickoff: started")
                else:
                    failed.append(f"agent run for {label} kickoff: hook failed, will retry")
            else:
                code = (run_lineup or _run_lineup)()
                if code == 0:
                    slot["lineup_done"] = True
                    did.append(f"lineup set for {label} kickoff")
                else:
                    failed.append(f"lineup for {label} kickoff failed (exit {code}), "
                                  "will retry")
            sched.save()   # after each job, so a crash cannot repeat it
    if failed:
        _alert("; ".join(failed))
    return did + failed


def _refresh(b, sched: "Schedule", now: float) -> None:
    """Re-read the week's kickoffs from ESPN. Runs nothing."""
    week = b.week()
    b.invalidate_rosters(week)
    sched.replace(week, slots_for(b.team_players(b.cfg.team_id, week), now * 1000), now)
    sched.save()


def _run_lineup() -> int:
    from .autolineup import run
    return run()


def _run_hook(slot: dict, hook: str, event: str = "agent") -> bool:
    """Hand the agent run to whatever drives the agent.

    The hook is called as: <hook> <event> <weekday> <kickoff, ISO 8601 UTC>,
    with the weekday in the manager's time zone. `event` is `agent` before a
    group of games and `roster` when a new week unlocks the roster (the
    kickoff is then the week's first). It should start the agent and
    return, not wait for it.
    """
    when = datetime.fromtimestamp(slot["kickoff_ms"] / 1000, ZoneInfo("UTC"))
    from .config import load_config
    day = when.astimezone(ZoneInfo(load_config().timezone)).strftime("%A").lower()
    try:
        subprocess.run([hook, event, day, when.strftime("%Y-%m-%dT%H:%M:%SZ")],
                       check=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"gametime hook failed: {exc}", file=sys.stderr)
        return False
    return True


def main() -> None:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(status())
        return
    try:
        for line in tick():
            print(time.strftime("%Y-%m-%d %H:%M:%S"), line)
    except Exception as exc:  # noqa: BLE001
        print(time.strftime("%Y-%m-%d %H:%M:%S"), f"tick failed: {type(exc).__name__}: {exc}")
        _alert(f"{type(exc).__name__}: {exc}")
        sys.exit(1)
    # Auto-apply what the policy allows, and chase what still waits on him.
    # After the tick, and never able to fail it.
    from .nudge import run as nudge
    for line in nudge():
        print(time.strftime("%Y-%m-%d %H:%M:%S"), line)
    # Look for news between scheduled runs; wakes Dodi only on real change.
    from .watch import run as watch
    try:
        for line in watch():
            print(time.strftime("%Y-%m-%d %H:%M:%S"), line)
    except Exception as exc:  # noqa: BLE001 - the watcher must never break the tick
        print(time.strftime("%Y-%m-%d %H:%M:%S"), f"watch failed: {type(exc).__name__}: {exc}")


def _alert(error: str) -> None:
    """Tell the manager the scheduler is broken, at most once an hour: if it
    stays broken, nothing sets the lineup."""
    from .config import load_config
    from .notify import push
    cfg = load_config()
    mark = cfg.state_root / "gametime-alerted"
    try:
        if mark.exists() and time.time() - mark.stat().st_mtime < 3600:
            return
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    except OSError:
        pass
    push(cfg, "Dodi: scheduler error",
         f"The game-time scheduler failed, so lineups are not being set.\n\n{error[:300]}",
         priority=4, tags=["warning"])


def status(now: float | None = None) -> str:
    """The week's plan, in the manager's time zone."""
    from .server import board
    now = time.time() if now is None else now
    cfg = board().cfg
    sched = Schedule(cfg.state_root / f"gametime-{cfg.league_id}-{cfg.season}.json")
    if sched.stale(now) and cfg.team_id:
        _refresh(board(), sched, now)      # looking must never set a lineup
    zone, lead = ZoneInfo(cfg.timezone), cfg.approval_lead_minutes
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, zone).strftime("%a %b %-d %-I:%M %p")  # noqa: E731
    lines = [f"Week {sched.data.get('week')}, times in {cfg.timezone}, "
             f"{lead} minutes to approve"]
    if not sched.slots:
        lines.append("Roster moves: every player has played and is locked. Waiting for "
                     "ESPN to roll over to the next week; checked every 20 minutes.")
    elif sched.data.get("roster_run_week") == sched.data.get("week"):
        lines.append("Roster moves: the agent was asked when this week unlocked.  done")
    else:
        lines.append(f"Roster moves: unlocked. The agent is asked at the next tick between "
                     f"{WAKING_HOURS[0]}:00 and {WAKING_HOURS[1]}:00.")
    for s in sched.slots:
        k = s["kickoff_ms"]
        lines.append(f"\nKickoff {fmt(k)}: {', '.join(s['players'])}")
        lines.append(f"  agent run     {fmt(k - (lead + AGENT_EXTRA) * MIN)}"
                     + ("  done" if s.get("agent_done") else ""))
        lines.append(f"  lineup set    {fmt(k - (lead + LINEUP_EXTRA) * MIN)}"
                     + ("  done" if s.get("lineup_done") else ""))
        lines.append(f"  approve by    {fmt(k - EXPIRES_BEFORE * MIN)}")
    if not sched.slots:
        lines.append("No games left this week.")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
