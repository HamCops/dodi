"""End-to-end tests over a stubbed ESPN client.

Exercises the real normalize -> value -> board -> draft-state path with
payloads shaped exactly like ESPN's, so the wiring is verified without
credentials or network access.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import Config  # noqa: E402

SEASON = 2026
TEAMS = 10

# Manual picks persist to disk; point tests at a throwaway dir so they never
# touch (or inherit) the project's real draft state.
_STATE_DIR = tempfile.mkdtemp(prefix="espn-mcp-test-")

CFG = Config(
    league_id="999",
    season=SEASON,
    team_id=3,
    espn_s2="x",
    swid="{y}",
    pool_ttl=900,
    state_dir=_STATE_DIR,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with no recorded picks."""
    for f in Path(_STATE_DIR).glob("*.json"):
        f.unlink()
    yield

POSITION_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}
ELIGIBLE = {
    "QB": [0, 20],
    "RB": [2, 23, 20],
    "WR": [4, 23, 20],
    "TE": [6, 23, 20],
    "K": [17, 20],
    "D/ST": [16, 20],
}


def player_entry(pid: int, name: str, pos: str, proj: float, adp: float) -> dict:
    return {
        "id": pid,
        "player": {
            "id": pid,
            "fullName": name,
            "defaultPositionId": POSITION_IDS[pos],
            "proTeamId": 12,
            "eligibleSlots": ELIGIBLE[pos],
            "injuryStatus": "ACTIVE",
            "ownership": {"averageDraftPosition": adp, "percentOwned": 99.0,
                          "percentStarted": 90.0, "auctionValueAverage": 20.0,
                          "averageDraftPositionPercentChange": 0.0,
                          "percentChange": 0.0, "date": 1786501821623},
            "draftRanksByRankType": {"PPR": {"rank": pid}, "STANDARD": {"rank": pid}},
            "stats": [
                {
                    "seasonId": SEASON,
                    "statSourceId": 1,
                    "statSplitTypeId": 0,
                    "scoringPeriodId": 0,
                    "appliedTotal": proj,
                    "stats": {},
                }
            ],
        },
    }


def build_pool() -> list[dict]:
    pool, pid = [], 1
    spec = [("QB", 24, 380, 6), ("RB", 60, 320, 3.5), ("WR", 70, 300, 2.8),
            ("TE", 24, 240, 7), ("K", 20, 140, 1.5), ("D/ST", 20, 150, 2.5)]
    for pos, count, top, step in spec:
        for i in range(count):
            proj = round(top - i * step, 1)
            # ESPN gives team defenses negative ids; keep the fixture faithful.
            ident = -(16001 + i) if pos == "D/ST" else pid
            pool.append(player_entry(ident, f"{pos} Player {i + 1}", pos, proj, float(pid)))
            pid += 1
    return pool


class FakeClient:
    """Stands in for ESPNClient with ESPN-shaped payloads."""

    def __init__(self, picks: list[dict] | None = None) -> None:
        self.picks = picks or []
        self.pool_calls = 0
        self.draft_calls = 0

    def settings(self) -> dict:
        return {
            "settings": {
                "name": "Test League",
                "size": TEAMS,
                "draftSettings": {
                    "type": "SNAKE",
                    "timePerSelection": 90,
                    "pickOrder": list(range(1, TEAMS + 1)),
                },
                "rosterSettings": {
                    "lineupSlotCounts": {
                        "0": 1, "2": 2, "4": 2, "6": 1, "23": 1,
                        "16": 1, "17": 1, "20": 7, "21": 1,
                    }
                },
                "scoringSettings": {
                    "scoringItems": [
                        {"statId": 53, "points": 1.0},
                        {"statId": 42, "points": 0.1},
                    ]
                },
            }
        }

    def teams(self) -> dict:
        return {
            "teams": [
                {"id": i, "location": "Team", "nickname": str(i), "abbrev": f"T{i}",
                 "roster": {"entries": []}}
                for i in range(1, TEAMS + 1)
            ]
        }

    def draft_detail(self) -> dict:
        self.draft_calls += 1
        return {"draftDetail": {"drafted": False, "inProgress": True, "picks": self.picks}}

    def player_pool(self, **_: object) -> list[dict]:
        self.pool_calls += 1
        return build_pool()


def espn_pick(overall: int, player_id: int) -> dict:
    team = DraftBoard.team_at_pick(overall, list(range(1, TEAMS + 1)), TEAMS)
    return {
        "overallPickNumber": overall,
        "roundId": (overall - 1) // TEAMS + 1,
        "roundPickNumber": (overall - 1) % TEAMS + 1,
        "teamId": team,
        "playerId": player_id,
        "autoDraftTypeId": 0,
    }


def make_board(picks: list[dict] | None = None) -> DraftBoard:
    return DraftBoard(CFG, client=FakeClient(picks))


def test_league_shape_parses():
    shape = make_board().shape()
    info = shape.describe()
    assert info["teams"] == TEAMS
    assert info["scoring_format"] == "PPR"
    assert info["starting_lineup"] == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1,
                                       "K": 1, "FLEX": 1}
    assert info["bench_spots"] == 7


def test_board_builds_and_caches():
    b = make_board()
    first = b.board()
    b.board()
    assert b.client.pool_calls == 1  # second call served from cache
    assert len(first["players"]) == 218
    assert first["players"][0]["overall_value_rank"] == 1


def test_rb_outranks_qb_despite_lower_projection():
    players = make_board().board()["by_id"]
    qb1 = next(p for p in players.values() if p["name"] == "QB Player 1")
    rb1 = next(p for p in players.values() if p["name"] == "RB Player 1")
    assert qb1["projected_points"] > rb1["projected_points"]
    assert rb1["overall_value_rank"] < qb1["overall_value_rank"]


def test_drafted_players_leave_the_available_pool():
    b = make_board()
    top = b.available(limit=1)[0]
    b2 = make_board(picks=[espn_pick(1, top["player_id"])])
    assert top["player_id"] not in {p["player_id"] for p in b2.available(limit=50)}
    assert b2.available(limit=1)[0]["player_id"] != top["player_id"]


def test_draft_state_tracks_clock_and_my_turn():
    picks = [espn_pick(i, i) for i in range(1, 6)]  # 5 picks made
    b = make_board(picks)
    state = b.draft_state()
    assert state["picks_made"] == 5
    assert state["next_overall_pick"] == 6
    assert state["on_the_clock_team_id"] == 6
    # Team 3 picks at 3 (taken), then 18 in the reversed round 2.
    assert b.upcoming_picks_for(3, count=2) == [18, 23]


def test_manual_picks_merge_with_espn_picks():
    b = make_board(picks=[espn_pick(1, 1)])
    b.record_manual_pick(player_id=25, team_id=2)
    state = b.draft_state()
    assert state["picks_made"] == 2
    assert state["picks"][-1]["source"] == "manual"
    assert 25 in b.drafted_ids()
    b.undo_manual_pick()
    assert b.draft_state()["picks_made"] == 1


class TestManualPickFallback:
    """Insurance for a draft where ESPN stops reporting picks. Has to work by
    name at speed -- looking up numeric ids on a 60-second clock is not viable."""

    def setup_board(self):
        import espn_mcp.server as srv

        b = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
        srv._board = b
        return srv, b

    def test_records_by_partial_name_and_infers_the_team(self):
        srv, b = self.setup_board()
        out = srv.record_pick.__wrapped__(player="RB Player 1")
        srv._board = None

        assert "error" not in out
        assert "RB Player 1" in out["recorded"]
        assert out["pick"]["team_id"] == 1  # whoever the schedule says picks first
        assert out["picks_made"] == 1
        assert out["on_the_clock_team_id"] == 2

    def test_recorded_player_leaves_the_pool(self):
        srv, b = self.setup_board()
        srv.record_pick.__wrapped__(player="RB Player 1")
        srv._board = None
        names = {p["name"] for p in b.available(limit=10_000)}
        assert "RB Player 1" not in names

    def test_ambiguous_name_records_nothing(self):
        srv, b = self.setup_board()
        out = srv.record_pick.__wrapped__(player="RB Player 1")  # also matches 10..19
        srv._board = None
        # "RB Player 1" is an exact match despite prefixing others, so it lands.
        assert "error" not in out

        srv, b = self.setup_board()
        b.clear_manual_picks()  # picks persist to disk; start this half clean
        out = srv.record_pick.__wrapped__(player="RB Player")
        srv._board = None
        assert "ambiguous" in out["error"].lower()
        assert len(out["candidates"]) > 1
        assert b.draft_state()["picks_made"] == 0

    def test_double_recording_is_refused(self):
        srv, b = self.setup_board()
        srv.record_pick.__wrapped__(player="RB Player 1")
        out = srv.record_pick.__wrapped__(player="RB Player 1")
        srv._board = None
        assert "already drafted" in out["error"]
        assert b.draft_state()["picks_made"] == 1

    def test_unknown_name_is_refused(self):
        srv, b = self.setup_board()
        out = srv.record_pick.__wrapped__(player="Nobody At All")
        srv._board = None
        assert "No player matching" in out["error"]
        assert b.draft_state()["picks_made"] == 0

    def test_undo_restores_the_pool(self):
        srv, b = self.setup_board()
        srv.record_pick.__wrapped__(player="RB Player 1")
        srv.undo_pick.__wrapped__()
        srv._board = None
        assert b.draft_state()["picks_made"] == 0
        assert "RB Player 1" in {p["name"] for p in b.available(limit=10_000)}


def test_available_keeps_kickers_and_defenses_below_skill_players():
    """Regression: the value board demoted K/DST, but available() re-sorted by
    raw VORP and undid it, floating defenses to the top of the board mid-draft.
    """
    b = make_board()
    top = b.available(limit=25)
    assert all(not p["late_round_position"] for p in top), (
        "a kicker or defense reached the top of the available list")

    # Filtering by the position still gives a sensibly ordered list.
    ks = b.available(position="K", limit=5)
    assert [p["name"] for p in ks] == [p["name"] for p in
                                       sorted(ks, key=lambda p: -p["vorp"])]


class TestFastPath:
    """Draft-night ergonomics: batch recording, persistence, one compact call.

    ESPN publishes no picks until a draft ends, so manual picks ARE the draft.
    Re-stating every pick each turn is what actually blew the 60-second clock.
    """

    def setup_board(self):
        import espn_mcp.server as srv

        b = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
        b.clear_manual_picks()
        srv._board = b
        return srv, b

    def test_batch_records_in_order_with_one_state_read(self):
        srv, b = self.setup_board()
        b.client.draft_calls = 0
        out = srv.record_picks.__wrapped__(
            players=["RB Player 1", "WR Player 1", "QB Player 1"])
        srv._board = None

        assert out["newly_recorded"] == 3
        assert out["picks_made"] == 3
        # One read for the batch, one for the summary -- not one per pick.
        assert b.client.draft_calls <= 3
        overalls = [p["overall"] for p in b.draft_state()["picks"]]
        assert overalls == [1, 2, 3]

    def test_batch_assigns_each_pick_to_the_right_team(self):
        srv, b = self.setup_board()
        srv.record_picks.__wrapped__(players=["RB Player 1", "WR Player 1"])
        srv._board = None
        picks = b.draft_state()["picks"]
        assert [p["team_id"] for p in picks] == [1, 2]

    def test_one_bad_name_does_not_block_the_rest(self):
        srv, b = self.setup_board()
        out = srv.record_picks.__wrapped__(
            players=["RB Player 1", "Nobody At All", "WR Player 1"])
        srv._board = None
        assert out["newly_recorded"] == 2
        assert [s["name"] for s in out["needs_attention"]] == ["Nobody At All"]

    def test_resending_the_whole_board_is_a_no_op(self):
        """Pasting the full pick history each turn is the intended sync path."""
        srv, b = self.setup_board()
        names = ["RB Player 1", "WR Player 1", "QB Player 1"]
        srv.record_picks.__wrapped__(players=names)
        again = srv.record_picks.__wrapped__(players=names + ["TE Player 1"])
        srv._board = None

        assert again["newly_recorded"] == 1      # only the genuinely new name
        assert again["already_had"] == 3
        assert "needs_attention" not in again    # known picks are not noise
        assert again["picks_made"] == 4
        overalls = [p["overall"] for p in b.draft_state()["picks"]]
        assert overalls == [1, 2, 3, 4]          # order preserved, no duplicates

    def test_picks_survive_a_fresh_board(self):
        srv, b = self.setup_board()
        srv.record_picks.__wrapped__(players=["RB Player 1", "WR Player 1"])
        srv._board = None
        # A new process must not have to be re-told the whole draft.
        again = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
        assert again.draft_state()["picks_made"] == 2
        again.clear_manual_picks()

    def test_reset_clears_persisted_state(self):
        srv, b = self.setup_board()
        srv.record_picks.__wrapped__(players=["RB Player 1"])
        out = srv.reset_draft.__wrapped__()
        srv._board = None
        assert out["cleared"] == 1
        assert make_board().draft_state()["picks_made"] == 0

    def test_next_pick_only_offers_players_who_fill_a_hole(self):
        srv, b = self.setup_board()
        out = srv.next_pick.__wrapped__(candidates=5)
        srv._board = None
        assert out["is_my_turn"] is False  # team 1 opens; we are team 3
        fillable = set(out["unfilled_starting_slots"]) | {"RB", "WR", "TE"}
        assert all(c["pos"] in fillable for c in out["candidates"])
        assert all("left_in_tier" in c for c in out["candidates"])

    def test_next_pick_payload_stays_small(self):
        import json

        srv, b = self.setup_board()
        out = srv.next_pick.__wrapped__(candidates=5)
        srv._board = None
        assert len(json.dumps(out)) < 3000, "on-the-clock payload must stay compact"


def test_position_filter_and_sorting():
    b = make_board()
    rbs = b.available(position="RB", limit=5)
    assert {p["position"] for p in rbs} == {"RB"}
    by_adp = b.available(limit=5, sort_by="espn_adp")
    adps = [p["espn_adp"] for p in by_adp]
    assert adps == sorted(adps)  # ADP sorts ascending, best first


def placeholder_pick(overall: int) -> dict:
    """An unstarted ESPN draft returns a full slate of picks with playerId -1."""
    p = espn_pick(overall, -1)
    p["playerId"] = -1
    return p


def test_drafted_defenses_are_counted_despite_negative_ids():
    """D/ST player ids are negative (-16001..-16034).

    Filtering placeholders with `playerId > 0` silently dropped every drafted
    defense: the pick count drifted and defenses stayed in the available pool
    after being taken. -1 must be matched exactly, not treated as "non-positive".
    """
    b = make_board()
    dst = next(p for p in b.board()["players"] if p["position"] == "D/ST")
    assert dst["player_id"] < 0, "fixture must use ESPN's negative D/ST ids"

    picks = [placeholder_pick(i) for i in range(1, TEAMS * 17 + 1)]
    picks[0]["playerId"] = dst["player_id"]
    b2 = make_board(picks=picks)

    state = b2.draft_state()
    assert state["picks_made"] == 1
    assert dst["player_id"] in b2.drafted_ids(state)
    assert dst["player_id"] not in {p["player_id"] for p in b2.available(limit=10_000)}


def test_placeholder_picks_are_not_counted_as_picks():
    """ESPN pre-seeds every slot of an unstarted draft with playerId -1."""
    b = make_board(picks=[placeholder_pick(i) for i in range(1, TEAMS * 17 + 1)])
    state = b.draft_state()
    assert state["picks_made"] == 0
    assert state["complete"] is False
    assert state["next_overall_pick"] == 1
    assert state["on_the_clock_team_id"] == 1
    assert b.drafted_ids(state) == set()
    # The full board must still be available before the draft starts.
    assert len(b.available(limit=10_000)) == 218


def scheduled_pick(overall: int, team_id: int, player_id: int = -1) -> dict:
    """A slot in ESPN's published schedule, drafted or not."""
    return {
        "overallPickNumber": overall,
        "roundId": (overall - 1) // TEAMS + 1,
        "roundPickNumber": (overall - 1) % TEAMS + 1,
        "teamId": team_id,
        "playerId": player_id,
        "autoDraftTypeId": 0,
    }


def schedule_for(order: list[int], rounds: int = 17) -> list[dict]:
    """Snake out a full pick schedule the way ESPN publishes it."""
    picks = []
    for rnd in range(1, rounds + 1):
        seq = order if rnd % 2 == 1 else list(reversed(order))
        for i, team in enumerate(seq):
            picks.append(scheduled_pick((rnd - 1) * TEAMS + i + 1, team))
    return picks


def test_pick_order_comes_from_espn_schedule_not_settings():
    """Leagues that randomize the order rewrite the schedule; settings may lag."""
    randomized = [7, 2, 12, 9, 1, 13, 3, 10, 6, 8]
    b = make_board(picks=schedule_for(randomized))
    state = b.draft_state()

    assert state["pick_order_source"] == "espn_schedule"
    assert state["pick_order"] == randomized
    # FakeClient's settings still report the old ascending order.
    assert b.shape().pick_order != randomized

    assert b.my_slot(12, state) == 3
    assert state["on_the_clock_team_id"] == 7
    # Team 12 drafts 3rd, so picks 3 and 18 (round two reverses).
    assert b.upcoming_picks_for(12, count=2, state=state) == [3, 18]


def test_randomized_order_survives_a_stale_settings_cache():
    """A session opened before the draw must not pin the provisional order."""
    b = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
    assert b.my_slot(10) == 10  # provisional: teams in id order

    randomized = [10, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    b.client.picks = schedule_for(randomized)
    # No refresh call, no cache expiry -- draft state is read live every time.
    state = b.draft_state()
    assert state["pick_order"] == randomized
    assert b.my_slot(10, state) == 1
    assert b.upcoming_picks_for(10, count=2, state=state) == [1, 20]


def test_provisional_order_is_flagged_before_randomization():
    import espn_mcp.server as srv

    srv._board = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
    provisional = srv.get_league_settings.__wrapped__()
    srv._board = make_board(picks=schedule_for([7, 2, 12, 9, 1, 13, 3, 10, 6, 8]))
    drawn = srv.get_league_settings.__wrapped__()
    srv._board = None

    assert provisional["draft_slot_is_provisional"] is True
    assert "randomized" in provisional["draft_slot_warning"]
    assert drawn["draft_slot_is_provisional"] is False
    assert drawn["my_draft_slot"] == 7  # team 3 sits 7th in the drawn order


def test_refresh_draft_order_reports_the_new_slot():
    import espn_mcp.server as srv

    b = make_board(picks=schedule_for(list(range(1, TEAMS + 1))))
    srv._board = b
    b.client.picks = schedule_for([13, 12, 10, 9, 8, 7, 6, 3, 2, 1])
    out = srv.refresh_draft_order.__wrapped__()
    srv._board = None

    assert out["looks_unrandomized"] is False
    assert out["my_draft_slot"] == 8  # team 3 sits 8th in [13,12,10,9,8,7,6,3,2,1]
    assert out["my_next_picks"][:2] == [8, 13]


def test_schedule_beats_snake_math_for_odd_formats():
    """Third-round reversal: ESPN's schedule encodes it, our arithmetic wouldn't."""
    order = list(range(1, TEAMS + 1))
    picks = []
    for rnd, seq in enumerate([order, list(reversed(order)), list(reversed(order))], 1):
        for i, team in enumerate(seq):
            picks.append(scheduled_pick((rnd - 1) * TEAMS + i + 1, team))
    state = make_board(picks=picks).draft_state()
    assert state["schedule"][21] == 10  # round 3 reversed, not back to team 1


class TestAdpMomentum:
    """ESPN reports ADP drift as a percent change on the ADP *number*, so a
    positive value means the player is being taken later, not that he is hot."""

    def board_with(self, adp_change: float, name: str = "RB Player 1"):
        class Client(FakeClient):
            def player_pool(self, **kw):
                pool = super().player_pool(**kw)
                for e in pool:
                    if e["player"]["fullName"] == name:
                        e["player"]["ownership"]["averageDraftPositionPercentChange"] = adp_change
                        e["player"]["ownership"]["percentChange"] = -0.25
                return pool

        b = DraftBoard(CFG, client=Client())
        return b, next(p for p in b.board()["players"] if p["name"] == name)

    def test_rising_adp_number_means_drafted_later(self):
        _, p = self.board_with(3.53)
        assert p["adp_moving"] == "later"
        assert p["adp_change_pct"] == 3.53

    def test_falling_adp_number_means_drafted_earlier(self):
        _, p = self.board_with(-0.62)
        assert p["adp_moving"] == "earlier"

    def test_noise_is_not_reported_as_movement(self):
        _, p = self.board_with(0.01)
        assert p["adp_moving"] is None

    def test_plus_minus_is_carried_through(self):
        """ESPN's '+/-': change in rostered percentage over the last week."""
        _, p = self.board_with(1.0)
        assert p["percent_owned_change"] == -0.25

    def test_slim_payload_only_carries_movement_when_it_exists(self):
        import espn_mcp.server as srv

        _, moving = self.board_with(3.53)
        _, flat = self.board_with(0.0, name="WR Player 1")
        assert srv._slim(moving)["adp_moving"] == "later"
        assert "adp_moving" not in srv._slim(flat)

    def test_fallers_are_annotated_with_direction_of_travel(self):
        import espn_mcp.server as srv

        b, _ = self.board_with(-5.0, name="RB Player 3")
        # Give the player an ADP far later than his value so he lands in the list.
        target = next(p for p in b.board()["players"] if p["name"] == "RB Player 3")
        target["espn_adp"] = 200.0
        target["value_vs_adp"] = 190.0

        srv._board = b
        ctx = srv.get_draft_context.__wrapped__(top_per_position=1)
        srv._board = None

        hit = next(p for p in ctx["falling_below_adp"] if p["name"] == "RB Player 3")
        assert "may not last" in hit["caution"]

    def test_adp_freshness_is_reported(self):
        b, _ = self.board_with(1.0)
        data = b.board()
        assert data["adp_age_hours"] is not None
        assert data["adp_as_of_local"]


def test_positions_without_projections_fall_back_to_adp():
    """ESPN publishes no season D/ST projections; VORP would be a uniform 0."""

    class NoDstProjections(FakeClient):
        def player_pool(self, **kw):
            pool = super().player_pool(**kw)
            for entry in pool:
                if entry["player"]["defaultPositionId"] == POSITION_IDS["D/ST"]:
                    entry["player"]["stats"][0]["appliedTotal"] = 0.0
            return pool

    b = DraftBoard(CFG, client=NoDstProjections())
    data = b.board()
    assert data["positions_without_projections"] == ["D/ST"]
    assert data["replacement_points"]["D/ST"] is None

    dst = [p for p in data["players"] if p["position"] == "D/ST"]
    assert all(p["vorp"] is None and p["tier"] is None for p in dst)
    assert all(p["value_basis"] == "espn_adp" for p in dst)
    assert all(p["value_vs_adp"] is None for p in dst)

    # Unprojected players sort below every player ranked on real value...
    worst_valued = max(
        p["overall_value_rank"] for p in data["players"] if p["value_basis"] == "vorp"
    )
    assert min(p["overall_value_rank"] for p in dst) > worst_valued

    # ...but stay ordered sensibly among themselves, best ADP first.
    adps = [p["espn_adp"] for p in b.available(position="D/ST", limit=10)]
    assert adps == sorted(adps)


def test_missing_values_never_sort_to_the_top():
    class NoDstProjections(FakeClient):
        def player_pool(self, **kw):
            pool = super().player_pool(**kw)
            for entry in pool:
                if entry["player"]["defaultPositionId"] == POSITION_IDS["D/ST"]:
                    entry["player"]["stats"][0]["appliedTotal"] = 0.0
            return pool

    b = DraftBoard(CFG, client=NoDstProjections())
    top = b.available(limit=5, sort_by="vorp")
    assert all(p["vorp"] is not None for p in top)
    assert top[0]["position"] != "D/ST"


def test_draft_context_makes_exactly_one_live_fetch():
    """On a 90-second clock, the hot path must not re-poll draft state."""
    import asyncio

    import espn_mcp.server as srv

    b = make_board(picks=[espn_pick(i, i) for i in range(1, 18)])
    b.board()  # warm the pool cache, as a real session would
    srv._board = b
    b.client.draft_calls = 0

    result = asyncio.run(srv.mcp.call_tool("get_draft_context", {"top_per_position": 3}))
    assert b.client.draft_calls == 1
    assert b.client.pool_calls == 1  # served from cache, no refetch
    srv._board = None
    assert result is not None


def test_draft_context_reports_needs_and_runs():
    import espn_mcp.server as srv

    b = make_board(picks=[espn_pick(i, i) for i in range(1, 18)])
    srv._board = b
    ctx = srv.get_draft_context.__wrapped__(top_per_position=3)
    srv._board = None

    assert ctx["on_the_clock_team_id"] == 3
    assert ctx["on_the_clock_is_me"] is True
    assert ctx["next_overall_pick"] == 18
    # Team 3 took pick 3 (a QB in this fixture), so QB is filled and RB is not.
    assert ctx["unfilled_starting_slots"]["RB"] == 2
    assert "QB" not in ctx["unfilled_starting_slots"]
    # Picks 8-17 were all QBs in the fixture ordering.
    assert ctx["position_runs_last_round"]["QB"] == 10
    # Back-to-back turn: team 3 picks 18 and 23, four picks between.
    assert ctx["my_next_picks"][:2] == [18, 23]
    assert ctx["picks_between_this_and_next"] == 4


def test_flex_allocation_reaches_replacement_ranks():
    b = make_board()
    ranks = b.board()["replacement_ranks"]
    assert ranks["QB"] == TEAMS
    # 2 RB + 2 WR + 1 TE dedicated, plus 10 flex spots spread across them.
    assert (ranks["RB"] - 2 * TEAMS) + (ranks["WR"] - 2 * TEAMS) + (ranks["TE"] - TEAMS) == TEAMS
