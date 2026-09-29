"""Auto-apply policy, the auto-apply path, and the reminders."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from espn_mcp.autopolicy import auto_ok
from espn_mcp.board import DraftBoard
from espn_mcp.nudge import due_nudge, remind
from espn_mcp.proposals import ProposalStore

from test_proposals import CFG, SeasonClient, _tool  # noqa: F401 - shared stubs

ET = ZoneInfo("America/New_York")


def et(d, h, m=0) -> float:
    return datetime(2026, 9, d, h, m, tzinfo=ET).timestamp()


# --- policy -----------------------------------------------------------------


def _add(pos, drop_pos, ros=0.0, week=0.0):
    return {"add": {"pos": pos}, "drop": [{"pos": drop_pos}] if drop_pos else [],
            "delta": {"starters_ros_per_game": ros, "starters_this_week": week}}


def test_adds_that_raise_the_lineup_are_made_and_the_rest_are_asked():
    assert auto_ok("add_player", {}, _add("RB", "WR", ros=1.95))[0]
    assert not auto_ok("add_player", {}, _add("RB", "WR", ros=0.0, week=2.5))[0]
    assert not auto_ok("add_player", {}, _add("WR", "RB", ros=-0.4))[0]


def test_defense_and_kicker_stream_like_for_like_on_this_week_alone():
    # Streaming loses rest-of-season value by design; this week is the point.
    assert auto_ok("add_player", {}, _add("D/ST", "D/ST", ros=-0.43, week=0.29))[0]
    assert auto_ok("add_player", {}, _add("K", "K", ros=-1.36, week=1.02))[0]
    assert not auto_ok("add_player", {}, _add("K", "K", week=-0.5))[0]
    # Never a skill player cut to stream.
    assert not auto_ok("add_player", {}, _add("D/ST", "WR", ros=-0.2, week=1.0))[0]


def test_trades_offered_only_when_they_help_me_and_should_be_accepted():
    good = {"me": {"delta": {"starters_ros_per_game": 0.8}}, "likely_accepted": True,
            "worth_offering": True}
    assert auto_ok("propose_trade", {}, good)[0]
    assert not auto_ok("propose_trade", {}, {**good, "likely_accepted": False})[0]
    assert not auto_ok("propose_trade", {}, {**good, "me": {"delta": {
        "starters_ros_per_game": -0.1}}})[0]
    assert not auto_ok("propose_trade", {}, {**good, "usage": {"warning": "Buying high"}})[0]
    # An old preview without the check is never sent on its own.
    assert not auto_ok("propose_trade", {}, {k: v for k, v in good.items()
                                             if k != "worth_offering"})[0]


def test_a_steal_for_them_is_not_a_trade_worth_making():
    """A trade the manager rejected as lopsided, with its real numbers."""
    from espn_mcp.market import trade_view, worth_offering

    watson = {"name": "Christian Watson", "market_value": 3653}
    wilson = {"name": "Michael Wilson", "market_value": 1362}
    view = trade_view([watson], [wilson])
    verdict = worth_offering(0.25, -0.82, view, {"outlook_change": -5.5})
    assert not verdict["ok"]
    text = " ".join(verdict["reasons"])
    assert "+0.25" in text and "this week" in text and "63%" in text and "workload" in text
    preview = {"me": {"delta": {"starters_ros_per_game": 0.25}}, "likely_accepted": True,
               "worth_offering": False, "not_worth_because": verdict["reasons"]}
    ok, why = auto_ok("propose_trade", {}, preview)
    assert not ok and "63%" in why

    # Maye for Pickens: I gain value and a lot of lineup. Worth offering.
    maye, pickens = {"market_value": 1049}, {"market_value": 3951}
    assert worth_offering(1.92, 0.39, trade_view([maye], [pickens]))["ok"]
    # A fair swap inside the overpay cap is fine; one just past it is not.
    assert worth_offering(0.6, 0.0, trade_view([{"market_value": 1200}],
                                               [{"market_value": 1000}]))["ok"]
    assert not worth_offering(0.6, 0.0, trade_view([{"market_value": 1400}],
                                                   [{"market_value": 1000}]))["ok"]


def test_accepting_a_trade_and_bare_drops_always_wait():
    assert not auto_ok("respond_to_trade", {"action": "accept"}, {})[0]
    assert auto_ok("respond_to_trade", {"action": "decline"}, {})[0]
    assert auto_ok("respond_to_trade", {"action": "withdraw"}, {})[0]
    assert not auto_ok("drop_player", {}, {})[0]
    assert not auto_ok("start_player", {}, {})[0]


# --- the path ---------------------------------------------------------------


@pytest.fixture
def auto_league(tmp_path, monkeypatch):
    import espn_mcp.approve as approve_mod
    import espn_mcp.server as srv

    cfg = dataclasses.replace(CFG, require_approval=True, auto_apply=True,
                              state_dir=str(tmp_path),
                              approve_base_url="https://host.example/dodi")
    b = DraftBoard(cfg, client=SeasonClient())
    pushes: list[dict] = []
    monkeypatch.setattr(srv, "_board", b)
    monkeypatch.setattr(srv, "_store", None)
    monkeypatch.setattr(srv, "_now_ms", lambda: 0)
    monkeypatch.setattr(srv, "_sleep", lambda s: None)
    monkeypatch.setattr(srv, "push_proposal",
                        lambda cfg, p: pushes.append({"title": p["title"]}) or {"sent": True})
    monkeypatch.setattr(approve_mod, "push", lambda cfg, title, message, **kw:
                        pushes.append({"title": title, "message": message}) or {"sent": True})
    b.client.fa_ids = {p["player_id"] for p in b.season_available()[:3]}
    b.season_board(refresh=True)
    return b, pushes


def test_a_move_the_policy_allows_is_made_and_he_is_told_after(auto_league, monkeypatch):
    import espn_mcp.server as srv

    b, pushes = auto_league
    free = b.season_available()[0]
    monkeypatch.setattr(srv, "auto_ok", lambda a, p, v: (True, "Raises the lineup."))
    out = _tool("request_approval", action="add_player", params={"add": free["name"]},
                reasoning="Best back available.")
    assert out["auto_applied"] is True and out["status"] == "applied"
    assert len(b.client.posts) == 1
    assert [p["title"] for p in pushes] == [f"Dodi did it: add {free['name']}"]
    assert "Rule: Raises the lineup." in pushes[0]["message"]
    assert srv.proposal_store().get(out["proposal"]["id"])["status"] == "applied"


def test_a_move_it_does_not_allow_is_asked_with_the_reason(auto_league, monkeypatch):
    import espn_mcp.server as srv

    b, pushes = auto_league
    free = b.season_available()[0]
    monkeypatch.setattr(srv, "auto_ok", lambda a, p, v: (False, "Too close to call."))
    out = _tool("request_approval", action="add_player", params={"add": free["name"]},
                reasoning="Maybe.")
    assert out["queued"] is True
    assert not hasattr(b.client, "posts")
    assert "Asked because: Too close to call." in out["proposal"]["summary"]


# --- reminders --------------------------------------------------------------


def _pending(created, expires, **kw):
    return {"id": "p1", "status": "pending", "title": "Dodi: add X?", "created_at": created,
            "expires_at": expires, **kw}


def test_one_reminder_after_the_wait_then_a_last_call_near_the_deadline():
    p = _pending(et(29, 13), et(30, 2, 50))          # claim closes before the 3 AM run
    assert due_nudge(p, et(29, 13, 30), "America/New_York", 60) is None
    assert due_nudge(p, et(29, 14, 0), "America/New_York", 60) == "reminded"
    p["reminded_at"] = et(29, 14, 0)
    assert due_nudge(p, et(29, 18, 0), "America/New_York", 60) is None
    # It would lapse overnight: the last call goes out before quiet hours.
    assert due_nudge(p, et(29, 22, 0), "America/New_York", 60) == "last_call"
    p["last_call_at"] = et(29, 22, 0)
    assert due_nudge(p, et(29, 22, 30), "America/New_York", 60) is None


def test_nobody_is_woken_up():
    p = _pending(et(29, 22, 30), et(30, 9, 0))
    for h in (23, 0, 3, 7):
        d = 30 if h < 12 else 29
        assert due_nudge(p, et(d, h), "America/New_York", 60) is None
    assert due_nudge(p, et(30, 8, 0), "America/New_York", 60) == "reminded"
    p["reminded_at"] = et(30, 8, 0)
    assert due_nudge(p, et(30, 8, 10), "America/New_York", 60) is None   # 15-min gap
    assert due_nudge(p, et(30, 8, 20), "America/New_York", 60) == "last_call"


def test_the_hourly_budget_caps_the_noise(tmp_path):
    store = ProposalStore(tmp_path / "q.db")
    now = et(29, 15)
    for i in range(5):
        store.create("drop_player", {"player": f"p{i}"}, title=f"t{i}", summary="s",
                     reasoning="r", expires_at=now + 20 * 3600, now=now - 2 * 3600)
    cfg = dataclasses.replace(CFG, remind_after_minutes=60)
    sent = remind(store, cfg, now, push_fn=lambda cfg, p: {"sent": True})
    assert len(sent) == 3
    assert remind(store, cfg, now + 60, push_fn=lambda cfg, p: {"sent": True}) == []
    assert len(remind(store, cfg, now + 3700, push_fn=lambda cfg, p: {"sent": True})) == 2
