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
