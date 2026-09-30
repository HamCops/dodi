"""Stash logic: dead roster spots and bench upside."""

from __future__ import annotations

from espn_mcp.autopolicy import auto_ok
from espn_mcp.stash import (STASH_FLOOR, dead_spots, replacement_per_game, stash_moves,
                            stash_score, weakest_stash)

REPL = {"QB": 16.4, "RB": 7.1, "WR": 7.06, "TE": 5.19, "K": 7.65, "D/ST": 4.58}


def p(pid, name, pos, ros=0.0, week=0.0, exp=None, adds=None, slot=20):
    out = {"player_id": pid, "name": name, "position": pos, "ros_per_game": ros,
           "week_proj": week, "adj_week_proj": week, "slot_id": slot}
    if exp is not None:
        out["usage"] = {"expected_ppg": exp, "games": 3}
    if adds is not None:
        out["adds_24h"] = adds
    return out


def roster():
    """A real week-4 roster shape: three defenses, a thin bench."""
    return [
        p(1, "Kyler Murray", "QB", 20.05, 20.7, slot=0),
        p(2, "Drake Maye", "QB", 18.48, 17.8),
        p(3, "Jonathan Taylor", "RB", 13.95, 16.5, 17.45, slot=2),
        p(4, "Jameson Williams", "WR", 6.75, 6.0, 6.79),
        p(5, "Dalton Kincaid", "TE", 4.95, 6.5, 7.16),
        p(-1, "Giants D/ST", "D/ST", 2.37, 5.38, slot=16),
        p(-2, "Lions D/ST", "D/ST", 3.61, 3.83),
        p(-3, "Saints D/ST", "D/ST", 3.98, 2.74),
        p(9, "Cameron Dicker", "K", 8.4, 8.29, slot=17),
    ]


def test_scores_compare_across_positions_above_replacement():
    # A waiver QB at replacement is worth nothing; a busy WR is.
    assert stash_score(p(10, "Sam Darnold", "QB", 16.4), REPL) == 0.0
    washington = p(11, "Malik Washington", "WR", 6.41, exp=8.88, adds=171_544)
    assert stash_score(washington, REPL) > 1.0
    assert stash_score(p(12, "Some K", "K", 9.0), REPL) == 0.0
    per_game = replacement_per_game({"RB": 92.4}, 13)
    assert round(per_game["RB"], 2) == 7.11


def test_extra_defenses_are_dead_spots_but_the_starter_and_pending_drops_are_not():
    dead = [x["name"] for x in dead_spots(roster())]
    assert dead == ["Saints D/ST", "Lions D/ST"]           # Giants start: best matchup
    # Saints already go out in a pending claim: his spot is spoken for.
    assert [x["name"] for x in dead_spots(roster(), {-3})] == ["Lions D/ST"]


def test_the_last_quarterback_and_starters_are_never_offered_up():
    starters = {1, 3, -1, 9}
    weak = [x["name"] for x in weakest_stash(roster(), starters, REPL)]
    assert "Kyler Murray" not in weak and "Jonathan Taylor" not in weak
    assert "Drake Maye" in weak
    one_qb = [x for x in roster() if x["name"] != "Drake Maye"]
    assert "Kyler Murray" not in [x["name"] for x in weakest_stash(one_qb, {3, -1, 9}, REPL)]


def test_dead_spots_are_filled_first_and_nobody_is_used_twice():
    wire = [p(20, "Malik Washington", "WR", 6.41, exp=8.88, adds=171_544),
            p(21, "Kenyon Sadiq", "TE", 3.7, exp=4.88, adds=1_943_037),
            p(22, "Jag Nobody", "WR", 3.0, exp=3.0)]
    moves = stash_moves(roster(), wire, {1, 3, -1, 9}, REPL, pending_drop_ids={-3})
    assert moves[0]["drop"] == "Lions D/ST" and moves[0]["add"] == "Malik Washington"
    assert len({m["add"] for m in moves}) == len(moves)
    assert len({m["drop"] for m in moves}) == len(moves)
    assert "Jag Nobody" not in [m["add"] for m in moves]    # below the floor
    assert all(m["drop"] != "Saints D/ST" for m in moves)    # pending claim has him


def test_no_second_backup_quarterback_as_a_stash():
    wire = [p(30, "Big QB", "QB", 19.0), p(31, "Other QB", "QB", 18.9)]
    moves = stash_moves(roster(), wire, {1, 3, -1, 9}, REPL)
    assert not any(m["add_pos"] == "QB" for m in moves)


def test_a_trending_tight_end_is_not_a_fourth_tight_end():
    """Kenyon Sadiq, week 4: 1.9M adds, but the roster already held three TEs."""
    tes = roster() + [p(6, "Brock Bowers", "TE", 8.55, 8.9, slot=6),
                      p(7, "Tyler Warren", "TE", 5.8, 7.5)]
    sadiq = p(21, "Kenyon Sadiq", "TE", 3.7, exp=4.88, adds=1_943_037)
    moves = stash_moves(tes, [sadiq], {1, 3, 6, -1, 9}, REPL, pending_drop_ids={-3})
    assert not any(m["add"] == "Kenyon Sadiq" and m["drop_pos"] != "TE" for m in moves)


def _preview(ros=0.0, week=0.0, add_pos="WR", drop_pos="D/ST", **stash):
    return {"delta": {"starters_ros_per_game": ros, "starters_this_week": week},
            "add": {"pos": add_pos}, "drop": [{"pos": drop_pos}],
            "stash": {"floor": STASH_FLOOR, **stash}}


def test_policy_fills_dead_spots_and_real_upgrades_on_its_own():
    assert auto_ok("add_player", {}, _preview(add_stash_score=1.27, drop_is_dead_spot=True))[0]
    assert not auto_ok("add_player", {}, _preview(add_stash_score=0.1, drop_is_dead_spot=True))[0]
    assert auto_ok("add_player", {}, _preview(drop_pos="WR", add_stash_score=1.3,
                                              drop_stash_score=-0.29))[0]
    assert not auto_ok("add_player", {}, _preview(drop_pos="WR", add_stash_score=1.0,
                                                  drop_stash_score=0.5))[0]
    # Never at the cost of this week's lineup.
    assert not auto_ok("add_player", {}, _preview(week=-1.2, add_stash_score=3.0,
                                                  drop_is_dead_spot=True))[0]
    # Nor a position already full.
    assert not auto_ok("add_player", {}, _preview(add_pos="TE", add_stash_score=1.1,
                                                  drop_is_dead_spot=True,
                                                  over_position_cap=True))[0]
