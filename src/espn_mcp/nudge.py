"""Chase the manager, a little. And make the moves he no longer has to make.

Run from the five-minute scheduler tick (gametime.main), after tick():

  sweep     With ESPN_AUTO_APPLY on, any pending proposal the auto policy now
            allows is applied. Covers moves queued before auto-apply was
            turned on, or queued by an older server.
  remind    A proposal still undecided REMIND_AFTER_MINUTES after it was sent
            is pushed again, once, at high priority. When little time is
            left, one last call goes out. Nothing between 23:00 and 08:00 in
            his time zone, except that a proposal which would expire in that
            window gets its last call before 23:00. At most MAX_PER_HOUR
            reminder pushes an hour, across all proposals.

Two nudges per proposal at most. Needy, not relentless.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MAX_PER_HOUR = 3
LAST_CALL_MINUTES = 45
QUIET = (23, 8)          # hours, local: from 23:00 to 08:00 nobody is chased


def _quiet(dt: datetime) -> bool:
    return dt.hour >= QUIET[0] or dt.hour < QUIET[1]


def _quiet_starts(dt: datetime) -> datetime:
    """The start of the next quiet window at or after `dt`."""
    start = dt.replace(hour=QUIET[0], minute=0, second=0, microsecond=0)
    return start if start >= dt else start + timedelta(days=1)


def due_nudge(p: dict, now: float, tz: str, remind_after_min: int) -> str | None:
    """'reminded', 'last_call' or None for one pending proposal."""
    zone = ZoneInfo(tz)
    here = datetime.fromtimestamp(now, zone)
    if _quiet(here) or p["status"] != "pending":
        return None
    left_min = (p["expires_at"] - now) / 60
    if left_min <= 0:
        return None
    expires = datetime.fromtimestamp(p["expires_at"], zone)
    # Would it run out while he is asleep, with no waking hour before then?
    lapses_overnight = expires >= _quiet_starts(here) and \
        (expires - _quiet_starts(here)) < timedelta(hours=24 - QUIET[0] + QUIET[1])
    closing = left_min <= LAST_CALL_MINUTES or (
        lapses_overnight and _quiet_starts(here) - here <= timedelta(minutes=60))
    if closing and not p.get("last_call_at"):
        # Never two pushes about one proposal inside 15 minutes.
        last = max(p.get("reminded_at") or 0, p["created_at"])
        if now - last >= 15 * 60:
            return "last_call"
        return None
    if not p.get("reminded_at") and not p.get("last_call_at") \
            and now - p["created_at"] >= remind_after_min * 60:
        return "reminded"
    return None


def remind(store, cfg, now: float | None = None,
           push_fn: Callable | None = None) -> list[str]:
    """Push the reminders that are due. Returns what was sent, for the log."""
    from .notify import push_proposal
    push_fn = push_fn or push_proposal
    now = time.time() if now is None else now
    budget = MAX_PER_HOUR - store.reminders_since(now - 3600)
    sent: list[str] = []
    # Soonest deadline first: if the hourly budget runs out, the urgent go.
    for p in sorted(store.list("pending", limit=50, now=now), key=lambda p: p["expires_at"]):
        if budget <= 0:
            break
        kind = due_nudge(p, now, cfg.timezone, cfg.remind_after_minutes)
        if not kind:
            continue
        prefix = "LAST CALL: " if kind == "last_call" else "Still waiting: "
        result = push_fn(cfg, {**p, "title": prefix + p["title"]})
        if result.get("sent"):
            store.mark_reminded(p["id"], kind, now)
            budget -= 1
            sent.append(f"{kind} {p['id']} {p['title']}")
    return sent


def sweep(now: float | None = None) -> list[str]:
    """Apply every pending proposal the auto policy allows today."""
    from .autopolicy import auto_ok
    from .server import _auto_apply, _writers, board, proposal_store
    if not board().cfg.auto_apply:
        return []
    done: list[str] = []
    for p in proposal_store().list("pending", limit=50, now=now):
        preview = _writers()[p["action"]](**p["params"], apply=False)
        if "error" in preview:
            continue
        ok, why = auto_ok(p["action"], p["params"], preview)
        if ok:
            out = _auto_apply(p, why)
            done.append(f"auto {p['id']} {p['title']}: {out['status']}")
    return done


def run(now: float | None = None) -> list[str]:
    """Both, for the scheduler. Neither may stop the other."""
    from .server import board, proposal_store
    lines: list[str] = []
    for step in (lambda: sweep(now),
                 lambda: remind(proposal_store(), board().cfg, now)):
        try:
            lines += step()
        except Exception as exc:  # noqa: BLE001 - a nudge must never break the tick
            lines.append(f"nudge step failed: {type(exc).__name__}: {exc}")
    return lines
