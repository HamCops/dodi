"""In-season tools: rest-of-season math, lineups, waivers and trades.

Pure-function tests over hand-built rosters, plus end-to-end runs of the four
in-season tools over a stubbed ESPN client whose payloads are shaped like the
real ones (lineup slots, weekly stat splits, matchup schedule, pro schedule).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.scoring import parse_settings  # noqa: E402
from espn_mcp.season import (  # noqa: E402
    ROS_KEY,
    WEEK_KEY,
    attach_ros,
    describe_transaction,
    drop_candidates,
    evaluate_trade,
    lineup_changes,
    optimal_lineup,
    plan_lineup,
    plan_move,
    roster_profile,
    roster_violations,
    starting_slots,
    waiver_gain,
)
from test_integration import (  # noqa: E402
    CFG,
    SEASON,
    TEAMS,
    FakeClient,
    build_pool,
)
from test_value import make_shape  # noqa: E402

WEEK = 5


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


def player(name: str, pos: str, ros_pg: float, week: float | None = None,
           slot_id: int = 20, vorp: float | None = None, **extra) -> dict:
    return {
        "player_id": abs(hash(name)) % 100_000,
        "name": name,
        "position": pos,
        ROS_KEY: ros_pg,
        WEEK_KEY: ros_pg if week is None else week,
        "slot_id": slot_id,
        "vorp": ros_pg - 8 if vorp is None else vorp,
        **extra,
    }


def roster() -> list[dict]:
    # 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 D/ST; ESPN slots as set.
    return [
        player("QB A", "QB", 20, slot_id=0),
        player("RB A", "RB", 15, slot_id=2),
        player("RB B", "RB", 12, slot_id=2),
        player("RB C", "RB", 11, slot_id=20),           # bench, better than FLEX
        player("WR A", "WR", 14, slot_id=4),
        player("WR B", "WR", 10, slot_id=4),
        player("WR C", "WR", 9, week=13, slot_id=23),   # flex; great matchup this week
        player("TE A", "TE", 8, slot_id=6),
        player("TE B", "TE", 5, slot_id=20),
        player("K A", "K", 8, slot_id=17),
        player("DST A", "D/ST", 7, slot_id=16),
        player("WR D", "WR", 4, slot_id=20),
    ]


def test_attach_ros_subtracts_actuals_and_skips_a_future_bye():
    p = {"projected_points": 200.0, "season_points": 40.0, "bye_week": 9,
         "week_projections": {5: 12.5}}
    q = {"projected_points": 200.0, "season_points": 40.0, "bye_week": 3,
         "week_projections": {5: 12.5}}
    attach_ros([p, q], current_week=5, final_week=17)
    assert p["ros_points"] == 160.0
    assert p["games_remaining"] == 12   # weeks 5..17 minus the week-9 bye
    assert q["games_remaining"] == 13   # bye already happened
    assert p[ROS_KEY] > q[ROS_KEY]   # same ROS total over fewer games
    assert p[WEEK_KEY] == 12.5 and p["on_bye"] is False


def test_attach_ros_zeroes_the_bye_week_projection():
    p = {"projected_points": 100.0, "season_points": 0.0, "bye_week": 5,
         "week_projections": {5: 9.0}}
    attach_ros([p], current_week=5, final_week=17)
    assert p[WEEK_KEY] == 0.0 and p["on_bye"] is True


def test_starting_slots_fill_dedicated_before_flex():
    slots = starting_slots(make_shape())
    names = [s for s, _ in slots]
    assert names.index("FLEX") > max(names.index(x) for x in ("QB", "RB", "WR", "TE"))
    assert names.count("RB") == 2 and names.count("FLEX") == 1


def test_optimal_lineup_puts_best_bench_player_in_flex():
    best = optimal_lineup(roster(), make_shape(), ROS_KEY)
    flex = next(p for s, p in best["starters"] if s == "FLEX")
    assert flex["name"] == "RB C"
    assert best["total"] == 20 + 15 + 12 + 14 + 10 + 8 + 11 + 8 + 7


def test_lineup_changes_use_this_weeks_projection_not_ros():
    ch = lineup_changes(roster(), make_shape(), WEEK_KEY)
    # WR C projects 13 this week: he moves into a WR slot over WR B (10) and
    # RB C (11) takes the flex. +1 on the week.
    assert [p["name"] for p in ch["start"]] == ["RB C"]
    assert [p["name"] for p in ch["sit"]] == ["WR B"]
    assert ch["gain"] == 1
    # Rest of season WR C is a 9, so it is WR C who sits.
    ch = lineup_changes(roster(), make_shape(), ROS_KEY)
    assert [p["name"] for p in ch["start"]] == ["RB C"]
    assert [p["name"] for p in ch["sit"]] == ["WR C"]
    assert ch["gain"] == 2


def test_waiver_gain_is_zero_when_the_player_would_sit():
    shape = make_shape()
    g = waiver_gain(roster(), player("WR X", "WR", 6), shape)
    assert g["lineup_gain_ros_per_game"] == 0 and g["would_start_ros"] is False
    g = waiver_gain(roster(), player("WR X", "WR", 13), shape)
    # Takes WR B's slot (10); WR B cannot beat RB C (11) for the flex. +3.
    assert g["lineup_gain_ros_per_game"] == 3
    assert g["would_start_ros"] is True


def test_drop_candidates_are_bench_only_and_protect_kickers():
    shape = make_shape()
    drops = drop_candidates(roster() + [player("K B", "K", 7.5)], shape, limit=10)
    names = [p["name"] for p in drops]
    assert "WR D" in names and "TE B" in names
    assert "K B" not in names          # only when the incoming player is a K
    assert "RB C" not in names         # starts on the ROS lineup
    assert "WR C" not in names         # starts this week
    drops = drop_candidates(roster() + [player("K B", "K", 7.5)], shape,
                            limit=10, protect_position="K")
    assert "K B" in [p["name"] for p in drops]


def test_evaluate_trade_reports_both_sides_and_roster_limits():
    shape = make_shape()
    mine = roster()
    theirs = [player(f"T {i}", "WR", 9 + i) for i in range(4)] + [
        player("T RB", "RB", 6), player("T QB", "QB", 18), player("T TE", "TE", 6),
        player("T K", "K", 7), player("T DST", "D/ST", 6)]
    give = [p for p in mine if p["name"] == "RB C"]
    receive = [p for p in theirs if p["name"] == "T 3"]  # a 12-point WR
    ev = evaluate_trade(mine, theirs, give, receive, shape)
    # Me: T 3 (12) takes a WR slot, WR B (10) drops to the flex where RB C (11)
    # was: +1. Them: an empty RB slot gets RB C (+11), their WRs slide from
    # 12/11/flex 10 to 11/10/flex 9 (-3). Net +8.
    assert ev["me"]["delta"]["starters_ros_per_game"] == 1
    assert ev["them"]["delta"]["starters_ros_per_game"] == 8
    assert ev["me"]["must_drop"] == 0 and not ev["me"]["violations"]

    # 2-for-1 leaves them a body over the limit.
    two = [p for p in mine if p["name"] in ("RB C", "WR D")]
    ev = evaluate_trade(mine, theirs, two, receive, shape)
    assert ev["them"]["must_drop"] == 0  # their roster was short to begin with
    big = theirs + [player(f"pad {i}", "WR", 1) for i in range(7)]  # 16 = full
    ev = evaluate_trade(mine, big, two, receive, shape)
    assert ev["them"]["must_drop"] == 1
    assert "must drop 1" in ev["them"]["violations"][0]


def test_plan_lineup_moves_the_better_bench_player_in_and_the_starter_out():
    shape = make_shape()
    ps = [
        player("QB1", "QB", 20, slot_id=0),
        player("RB1", "RB", 15, slot_id=2), player("RB2", "RB", 12, slot_id=2),
        player("WR1", "WR", 11, slot_id=4), player("WR2", "WR", 6, slot_id=4),
        player("WR3", "WR", 9, slot_id=20),  # better than WR2, on the bench
        player("TE1", "TE", 8, slot_id=6),
        player("RB3", "RB", 10, slot_id=23),
        player("K", "K", 7, slot_id=17), player("D", "D/ST", 6, slot_id=16),
        player("IR guy", "WR", 30, slot_id=21),  # would start, but IR stays IR
    ]
    plan = plan_lineup(ps, shape, WEEK_KEY)
    moves = {(m["name"], m["from_slot"], m["to_slot"]) for m in plan["moves"]}
    assert moves == {("WR3", "BE", "WR"), ("WR2", "WR", "BE")}
    assert plan["gain"] == 3
    assert plan["unfilled"] == []
    assert "IR guy" not in {p["name"] for _, p in plan["starters"]}


def test_plan_lineup_keeps_locked_players_where_they_are():
    shape = make_shape()
    ps = [
        player("QB1", "QB", 20, slot_id=0),
        player("RB1", "RB", 15, slot_id=2), player("RB2", "RB", 12, slot_id=2),
        player("WR1", "WR", 11, slot_id=4),
        player("WR2", "WR", 6, slot_id=4, kickoff_ms=100),   # game started
        player("WR3", "WR", 9, slot_id=20, kickoff_ms=100),  # also started, on bench
        player("WR4", "WR", 8, slot_id=20, kickoff_ms=900),  # not yet
        player("TE1", "TE", 8, slot_id=6),
        player("RB3", "RB", 10, slot_id=23),
        player("K", "K", 7, slot_id=17), player("D", "D/ST", 6, slot_id=16),
    ]
    plan = plan_lineup(ps, shape, WEEK_KEY, now_ms=500)
    assert {p["name"] for p in plan["locked"]} == {"WR2", "WR3"}
    assert plan["moves"] == []  # WR2 is locked in; WR3 locked out
    assert plan["gain"] == 0
    # Once nothing is locked, WR3 should replace WR2.
    assert {m["name"] for m in plan_lineup(ps, shape, WEEK_KEY, now_ms=50)["moves"]} == {"WR2", "WR3"}


def test_plan_lineup_reports_slots_nobody_can_fill():
    shape = make_shape()
    ps = [player("QB1", "QB", 20, slot_id=0), player("RB1", "RB", 15, slot_id=2)]
    plan = plan_lineup(ps, shape, WEEK_KEY)
    assert "K" in plan["unfilled"] and "D/ST" in plan["unfilled"]
    assert plan["moves"] == []


def test_plan_move_swaps_out_the_weakest_occupant_of_a_full_slot():
    shape = make_shape()
    wr1 = player("WR1", "WR", 11, slot_id=4, eligible_slots=["WR", "FLEX", "BE"])
    wr2 = player("WR2", "WR", 6, slot_id=4, eligible_slots=["WR", "FLEX", "BE"])
    wr3 = player("WR3", "WR", 9, slot_id=20, eligible_slots=["WR", "FLEX", "BE"])
    plan = plan_move([wr1, wr2, wr3], shape, wr3, 4, WEEK_KEY)
    assert plan["displaced"]["name"] == "WR2"
    assert [(m["name"], m["to_slot"]) for m in plan["moves"]] == [("WR3", "WR"), ("WR2", "BE")]
    # Flex -> WR: the displaced WR takes the vacated flex slot.
    wr3["slot_id"] = 23
    plan = plan_move([wr1, wr2, wr3], shape, wr3, 4, WEEK_KEY)
    assert [(m["name"], m["to_slot"]) for m in plan["moves"]] == [("WR3", "WR"), ("WR2", "FLEX")]


def test_plan_move_refuses_illegal_moves():
    shape = make_shape()
    te = player("TE1", "TE", 8, slot_id=20, eligible_slots=["TE", "FLEX", "BE"],
                injury_status="ACTIVE")
    assert "not eligible" in plan_move([te], shape, te, 0, WEEK_KEY)["error"]  # TE -> QB
    assert "already" in plan_move([te], shape, te, 20, WEEK_KEY)["error"]
    assert "IR" in plan_move([te], shape, te, 21, WEEK_KEY)["error"]  # healthy -> IR
    te["kickoff_ms"] = 1
    assert "locked" in plan_move([te], shape, te, 6, WEEK_KEY, now_ms=2)["error"]
    # IR -> bench with the bench full.
    ir = player("IR guy", "WR", 9, slot_id=21, eligible_slots=["WR", "FLEX", "BE", "IR"])
    bench = [player(f"B{i}", "WR", 1, slot_id=20) for i in range(shape.lineup_slots[20])]
    assert "bench is full" in plan_move(bench + [ir], shape, ir, 20, WEEK_KEY)["error"]
    assert plan_move(bench[:-1] + [ir], shape, ir, 20, WEEK_KEY)["moves"][0]["to_slot"] == "BE"


def test_position_limits_are_enforced():
    shape = make_shape()
    object.__setattr__(shape, "position_limits", {"RB": 3})
    four_rbs = [player(f"RB {i}", "RB", 10) for i in range(4)]
    assert roster_violations(four_rbs, shape) == ["4 RB exceeds the league limit of 3"]


def test_roster_profile_starter_strength_counts_flex_toward_its_position():
    prof = roster_profile(roster(), make_shape())
    # RBs: A(15), B(12), C(11 in flex) -> avg 12.67
    assert prof["positions"]["RB"]["starter_avg"] == pytest.approx(12.67, abs=0.01)
    assert prof["positions"]["WR"]["weakest_starter"]["name"] == "WR B"
    assert [p["name"] for p in prof["positions"]["WR"]["bench"]] == ["WR C", "WR D"]


# --------------------------------------------------------------------------
# Tools over a stubbed in-season league
# --------------------------------------------------------------------------


def _slots_for(pos_list: list[str]) -> list[int]:
    """Assign ESPN lineup slot ids to a roster in draft order."""
    dedicated = {"QB": [0], "RB": [2, 2], "WR": [4, 4], "TE": [6], "K": [17], "D/ST": [16]}
    flex_left = 1
    out = []
    for pos in pos_list:
        if dedicated[pos]:
            out.append(dedicated[pos].pop())
        elif pos in ("RB", "WR", "TE") and flex_left:
            out.append(23)
            flex_left -= 1
        else:
            out.append(20)
    return out


class SeasonClient(FakeClient):
    """FakeClient plus the in-season views, with a snake-drafted league."""

    def __init__(self) -> None:
        super().__init__()
        pool = build_pool()
        # Draft: each team takes 16 in snake order, by fixture ordering that
        # roughly follows value (QBs, then RBs, ...). Good enough for lineups.
        order = sorted(pool, key=lambda e: e["player"]["ownership"]["averageDraftPosition"])
        self.drafted: dict[int, list[dict]] = {t: [] for t in range(1, TEAMS + 1)}
        # Give every team a legal-ish roster: 2 QB, 4 RB, 5 WR, 2 TE, 1 K, 1 D/ST, 1 extra.
        by_pos: dict[str, list[dict]] = {}
        for e in order:
            pos = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}[e["player"]["defaultPositionId"]]
            by_pos.setdefault(pos, []).append(e)
        want = [("QB", 2), ("RB", 4), ("WR", 5), ("TE", 2), ("K", 1), ("D/ST", 1)]
        for pos, n in want:
            for _ in range(n):
                for t in range(1, TEAMS + 1):
                    if by_pos[pos]:
                        self.drafted[t].append(by_pos[pos].pop(0))
        self.owner = {e["id"]: t for t, es in self.drafted.items() for e in es}

    def settings(self) -> dict:
        s = super().settings()
        s["status"] = {"isActive": True, "latestScoringPeriod": WEEK,
                       "currentMatchupPeriod": WEEK, "finalScoringPeriod": 17}
        s["settings"]["rosterSettings"]["positionLimits"] = {"2": 8, "3": 8, "1": 4}
        s["settings"]["acquisitionSettings"] = {
            "acquisitionType": "WAIVERS_TRADITIONAL", "isUsingAcquisitionBudget": False,
            "waiverHours": 24, "waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 8,
            "acquisitionLimit": -1, "matchupAcquisitionLimit": -1,
        }
        s["settings"]["tradeSettings"] = {"deadlineDate": 4102444800000, "vetoVotesRequired": 4}
        return s

    def _with_week(self, entry: dict, week: int) -> dict:
        e = {k: v for k, v in entry.items()}
        pl = dict(e["player"])
        proj = pl["stats"][0]["appliedTotal"]
        weekly = round(proj / 17, 2)
        pl["stats"] = list(pl["stats"]) + [
            {"seasonId": SEASON, "statSourceId": 1, "statSplitTypeId": 1,
             "scoringPeriodId": week, "appliedTotal": weekly, "stats": {}},
            {"seasonId": SEASON, "statSourceId": 0, "statSplitTypeId": 0,
             "scoringPeriodId": 0, "appliedTotal": round(weekly * (week - 1), 2), "stats": {}},
        ]
        e["player"] = pl
        owner = self.owner.get(e["id"])
        e["status"] = "ONTEAM" if owner else "WAIVERS"
        e["onTeamId"] = owner or 0
        if not owner:
            e["waiverProcessDate"] = 1790000000000
        return e

    def player_pool(self, week: int | None = None, **_: object) -> list[dict]:
        self.pool_calls += 1
        if not week:
            return build_pool()
        return [self._with_week(e, week) for e in build_pool()]

    def rosters(self, week: int) -> dict:
        teams = []
        for t in range(1, TEAMS + 1):
            entries = self.drafted[t]
            slots = _slots_for([{1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
                                [e["player"]["defaultPositionId"]] for e in entries])
            teams.append({
                "id": t, "name": f"Team {t}", "abbrev": f"T{t}", "waiverRank": t,
                "record": {"overall": {"wins": t % 3, "losses": 3 - t % 3, "ties": 0,
                                       "pointsFor": 100.0 * t, "pointsAgainst": 90.0}},
                "transactionCounter": {"acquisitionBudgetSpent": 0},
                "tradeBlock": {"players": {str(entries[3]["id"]): "ON_THE_BLOCK"}} if t == 4 else {},
                "roster": {"entries": [
                    {"playerId": e["id"],
                     "lineupSlotId": getattr(self, "slot_overrides", {}).get(e["id"], slot),
                     "acquisitionType": "DRAFT",
                     "playerPoolEntry": self._with_week(e, week)}
                    for e, slot in zip(entries, slots)
                ]},
            })
        return {"teams": teams}

    def set_lineup(self, team_id: int, week: int, moves: list[dict]) -> dict:
        self.writes = getattr(self, "writes", [])
        self.writes.append({"team_id": team_id, "week": week, "moves": moves})
        overrides = self.slot_overrides = getattr(self, "slot_overrides", {})
        for m in moves:
            overrides[int(m["player_id"])] = int(m["to_slot_id"])
        return {"status": "EXECUTED", "id": f"tx-{len(self.writes)}"}

    def transactions(self) -> list[dict]:
        """A draft pick, a lineup swap by team 1, and a waiver claim by team 2."""
        t1 = self.drafted[1]
        t2 = self.drafted[2]
        return [
            {"id": "d1", "type": "DRAFT", "status": "EXECUTED", "teamId": 1,
             "scoringPeriodId": 0, "proposedDate": 1_000_000,
             "items": [{"type": "DRAFT", "playerId": t1[0]["id"], "fromLineupSlotId": -1,
                        "toLineupSlotId": 0}]},
            {"id": "r1", "type": "ROSTER", "status": "EXECUTED", "teamId": 1,
             "scoringPeriodId": WEEK, "proposedDate": 3_000_000,
             "items": [{"type": "LINEUP", "playerId": t1[2]["id"], "fromLineupSlotId": 20,
                        "toLineupSlotId": 2},
                       {"type": "LINEUP", "playerId": t1[3]["id"], "fromLineupSlotId": 2,
                        "toLineupSlotId": 20}]},
            {"id": "w1", "type": "WAIVER", "status": "EXECUTED", "teamId": 2,
             "scoringPeriodId": WEEK, "proposedDate": 2_000_000, "bidAmount": 7,
             "items": [{"type": "ADD", "playerId": 999_999, "fromLineupSlotId": -1,
                        "toLineupSlotId": 20},
                       {"type": "DROP", "playerId": t2[-1]["id"], "fromLineupSlotId": 20,
                        "toLineupSlotId": -1}]},
        ]

    def matchups(self, week: int) -> list[dict]:
        ids = list(range(1, TEAMS + 1))
        out = []
        for w in range(1, 15):
            rot = ids[:1] + ids[1:][-(w - 1) % (TEAMS - 1):] + ids[1:][:-(w - 1) % (TEAMS - 1) or None]
            for i in range(TEAMS // 2):
                out.append({"matchupPeriodId": w, "winner": "UNDECIDED",
                            "home": {"teamId": rot[i], "totalPoints": 0.0,
                                     "totalProjectedPoints": 100.0, "winProbability": 0.55},
                            "away": {"teamId": rot[-1 - i], "totalPoints": 0.0,
                                     "totalProjectedPoints": 95.0}})
        return out

    def pro_schedule(self) -> dict[int, dict]:
        # Every fixture player is on proTeamId 12; give it a week-7 bye and a
        # week-5 opponent so the enrichment path is exercised.
        return {
            12: {"abbrev": "KC", "bye": 7,
                 "games": {w: {"opponent_id": 7, "home": w % 2 == 0, "kickoff_ms": 1790000000000}
                           for w in range(1, 18) if w != 7}},
            7: {"abbrev": "DEN", "bye": 9, "games": {}},
        }

    def positional_ratings(self, week: int) -> dict:
        return {2: {7: {"average": 25.0, "rank": 3}}}  # RBs vs DEN: a soft matchup


def season_board() -> DraftBoard:
    return DraftBoard(CFG, client=SeasonClient())


def call(name: str, **kw):
    import espn_mcp.server as srv

    srv._board = season_board()
    try:
        result = asyncio.run(srv.mcp.call_tool(name, kw))
    finally:
        srv._board = None
    data = getattr(result, "structured_content", None)
    if data is None:
        import json

        data = json.loads(result.content[0].text)
    return data


def test_shape_parses_in_season_settings():
    shape = parse_settings(SeasonClient().settings())
    assert shape.is_active and shape.current_week == WEEK and shape.final_week == 17
    assert shape.position_limits == {"QB": 4, "RB": 8, "WR": 8}
    assert shape.waivers["type"] == "WAIVERS_TRADITIONAL"
    assert shape.waivers["uses_faab"] is False
    assert shape.season_describe()["trade_deadline_passed"] is False


def test_season_board_values_rest_of_season_and_tags_the_week():
    b = season_board()
    board = b.season_board()
    assert board["week"] == WEEK
    p = board["players"][0]
    assert p["ros_points"] == pytest.approx(p["projected_points"] - p["season_points"], abs=0.01)
    assert p[WEEK_KEY] is not None
    assert p["nfl_opponent"] in ("DEN", "@DEN")
    assert p["bye_week"] == 7 and p["games_remaining"] == 12
    # VORP is over ROS points, not the full season.
    assert p["vorp"] == pytest.approx(p["ros_points"] - p["replacement_points"], abs=0.01)
    rb = next(q for q in board["players"] if q["position"] == "RB")
    assert rb["opp_rank_vs_pos"] == 3


def test_team_players_carry_lineup_slots_and_rostered_ids_drive_availability():
    b = season_board()
    mine = b.team_players(CFG.team_id)
    assert len(mine) == 15
    assert {p["slot"] for p in mine} >= {"QB", "RB", "WR", "TE", "FLEX", "K", "D/ST", "BE"}
    taken = b.rostered_ids()
    assert all(p["player_id"] not in taken for p in b.season_available())
    assert len(b.season_available()) == 218 - 15 * TEAMS


def test_get_matchup_finds_opponent_and_start_sit():
    out = call("get_matchup")
    assert out["week"] == WEEK
    assert out["me"]["team_id"] == CFG.team_id
    assert out["opponent"]["team_id"] != CFG.team_id
    assert out["espn"]["my_win_probability"] in (0.55, 0.45)
    lineup = out["my_lineup"]
    assert lineup["optimal_total"] >= lineup["set_total"]
    assert lineup["gain_from_optimal"] == pytest.approx(
        lineup["optimal_total"] - lineup["set_total"], abs=0.01)
    assert len(lineup["starters_now"]) == 9
    assert out["opponent_lineup"]["starters_now"]
    assert "margin_if_both_optimal" in out


def test_get_matchup_next_week_uses_that_weeks_projection():
    out = call("get_matchup", week=WEEK + 1)
    assert out["week"] == WEEK + 1
    assert out["my_lineup"]["starters_now"][0]["week_proj"] is not None


def test_get_matchup_flags_bye_holes():
    out = call("get_matchup", week=7)  # every fixture player is on bye
    assert out["my_lineup"]["holes"]
    assert all(h["why"] == "bye" for h in out["my_lineup"]["holes"])
    assert out["my_lineup"]["optimal_total"] == 0


def _unlock(monkeypatch):
    """Fixture kickoffs are in the past; pretend it is before them."""
    import espn_mcp.server as srv
    monkeypatch.setattr(srv, "_now_ms", lambda: 0)


def _tool(name: str, **kw):
    """`call` without swapping the board, so writes persist across calls."""
    import espn_mcp.server as srv

    result = asyncio.run(srv.mcp.call_tool(name, kw))
    data = getattr(result, "structured_content", None)
    if data is None:
        import json

        data = json.loads(result.content[0].text)
    return data


def test_set_lineup_previews_then_applies_the_matchup_swaps(monkeypatch):
    _unlock(monkeypatch)
    import espn_mcp.server as srv

    b = season_board()
    srv._board = b
    # The fixture lineup starts optimal: bench the best RB and start the worst.
    rbs = sorted((p for p in b.team_players(CFG.team_id) if p["position"] == "RB"),
                 key=lambda p: -p[WEEK_KEY])
    b.client.slot_overrides = {rbs[0]["player_id"]: 20, rbs[-1]["player_id"]: 2}
    b.league_rosters(refresh=True)
    try:
        matchup = _tool("get_matchup")
        assert matchup["my_lineup"]["gain_from_optimal"] > 0
        preview = _tool("set_lineup")
        assert preview["applied"] is False
        assert preview["gain"] == pytest.approx(matchup["my_lineup"]["gain_from_optimal"], abs=0.01)
        assert {m["player"] for m in preview["moves"]} >= {
            p["name"] for p in matchup["my_lineup"]["start"] + matchup["my_lineup"]["sit"]}
        assert len(preview["starters_after"]) == 9
        # Slots are the planned ones, not where the player sits now.
        assert {s["slot"] for s in preview["starters_after"]} == {
            "QB", "RB", "WR", "TE", "FLEX", "K", "D/ST"}
        assert not hasattr(b.client, "writes")

        done = _tool("set_lineup", apply=True)
        assert done["applied"] is True and done["espn_status"] == "EXECUTED"
        assert b.client.writes[0]["team_id"] == CFG.team_id
        assert b.client.writes[0]["week"] == WEEK
        assert {m["player_id"] for m in b.client.writes[0]["moves"]} == {
            p["id"] for p in matchup["my_lineup"]["start"] + matchup["my_lineup"]["sit"]}

        # The roster cache was refreshed: the lineup is now optimal.
        again = _tool("set_lineup", apply=True)
        assert again["moves"] == [] and again["applied"] is False
        assert len(b.client.writes) == 1
        after = _tool("get_matchup")
        assert after["my_lineup"]["gain_from_optimal"] == 0
    finally:
        srv._board = None


def test_set_lineup_locks_players_whose_game_started():
    # Fixture kickoffs are in the past relative to the real clock.
    out = call("set_lineup")
    assert out["locked"]
    assert out["moves"] == []


def test_move_player_swaps_into_a_full_slot_and_writes_it(monkeypatch):
    _unlock(monkeypatch)
    import espn_mcp.server as srv

    b = season_board()
    srv._board = b
    try:
        mine = b.team_players(CFG.team_id)
        bench_wr = next(p for p in mine if p["position"] == "WR" and p["slot"] == "BE")
        out = _tool("move_player", player=bench_wr["name"], to_slot="wr")
        assert out["applied"] is True
        assert out["moves"][0] == {"player": bench_wr["name"], "from": "BE", "to": "WR"}
        assert out["displaced"] and out["moves"][1]["from"] == "WR"
        assert b.client.writes[0]["moves"][0]["to_slot_id"] == 4
        assert next(p for p in b.team_players(CFG.team_id)
                    if p["player_id"] == bench_wr["player_id"])["slot"] == "WR"

        bad = _tool("move_player", player=bench_wr["name"], to_slot="QB")
        assert "not eligible" in bad["error"]
        nope = _tool("move_player", player="Nobody Real", to_slot="BE")
        assert "not on your roster" in nope["error"]
        slot = _tool("move_player", player=bench_wr["name"], to_slot="LB")
        assert "Unknown slot" in slot["error"]
    finally:
        srv._board = None


def test_get_waiver_targets_ranks_by_lineup_gain_and_suggests_drops():
    out = call("get_waiver_targets", limit=5)
    assert out["my_waiver_priority"] == CFG.team_id
    assert out["waiver_rules"]["type"] == "WAIVERS_TRADITIONAL"
    targets = out["targets"]
    assert len(targets) == 5
    gains = [t["lineup_gain_ros_per_game"] for t in targets]
    assert gains == sorted(gains, reverse=True)
    assert all(t["status"] == "WAIVERS" and t["waivers_clear"] for t in targets)
    drops = out["drop_candidates"]
    assert drops and all(d["pos"] not in ("K", "D/ST") for d in drops)
    # A kicker search may propose dropping a kicker; a WR search never does.
    out = call("get_waiver_targets", position="WR")
    assert all(t["pos"] == "WR" for t in out["targets"])


def test_analyze_trade_infers_partner_and_checks_limits():
    b = season_board()
    mine = b.team_players(CFG.team_id)
    partner = next(t for t in b.league_rosters() if t != CFG.team_id)
    theirs = b.team_players(partner)
    my_rb = next(p for p in mine if p["position"] == "RB")
    their_wr = next(p for p in theirs if p["position"] == "WR")

    out = call("analyze_trade", give=[my_rb["name"]], receive=[their_wr["name"]])
    assert out["partner"]["team_id"] == partner
    assert out["give"][0]["name"] == my_rb["name"]
    for side in ("me", "them"):
        assert out[side]["must_drop"] == 0
        assert set(out[side]["delta"]) == {"starters_ros_per_game", "starters_this_week", "bench_ros_vorp"}
        assert len(out[side]["lineup_after"]) == 9
    assert "trade_deadline" in out and out["veto_votes_required"] == 4

    # 2-for-1 pushes them over the roster limit and asks for a drop.
    my_wr = next(p for p in mine if p["position"] == "WR")
    out = call("analyze_trade", give=[my_rb["name"], my_wr["name"]],
               receive=[their_wr["name"]], partner_team_id=partner)
    assert out["them"]["must_drop"] == 0  # fixture rosters are 15 of 16
    assert out["me"]["after"]["roster_size"] == 14


def test_analyze_trade_rejects_unknown_or_ambiguous_names():
    out = call("analyze_trade", give=["Nobody"], receive=["RB Player 1"], partner_team_id=1)
    assert out["error"] and out["problems"][0]["problem"].startswith("not on")
    out = call("analyze_trade", give=["RB Player"], receive=["WR Player 1"], partner_team_id=1)
    assert out["problems"][0]["problem"] == "ambiguous"


def test_find_trade_partners_reports_needs_and_mutual_swaps():
    out = call("find_trade_partners")
    assert set(out["league_starter_avg_ros_per_game"]) >= {"QB", "RB", "WR", "TE"}
    assert "my_needs" in out and "partners" in out
    for t in out["partners"]:
        assert t["they_could_send"] and t["they_could_send"][0]["my_lineup_gain"] > 0
        if t["best_1_for_1"]:
            assert {"give", "receive", "my_gain", "their_gain"} <= set(t["best_1_for_1"])
    # The team with a trade block advertises it.
    t4 = next((t for t in out["partners"] if t["team_id"] == 4), None)
    if t4:
        assert t4["trade_block"]
    out = call("find_trade_partners", position="TE")
    assert all(p["pos"] == "TE" for t in out["partners"] for p in t["they_could_send"])


def test_get_roster_switches_to_the_season_view():
    out = call("get_roster")
    assert out["week"] == WEEK
    assert len(out["starters"]) == 9
    assert "starters_ros_per_game" in out and out["position_limits"]["RB"] == 8


def test_draft_tools_still_work_in_season():
    """The draft board and the season board are separate caches."""
    out = call("get_available_players", limit=3)
    assert out["count"] == 3 and "proj" in out["players"][0]


def test_describe_transaction_reads_lineup_moves_and_waivers():
    names = {10: "A. Back", 11: "B. Back", 12: "C. Wideout"}
    teams = {1: "Alpha", 2: "Bravo"}
    move = describe_transaction(
        {"id": "x", "type": "ROSTER", "status": "EXECUTED", "teamId": 1,
         "scoringPeriodId": 3, "proposedDate": 5,
         "items": [{"type": "LINEUP", "playerId": 10, "fromLineupSlotId": 20, "toLineupSlotId": 23},
                   {"type": "LINEUP", "playerId": 11, "fromLineupSlotId": 23, "toLineupSlotId": 20}]},
        names.get, teams.get)
    assert move["kind"] == "lineup" and move["team"] == "Alpha" and move["week"] == 3
    assert move["items"][0] == {"action": "LINEUP", "player_id": 10, "player": "A. Back",
                                "from": "BE", "to": "FLEX"}
    assert move["summary"] == "A. Back BE -> FLEX; B. Back FLEX -> BE"

    claim = describe_transaction(
        {"id": "y", "type": "WAIVER", "status": "EXECUTED", "teamId": 2, "bidAmount": 12,
         "scoringPeriodId": 3, "proposedDate": 6,
         "items": [{"type": "ADD", "playerId": 12, "fromLineupSlotId": -1, "toLineupSlotId": 20},
                   {"type": "DROP", "playerId": 77, "fromLineupSlotId": 20, "toLineupSlotId": -1}]},
        names.get, teams.get)
    assert claim["kind"] == "waiver" and claim["bid"] == 12
    assert claim["summary"] == "+C. Wideout; -player 77"
    assert claim["items"][1]["from"] == "BE" and "to" not in claim["items"][1]

    trade = describe_transaction(
        {"id": "z", "type": "TRADE_ACCEPT", "status": "EXECUTED", "teamId": 1,
         "scoringPeriodId": 3, "proposedDate": 7,
         "items": [{"type": "TRADE", "playerId": 10, "fromTeamId": 1, "toTeamId": 2}]},
        names.get, teams.get)
    assert trade["kind"] == "trade"
    assert trade["items"][0]["from_team"] == "Alpha" and trade["items"][0]["to_team"] == "Bravo"


def test_get_transactions_hides_draft_filters_and_names_players():
    out = call("get_transactions")
    kinds = [t["kind"] for t in out["transactions"]]
    assert "draft" not in kinds
    assert kinds == ["lineup", "waiver"]  # newest first
    assert out["total"] == 2 and out["my_team_id"] == CFG.team_id
    move = out["transactions"][0]
    assert move["team_id"] == 1 and move["week"] == WEEK and move["when"]
    assert all(i["player"] and not i["player"].startswith("player ") for i in move["items"])
    assert move["items"][0]["to"] == "RB" and move["items"][1]["to"] == "BE"

    claim = out["transactions"][1]
    assert claim["bid"] == 7
    # A player outside every roster and the pool falls back to the id.
    assert claim["items"][0]["player"] == "player 999999"

    only_two = call("get_transactions", team_id=2)
    assert [t["team_id"] for t in only_two["transactions"]] == [2]
    assert only_two["team"]

    drafted = call("get_transactions", kind="draft")
    assert [t["kind"] for t in drafted["transactions"]] == ["draft"]

    with_draft = call("get_transactions", include_draft=True, limit=1)
    assert with_draft["shown"] == 1 and with_draft["total"] == 3
