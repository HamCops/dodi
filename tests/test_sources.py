"""Outside data: the cache, the id crosswalk, the join onto ESPN's players,
and the trade arithmetic on market values. No network: every fetch is a stub.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.market import acceptable, trade_view  # noqa: E402
from espn_mcp.sources import crosswalk, fantasycalc, sleeper  # noqa: E402
from espn_mcp.sources.cache import RETRY_AFTER, DiskCache  # noqa: E402
from espn_mcp.sources.http import SourceError, fetch_json  # noqa: E402
from espn_mcp.sources.signals import Signals, market_view  # noqa: E402
from test_integration import CFG  # noqa: E402
from test_season import WEEK, SeasonClient, _tool  # noqa: E402


# --- cache -------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_cache_serves_fresh_refetches_stale_and_keeps_old_data_on_failure(tmp_path):
    import os

    clock = Clock()
    cache = DiskCache(tmp_path, clock)
    calls = []

    def load():
        calls.append(1)
        return {"n": len(calls)}

    def age(key, seconds):
        at = clock.now - seconds
        os.utime(tmp_path / f"{key}.json", (at, at))

    assert cache.get("k", 60, load) == {"n": 1}
    age("k", 10)
    assert cache.get("k", 60, load) == {"n": 1} and len(calls) == 1
    age("k", 120)
    assert cache.get("k", 60, load) == {"n": 2}

    def broken():
        raise SourceError("down")

    age("k", 120)
    assert cache.get("k", 60, broken) == {"n": 2}
    assert cache.status["k"]["stale"] is True and "down" in cache.status["k"]["error"]


def test_a_dead_source_is_not_retried_on_every_call(tmp_path):
    clock = Clock()
    cache = DiskCache(tmp_path, clock)
    calls = []

    def broken():
        calls.append(1)
        raise SourceError("down")

    assert cache.get("k", 60, broken) is None
    assert cache.get("k", 60, broken) is None
    assert len(calls) == 1 and cache.status["k"]["ok"] is False
    clock.now += RETRY_AFTER + 1
    cache.get("k", 60, broken)
    assert len(calls) == 2


def test_only_known_hosts_over_https_are_fetched():
    with pytest.raises(SourceError, match="not an allowed"):
        fetch_json("https://example.com/values")
    with pytest.raises(SourceError, match="not an allowed"):
        fetch_json("http://api.sleeper.app/v1/players/nfl")


# --- adapters ----------------------------------------------------------------


def test_sleeper_players_are_slimmed_to_fantasy_positions():
    raw = {
        "1": {"full_name": "A Back", "position": "RB", "team": "KC", "espn_id": 11,
              "active": True, "hashtag": "#x", "injury_status": "Out"},
        "2": {"full_name": "A Lineman", "position": "OT", "active": True},
        "3": {"first_name": "Old", "last_name": "Timer", "position": "WR", "active": False},
        "KC": {"position": "DEF", "team": "KC", "active": True},
    }
    out = sleeper.players(lambda url, **kw: raw)
    assert [p["player_id"] for p in out] == ["1", "KC"]
    assert "hashtag" not in out[0] and out[0]["injury_status"] == "Out"


def test_sleeper_projection_uses_the_leagues_scoring():
    assert [sleeper.scoring_key(v) for v in (0, 0.5, 1)] == [
        "pts_std", "pts_half_ppr", "pts_ppr"]
    seen = {}

    def fetch(url, params=None, **kw):
        seen.update(url=url, params=params)
        return [{"player_id": "1", "stats": {"pts_std": 10.123, "pts_ppr": 14.0}},
                {"player_id": "2", "stats": {}}]

    assert sleeper.projections(fetch, 2026, 3, 0.0) == {"1": 10.12}
    assert seen["url"].endswith("/projections/nfl/2026/3")
    assert ("order_by", "pts_std") in seen["params"]


def test_fantasycalc_is_asked_for_this_leagues_format():
    assert fantasycalc.league_params(10, 0.0) == [
        ("isDynasty", "false"), ("numQbs", 1), ("numTeams", 10), ("ppr", 0)]
    assert ("ppr", 0.5) in fantasycalc.league_params(12, 0.4)
    assert ("numQbs", 2) in fantasycalc.league_params(12, 1.0, qb_slots=2)
    rows = [{"player": {"name": "A Back", "position": "RB", "espnId": "11",
                        "sleeperId": "1"}, "value": 5000, "overallRank": 3,
             "positionRank": 2, "trend30Day": -40},
            {"player": {"name": "No Value"}, "value": None}]
    [v] = fantasycalc.values(lambda url, **kw: rows, 10, 0.0)
    assert v["espn_id"] == "11" and v["value"] == 5000 and v["position_rank"] == 2


# --- crosswalk ---------------------------------------------------------------


def test_names_are_compared_without_suffixes_accents_or_punctuation():
    assert crosswalk.norm_name("Ollie Gordon II") == "ollie gordon"
    assert crosswalk.norm_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert crosswalk.norm_name("D'Andre Swift Jr.") == "dandre swift"
    assert crosswalk.norm_team("WSH") == "WAS" and crosswalk.norm_team("kc") == "KC"


def test_crosswalk_prefers_ids_and_only_trusts_a_name_that_is_unique():
    espn = [
        {"player_id": 11, "name": "A Back", "position": "RB", "pro_team": "KC"},
        {"player_id": 12, "name": "Renamed Guy Jr.", "position": "WR", "pro_team": "DAL"},
        {"player_id": 13, "name": "John Smith", "position": "WR", "pro_team": "NYG"},
        {"player_id": 14, "name": "John Smith", "position": "WR", "pro_team": "SF"},
        {"player_id": 15, "name": "Priced Only", "position": "TE", "pro_team": "SEA"},
        {"player_id": -16028, "name": "Commanders D/ST", "position": "D/ST",
         "pro_team": "WSH"},
        {"player_id": 16, "name": "Nobody Knows", "position": "RB", "pro_team": "KC"},
    ]
    directory = [
        {"player_id": "s11", "full_name": "Totally Different", "position": "RB",
         "team": "KC", "espn_id": 11},
        {"player_id": "s12", "full_name": "Renamed Guy", "position": "WR", "team": "DAL"},
        {"player_id": "s13", "full_name": "John Smith", "position": "WR", "team": "NYG"},
        {"player_id": "s14", "full_name": "John Smith", "position": "WR", "team": "LV"},
        {"player_id": "WAS", "position": "DEF", "team": "WAS"},
    ]
    market = [{"espn_id": "15", "sleeper_id": "s15"}]
    ids = crosswalk.build(espn, directory, market)
    assert ids == {11: "s11", 12: "s12", 13: "s13", 15: "s15", -16028: "WAS"}


# --- the join ----------------------------------------------------------------


def test_an_injury_report_is_about_the_game_ahead_not_the_one_played():
    from espn_mcp.sources.signals import _injury_report

    hour = 3_600_000
    kickoff = 1_000 * hour
    s = {"injury_body_part": "Ribs", "news_updated": kickoff + 20 * hour}
    p = {"kickoff_ms": kickoff}
    # Hurt in the game, listed the day after: nothing to do with that game.
    after = _injury_report("OUT", s, p, kickoff + 30 * hour)
    assert after == {"status": "OUT", "source": "sleeper", "body_part": "Ribs",
                     "updated_hours_ago": 10.0, "about": "next game"}
    # Listed on Friday for Sunday: about this week.
    before = _injury_report("OUT", {"news_updated": kickoff - 48 * hour}, p,
                            kickoff - 24 * hour)
    assert before["about"] == "this week's game" and before["updated_hours_ago"] == 24.0
    old = _injury_report("OUT", {"news_updated": kickoff - 400 * hour}, p,
                         kickoff - 24 * hour)
    assert old["about"].startswith("unclear")


def test_a_differing_injury_report_is_attached_with_its_context(tmp_path):
    plain = DraftBoard(CFG, client=SeasonClient()).season_board()["players"]
    rb = next(q for q in plain if q["position"] == "RB")
    b, _ = league(tmp_path, market=[])
    sid = f"s{rb['player_id']}"
    b.signals.fetch = fake_fetch(
        {sid: {"full_name": rb["name"], "position": "RB", "team": "KC", "active": True,
               "espn_id": rb["player_id"], "injury_status": "Out",
               "injury_body_part": "Ribs", "news_updated": rb["kickoff_ms"] + 1}},
        market=[])
    report = b.season_board()["by_id"][rb["player_id"]]["injury_alt"]
    # Fixture kickoffs are in the past, so the report is about the next game.
    assert report["status"] == "OUT" and report["body_part"] == "Ribs"
    assert report["about"] == "next game"


def fake_fetch(directory, market, adds=None, drops=None, proj=None, fail=(),
               scoreboard=None):
    def fetch(url, params=None, **kw):
        for part in fail:
            if part in url:
                raise SourceError(f"{part} is down")
        if url.endswith("/v1/players/nfl"):
            return directory
        if "trending/add" in url:
            return [{"player_id": k, "count": v} for k, v in (adds or {}).items()]
        if "trending/drop" in url:
            return [{"player_id": k, "count": v} for k, v in (drops or {}).items()]
        if "/projections/" in url:
            return [{"player_id": k, "stats": {"pts_std": v, "pts_half_ppr": v,
                                               "pts_ppr": v}}
                    for k, v in (proj or {}).items()]
        if "fantasycalc" in url:
            return market
        if "scoreboard" in url:
            return scoreboard or {"events": []}
        if "geocoding" in url:
            return {"results": [{"latitude": 39.1, "longitude": -94.5,
                                 "country_code": "US", "admin1": "Missouri"}]}
        if "forecast" in url:
            return {"hourly": {"time": ["2099-01-04T18:00"], "temperature_2m": [28.4],
                               "precipitation_probability": [10],
                               "wind_speed_10m": [17.6], "wind_gusts_10m": [29.0]}}
        raise AssertionError(url)
    return fetch


STATS_HEAD = ("player_id,position,season_type,week,fantasy_points,fantasy_points_ppr,"
              "carries,targets,receiving_air_yards")


def fake_text(stats):
    """nflverse's two files: game rows as given, and ids as g<espn id>."""
    def fetch_text(url, **kw):
        if stats is None:
            raise SourceError("nflverse is down")
        if url.endswith("players.csv"):
            ids = sorted({r.split(",")[0] for r in stats})
            return "gsis_id,espn_id\n" + "\n".join(f"{g},{g[1:]}" for g in ids)
        return STATS_HEAD + "\n" + "\n".join(stats)
    return fetch_text


def league(tmp_path, **sources):
    """The stubbed season league, with outside data for its first few players."""
    plain = DraftBoard(CFG, client=SeasonClient())
    pool = plain.season_board()["players"]
    directory = {f"s{p['player_id']}": {
        "full_name": p["name"], "position": p["position"], "team": "KC",
        "espn_id": p["player_id"], "active": True} for p in pool
        if p["position"] not in ("K", "D/ST")}
    stats = sources.pop("stats", None)
    signals = Signals(DiskCache(tmp_path / "cache"),
                      fetch=fake_fetch(directory, **sources), season=CFG.season,
                      fetch_text=fake_text(stats))
    return DraftBoard(CFG, client=SeasonClient(), signals=signals), pool


def priced(p, value, rank, pos_rank, trend=0):
    return {"player": {"name": p["name"], "position": p["position"],
                       "espnId": str(p["player_id"]), "sleeperId": f"s{p['player_id']}"},
            "value": value, "overallRank": rank, "positionRank": pos_rank,
            "trend30Day": trend}


def test_outside_fields_land_on_the_right_players(tmp_path):
    plain = DraftBoard(CFG, client=SeasonClient()).season_board()["players"]
    rbs = sorted((p for p in plain if p["position"] == "RB"), key=lambda p: -p["ros_points"])
    best, tenth = rbs[0], rbs[9]
    b, _ = league(
        tmp_path,
        market=[priced(best, 9000, 1, 1, trend=120), priced(tenth, 7000, 4, 2)],
        adds={f"s{tenth['player_id']}": 5000}, proj={f"s{best['player_id']}": 21.5})
    by_id = b.season_board()["by_id"]

    top = by_id[best["player_id"]]
    assert top["market_value"] == 9000 and top["market_trend_30d"] == 120
    assert top["model_pos_rank"] == 1 and top["market_gap"] == 0
    assert top["alt_week_proj"] == 21.5 and market_view(top) is None

    hyped = by_id[tenth["player_id"]]
    assert hyped["model_pos_rank"] == 10 and hyped["market_gap"] == 8
    assert market_view(hyped) == "sell" and hyped["adds_24h"] == 5000

    other = by_id[rbs[3]["player_id"]]
    assert "market_value" not in other and "adds_24h" not in other
    assert set(b.signals.status()) == {"sleeper_players", "sleeper_adds", "sleeper_drops",
                                       "sleeper_projections", "fantasycalc_values",
                                       "espn_betting_lines", "nflverse_usage"}


def test_a_source_that_is_down_costs_only_its_own_fields(tmp_path):
    plain = DraftBoard(CFG, client=SeasonClient()).season_board()["players"]
    rb = next(p for p in plain if p["position"] == "RB")
    b, _ = league(tmp_path, market=[priced(rb, 9000, 1, 1)],
                  adds={f"s{rb['player_id']}": 10}, fail=("fantasycalc",))
    p = b.season_board()["by_id"][rb["player_id"]]
    assert "market_value" not in p and p["adds_24h"] == 10
    assert p["vorp"] is not None
    status = b.signals.status()
    assert status["fantasycalc_values"]["ok"] is False
    assert status["sleeper_adds"]["ok"] is True


def test_everything_down_leaves_espn_untouched(tmp_path):
    b, plain = league(tmp_path, market=[], fail=("sleeper", "fantasycalc"))
    got = b.season_board()["players"]
    assert [p["player_id"] for p in got] == [p["player_id"] for p in plain]
    assert all("market_value" not in p and "sleeper_id" not in p for p in got)


# --- trade arithmetic --------------------------------------------------------


def p(name, value):
    return {"name": name, "market_value": value}


def test_trade_view_is_from_the_other_managers_side():
    assert trade_view([p("a", None)], [p("b", None)]) is None
    win = trade_view([p("a", 3000)], [p("b", 2000)])
    assert win["their_market_gain"] == 1000 and win["their_market_gain_pct"] == 33
    assert win["verdict"].startswith("they win")
    assert trade_view([p("a", 2000)], [p("b", 3000)])["verdict"].startswith("I win")
    assert trade_view([p("a", 2000)], [p("b", 2100)])["verdict"] == "even on market value"
    uneven = trade_view([p("a", 2000), p("k", None)], [p("b", 3000)])
    assert uneven["unpriced"] == ["k"] and "Uneven" in uneven["note"]


def test_acceptable_needs_a_reason_for_him_to_say_yes():
    ahead = trade_view([p("a", 3000)], [p("b", 2000)])
    even = trade_view([p("a", 2000)], [p("b", 2000)])
    behind = trade_view([p("a", 2000)], [p("b", 3000)])
    assert acceptable(-0.2, ahead) and not acceptable(-1.0, ahead)
    assert acceptable(0.1, even) and not acceptable(-0.1, even)
    assert not acceptable(2.0, behind)
    assert acceptable(0.1, None) and not acceptable(0.0, None)


# --- through the tools -------------------------------------------------------


def test_tools_carry_the_outside_data_and_say_where_it_came_from(tmp_path, monkeypatch):
    import espn_mcp.server as srv

    plain = DraftBoard(CFG, client=SeasonClient())
    mine = plain.team_players(CFG.team_id)
    free = plain.season_available()[0]
    star = max(mine, key=lambda q: q.get("vorp") or 0)
    b, _ = league(tmp_path, market=[priced(star, 8000, 2, 1)],
                  adds={f"s{free['player_id']}": 777})
    monkeypatch.setattr(srv, "_board", b)

    waivers = _tool("get_waiver_targets", limit=5)
    assert waivers["trending_pickups"][0]["name"] == free["name"]
    assert waivers["trending_pickups"][0]["adds_24h"] == 777
    assert waivers["data_sources"]["sleeper_adds"]["ok"] is True

    roster = _tool("get_roster")
    shown = next(q for q in roster["starters"] + roster["bench"] if q["name"] == star["name"])
    assert shown["market"]["value"] == 8000

    partners = _tool("find_trade_partners")
    assert "sell_candidates" in partners and "data_sources" in partners
    assert all("best_by_market" in t for t in partners["partners"])


def test_without_outside_data_the_tools_look_as_they_did(monkeypatch):
    import espn_mcp.server as srv

    monkeypatch.setattr(srv, "_board", DraftBoard(CFG, client=SeasonClient()))
    waivers = _tool("get_waiver_targets", limit=5)
    assert "trending_pickups" not in waivers and "data_sources" not in waivers
    assert "market" not in waivers["targets"][0]
    assert "sell_candidates" not in _tool("find_trade_partners")
    assert WEEK == waivers["week"]


# --- the game: betting line and forecast -------------------------------------


def event(home, away, total, spread, favorite, indoor=False, kickoff="2099-01-04T18:00Z"):
    return {"date": kickoff, "competitions": [{
        "venue": {"indoor": indoor, "address": {"city": "Kansas City", "state": "MO",
                                                "country": "USA"}},
        "competitors": [{"homeAway": "home", "team": {"abbreviation": home}},
                        {"homeAway": "away", "team": {"abbreviation": away}}],
        "odds": [{"overUnder": total, "spread": spread,
                  "homeTeamOdds": {"favorite": favorite == home},
                  "awayTeamOdds": {"favorite": favorite == away}}]}]}


def test_implied_totals_follow_the_favorite_whatever_the_sign_of_the_spread():
    from espn_mcp.sources import games

    board = {"events": [event("KC", "DEN", 48.0, -7.0, "KC"),
                        event("CLE", "PIT", 38.5, 2.5, "PIT"),
                        event("BUF", "NE", 44.0, 0, None)]}
    g = games.week_games(lambda url, **kw: board, 2026, 4)
    assert g["KC"]["implied_total"] == 27.5 and g["KC"]["favored_by"] == 7.0
    assert g["DEN"]["implied_total"] == 20.5 and g["DEN"]["favored_by"] == -7.0
    assert g["PIT"]["implied_total"] == 20.5 and g["CLE"]["implied_total"] == 18.0
    assert g["BUF"]["implied_total"] == g["NE"]["implied_total"] == 22.0
    assert g["DEN"]["opponent"] == "KC" and g["DEN"]["home"] is False
    # No line posted yet: the game is still known, just not priced.
    unpriced = event("KC", "DEN", None, None, None)
    assert "implied_total" not in games.week_games(lambda url, **kw: {"events": [unpriced]},
                                                   2026, 4)["KC"]


def test_projection_is_nudged_by_the_line_and_weather_is_only_shown(tmp_path):
    from espn_mcp.sources.signals import VEGAS_PER_POINT

    plain = DraftBoard(CFG, client=SeasonClient()).season_board()["players"]
    rb = next(q for q in plain if q["position"] == "RB")
    k = next(q for q in plain if q["position"] == "K")
    assert rb["pro_team"] == "KC"
    b, _ = league(tmp_path, market=[], scoreboard={"events": [
        event("KC", "DEN", 50.0, 10.0, "KC"), event("BUF", "NE", 40.0, 0, None)]})
    by_id = b.season_board()["by_id"]
    got = by_id[rb["player_id"]]
    # KC is expected to score 30; the week's four teams average 22.5.
    assert got["game"]["implied_total"] == 30.0
    assert got["adj_week_proj"] == round(rb["week_proj"] + VEGAS_PER_POINT["RB"] * 7.5, 2)
    assert got["game"]["wind_mph"] == 18 and got["game"]["temp_f"] == 28
    assert got["week_proj"] == rb["week_proj"]          # ESPN's number is untouched
    # Both games are outdoors, so a kicker is moved nowhere.
    assert by_id[k["player_id"]]["adj_week_proj"] == k["week_proj"]

    indoor, _ = league(tmp_path / "in", market=[], scoreboard={"events": [
        event("KC", "DEN", 50.0, 10.0, "KC", indoor=True)]})
    dome = indoor.season_board()["by_id"][rb["player_id"]]["game"]
    assert dome["indoor"] is True and "wind_mph" not in dome


def test_a_game_already_played_is_not_attached(tmp_path):
    plain = DraftBoard(CFG, client=SeasonClient()).season_board()["players"]
    rb = next(q for q in plain if q["position"] == "RB")
    b, _ = league(tmp_path, market=[], scoreboard={"events": [
        event("KC", "DEN", 50.0, 10.0, "KC", kickoff="2020-01-05T18:00Z")]})
    got = b.season_board()["by_id"][rb["player_id"]]
    assert "game" not in got and "adj_week_proj" not in got


def test_the_lineup_is_set_by_the_adjusted_number(monkeypatch):
    import espn_mcp.server as srv
    from espn_mcp.season import START_KEY, with_start_proj

    a, c = with_start_proj([{"week_proj": 7.0, "adj_week_proj": 7.6}, {"week_proj": 7.2}])
    assert a[START_KEY] == 7.6 and c[START_KEY] == 7.2

    b = DraftBoard(CFG, client=SeasonClient())
    monkeypatch.setattr(srv, "_board", b)
    monkeypatch.setattr(srv, "_now_ms", lambda: 0)
    mine = b.team_players(CFG.team_id)
    te = next(q for q in mine if q["slot"] == "TE")
    bench = next(q for q in mine if q["slot"] == "BE" and q["position"] == "TE")
    real = b.team_players

    def nudged(team_id, week=None):
        out = real(team_id, week)
        for q in out:
            if q["player_id"] == bench["player_id"]:
                q["adj_week_proj"] = te["week_proj"] + 0.4
        return out

    monkeypatch.setattr(b, "team_players", nudged)
    plan = _tool("set_lineup")
    assert {"player": bench["name"], "from": "BE", "to": "TE"} in plan["moves"] or any(
        m["player"] == bench["name"] and m["from"] == "BE" for m in plan["moves"])
    assert "set_by" in plan


# --- workload ----------------------------------------------------------------


def test_workload_separates_the_role_from_the_results():
    from espn_mcp.usage import profile

    # Three games, 8 targets and 90 air yards a game, and nothing to show for it.
    cold = profile({"games": 3, "points_standard": 9.0, "points_ppr": 24.0,
                    "targets": 24, "air_yards": 270, "carries": 0}, "WR", 0.0)
    assert cold["ppg"] == 3.0 and cold["expected_ppg"] == 9.11
    assert cold["gap"] == -6.11 and cold["view"] == "running cold"
    assert cold["ppg"] < cold["outlook_ppg"] < cold["expected_ppg"]

    # A real role, and touchdowns well beyond it.
    hot = profile({"games": 3, "points_standard": 45.0, "points_ppr": 60.0,
                   "targets": 15, "air_yards": 360, "carries": 0}, "WR", 0.0)
    assert hot["view"] == "running hot" and hot["outlook_ppg"] < hot["ppg"]

    # The same role reads higher where catches score.
    assert profile({"games": 3, "points_standard": 9.0, "points_ppr": 24.0,
                    "targets": 24, "air_yards": 270}, "WR", 1.0)["expected_ppg"] > 9.11
    # Too few games, or too small a role, and no view is offered.
    early = profile({"games": 2, "points_standard": 40.0, "points_ppr": 44.0,
                     "targets": 8, "air_yards": 100}, "WR", 0.0)
    assert "view" not in early
    bit_part = profile({"games": 4, "points_standard": 40.0, "points_ppr": 44.0,
                        "targets": 8, "air_yards": 40}, "TE", 0.0)
    assert "view" not in bit_part
    assert profile({"games": 3, "points_standard": 60.0}, "QB", 0.0) is None
    assert profile({"games": 0}, "RB", 0.0) is None


def test_workload_reaches_the_tools(tmp_path, monkeypatch):
    import espn_mcp.server as srv

    plain = DraftBoard(CFG, client=SeasonClient())
    mine = plain.team_players(CFG.team_id)
    free = next(q for q in plain.season_available() if q["position"] == "WR")
    star = next(q for q in mine if q["position"] == "RB")
    other = next(q for q in plain.team_players(2 if CFG.team_id != 2 else 3)
                 if q["position"] == "WR")
    rows = []
    for week in (1, 2, 3):
        rows.append(f"g{free['player_id']},WR,REG,{week},2.0,6.0,0,9,110")    # role, no points
        rows.append(f"g{star['player_id']},RB,REG,{week},24.0,25.0,9,1,0")    # points, no role
        rows.append(f"g{other['player_id']},WR,REG,{week},3.0,9.0,0,10,120")
        rows.append(f"g{free['player_id']},WR,POST,{week},50.0,50.0,0,0,0")   # not counted
    b, _ = league(tmp_path, market=[], stats=rows)
    monkeypatch.setattr(srv, "_board", b)

    got = b.season_board()["by_id"][free["player_id"]]["usage"]
    # The fixture league scores a point per reception.
    assert got["games"] == 3 and got["through_week"] == 3 and got["ppg"] == 6.0
    assert got["view"] == "running cold"

    waivers = _tool("get_waiver_targets", limit=5)
    assert waivers["workload_targets"][0]["name"] == free["name"]
    partners = _tool("find_trade_partners")
    assert [q["name"] for q in partners["sell_high"]] == [star["name"]]
    assert partners["buy_low"][0]["name"] == other["name"]
    assert partners["buy_low"][0]["owner"]

    trade = _tool("analyze_trade", give=[star["name"]], receive=[other["name"]])
    assert trade["usage"]["ppg_so_far"] == {"give": 25.0, "receive": 9.0}
    assert trade["usage"]["outlook_change"] > trade["usage"]["ppg_so_far"]["receive"] - 25.0
    assert trade["usage"]["receive"][0]["view"] == "running cold"
    back = _tool("analyze_trade", give=[mine[-1]["name"]], receive=[other["name"]])
    assert "warning" not in back.get("usage", {}) or "Buying high" not in back["usage"]["warning"]


def test_text_files_only_come_from_known_hosts():
    from espn_mcp.sources.http import fetch_text

    with pytest.raises(SourceError, match="not an allowed"):
        fetch_text("https://example.com/players.csv")
    with pytest.raises(SourceError, match="not an allowed"):
        fetch_text("http://github.com/nflverse/x.csv")


def test_a_defense_is_moved_by_who_it_faces_and_a_kicker_by_the_roof():
    from espn_mcp.sources.signals import (DEFENSE_PER_OPPONENT_POINT, KICKER_INDOORS,
                                          _line_adjustment)

    # Facing an offense expected to score 15 when the week's average is 22.5.
    soft = _line_adjustment("D/ST", {"implied_total": 27.0}, 15.0, 22.5, 0.25)
    assert soft == DEFENSE_PER_OPPONENT_POINT * -7.5 and soft > 2.5
    assert _line_adjustment("D/ST", {}, 30.0, 22.5, 0.25) < -2.5
    assert _line_adjustment("D/ST", {}, None, 22.5, 0.25) is None
    # A quarter of the week's games are indoors.
    assert _line_adjustment("K", {"indoor": True}, None, None, 0.25) == KICKER_INDOORS * 0.75
    assert _line_adjustment("K", {"indoor": False}, None, None, 0.25) == KICKER_INDOORS * -0.25
    # His own team's total does nothing for a kicker.
    assert _line_adjustment("K", {"indoor": False, "implied_total": 35.0}, None, 22.5, 0.25) \
        == _line_adjustment("K", {"indoor": False, "implied_total": 10.0}, None, 22.5, 0.25)


def test_streaming_compares_my_defense_with_the_best_free_one(tmp_path, monkeypatch):
    import espn_mcp.server as srv

    plain = DraftBoard(CFG, client=SeasonClient())
    mine = next(q for q in plain.team_players(CFG.team_id) if q["position"] == "D/ST")
    free = next(q for q in plain.season_available() if q["position"] == "D/ST")
    assert mine["pro_team"] == free["pro_team"] == "KC"     # the fixture has one NFL team
    b, _ = league(tmp_path, market=[], scoreboard={"events": [
        event("KC", "DEN", 40.0, 14.0, "KC"), event("BUF", "NE", 50.0, 0, None)]})
    monkeypatch.setattr(srv, "_board", b)
    monkeypatch.setattr(srv, "_now_ms", lambda: 0)
    got = _tool("get_waiver_targets", limit=3)["streaming"]["D/ST"]
    assert got["hold_count"] == 1 and got["mine"][0]["name"] == mine["name"]
    # KC faces an offense expected to score 13; the week's average is 22.5.
    assert got["mine"][0]["opponent_implied_total"] == 13.0
    assert got["mine"][0]["adj_week_proj"] > got["mine"][0]["week_proj"] + 3
    assert got["best_available"] and "upgrade" in got


def test_cache_file_names_cannot_leave_the_cache_directory(tmp_path):
    cache = DiskCache(tmp_path / "cache")
    for key in ("place-../../etc/passwd", "place-/abs/olute", "..", "forecast-a b\\c-2026",
                "place-" + "x" * 500, ""):
        path = cache._path(key)
        assert path.parent == tmp_path / "cache", key
        assert "/" not in path.name and not path.name.startswith("."), key
        assert cache.get(key, 60, lambda: {"ok": 1}) == {"ok": 1}
    assert {p.parent for p in (tmp_path / "cache").iterdir()} == {tmp_path / "cache"}
    assert not (tmp_path / "etc").exists()
    # Ordinary keys keep the names they had, so nothing cached is orphaned.
    assert cache._path("sleeper-players-v2").name == "sleeper-players-v2.json"
    assert cache._path("fantasycalc-10t-0.0ppr-1qb").name == "fantasycalc-10t-0.0ppr-1qb.json"
