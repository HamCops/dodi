"""The watcher: what counts as news, and when it may wake Dodi."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from espn_mcp.watch import WAKE_GAP, diff, should_wake

ET = ZoneInfo("America/New_York")
TZ = "America/New_York"


def at(h, m=0) -> float:
    return datetime(2026, 10, 1, h, m, tzinfo=ET).timestamp()


def snap(**over):
    base = {"week": 4, "injury": {"1": "ACTIVE", "2": "ACTIVE", "3": "QUESTIONABLE"},
            "names": {"1": "Jonathan Taylor", "2": "Tyler Warren", "3": "Mike Evans"},
            "starters": [1, 3], "trending": [], "stash": [], "trades": []}
    base.update(over)
    return base


def test_nothing_changed_is_no_news_and_a_first_look_reports_nothing():
    assert diff(snap(), snap()) == []
    assert diff({}, snap()) == []
    assert diff(snap(week=3), snap()) == []


def test_a_starter_going_out_is_urgent_a_bench_change_is_not():
    ev = diff(snap(), snap(injury={"1": "OUT", "2": "QUESTIONABLE", "3": "QUESTIONABLE"}))
    by = {e["text"].split(":")[0]: e for e in ev}
    assert by["Jonathan Taylor"]["urgent"] and "starter" in by["Jonathan Taylor"]["text"]
    assert not by["Tyler Warren"]["urgent"]


def test_trending_stash_and_trades_are_reported_once():
    old = snap(trending=["A Guy"], stash=["X for Lions D/ST"], trades=["t1"])
    new = snap(trending=["A Guy", "B Guy"], stash=["X for Lions D/ST", "Y for Kincaid"],
               trades=["t2"])
    texts = [e["text"] for e in diff(old, new)]
    assert "B Guy is trending on waivers" in texts and not any("A Guy" in t for t in texts)
    assert "New stash move: Y for Kincaid" in texts
    assert "New trade offer" in texts and "A trade offer was answered" in texts


def test_wakes_are_rationed():
    news = [{"kind": "trending", "urgent": False, "text": "x"}]
    urgent = [{"kind": "injury", "urgent": True, "text": "y"}]
    assert should_wake(news, {}, at(14), TZ, False)
    assert not should_wake(news, {"last_wake": at(13)}, at(14), TZ, False)       # gap
    assert should_wake(news, {"last_wake": at(14) - WAKE_GAP}, at(14), TZ, False)
    assert should_wake(urgent, {"last_wake": at(13, 59)}, at(14), TZ, False)     # gap waived
    assert not should_wake(news, {}, at(23, 30), TZ, False)                     # quiet hours
    assert should_wake(urgent, {}, at(23, 30), TZ, False)                       # except out
    assert not should_wake(urgent, {}, at(14), TZ, True)       # game-time run is about to
    assert not should_wake([], {}, at(14), TZ, False)


def test_a_decided_claim_is_won_or_lost_by_whether_he_is_on_the_roster():
    old = snap(claims={"4711533": "Ollie Gordon II", "-16009": "Packers D/ST"},
               roster_ids=[1, 2, 3])
    new = snap(claims={}, roster_ids=[1, 2, 3, -16009])
    got = {e["text"]: e["won"] for e in diff(old, new) if e["kind"] == "claim"}
    assert got == {"Claim lost: Ollie Gordon II": False, "Claim won: Packers D/ST": True}
    # Still pending: nothing to say yet.
    assert not [e for e in diff(old, old) if e["kind"] == "claim"]


def test_overnight_news_is_held_for_the_morning_not_dropped(tmp_path, monkeypatch):
    """The 3 AM waiver run: results wait until 8 AM, then one push and one wake."""
    import dataclasses

    import espn_mcp.watch as w
    from test_integration import CFG

    cfg = dataclasses.replace(CFG, state_dir=str(tmp_path))

    class B:
        pass
    b = B()
    b.cfg = cfg
    snaps = iter([
        snap(claims={"4711533": "Ollie Gordon II"}, roster_ids=[1, 2, 3]),  # evening
        snap(claims={}, roster_ids=[1, 2, 3]),                              # 3 AM: lost
        snap(claims={}, roster_ids=[1, 2, 3]),                              # 8 AM
    ])
    monkeypatch.setattr(w, "snapshot", lambda b: next(snaps))
    monkeypatch.setattr(w, "_agent_due_soon", lambda b, now: False)
    pushed, woke = [], []
    monkeypatch.setattr(w, "_push_claims", lambda cfg, r: pushed.append(r) or True)
    wake = lambda ev: woke.append(ev) or True  # noqa: E731

    night = datetime(2026, 9, 29, 20, 0, tzinfo=ET).timestamp()
    w.run(night, board_fn=lambda: b, wake=wake)
    lines = w.run(night + 7 * 3600, board_fn=lambda: b, wake=wake)          # 3 AM
    assert "watch: Claim lost: Ollie Gordon II" in lines
    assert pushed == [] and woke == []                                     # asleep
    w.run(night + 12 * 3600, board_fn=lambda: b, wake=wake)                # 8 AM
    assert len(pushed) == 1 and pushed[0][0]["text"] == "Claim lost: Ollie Gordon II"
    assert len(woke) == 1 and woke[0][0]["kind"] == "claim"   # Dodi looks for the next man
