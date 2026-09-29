"""Scheduling by the kickoff clock: which run is due when, and that each
happens once."""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp import gametime  # noqa: E402
from espn_mcp.gametime import MIN, due, slots_for, tick  # noqa: E402
from espn_mcp.notify import deadline  # noqa: E402
from test_integration import CFG  # noqa: E402

ET = ZoneInfo("America/New_York")


def at(day: int, hour: int, minute: int = 0) -> int:
    """A kickoff in October 2026, Eastern time, as epoch milliseconds."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp() * 1000)


def roster():
    return [
        {"name": "Thursday Guy", "kickoff_ms": at(1, 20, 15), "slot_id": 2},
        {"name": "London Guy", "kickoff_ms": at(4, 9, 30), "slot_id": 4},
        {"name": "Early A", "kickoff_ms": at(4, 13), "slot_id": 0},
        {"name": "Early B", "kickoff_ms": at(4, 13), "slot_id": 20},
        {"name": "Late A", "kickoff_ms": at(4, 16, 5), "slot_id": 23},
        {"name": "Late B", "kickoff_ms": at(4, 16, 25), "slot_id": 6},
        {"name": "Night Guy", "kickoff_ms": at(4, 20, 20), "slot_id": 17},
        {"name": "Monday Guy", "kickoff_ms": at(5, 20, 15), "slot_id": 16},
        {"name": "On IR", "kickoff_ms": at(4, 13), "slot_id": 21},
        {"name": "On Bye", "kickoff_ms": None, "slot_id": 20},
    ]


def test_kickoffs_are_grouped_into_decisions():
    slots = slots_for(roster(), at(1, 12))
    assert [s["players"] for s in slots] == [
        ["Thursday Guy"], ["London Guy"], ["Early A", "Early B"],
        ["Late A", "Late B"], ["Night Guy"], ["Monday Guy"]]
    assert slots[3]["kickoff_ms"] == at(4, 16, 5)      # the earlier of the two
    # Games already started are nobody's decision.
    assert len(slots_for(roster(), at(4, 13, 1))) == 3


def test_runs_are_counted_back_from_kickoff_with_time_to_approve():
    k = at(4, 13)
    slot = {"kickoff_ms": k}
    lead = 30
    assert due(slot, k - 70 * MIN, lead) == []             # too early
    assert due(slot, k - 65 * MIN, lead) == ["agent"]      # 11:55 AM for a 1:00 game
    assert due(slot, k - 46 * MIN, lead) == ["agent"]
    assert due(slot, k - 45 * MIN, lead) == ["lineup"]     # 12:15 PM
    # Missed its window: the agent is skipped, the lineup is still set.
    assert due(slot, k - 20 * MIN, lead) == ["lineup"]
    assert due(slot, k - 4 * MIN, lead) == []              # too late to matter
    done = {"kickoff_ms": k, "agent_done": True, "lineup_done": True}
    assert due(done, k - 45 * MIN, lead) == []
    # A manager who wants an hour gets everything half an hour sooner.
    assert due(slot, k - 95 * MIN, 60) == ["agent"]
    assert due(slot, k - 75 * MIN, 60) == ["lineup"]


class Board:
    def __init__(self, cfg, players):
        self.cfg, self.players, self.reads = cfg, players, 0

    def week(self):
        return 4

    def invalidate_rosters(self, week):
        pass

    def team_players(self, team_id, week):
        self.reads += 1
        return self.players


def test_each_run_happens_once_and_idle_ticks_do_not_call_espn(tmp_path):
    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path), gametime_hook="/bin/true")
    b = Board(cfg, roster())
    log: list[str] = []
    kw = dict(board_fn=lambda: b, run_lineup=lambda: log.append("lineup") or 0,
              run_agent=lambda slot, hook, event="agent": log.append(event) or True)
    # The first look at a new week asks about roster moves, once, and sets
    # the week's lineup straight away rather than leaving last week's.
    assert tick(at(1, 12) / 1000, **kw) == [
        "roster run for week 4: started (players unlocked)",
        "lineup set for week 4 (players unlocked)"] and b.reads == 1
    assert log == ["roster", "lineup"]
    assert tick(at(1, 12, 5) / 1000, **kw) == [] and b.reads == 1     # idle: from disk
    log.clear()

    did = tick((at(1, 20, 15) - 62 * MIN) / 1000, **kw)
    assert log == ["agent"] and "Thu 8:15 PM" in did[0]
    assert tick((at(1, 20, 15) - 58 * MIN) / 1000, **kw) == []
    tick((at(1, 20, 15) - 44 * MIN) / 1000, **kw)
    assert log == ["agent", "lineup"]

    # Sunday, five-minute ticks through the 1 PM window: once each, in order.
    log.clear()
    for minute in range(11 * 60 + 40, 13 * 60 + 5, 5):
        tick(at(4, minute // 60, minute % 60) / 1000, **kw)
    assert log == ["agent", "lineup"]


def _mark_roster_run(tmp_path, cfg):
    sched = gametime.Schedule(tmp_path / f"gametime-{cfg.league_id}-{cfg.season}.json")
    sched.data["roster_run_week"] = sched.data["week"]
    sched.save()


def _status(b, now, monkeypatch):
    import espn_mcp.server as srv
    monkeypatch.setattr(srv, "_board", b)
    return gametime.status(now)


def test_status_of_a_kickoff_that_moves_is_kept(tmp_path):
    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path))
    sched = gametime.Schedule(tmp_path / "s.json")
    sched.replace(4, [{"kickoff_ms": at(4, 13), "players": ["A"], "lineup_done": True}], 0)
    sched.replace(4, [{"kickoff_ms": at(4, 13, 5), "players": ["A", "B"]}], 1)
    assert sched.slots[0]["lineup_done"] is True
    sched.replace(5, [{"kickoff_ms": at(4, 13, 5), "players": ["A"]}], 2)
    assert not sched.slots[0].get("lineup_done")      # a new week starts clean
    assert cfg.approval_lead_minutes == 30


def test_deadline_is_told_in_the_managers_time_and_flags_short_notice():
    now = at(4, 12, 15) / 1000
    by = deadline(CFG, {"expires_at": at(4, 12, 55) / 1000}, now)
    assert by == {"text": "12:55 PM (40 min)", "minutes": 40, "short_notice": False}
    rushed = deadline(CFG, {"expires_at": at(4, 12, 55) / 1000}, at(4, 12, 40) / 1000)
    assert rushed["short_notice"] is True and rushed["minutes"] == 15
    later = deadline(CFG, {"expires_at": at(5, 3) / 1000}, now)
    assert later["text"] == "Mon 3:00 AM" and later["short_notice"] is False


def test_a_failed_run_stays_owed_and_the_manager_is_told(tmp_path, monkeypatch):
    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path), gametime_hook="/bin/false")
    b = Board(cfg, roster())
    alerts: list[str] = []
    monkeypatch.setattr(gametime, "_alert", alerts.append)
    codes = iter([1, 0])
    hooks = iter([False, True])
    log: list[str] = []
    kw = dict(board_fn=lambda: b,
              run_lineup=lambda: log.append("lineup") or next(codes),
              run_agent=lambda slot, hook, event="agent": log.append(event) or next(hooks))
    k = at(1, 20, 15)
    # Too early in the day for roster moves, so this only reads the schedule.
    tick(at(1, 6) / 1000, **kw)
    b.cfg = cfg
    gametime.Schedule(tmp_path / f"gametime-{cfg.league_id}-{cfg.season}.json")
    _mark_roster_run(tmp_path, cfg)

    assert "will retry" in tick((k - 62 * MIN) / 1000, **kw)[0]
    assert "started" in tick((k - 57 * MIN) / 1000, **kw)[0]
    assert "failed" in tick((k - 44 * MIN) / 1000, **kw)[0]
    assert tick((k - 39 * MIN) / 1000, **kw) == ["lineup set for Thu 8:15 PM kickoff"]
    assert tick((k - 34 * MIN) / 1000, **kw) == []
    assert log == ["agent", "agent", "lineup", "lineup"] and len(alerts) == 2


def test_looking_at_the_plan_runs_nothing(tmp_path, monkeypatch):
    import espn_mcp.server as srv

    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path), gametime_hook="/bin/true")
    b = Board(cfg, roster())
    monkeypatch.setattr(srv, "_board", b)
    ran: list[str] = []
    monkeypatch.setattr(gametime, "_run_lineup", lambda: ran.append("lineup") or 0)
    monkeypatch.setattr(gametime, "_run_hook", lambda s, h: ran.append("agent") or True)
    text = gametime.status((at(4, 13) - 44 * MIN) / 1000)    # inside the lineup window
    assert "Kickoff Sun Oct 4 1:00 PM" in text and ran == []


def test_the_agent_is_asked_about_roster_moves_when_the_week_unlocks(tmp_path, monkeypatch):
    monkeypatch.setattr(gametime, "_alert", lambda e: None)
    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path), gametime_hook="/bin/true")

    class Rolling(Board):
        def __init__(self, cfg):
            super().__init__(cfg, [])
            self.wk = 3

        def week(self):
            return self.wk

    b = Rolling(cfg)
    log: list[str] = []
    kw = dict(board_fn=lambda: b, run_lineup=lambda: 0,
              run_agent=lambda slot, hook, event="agent": log.append(event) or True)
    sep = lambda d, h, m=0: int(datetime(2026, 9, d, h, m, tzinfo=ET).timestamp())  # noqa: E731

    # Monday night: week 3 is over, every game played. Nothing to ask.
    assert tick(sep(28, 21, 45), **kw) == [] and log == []
    # Watching for the rollover: re-read every 20 minutes, not every 6 hours.
    reads = b.reads
    tick(sep(28, 21, 50), **kw)
    assert b.reads == reads
    tick(sep(28, 22, 10), **kw)
    assert b.reads == reads + 1

    # ESPN rolls over at 4 AM. Seen at once, and held until he is awake.
    b.wk, b.players = 4, roster()
    assert tick(sep(29, 4, 0), **kw) == [] and log == []
    assert "Roster moves: unlocked" in _status(b, sep(29, 4, 1), monkeypatch)
    assert tick(sep(29, 8, 55), **kw) == []
    assert tick(sep(29, 9, 0), **kw) == ["roster run for week 4: started (players unlocked)",
                                         "lineup set for week 4 (players unlocked)"]
    assert log == ["roster"]
    # Once a week, however many ticks follow and however often it re-reads.
    for hour in (9, 12, 15, 20):
        tick(sep(29, hour, 30), **kw)
    tick(sep(30, 10, 0), **kw)
    assert log == ["roster"]


def test_a_failed_roster_run_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(gametime, "_alert", lambda e: None)
    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path), gametime_hook="/bin/true")
    b = Board(cfg, roster())
    answers = iter([False, True])
    log: list[str] = []
    kw = dict(board_fn=lambda: b, run_lineup=lambda: 0,
              run_agent=lambda slot, hook, event="agent": log.append(event) or next(answers))
    sep = lambda d, h, m=0: int(datetime(2026, 9, d, h, m, tzinfo=ET).timestamp())  # noqa: E731
    assert "will retry" in tick(sep(29, 10, 0), **kw)[0]
    assert "started" in tick(sep(29, 10, 5), **kw)[0]
    assert tick(sep(29, 10, 10), **kw) == [] and log == ["roster", "roster"]
