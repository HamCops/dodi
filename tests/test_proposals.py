"""The approval queue: the store's state machine, the gate on the writing
tools, and the service behind the notification buttons.

No network: ESPN is the stubbed season client and ntfy pushes are captured.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.espn import ESPNError  # noqa: E402
from espn_mcp.notify import approval_actions  # noqa: E402
from espn_mcp.proposals import ProposalError, ProposalStore, public  # noqa: E402
from test_integration import CFG  # noqa: E402
from test_season import SeasonClient, _tool  # noqa: E402

ADD = {"add": "New Guy", "drop": "Old Guy"}


def make(store: ProposalStore, params=ADD, **kw) -> dict:
    return store.create("add_player", params, title="t", summary="s", reasoning="r", **kw)


@pytest.fixture
def store(tmp_path) -> ProposalStore:
    return ProposalStore(tmp_path / "p.db")


def test_store_file_is_private(store):
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_unknown_actions_and_arguments_are_refused(store):
    with pytest.raises(ProposalError, match="Unknown action"):
        store.create("set_lineup", {}, title="t", summary="s", reasoning="r")
    with pytest.raises(ProposalError, match="does not take apply"):
        make(store, {"add": "X", "apply": True})


def test_decide_happens_exactly_once(store):
    p = make(store)
    assert store.decide(p["id"], "approved")["status"] == "approved"
    with pytest.raises(ProposalError, match="Already approved"):
        store.decide(p["id"], "approved")
    with pytest.raises(ProposalError, match="Already approved"):
        store.decide(p["id"], "rejected")
    assert store.finish(p["id"], True, {"applied": True})["status"] == "applied"


def test_expired_proposals_cannot_be_approved(store):
    p = make(store, ttl_hours=1, now=1000.0)
    with pytest.raises(ProposalError, match="Already expired"):
        store.decide(p["id"], "approved", now=1000.0 + 3601)
    with pytest.raises(ProposalError, match="already closed"):
        make(store, {"add": "Late"}, expires_at=999.0, now=1000.0)


def test_same_move_is_not_queued_twice_and_a_rejection_holds_for_a_while(store):
    p = make(store, now=1000.0)
    same = {"add": " new guy ", "drop": "OLD GUY"}
    assert store.find_blocking("add_player", same, now=1001.0)["id"] == p["id"]
    assert store.find_blocking("add_player", {"add": "Other"}, now=1001.0) is None
    store.decide(p["id"], "rejected", now=1002.0)
    assert store.find_blocking("add_player", same, now=1002.0 + 3600)["status"] == "rejected"
    assert store.find_blocking("add_player", same, now=1002.0 + 73 * 3600) is None


def test_token_is_checked_and_never_shown(store):
    p = make(store)
    assert store.authorized(p["id"], p["token"])["id"] == p["id"]
    assert store.authorized(p["id"], "wrong") is None
    assert store.authorized(p["id"], None) is None
    assert store.authorized("nope", p["token"]) is None
    assert "token" not in public(p)


def test_buttons_carry_the_token_only_when_a_base_url_is_set(store):
    p = make(store)
    assert approval_actions(CFG, p) == []
    cfg = dataclasses.replace(CFG, approve_base_url="https://host.example/dodi/")
    approve, reject, view = approval_actions(cfg, p)
    assert approve["url"] == f"https://host.example/dodi/p/{p['id']}/approve"
    assert approve["method"] == "POST"
    assert approve["headers"] == {"Authorization": f"Bearer {p['token']}"}
    assert reject["url"].endswith("/reject") and view["action"] == "view"


# --- the gate and the queue, over the stubbed league ------------------------


@pytest.fixture
def league(tmp_path, monkeypatch):
    """A board that requires approval, with pushes captured instead of sent."""
    import espn_mcp.approve as approve_mod
    import espn_mcp.server as srv

    cfg = dataclasses.replace(CFG, require_approval=True, state_dir=str(tmp_path),
                              approve_base_url="https://host.example/dodi")
    b = DraftBoard(cfg, client=SeasonClient())
    pushes: list[dict] = []

    def fake_push_proposal(cfg, proposal):
        pushes.append({"title": proposal["title"], "message": proposal["summary"]})
        return {"sent": True}

    def fake_push(cfg, title, message, **kw):
        pushes.append({"title": title, "message": message})
        return {"sent": True}

    monkeypatch.setattr(srv, "_board", b)
    monkeypatch.setattr(srv, "_store", None)
    monkeypatch.setattr(srv, "_now_ms", lambda: 0)
    monkeypatch.setattr(srv, "_sleep", lambda s: None)
    monkeypatch.setattr(srv, "push_proposal", fake_push_proposal)
    monkeypatch.setattr(approve_mod, "push", fake_push)
    b.client.fa_ids = {p["player_id"] for p in b.season_available()[:3]}
    b.season_board(refresh=True)
    return b, pushes


def test_direct_writes_are_refused_but_previews_and_lineups_are_not(league):
    b, _ = league
    free = b.season_available()[0]
    bench = next(p for p in b.team_players(CFG.team_id) if p["slot"] == "BE")

    refused = _tool("add_player", add=free["name"], apply=True)
    assert "needs the manager's approval" in refused["error"]
    assert "request_approval" in refused["hint"]
    assert "needs the manager's approval" in _tool(
        "drop_player", player=bench["name"], apply=True)["error"]
    assert "needs the manager's approval" in _tool(
        "propose_trade", give=["a"], receive=["b"], apply=True)["error"]
    assert "needs the manager's approval" in _tool(
        "respond_to_trade", trade_id="1", action="accept", apply=True)["error"]
    assert not hasattr(b.client, "posts")

    assert _tool("add_player", add=free["name"])["applied"] is False
    assert "needs the manager's approval" not in str(_tool("set_lineup", apply=True))


def test_request_approval_queues_notifies_and_does_not_touch_espn(league):
    b, pushes = league
    free = b.season_available()[0]
    out = _tool("request_approval", action="add_player", params={"add": free["name"]},
                reasoning="Best back available.")
    assert out["queued"] is True and out["notified"] is True
    assert "token" not in out["proposal"]
    assert free["name"] in out["proposal"]["summary"]
    assert pushes[0]["title"] == f"Dodi: add {free['name']}?"
    assert not hasattr(b.client, "posts")

    again = _tool("request_approval", action="add_player", params={"add": free["name"]},
                  reasoning="Still the best.")
    assert again["queued"] is False and again["already"] == "pending"
    assert len(pushes) == 1
    assert _tool("get_proposals", status="pending")["count"] == 1


def test_a_move_that_fails_its_preview_is_not_queued(league):
    _, pushes = league
    out = _tool("request_approval", action="add_player", params={"add": "Nobody Atall"},
                reasoning="x")
    assert out["queued"] is False and out["error"]
    bad = _tool("request_approval", action="set_lineup", params={}, reasoning="x")
    assert bad["queued"] is False and "Unknown action" in bad["error"]
    assert pushes == []


def _client():
    from starlette.testclient import TestClient

    from espn_mcp.approve import app
    return TestClient(app)


def _queued(b) -> dict:
    import espn_mcp.server as srv

    free = b.season_available()[0]
    out = _tool("request_approval", action="add_player", params={"add": free["name"]},
                reasoning="Best back available.")
    return {**srv.proposal_store().get(out["proposal"]["id"]), "player": free}


def test_approve_button_applies_the_move_once(league):
    import espn_mcp.server as srv

    b, pushes = league
    p = _queued(b)
    auth = {"Authorization": f"Bearer {p['token']}"}
    with _client() as c:
        assert c.post(f"/p/{p['id']}/approve").status_code == 404
        assert c.post(f"/p/{p['id']}/approve",
                      headers={"Authorization": "Bearer nope"}).status_code == 404
        assert not hasattr(b.client, "posts")

        r = c.post(f"/p/{p['id']}/approve", headers=auth)
        assert r.status_code == 202 and "token" not in r.text
        assert len(b.client.posts) == 1
        assert b.client.posts[0]["items"][0]["playerId"] == p["player"]["player_id"]

        # A second tap, or the phone retrying, must not send it again.
        assert c.post(f"/p/{p['id']}/approve", headers=auth).status_code == 409
        assert len(b.client.posts) == 1

    done = srv.proposal_store().get(p["id"])
    assert done["status"] == "applied" and done["result"]["applied"] is True
    assert pushes[-1]["title"] == "Dodi: done"


def test_reject_button_sends_nothing(league):
    import espn_mcp.server as srv

    b, _ = league
    p = _queued(b)
    with _client() as c:
        r = c.post(f"/p/{p['id']}/reject", headers={"Authorization": f"Bearer {p['token']}"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert not hasattr(b.client, "posts")
    assert srv.proposal_store().get(p["id"])["status"] == "rejected"
    again = _tool("request_approval", action="add_player",
                  params={"add": p["player"]["name"]}, reasoning="x")
    assert again["queued"] is False and again["already"] == "rejected"


def test_a_move_espn_refuses_is_recorded_as_failed(league):
    import espn_mcp.server as srv

    b, pushes = league
    p = _queued(b)
    def refuse(body):
        raise ESPNError("ESPN 409: player is no longer available")

    b.client.post_transaction = refuse
    with _client() as c:
        c.post(f"/p/{p['id']}/approve", headers={"Authorization": f"Bearer {p['token']}"})
    done = srv.proposal_store().get(p["id"])
    assert done["status"] == "failed" and done["result"]["error"]
    assert pushes[-1]["title"] == "Dodi: move failed"


def test_details_page_has_working_buttons_and_escapes_its_text(league):
    import espn_mcp.server as srv

    b, _ = league
    store = srv.proposal_store()
    p = store.create("drop_player", {"player": "x"}, title="<b>t</b>", summary="s",
                     reasoning="<script>alert(1)</script>")
    with _client() as c:
        assert c.get(f"/p/{p['id']}", headers={"Accept": "text/html"}).status_code == 404
        page = c.get(f"/p/{p['id']}?t={p['token']}", headers={"Accept": "text/html"})
        assert page.status_code == 200
        assert "<script>" not in page.text and "&lt;script&gt;" in page.text
        assert f'formaction="{p["id"]}/approve"' in page.text

        r = c.post(f"/p/{p['id']}/reject", data={"t": p["token"]},
                   headers={"Accept": "text/html"})
        assert r.status_code == 200 and "Rejected" in r.text
        assert "formaction" not in r.text


def test_interrupted_applies_are_closed_not_retried(league):
    import espn_mcp.server as srv
    from espn_mcp.approve import recover_interrupted

    b, _ = league
    p = _queued(b)
    srv.proposal_store().decide(p["id"], "approved")
    assert recover_interrupted() == 1
    assert srv.proposal_store().get(p["id"])["status"] == "failed"
    assert not hasattr(b.client, "posts")


# --- close calls: the start/sit decisions a projection cannot make ------------


def _p(pid, name, pos, slot_id, week, ros, market=None, **kw):
    flex = ["FLEX"] if pos in ("RB", "WR", "TE") else []
    return {"player_id": pid, "name": name, "position": pos, "slot_id": slot_id,
            "week_proj": week, "ros_per_game": ros, "market_value": market,
            "eligible_slots": [pos, *flex, "BE"], "injury_status": "ACTIVE",
            "kickoff_ms": 5_000, **kw}


def test_a_star_benched_by_a_rounding_error_is_a_close_call():
    from espn_mcp.season import close_calls

    roster = [
        _p(1, "Replacement TE", "TE", 6, 7.72, 6.0, 2360),
        _p(2, "Star TE", "TE", 20, 7.14, 7.9, 4662),       # back from injury
        _p(3, "Flex WR", "WR", 23, 7.17, 6.5, 1311),
        _p(4, "Real Starter", "RB", 2, 16.0, 15.0, 8000),   # not close
        _p(5, "Depth WR", "WR", 20, 7.0, 5.0, 585),         # close, but no better
        _p(6, "Hurt Star", "TE", 20, 7.1, 9.0, 5000, injury_status="OUT"),
        _p(7, "Kicker", "K", 20, 7.4, 9.0),
    ]
    [call] = close_calls(roster, None, "week_proj", now_ms=0)
    assert call["start"]["name"] == "Star TE"
    # The TE is 0.58 ahead: a real gap. The FLEX is 0.03 ahead: not one.
    assert call["sit"]["name"] == "Flex WR"
    assert call["week_gap"] <= 0.5 and len(call["reasons"]) == 2

    # Once either game has kicked off there is nothing left to decide.
    assert close_calls(roster, None, "week_proj", now_ms=5_000) == []
    # A real gap is the optimizer's to settle, not a close call.
    roster[1]["week_proj"] = 4.0
    assert close_calls(roster, None, "week_proj", now_ms=0) == []


def test_an_approved_start_is_applied_and_survives_the_optimizer(league, monkeypatch):
    import espn_mcp.server as srv

    b, pushes = league
    # Fixture kickoffs are in the past by the wall clock; the offer would expire.
    monkeypatch.setattr(srv, "_expiry", lambda action, preview: None)
    mine = b.team_players(CFG.team_id)
    sit = next(p for p in mine if p["slot"] == "TE")
    up = next(p for p in mine if p["slot"] == "BE" and p["position"] == "TE")
    assert (up.get("week_proj") or 0) <= (sit.get("week_proj") or 0)

    out = _tool("request_approval", action="start_player",
                params={"player": up["name"], "over": sit["name"]}, reasoning="He is back.")
    assert out["queued"] is True
    assert pushes[-1]["title"] == f"Dodi: start {up['name']} over {sit['name']}?"
    p = srv.proposal_store().get(out["proposal"]["id"])
    with _client() as c:
        r = c.post(f"/p/{p['id']}/approve", headers={"Authorization": f"Bearer {p['token']}"})
    assert r.status_code == 202
    assert srv.proposal_store().get(p["id"])["status"] == "applied"
    slots = {q["name"]: q["slot"] for q in b.team_players(CFG.team_id)}
    assert slots[up["name"]] == "TE" and slots[sit["name"]] == "BE"

    # The optimizer prefers the other one by projection. It must not undo this.
    again = _tool("set_lineup", apply=True)
    assert not any(m["player"] in (up["name"], sit["name"]) for m in again["moves"])
    assert again["manager_decisions"] == [f"start {up['name']} over {sit['name']}"]
    slots = {q["name"]: q["slot"] for q in b.team_players(CFG.team_id)}
    assert slots[up["name"]] == "TE" and slots[sit["name"]] == "BE"


def test_start_player_refuses_what_cannot_be_done(league):
    b, _ = league
    mine = b.team_players(CFG.team_id)
    qb = next(p for p in mine if p["slot"] == "QB")
    te = next(p for p in mine if p["slot"] == "TE")
    bench_te = next(p for p in mine if p["slot"] == "BE" and p["position"] == "TE")
    bad = _tool("request_approval", action="start_player",
                params={"player": bench_te["name"], "over": qb["name"]}, reasoning="x")
    assert bad["queued"] is False and "cannot play QB" in bad["error"]
    bad = _tool("request_approval", action="start_player",
                params={"player": te["name"], "over": qb["name"]}, reasoning="x")
    assert bad["queued"] is False and "not on the bench" in bad["error"]


# --- the weekly review --------------------------------------------------------


def test_review_separates_what_happened_from_what_was_knowable(league, monkeypatch):
    import espn_mcp.report as report

    b, _ = league
    week = b.week()
    mine = b.team_players(CFG.team_id, week)
    bench = next(p for p in mine if p["slot"] == "BE" and p["position"] == "WR")
    real = b.team_players

    def with_scores(team_id, wk=None):
        out = real(team_id, wk)
        for p in out:
            p["week_points"] = {week: 30.0 if p["player_id"] == bench["player_id"] else 5.0}
        return out

    monkeypatch.setattr(b, "team_players", with_scores)
    now = max(p["kickoff_ms"] for p in mine if p.get("kickoff_ms")) / 1000 + 5 * 3600
    assert report.last_finished_week(b, now) == week
    assert report.last_finished_week(b, now - 5 * 3600) in (None, week - 1)

    r = report.build(b, now)
    starters = sum(1 for p in mine if p["slot"] not in ("BE", "IR"))
    assert r["lineup"]["scored"] == 5.0 * starters
    assert r["lineup"]["left_on_bench"] == 25.0
    assert r["lineup"]["should_have_started"][0]["name"] == bench["name"]
    assert r["accuracy"] is None                      # nothing was recorded beforehand
    text = report.render(r)
    assert f"Benched: {bench['name']}" in text and "cannot be scored" in text
    assert "hindsight" in text


def test_projections_are_scored_only_against_what_was_recorded_before_kickoff(tmp_path):
    import sqlite3

    import espn_mcp.report as report
    from espn_mcp.tracking import SCHEMA

    db = sqlite3.connect(tmp_path / "t.db")
    db.executescript(SCHEMA)
    for pid in range(30):
        # An early snapshot that was wrong, and the last one before kickoff.
        for taken, proj in ((100.0, 20.0), (200.0, 10.0)):
            db.execute("INSERT INTO snapshots (season, week, taken_at, player_id, week_proj, "
                       "alt_week_proj) VALUES (2026, 4, ?, ?, ?, ?)",
                       (taken, pid, proj, 12.0 if pid < 25 else None))
        db.execute("INSERT INTO actuals VALUES (2026, 4, ?, 13.0)", (pid,))
    acc = report.source_accuracy(db, 2026, 4)
    assert acc == {"players": 30, "espn_avg_miss": 3.0, "compared": 25,
                   "espn_avg_miss_same_players": 3.0, "sleeper_avg_miss": 1.0}
    assert report.source_accuracy(db, 2026, 5) is None


def test_a_benched_player_who_loses_his_first_choice_falls_to_his_next():
    from espn_mcp.season import close_calls

    roster = [
        _p(1, "Flex One", "WR", 23, 7.2, 5.0, 1000),
        _p(2, "Wideout", "WR", 4, 7.3, 5.5, 1000),
        _p(3, "Star A", "WR", 20, 7.1, 9.0, 5000),
        _p(4, "Star B", "WR", 20, 7.0, 8.0, 4000),
    ]
    calls = close_calls(roster, None, "week_proj", now_ms=0)
    assert [(c["start"]["name"], c["sit"]["name"]) for c in calls] == [
        ("Star A", "Flex One"), ("Star B", "Wideout")]


def test_a_pinned_starter_who_is_ruled_out_is_not_started(league, monkeypatch):
    import espn_mcp.server as srv

    b, _ = league
    monkeypatch.setattr(srv, "_expiry", lambda action, preview: None)
    mine = b.team_players(CFG.team_id)
    sit = next(p for p in mine if p["slot"] == "TE")
    up = next(p for p in mine if p["slot"] == "BE" and p["position"] == "TE")
    out = _tool("request_approval", action="start_player",
                params={"player": up["name"], "over": sit["name"]}, reasoning="x")
    p = srv.proposal_store().get(out["proposal"]["id"])
    with _client() as c:
        c.post(f"/p/{p['id']}/approve", headers={"Authorization": f"Bearer {p['token']}"})
    assert _tool("set_lineup")["manager_decisions"]

    real = b.team_players

    def hurt(team_id, week=None):
        rows = real(team_id, week)
        for q in rows:
            if q["player_id"] == up["player_id"]:
                q["injury_status"], q["week_proj"] = "OUT", 0.0
        return rows

    monkeypatch.setattr(b, "team_players", hurt)
    plan = _tool("set_lineup")
    assert "manager_decisions" not in plan
    assert up["name"] in plan["decisions_set_aside"][0]
    assert {"player": sit["name"], "from": "BE", "to": "TE"} in plan["moves"]


def test_approving_a_start_the_lineup_already_made_still_keeps_it(league, monkeypatch):
    import espn_mcp.server as srv

    b, pushes = league
    monkeypatch.setattr(srv, "_expiry", lambda action, preview: None)
    mine = b.team_players(CFG.team_id)
    sit = next(p for p in mine if p["slot"] == "TE")
    up = next(p for p in mine if p["slot"] == "BE" and p["position"] == "TE")
    out = _tool("request_approval", action="start_player",
                params={"player": up["name"], "over": sit["name"]}, reasoning="x")
    # Before he answers, the lineup makes the same swap on its own.
    b.client.set_lineup(CFG.team_id, b.week(), [
        {"player_id": up["player_id"], "to_slot_id": 6},
        {"player_id": sit["player_id"], "to_slot_id": 20}])
    b.invalidate_rosters()
    writes = len(b.client.writes)
    p = srv.proposal_store().get(out["proposal"]["id"])
    with _client() as c:
        c.post(f"/p/{p['id']}/approve", headers={"Authorization": f"Bearer {p['token']}"})
    assert srv.proposal_store().get(p["id"])["status"] == "applied"
    assert len(b.client.writes) == writes and pushes[-1]["title"] == "Dodi: done"
    assert _tool("set_lineup")["manager_decisions"] == [
        f"start {up['name']} over {sit['name']}"]


# --- stored moves name players by id ------------------------------------------


def test_a_queued_move_is_stored_by_player_id_and_replayed_by_it(league):
    import espn_mcp.server as srv

    b, _ = league
    free = b.season_available()[0]
    fragment = free["name"].split()[-1]            # how an agent might name him
    out = _tool("request_approval", action="add_player", params={"add": free["name"]},
                reasoning="x")
    stored = srv.proposal_store().get(out["proposal"]["id"])
    assert stored["params"] == {"add": f"id:{free['player_id']}"}
    assert free["name"] in stored["summary"]
    # Asked for again under another spelling, it is the same move.
    same = _tool("request_approval", action="add_player",
                 params={"add": free["name"].upper()}, reasoning="x")
    assert same["queued"] is False and same["already"] == "pending"
    assert fragment

    with _client() as c:
        c.post(f"/p/{stored['id']}/approve",
               headers={"Authorization": f"Bearer {stored['token']}"})
    assert srv.proposal_store().get(stored["id"])["status"] == "applied"
    assert b.client.posts[0]["items"][0]["playerId"] == free["player_id"]


def test_an_id_reference_matches_that_player_only(league):
    import espn_mcp.server as srv

    b, _ = league
    mine = b.team_players(CFG.team_id)
    found, problems = srv._resolve([f"id:{mine[0]['player_id']}"], mine, "your roster")
    assert found == [mine[0]] and problems == []
    found, problems = srv._resolve(["id:999999999"], mine, "your roster")
    assert found == [] and problems[0]["problem"] == "not on your roster"
    # A number inside a name is still a name.
    assert srv._resolve(["id:12 extra"], mine, "your roster")[1]


def test_a_token_that_is_not_ascii_is_simply_wrong(store):
    p = make(store)
    assert store.authorized(p["id"], "t\u00f6ken\u2603") is None
    assert store.authorized("nope", "\u2603") is None
    assert store.authorized(p["id"], p["token"])["id"] == p["id"]
