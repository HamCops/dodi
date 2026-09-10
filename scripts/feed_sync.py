#!/usr/bin/env python3
"""Feed this league into the Omarchy Fantasy Feed plugin.

The plugin (https://github.com/HamCops/omarchy-fantasy-feed) follows NFL plays
with no fantasy account. This script is the bridge: it reads the league with
the credentials in .env and writes two files the plugin watches, so the plugin
never touches ESPN's fantasy API or the cookies.

  ~/.config/omarchy/fantasy-feed.json      favorites = this week's starters on
                                           both sides of your matchup, tagged
                                           side: "me" | "opp", plus a `league`
                                           block with the league's scoring
                                           rules so the plugin can score plays
                                           the way ESPN will.
  ~/.cache/fantasy-feed/league.json        ESPN's own live matchup totals and
                                           win probability -- ground truth,
                                           including K and D/ST, which the
                                           play parser cannot score.

Run it once, or as a loop: every 15 minutes while nothing is on, every minute
(lineups and live totals both) while games are live:

  ./.venv/bin/python scripts/feed_sync.py --once
  ./.venv/bin/python scripts/feed_sync.py --loop
  ./.venv/bin/python scripts/feed_sync.py --install-service   # systemd --user
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402
from espn_mcp.constants import NON_STARTING_SLOTS  # noqa: E402
from espn_mcp.espn import ESPNError  # noqa: E402

PLUGIN_ID = "io.github.studioxvii.fantasy-feed"
FEED_POSITIONS = {"QB", "RB", "WR", "TE"}

# The plugin's scoring keys, mapped to the ESPN statIds that can carry them.
# ESPN has two ways to score yardage: per yard (statId 3/24/42, e.g. 0.04) or
# per full bucket (statId 8/28/48: 1 point per complete 25/10/10 yards, floor).
# Republic of Red Zone uses the bucket form, which is why the plugin's fixed
# 0.04-per-yard "standard" table never matches ESPN's number.
_YARDAGE = {
    "passing_yards": ((3, 1), (8, 25)),
    "rushing_yards": ((24, 1), (28, 10)),
    "receiving_yards": ((42, 1), (48, 10)),
}
_COUNTING = {
    "passing_touchdown": 4,
    "interception_thrown": 20,
    "rushing_touchdown": 25,
    "reception": 53,
    "receiving_touchdown": 43,
    "passing_two_point_conversion": 19,
    "rushing_two_point_conversion": 26,
    "receiving_two_point_conversion": 44,
    "fumble_lost": 72,
}


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if value.startswith("/") else Path.home() / default


def favorites_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "omarchy" / "fantasy-feed.json"


def live_path() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "fantasy-feed" / "league.json"


def snapshot_path() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "fantasy-feed" / "snapshot.json"


def write_atomic(path: Path, payload: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sync-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def scoring_rules(shape) -> dict[str, dict]:
    """The league's rules in the plugin's vocabulary."""
    points = {item.stat_id: item.points for item in shape.scoring_items}
    rules: dict[str, dict] = {}
    for key, candidates in _YARDAGE.items():
        # Prefer the bucket form when the league sets it; per-yard otherwise.
        for stat_id, per in reversed(candidates):
            value = points.get(stat_id, 0.0)
            if value:
                rules[key] = {"points": value, "per": per}
                break
        else:
            rules[key] = {"points": 0.0, "per": 1}
    for key, stat_id in _COUNTING.items():
        rules[key] = {"points": points.get(stat_id, 0.0), "per": 1}
    # Some leagues score every two-point conversion under one combined stat
    # (statId 62) rather than the three per-type ids.
    combined = points.get(62, 0.0)
    if combined:
        for key in ("passing_two_point_conversion", "rushing_two_point_conversion",
                    "receiving_two_point_conversion"):
            if not rules[key]["points"]:
                rules[key] = {"points": combined, "per": 1}
    return rules


def _favorite(p: dict, side: str) -> dict:
    return {
        "playerId": str(p["player_id"]),
        "displayName": p["name"],
        "team": p["pro_team"],
        "position": p["position"],
        "side": side,
        "slot": p.get("slot"),
    }


_SLOT_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4, "RB/WR": 4, "WR/TE": 4, "OP": 4}


def starters(players: list[dict]) -> list[dict]:
    """Starting QB/RB/WR/TE in lineup order (QB, RB, WR, TE, then flex)."""
    out = [p for p in players
           if p.get("slot_id") not in NON_STARTING_SLOTS and p["position"] in FEED_POSITIONS]
    out.sort(key=lambda p: (_SLOT_ORDER.get(p.get("slot") or "", 9), p["name"]))
    return out


def build_favorites(b: DraftBoard, week: int) -> tuple[list[dict], dict]:
    me = b.cfg.team_id
    if not me:
        raise SystemExit("ESPN_TEAM_ID is not set.")
    shape = b.shape()
    teams = b.league_rosters(week, refresh=True)
    game = next((m for m in b.matchups(week) if me in (m["home_team_id"], m["away_team_id"])), None)
    opp = None
    if game:
        opp = game["away_team_id"] if game["home_team_id"] == me else game["home_team_id"]

    favorites = [_favorite(p, "me") for p in starters(b.team_players(me, week))]
    if opp:
        favorites += [_favorite(p, "opp") for p in starters(b.team_players(opp, week))]

    league = {
        "name": shape.name,
        "season": b.cfg.season,
        "week": week,
        "me": {"teamId": me, "name": teams[me]["name"], "abbrev": teams[me]["abbrev"]},
        "opponent": ({"teamId": opp, "name": teams[opp]["name"], "abbrev": teams[opp]["abbrev"]}
                     if opp else None),
        "scoring": scoring_rules(shape),
        "syncedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return favorites, league


def sync_favorites(b: DraftBoard, week: int, dry_run: bool = False) -> dict:
    path = favorites_path()
    current: dict = {}
    try:
        current = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        current = {}
    favorites, league = build_favorites(b, week)
    settings = dict(current.get("settings") or {})
    # First sync: switch the plugin to league scoring. After that the user's
    # choice in the dropdown is theirs to keep.
    if not current.get("league"):
        settings["scoringMode"] = "league"
    settings.setdefault("alertPreset", "off")
    payload = {"version": 1, "favorites": favorites, "settings": settings, "league": league}
    if not dry_run:
        write_atomic(path, payload)
    return payload


def sync_live(b: DraftBoard, week: int, dry_run: bool = False) -> dict | None:
    me = b.cfg.team_id
    game = next((m for m in b.matchups(week) if me in (m["home_team_id"], m["away_team_id"])), None)
    if not game:
        return None
    teams = b.league_rosters(week)
    home = game["home_team_id"] == me
    opp = game["away_team_id"] if home else game["home_team_id"]

    def side(tid: int, is_home: bool) -> dict:
        return {
            "teamId": tid,
            "name": teams[tid]["name"],
            "abbrev": teams[tid]["abbrev"],
            "points": round(float((game["home_points"] if is_home else game["away_points"]) or 0.0), 2),
            "projected": round(float((game["home_espn_proj"] if is_home else game["away_espn_proj"]) or 0.0), 2),
        }

    prob = game["home_win_prob"]
    if prob is not None and not home:
        prob = round(1 - prob, 3)
    payload = {
        "version": 1,
        "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "week": week,
        "status": game["winner"],
        "me": side(me, home),
        "opponent": side(opp, not home),
        "winProbability": prob,
    }
    if not dry_run:
        write_atomic(live_path(), payload, mode=0o600)
    return payload


def feed_is_live() -> bool:
    """Borrow the plugin's own view of the slate instead of asking ESPN again."""
    try:
        snap = json.loads(snapshot_path().read_text())
        return snap.get("sourceState") == "live"
    except (OSError, ValueError):
        return False


def run_once(args) -> int:
    cfg = load_config()
    b = DraftBoard(cfg)
    week = args.week or b.week()
    fav = sync_favorites(b, week, args.dry_run)
    live = sync_live(b, week, args.dry_run)
    mine = [f["displayName"] for f in fav["favorites"] if f["side"] == "me"]
    theirs = [f["displayName"] for f in fav["favorites"] if f["side"] == "opp"]
    print(f"week {week}: {len(mine)} of mine, {len(theirs)} of {fav['league']['opponent']['name'] if fav['league']['opponent'] else 'nobody'}")
    print("  me : " + ", ".join(mine))
    print("  opp: " + ", ".join(theirs))
    if live:
        print(f"  live: {live['me']['points']} - {live['opponent']['points']} "
              f"(proj {live['me']['projected']} - {live['opponent']['projected']}, "
              f"win {live['winProbability']})")
    if args.dry_run:
        print(json.dumps(fav, indent=2)[:1500])
    else:
        print(f"  wrote {favorites_path()} and {live_path()}")
    return 0


def run_loop(args) -> int:
    cfg = load_config()
    b = DraftBoard(cfg)
    last_favorites = 0.0
    while True:
        try:
            week = args.week or b.week()
            live = feed_is_live()
            # Lineups change right up to kickoff and between games, so while
            # the slate is live re-sync them every pass, not just every 15 min.
            if live or time.time() - last_favorites > 15 * 60:
                sync_favorites(b, week)
                last_favorites = time.time()
            sync_live(b, week)
            delay = 60 if live else 15 * 60
        except ESPNError as exc:
            print(f"espn: {exc}", file=sys.stderr)
            delay = 5 * 60
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            delay = 5 * 60
        sys.stdout.flush()
        time.sleep(delay)


UNIT = """[Unit]
Description=Sync ESPN fantasy matchup into Omarchy Fantasy Feed
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={python} {script} --loop
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""


def install_service() -> int:
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit(f"{python} not found -- create the venv first (see README).")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "fantasy-feed-sync.service"
    unit.write_text(UNIT.format(root=root, python=python, script=Path(__file__).resolve()))
    print(f"wrote {unit}")
    print("enable with:\n  systemctl --user daemon-reload && systemctl --user enable --now fantasy-feed-sync.service")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="sync once and exit (default)")
    mode.add_argument("--loop", action="store_true", help="keep syncing; 60s while games are live")
    mode.add_argument("--install-service", action="store_true", help="write a systemd --user unit")
    ap.add_argument("--week", type=int, help="override the current week")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args()
    if args.install_service:
        return install_service()
    if args.loop:
        return run_loop(args)
    return run_once(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ESPNError as exc:
        print(f"espn: {exc}", file=sys.stderr)
        sys.exit(2)
