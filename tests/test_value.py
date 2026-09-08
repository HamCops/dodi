"""Unit tests for the parts that must be right regardless of the network."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.scoring import LeagueShape, ScoringItem, score_stat_line  # noqa: E402
from espn_mcp.value import (  # noqa: E402
    assign_tiers,
    build_value_board,
    replacement_points,
    replacement_ranks,
)


def make_shape(teams: int = 12) -> LeagueShape:
    # 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 D/ST, 7 bench
    return LeagueShape(
        name="Test",
        teams=teams,
        draft_type="SNAKE",
        seconds_per_pick=90,
        draft_date_ms=None,
        pick_order=list(range(1, teams + 1)),
        lineup_slots={0: 1, 2: 2, 4: 2, 6: 1, 23: 1, 17: 1, 16: 1, 20: 7},
        scoring_items=[ScoringItem(stat_id=53, points=1.0)],
        roster_size=16,
    )


def make_players(pos: str, count: int, top: float, step: float) -> list[dict]:
    return [
        {
            "player_id": hash((pos, i)) & 0xFFFF,
            "name": f"{pos}{i}",
            "position": pos,
            "projected_points": round(top - i * step, 2),
        }
        for i in range(count)
    ]


def sample_pool() -> list[dict]:
    return (
        make_players("QB", 40, 380, 5)
        + make_players("RB", 80, 320, 3)
        + make_players("WR", 90, 300, 2.5)
        + make_players("TE", 40, 240, 6)
        + make_players("K", 32, 140, 1)
        + make_players("D/ST", 32, 150, 2)
    )


def test_dedicated_starters_scale_with_team_count():
    shape = make_shape(teams=12)
    ranks = replacement_ranks(sample_pool(), shape)
    assert ranks["QB"] == 12  # 1 QB slot x 12 teams, QB is not flex eligible
    assert ranks["K"] == 12
    assert ranks["D/ST"] == 12


def test_flex_slots_are_allocated_to_real_positions():
    shape = make_shape(teams=12)
    ranks = replacement_ranks(sample_pool(), shape)
    # 2 RB + 2 WR + 1 TE dedicated = 24/24/12, plus 12 flex spread across them.
    flex_total = (ranks["RB"] - 24) + (ranks["WR"] - 24) + (ranks["TE"] - 12)
    assert flex_total == 12
    assert ranks["RB"] >= 24 and ranks["WR"] >= 24


def test_replacement_point_is_first_player_below_cutoff():
    players = make_players("RB", 10, 100, 10)  # 100, 90, ... 10
    points = replacement_points(players, {"RB": 3})
    assert points["RB"] == 70  # 4th best, i.e. index 3


def test_vorp_is_relative_to_position_not_raw_projection():
    shape = make_shape()
    board = build_value_board(sample_pool(), shape)
    by_name = {p["name"]: p for p in board["players"]}
    # QB1 outprojects RB1 outright, but QBs are deep in a 1-QB league.
    assert by_name["QB0"]["projected_points"] > by_name["RB0"]["projected_points"]
    assert by_name["RB0"]["vorp"] > by_name["QB0"]["vorp"]


def test_value_ranks_are_dense_and_ordered():
    shape = make_shape()
    board = build_value_board(sample_pool(), shape)
    ranks = [p["overall_value_rank"] for p in board["players"]]
    assert ranks == list(range(1, len(ranks) + 1))
    # Ordering is by VORP within each group, not globally -- see below.
    skill = [p["vorp"] for p in board["players"] if not p["late_round_position"]]
    assert skill == sorted(skill, reverse=True)


def test_kickers_and_defenses_rank_below_every_skill_player():
    """A kicker's VORP is not comparable to a running back's.

    Kicker projections cluster tightly, so the top kicker shows a VORP on par
    with a good TE. Ranked naively, a value-following drafter takes kickers in
    the middle rounds. They keep their within-position VORP but sort last.
    """
    shape = make_shape()
    board = build_value_board(sample_pool(), shape)
    players = board["players"]

    top_k = next(p for p in players if p["position"] == "K")
    worst_skill = max(
        p["overall_value_rank"] for p in players if not p["late_round_position"]
    )
    assert top_k["overall_value_rank"] > worst_skill
    assert top_k["late_round_position"] is True
    # The VORP itself is preserved -- it still ranks kickers against kickers.
    assert top_k["vorp"] > 0
    ks = [p["vorp"] for p in players if p["position"] == "K"]
    assert ks == sorted(ks, reverse=True)


def test_skill_players_are_not_flagged_late_round():
    shape = make_shape()
    board = build_value_board(sample_pool(), shape)
    for p in board["players"]:
        if p["position"] in ("QB", "RB", "WR", "TE"):
            assert p["late_round_position"] is False


def test_tiers_follow_natural_clusters():
    players = [
        {"position": "RB", "vorp": v}
        for v in [100, 99, 98, 60, 59, 58, 20, 19]
    ]
    assign_tiers(players, ranks={"RB": 8}, teams=2)
    tiers = [p["tier"] for p in players]
    assert tiers[0] == tiers[1] == tiers[2]      # the 100s cluster together
    assert tiers[3] == tiers[4] == tiers[5]      # so do the 60s
    assert tiers[6] == tiers[7]                  # and the 20s
    assert tiers[0] < tiers[3] < tiers[6]


def test_tiers_are_contiguous_and_start_at_one():
    """k-means can leave a centroid empty; tier numbers must not skip."""
    players = [{"position": "WR", "vorp": v} for v in [200, 10, 9, 8, 7, 6, 5, 4]]
    assign_tiers(players, ranks={"WR": 8}, teams=2)
    tiers = sorted({p["tier"] for p in players})
    assert tiers == list(range(1, len(tiers) + 1))


def test_elite_players_do_not_each_become_their_own_tier():
    """A global gap threshold made every top player a singleton -- useless signal."""
    vorps = [96, 80, 73, 68, 55, 52, 50, 47, 45, 44, 42, 40, 38, 35, 33, 30, 28, 25]
    players = [{"position": "WR", "vorp": v} for v in vorps]
    assign_tiers(players, ranks={"WR": 25}, teams=10)
    sizes: dict[int, int] = {}
    for p in players:
        sizes[p["tier"]] = sizes.get(p["tier"], 0) + 1
    assert max(sizes.values()) >= 3          # real groups exist
    assert sum(1 for n in sizes.values() if n == 1) <= 2  # few singletons


def test_players_past_draftable_depth_share_a_trailing_tier():
    players = [{"position": "TE", "vorp": float(100 - i)} for i in range(40)]
    assign_tiers(players, ranks={"TE": 10}, teams=10)
    # depth = replacement rank (10) + one round (10) = 20 clustered, 20 trailing.
    trailing = [p for p in players if p["vorp"] <= 100 - 20]
    assert len({p["tier"] for p in trailing}) == 1
    assert all(p["tier"] == max(x["tier"] for x in players) for p in trailing)


class TestDraftTiming:
    """ESPN reports the draft time as epoch ms, which is next-day in UTC for
    any US evening draft. It must render in the local zone to match the site."""

    def shape_at(self, ms: int | None):
        s = make_shape()
        return LeagueShape(**{**s.__dict__, "draft_date_ms": ms})

    def test_unscheduled_draft(self):
        assert self.shape_at(None).draft_timing() == {"draft_scheduled": False}

    def test_local_rendering_matches_the_league_page(self):
        # 2026-09-08T00:00:00Z is Mon Sep 7, 8:00 PM US/Eastern.
        timing = self.shape_at(1788825600000).draft_timing()
        assert timing["draft_scheduled"] is True
        assert timing["draft_time_utc"] == "2026-09-08T00:00:00+00:00"
        local = datetime.fromtimestamp(1788825600000 / 1000, timezone.utc).astimezone()
        assert local.strftime("%I:%M %p").lstrip("0") in timing["draft_time_local"]

    def test_past_draft_is_flagged_started(self):
        timing = self.shape_at(1_000_000_000_000).draft_timing()
        assert timing["draft_has_started"] is True
        assert timing["hours_until_draft"] == 0

    def test_future_draft_counts_down(self):
        future_ms = int(
            (datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000
        )
        timing = self.shape_at(future_ms).draft_timing()
        assert timing["draft_has_started"] is False
        assert 2.9 < timing["days_until_draft"] < 3.1


def test_scoring_applies_position_overrides():
    items = [ScoringItem(stat_id=53, points=0.5, overrides={4: 1.5})]  # TE premium
    line = {"53": 100}
    assert score_stat_line(line, 3, items) == 50.0   # WR
    assert score_stat_line(line, 4, items) == 150.0  # TE


class TestSnakeOrder:
    order = [10, 20, 30, 40]  # 4 teams, arbitrary team ids

    def at(self, overall: int):
        return DraftBoard.team_at_pick(overall, self.order, len(self.order))

    def test_first_round_follows_pick_order(self):
        assert [self.at(i) for i in (1, 2, 3, 4)] == [10, 20, 30, 40]

    def test_second_round_reverses(self):
        assert [self.at(i) for i in (5, 6, 7, 8)] == [40, 30, 20, 10]

    def test_third_round_returns_to_original_order(self):
        assert [self.at(i) for i in (9, 10, 11, 12)] == [10, 20, 30, 40]

    def test_turn_is_back_to_back(self):
        assert self.at(4) == self.at(5) == 40
