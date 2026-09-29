"""The week in review: what was scored, what was left, and how the numbers did.

Two kinds of question, kept apart. What happened is known afterwards: the
lineup scored this, the best possible lineup scored that. Whether the
decisions were good can only be judged against what was known before
kickoff, which is what tracking.py writes down; without a snapshot for the
week, that half of the report says so instead of guessing.
"""

from __future__ import annotations

import sqlite3
import sys
import time

from .board import DraftBoard
from .notify import push
from .season import WEEK_KEY, current_starters, optimal_lineup

FINAL_AFTER = 4 * 3600   # a game is over this long after kickoff


def last_finished_week(b: DraftBoard, now: float) -> int | None:
    """The latest week in which every game on my roster is over."""
    for week in range(b.week(), 0, -1):
        players = b.team_players(b.cfg.team_id, week)
        kickoffs = [p["kickoff_ms"] / 1000 for p in players if p.get("kickoff_ms")]
        played = any((p.get("week_points") or {}).get(week) is not None for p in players)
        if played and kickoffs and max(kickoffs) + FINAL_AFTER < now:
            return week
    return None


def lineup_review(players: list[dict], shape, week: int) -> dict:
    """What the lineup scored against what the roster could have scored."""
    scored = [{**p, "_actual": float((p.get("week_points") or {}).get(week) or 0.0)}
              for p in players]
    starters = current_starters(scored)
    best = optimal_lineup(scored, shape, "_actual")
    started = {p["player_id"] for p in starters}
    by_projection = optimal_lineup(scored, shape, WEEK_KEY)
    missed = sorted((p for _, p in best["starters"] if p and p["player_id"] not in started),
                    key=lambda p: -p["_actual"])
    total = round(sum(p["_actual"] for p in starters), 1)
    return {
        "scored": total,
        "best_possible": round(best["total"], 1),
        "left_on_bench": round(best["total"] - total, 1),
        "by_projection": round(sum(p["_actual"] for _, p in by_projection["starters"] if p), 1),
        "should_have_started": [
            {"name": p["name"], "pos": p["position"], "scored": p["_actual"],
             "projected": p.get(WEEK_KEY)} for p in missed[:3]],
    }


def source_accuracy(db: sqlite3.Connection, season: int, week: int) -> dict | None:
    """How far each pre-kickoff number was from what players scored."""
    rows = db.execute(
        """SELECT s.week_proj, s.alt_week_proj, a.points
           FROM snapshots s JOIN actuals a USING (season, week, player_id)
           WHERE s.season=? AND s.week=? AND s.week_proj >= 4
             AND s.taken_at = (SELECT MAX(taken_at) FROM snapshots
                               WHERE season=s.season AND week=s.week
                                 AND player_id=s.player_id)""",
        (season, week)).fetchall()
    if len(rows) < 20:
        return None
    both = [(e, a, y) for e, a, y in rows if a is not None]
    out = {"players": len(rows),
           "espn_avg_miss": round(sum(abs(e - y) for e, _, y in rows) / len(rows), 2)}
    if len(both) >= 20:
        out["compared"] = len(both)
        out["espn_avg_miss_same_players"] = round(
            sum(abs(e - y) for e, _, y in both) / len(both), 2)
        out["sleeper_avg_miss"] = round(sum(abs(a - y) for _, a, y in both) / len(both), 2)
    return out


def decisions(b: DraftBoard, week: int, since: float, until: float) -> list[dict]:
    """What the manager approved or rejected, and how it has turned out."""
    from .server import proposal_store
    by_name = {p["name"]: p for tid in b.league_rosters(week)
               for p in b.team_players(tid, week)}
    by_name.update({p["name"]: p for p in b.season_board(week)["players"]
                    if p["name"] not in by_name})
    by_ref = {f"id:{p['player_id']}": p["name"] for p in by_name.values()}
    pts = lambda name: (by_name.get(name, {}).get("week_points") or {}).get(week)  # noqa: E731
    out = []
    for p in proposal_store().list(limit=100):
        if not (since <= p["created_at"] < until) or p["status"] == "pending":
            continue
        row = {"what": p["title"].removeprefix("Dodi: ").rstrip("?"), "status": p["status"]}
        if p["action"] == "start_player":
            a, c = pts(_full(by_name, p["params"]["player"], by_ref)), pts(_full(by_name, p["params"]["over"], by_ref))
            if a is not None and c is not None:
                took = p["status"] == "applied"
                row["outcome"] = (f"{a} vs {c}: " + (
                    "right call" if (a >= c) == took else "wrong call"))
        elif p["action"] == "add_player" and p["params"].get("drop"):
            a, c = pts(_full(by_name, p["params"]["add"], by_ref)), pts(_full(by_name, p["params"]["drop"], by_ref))
            if a is not None and c is not None:
                row["outcome"] = f"added player scored {a}, dropped player {c} (one week)"
        out.append(row)
    return out


def _full(by_name: dict, fragment: str, by_ref: dict | None = None) -> str:
    if by_ref and fragment in by_ref:
        return by_ref[fragment]
    hits = [n for n in by_name if fragment.lower() in n.lower()]
    return hits[0] if len(hits) == 1 else fragment


def build(b: DraftBoard, now: float | None = None) -> dict | None:
    from .tracking import _db, record_snapshot
    now = time.time() if now is None else now
    week = last_finished_week(b, now)
    if week is None:
        return None
    record_snapshot(b, now)     # also stores the final scores of finished weeks
    players = b.team_players(b.cfg.team_id, week)
    kickoffs = [p["kickoff_ms"] / 1000 for p in players if p.get("kickoff_ms")]
    report = {"week": week, "lineup": lineup_review(players, b.shape(), week)}
    matchup = next((m for m in b.matchups(week)
                    if b.cfg.team_id in (m.get("home_team_id"), m.get("away_team_id"))), None)
    if matchup:
        mine_home = matchup.get("home_team_id") == b.cfg.team_id
        report["result"] = {
            "for": matchup.get("home_points" if mine_home else "away_points"),
            "against": matchup.get("away_points" if mine_home else "home_points")}
    with _db(b) as db:
        _store_actuals(db, b, week)
        report["accuracy"] = source_accuracy(db, b.cfg.season, week)
        db.execute("CREATE TABLE IF NOT EXISTS weekly (season INTEGER, week INTEGER, "
                   "scored REAL, best_possible REAL, by_projection REAL, "
                   "PRIMARY KEY (season, week))")
        have = {w for (w,) in db.execute("SELECT week FROM weekly WHERE season=?",
                                         (b.cfg.season,))}
        for wk in range(1, week + 1):
            # Earlier weeks are filled in once, so the season line is whole.
            if wk != week and wk in have:
                continue
            r = report["lineup"] if wk == week else lineup_review(
                b.team_players(b.cfg.team_id, wk), b.shape(), wk)
            db.execute("INSERT OR REPLACE INTO weekly VALUES (?,?,?,?,?)",
                       (b.cfg.season, wk, r["scored"], r["best_possible"],
                        r["by_projection"]))
        report["season"] = [dict(zip(("week", "scored", "best_possible", "by_projection"), row))
                            for row in db.execute(
            "SELECT week, scored, best_possible, by_projection FROM weekly "
            "WHERE season=? ORDER BY week", (b.cfg.season,))]
    report["decisions"] = decisions(b, week, min(kickoffs) - 6 * 86400, max(kickoffs))
    return report


def _store_actuals(db: sqlite3.Connection, b: DraftBoard, week: int) -> None:
    rows = [(b.cfg.season, week, p["player_id"], float(pts))
            for tid in b.league_rosters(week) for p in b.team_players(tid, week)
            if (pts := (p.get("week_points") or {}).get(week)) is not None]
    db.executemany("INSERT OR REPLACE INTO actuals VALUES (?,?,?,?)", rows)


def render(r: dict) -> str:
    lu = r["lineup"]
    lines = [f"DODI WEEK {r['week']} REVIEW"]
    if r.get("result") and r["result"]["for"] is not None:
        res = r["result"]
        verdict = "WIN" if res["for"] > res["against"] else "LOSS" if res["for"] < res["against"] else "TIE"
        lines.append(f"{verdict} {res['for']} - {res['against']}")
    lines += ["", f"Lineup scored {lu['scored']}. Best possible was {lu['best_possible']} "
                  f"({lu['left_on_bench']} left on the bench).",
              f"Starting strictly by projection would have scored {lu['by_projection']}."]
    for p in lu["should_have_started"]:
        lines.append(f"  Benched: {p['name']} ({p['pos']}) scored {p['scored']}, "
                     f"projected {p['projected']}")
    lines.append("")
    acc = r.get("accuracy")
    if acc:
        lines.append(f"Projections, {acc['players']} rostered players: ESPN missed by "
                     f"{acc['espn_avg_miss']} a player on average.")
        if "sleeper_avg_miss" in acc:
            lines.append(f"  Same {acc['compared']} players: ESPN {acc['espn_avg_miss_same_players']}, "
                         f"Sleeper {acc['sleeper_avg_miss']}.")
    else:
        lines.append("No pre-kickoff snapshot for this week, so the projections "
                     "cannot be scored. They are recorded from week 4 on.")
    if r["decisions"]:
        lines += ["", "Your decisions:"]
        for d in r["decisions"]:
            lines.append(f"  {d['status']}: {d['what']}"
                         + (f" -> {d['outcome']}" if d.get("outcome") else ""))
    if len(r["season"]) > 1:
        s = r["season"]
        left = sum(w["best_possible"] - w["scored"] for w in s) / len(s)
        vs = sum(w["scored"] - w["by_projection"] for w in s) / len(s)
        lines += ["", f"Season, {len(s)} weeks: {left:.1f} a week left on the bench; "
                      f"lineups {vs:+.1f} a week against starting strictly by projection."]
    lines += ["", "Points left on the bench are hindsight: the best possible lineup is "
                  "never knowable beforehand. The number to watch is the gap to "
                  "projection, over many weeks."]
    return "\n".join(lines)


def main() -> None:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .server import board
    b = board()
    report = build(b)
    if report is None:
        return
    text = render(report)
    print(text)
    if "--quiet" not in sys.argv:
        lu = report["lineup"]
        push(b.cfg, f"Dodi: week {report['week']} review",
             "\n".join(text.splitlines()[1:12]), tags=["bar_chart"])


if __name__ == "__main__":
    main()
