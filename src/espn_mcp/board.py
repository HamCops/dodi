"""Cached draft board plus live draft state.

Split by volatility: the player pool and the value math are expensive and
change slowly (cached, TTL-bounded), while draft picks change every few seconds
and are never cached. During a live draft the on-the-clock call should only
ever hit the picks endpoint.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .constants import SLOT_BY_ID
from .espn import ESPNClient, ESPNError
from .scoring import LeagueShape, normalize_player, parse_settings
from .season import attach_ros
from .value import build_value_board, value_vs_adp

# ESPN marks an undrafted schedule slot with this player id. Real player ids
# are positive except D/ST, which are negative -- so -1 must be matched
# exactly, never treated as "any non-positive id".
PLACEHOLDER_PLAYER_ID = -1


class DraftBoard:
    def __init__(self, cfg: Config, client: ESPNClient | None = None) -> None:
        self.cfg = cfg
        self.client = client or ESPNClient(cfg)
        self._lock = threading.Lock()
        self._shape: LeagueShape | None = None
        self._shape_at: float = 0.0
        self._board: dict | None = None
        self._board_at: float = 0.0
        self._teams: dict | None = None
        # In-season caches. Keyed by week where the payload depends on it.
        self._season_boards: dict[int, tuple[float, dict]] = {}
        self._rosters: dict[int, tuple[float, dict]] = {}
        self._pro_schedule: dict | None = None
        self._pro_schedule_at: float = 0.0
        self._ratings: dict[int, tuple[float, dict]] = {}
        # Manually recorded picks. ESPN does not publish picks until a draft
        # ends, so during a live draft these ARE the draft -- persist them so a
        # new process (or a new assistant turn) does not have to be re-told
        # every pick from the top.
        self._manual_picks: list[dict] = self._load_manual_picks()

    # --- manual pick persistence -----------------------------------------

    @property
    def _state_path(self) -> Path:
        root = (Path(self.cfg.state_dir) if self.cfg.state_dir
                else Path(__file__).resolve().parents[2] / "state")
        return root / f"picks-{self.cfg.league_id}-{self.cfg.season}.json"

    def _load_manual_picks(self) -> list[dict]:
        try:
            if self._state_path.is_file():
                return json.loads(self._state_path.read_text()) or []
        except (OSError, ValueError):
            pass
        return []

    def _save_manual_picks(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._manual_picks))
        except OSError:
            pass  # persistence is a convenience, never fail a draft over it

    def clear_manual_picks(self) -> int:
        n = len(self._manual_picks)
        self._manual_picks = []
        self._save_manual_picks()
        return n

    # --- cached layers ----------------------------------------------------

    def shape(self, refresh: bool = False) -> LeagueShape:
        """League settings, TTL-cached.

        Settings are not immutable: leagues that randomize the draft order
        rewrite `pickOrder` shortly before the draft. Caching this forever
        would pin a stale order for the whole session.
        """
        with self._lock:
            fresh = self._shape is not None and (time.time() - self._shape_at) < self.cfg.pool_ttl
            if fresh and not refresh:
                return self._shape
        shape = parse_settings(self.client.settings())
        with self._lock:
            self._shape = shape
            self._shape_at = time.time()
        return shape

    def board(self, refresh: bool = False) -> dict:
        with self._lock:
            fresh = self._board is not None and (time.time() - self._board_at) < self.cfg.pool_ttl
            if fresh and not refresh:
                return self._board

        shape = self.shape(refresh=refresh)
        raw = self.client.player_pool()
        players = [normalize_player(entry, self.cfg.season, shape) for entry in raw]
        players = [p for p in players if p["position"] in ("QB", "RB", "WR", "TE", "K", "D/ST")]
        board = build_value_board(players, shape)
        for p in board["players"]:
            p["value_vs_adp"] = value_vs_adp(p)
        # Bye weeks live on a separate endpoint, so stitch them in here.
        try:
            byes = self.client.pro_team_byes()
        except Exception:
            byes = {}
        pro_ids = {}
        for entry in raw:
            pl = entry.get("player") or entry
            pro_ids[int(pl.get("id"))] = int(pl.get("proTeamId") or 0)
        for p in board["players"]:
            p["bye_week"] = byes.get(pro_ids.get(p["player_id"], 0))

        board["by_id"] = {p["player_id"]: p for p in board["players"]}

        # ESPN stamps each player's ownership block with when it was computed.
        # ADP drives every "will he last?" call, so its age is worth reporting
        # rather than assuming it is current.
        stamps = [p["ownership_as_of_ms"] for p in board["players"] if p.get("ownership_as_of_ms")]
        board["adp_as_of_local"] = None
        board["adp_age_hours"] = None
        if stamps:
            newest = max(stamps) / 1000
            board["adp_as_of_local"] = (
                datetime.fromtimestamp(newest, timezone.utc)
                .astimezone()
                .strftime("%a %b %d, %I:%M %p %Z")
            )
            board["adp_age_hours"] = round(
                (datetime.now(timezone.utc).timestamp() - newest) / 3600, 1
            )

        with self._lock:
            self._board = board
            self._board_at = time.time()
        return board

    def teams(self, refresh: bool = False) -> dict[int, dict]:
        with self._lock:
            if self._teams is not None and not refresh:
                return self._teams
        payload = self.client.teams()
        teams = {}
        for t in payload.get("teams") or []:
            teams[int(t["id"])] = {
                "team_id": int(t["id"]),
                "name": (t.get("name") or f"{t.get('location','')} {t.get('nickname','')}").strip(),
                "abbrev": t.get("abbrev"),
                "roster_player_ids": [
                    int(e["playerId"])
                    for e in ((t.get("roster") or {}).get("entries") or [])
                ],
            }
        with self._lock:
            self._teams = teams
        return teams

    # --- live state -------------------------------------------------------

    def draft_state(self) -> dict:
        shape = self.shape()
        payload = self.client.draft_detail()
        detail = payload.get("draftDetail") or {}

        picks = [
            {
                "overall": int(p.get("overallPickNumber") or 0),
                "round": int(p.get("roundId") or 0),
                "round_pick": int(p.get("roundPickNumber") or 0),
                "team_id": int(p.get("teamId") or 0),
                "player_id": int(p.get("playerId") or 0),
                "auto": bool(p.get("autoDraftTypeId")),
                "source": "espn",
            }
            for p in (detail.get("picks") or [])
            # ESPN pre-seeds every slot of an unstarted draft with playerId -1.
            # Those are placeholders, not picks. Do NOT filter on `> 0`:
            # D/ST player ids are negative (-16001..-16034), so that would
            # silently drop every drafted defense from the pick list.
            if int(p.get("playerId") or 0) not in (0, PLACEHOLDER_PLAYER_ID)
        ]
        # Manual picks stand in for what ESPN withholds during a live draft.
        # Once the draft ends ESPN publishes the same picks, so merging blindly
        # would double every one of them -- keep ESPN's copy and drop manual
        # entries for players it now reports.
        espn_players = {p["player_id"] for p in picks}
        picks.extend(m for m in self._manual_picks
                     if m["player_id"] not in espn_players)
        picks.sort(key=lambda p: p["overall"])

        # ESPN publishes the whole pick schedule -- every slot carries a teamId
        # even before anyone has picked. That is authoritative: leagues that
        # randomize the draft order rewrite it, and it already encodes the
        # snake reversal (and any third-round-reversal variant), so prefer it
        # over settings.pickOrder or our own arithmetic.
        schedule: dict[int, int] = {}
        for p in detail.get("picks") or []:
            overall = int(p.get("overallPickNumber") or 0)
            team = int(p.get("teamId") or 0)
            if overall > 0 and team > 0:
                schedule[overall] = team

        teams = shape.teams or len(self.teams())
        # Only trust the schedule for the order if it covers a full first
        # round; a partial response would yield a truncated, unusable order.
        if teams and all(i in schedule for i in range(1, teams + 1)):
            pick_order = [schedule[i] for i in range(1, teams + 1)]
            order_source = "espn_schedule"
        else:
            pick_order = shape.pick_order or sorted(self.teams().keys())
            order_source = "settings" if shape.pick_order else "team_id_fallback"

        teams = teams or len(pick_order)
        next_overall = len(picks) + 1
        total_picks = teams * shape.roster_size

        return {
            "in_progress": bool(detail.get("inProgress")),
            "complete": bool(detail.get("drafted")) or next_overall > total_picks,
            "draft_type": shape.draft_type,
            "teams": teams,
            "rounds": shape.roster_size,
            "picks_made": len(picks),
            "next_overall_pick": next_overall if next_overall <= total_picks else None,
            "on_the_clock_team_id": self.team_at_pick(
                next_overall, pick_order, teams, schedule
            ),
            "pick_order": pick_order,
            "pick_order_source": order_source,
            "schedule": schedule,
            "picks": picks,
        }

    @staticmethod
    def team_at_pick(overall: int, pick_order: list[int], teams: int,
                     schedule: dict[int, int] | None = None) -> int | None:
        """Who owns a given overall pick.

        Uses ESPN's published schedule when available; falls back to snake
        arithmetic (odd rounds follow the order, even rounds reverse it).
        """
        if overall < 1:
            return None
        if schedule and overall in schedule:
            return schedule[overall]
        if not pick_order or teams <= 0:
            return None
        rnd = (overall - 1) // teams + 1
        idx = (overall - 1) % teams
        if rnd % 2 == 0:
            idx = teams - 1 - idx
        if idx >= len(pick_order):
            return None
        return pick_order[idx]

    def my_slot(self, team_id: int, state: dict | None = None) -> int | None:
        """1-based position in round one, from the live schedule when possible."""
        state = state or self.draft_state()
        order = state.get("pick_order") or self.shape().pick_order
        return order.index(team_id) + 1 if team_id in order else None

    def upcoming_picks_for(self, team_id: int, count: int = 4,
                           picks_made: int | None = None,
                           state: dict | None = None) -> list[int]:
        """Overall pick numbers still ahead for a team.

        Reads ESPN's published schedule so it stays correct through a draft
        order randomization. Pass `state` to avoid re-fetching it.
        """
        shape = self.shape()
        if state is None and picks_made is None:
            state = self.draft_state()
        made = state["picks_made"] if picks_made is None else picks_made

        teams = shape.teams
        schedule = (state or {}).get("schedule") or {}
        total = teams * shape.roster_size

        out = [ov for ov, tid in schedule.items() if tid == team_id and ov > made]

        # ESPN normally publishes every slot, but fall back to snake arithmetic
        # for any part of the draft the schedule doesn't cover.
        covered = max(schedule) if schedule else 0
        if covered < total:
            slot = self.my_slot(team_id, state)
            if slot and teams:
                for rnd in range(1, shape.roster_size + 1):
                    in_round = slot if rnd % 2 == 1 else teams - slot + 1
                    overall = (rnd - 1) * teams + in_round
                    if overall > max(made, covered):
                        out.append(overall)

        return sorted(set(out))[:count]

    # --- derived views ----------------------------------------------------

    def drafted_ids(self, state: dict | None = None) -> set[int]:
        state = state or self.draft_state()
        taken = {p["player_id"] for p in state["picks"]}
        # Pre-draft, keeper/dynasty rosters also remove players from the pool.
        for team in self.teams().values():
            taken.update(team["roster_player_ids"])
        return taken

    def available(self, position: str | None = None, limit: int = 30,
                  sort_by: str = "vorp", taken: set[int] | None = None) -> list[dict]:
        """Undrafted players, best first.

        Pass `taken` when the caller already has draft state in hand -- during a
        live draft that saves a round-trip on a short clock.
        """
        board = self.board()
        if taken is None:
            taken = self.drafted_ids()

        pool = [p for p in board["players"] if p["player_id"] not in taken]
        if position:
            wanted = position.upper()
            pool = [p for p in pool if p["position"] == wanted]

        key = sort_by if sort_by in ("vorp", "projected_points", "espn_adp") else "vorp"
        ascending = key == "espn_adp"  # a low ADP is a good ADP
        # Kickers and defenses sort below every skill player regardless of the
        # metric. Sorting on raw value alone floats them to the top of the
        # board in the middle rounds, which is exactly the trap the value
        # ordering exists to avoid -- and a caller filtering by position still
        # gets a correctly ordered list of just kickers or just defenses.
        pool.sort(
            key=lambda p: (
                bool(p.get("late_round_position")),
                p.get(key) is None,
                (p.get(key) or 0) if ascending else -(p.get(key) or 0),
            )
        )
        return pool[:limit]

    def record_manual_pick(self, player_id: int, team_id: int | None = None) -> dict:
        """Fallback for when ESPN is not reporting picks.

        The team defaults to whoever the published schedule says is on the
        clock, so recording a pick only needs the player.
        """
        shape = self.shape()
        state = self.draft_state()
        overall = state["picks_made"] + 1
        teams = shape.teams
        if team_id is None:
            team_id = state["on_the_clock_team_id"]
        pick = {
            "overall": overall,
            "round": (overall - 1) // teams + 1,
            "round_pick": (overall - 1) % teams + 1,
            "team_id": team_id,
            "player_id": player_id,
            "auto": False,
            "source": "manual",
        }
        self._manual_picks.append(pick)
        self._save_manual_picks()
        return pick

    def record_manual_picks(self, player_ids: list[int],
                            team_ids: list[int] | None = None) -> list[dict]:
        """Record several picks with a single draft-state fetch.

        Recording one at a time re-reads draft state per pick, which on a live
        clock is hundreds of milliseconds of pure waste.
        """
        shape = self.shape()
        state = self.draft_state()
        overall = state["picks_made"] + 1
        teams = shape.teams
        schedule = state.get("schedule") or {}
        made = []
        for i, pid in enumerate(player_ids):
            # A team id reported by the live draft feed is authoritative: the
            # published schedule is stale whenever the order was randomized at
            # draft time, which would misattribute every pick.
            explicit = team_ids[i] if team_ids and i < len(team_ids) else None
            pick = {
                "overall": overall,
                "round": (overall - 1) // teams + 1,
                "round_pick": (overall - 1) % teams + 1,
                "team_id": explicit or schedule.get(overall) or self.team_at_pick(
                    overall, state["pick_order"], teams, schedule),
                "player_id": int(pid),
                "auto": False,
                "source": "manual",
            }
            self._manual_picks.append(pick)
            made.append(pick)
            overall += 1
        self._save_manual_picks()
        return made

    def undo_manual_pick(self) -> dict | None:
        if not self._manual_picks:
            return None
        pick = self._manual_picks.pop()
        self._save_manual_picks()
        return pick

    def player(self, player_id: int) -> dict | None:
        return self.board()["by_id"].get(player_id)

    def find_players(self, query: str, limit: int = 10) -> list[dict]:
        q = query.lower().strip()
        hits = [p for p in self.board()["players"] if q in (p["name"] or "").lower()]
        hits.sort(key=lambda p: p["vorp"], reverse=True)
        return hits[:limit]

    # --- in-season --------------------------------------------------------

    ROSTER_TTL = 60  # waivers and trades move rosters; keep this short

    def week(self) -> int:
        return self.shape().current_week

    def pro_schedule(self) -> dict[int, dict]:
        with self._lock:
            fresh = (self._pro_schedule is not None
                     and (time.time() - self._pro_schedule_at) < self.cfg.pool_ttl)
            if fresh:
                return self._pro_schedule
        try:
            sched = self.client.pro_schedule()
        except Exception:
            sched = {}
        with self._lock:
            self._pro_schedule = sched
            self._pro_schedule_at = time.time()
        return sched

    def positional_ratings(self, week: int) -> dict[int, dict[int, dict]]:
        with self._lock:
            hit = self._ratings.get(week)
            if hit and (time.time() - hit[0]) < self.cfg.pool_ttl:
                return hit[1]
        try:
            ratings = self.client.positional_ratings(week)
        except Exception:
            ratings = {}
        with self._lock:
            self._ratings[week] = (time.time(), ratings)
        return ratings

    def _enrich_for_week(self, players: list[dict], week: int) -> None:
        """Bye, NFL opponent, kickoff and opponent rank vs position, in place."""
        shape = self.shape()
        sched = self.pro_schedule()
        ratings = self.positional_ratings(week)
        for p in players:
            team = sched.get(p.get("pro_team_id") or 0) or {}
            p["bye_week"] = team.get("bye") or p.get("bye_week")
            game = (team.get("games") or {}).get(week)
            if game:
                opp = sched.get(game["opponent_id"]) or {}
                p["nfl_opponent"] = ("" if game["home"] else "@") + str(opp.get("abbrev") or "?")
                p["kickoff_ms"] = game.get("kickoff_ms")
                rank = (ratings.get(p.get("position_id") or 0) or {}).get(game["opponent_id"])
                if rank:
                    # ESPN's OPRK: 1 = the defense giving up the most to this
                    # position (best matchup), 32 = the stingiest.
                    p["opp_rank_vs_pos"] = rank.get("rank")
            else:
                p["nfl_opponent"] = "BYE" if team else None
        attach_ros(players, shape.current_week, shape.final_week, week=week)

    def season_board(self, week: int | None = None, refresh: bool = False) -> dict:
        """The player pool valued over rest-of-season, with one week's projection.

        Same value math as the draft board, but the projection being valued is
        rest-of-season points, so VORP and tiers answer "how much is this
        roster spot worth from here on" rather than "for the whole year".
        """
        week = week or self.week()
        with self._lock:
            hit = self._season_boards.get(week)
            if hit and not refresh and (time.time() - hit[0]) < self.cfg.pool_ttl:
                return hit[1]
        shape = self.shape(refresh=refresh)
        raw = self.client.player_pool(week=week)
        players = [normalize_player(entry, self.cfg.season, shape) for entry in raw]
        players = [p for p in players if p["position"] in ("QB", "RB", "WR", "TE", "K", "D/ST")]
        self._enrich_for_week(players, week)
        board = build_value_board(players, shape, key="ros_points")
        board["week"] = week
        board["by_id"] = {p["player_id"]: p for p in board["players"]}
        with self._lock:
            self._season_boards[week] = (time.time(), board)
        return board

    def league_rosters(self, week: int | None = None, refresh: bool = False) -> dict[int, dict]:
        """Every team's roster as set for `week`, with lineup slots and standings."""
        week = week or self.week()
        with self._lock:
            hit = self._rosters.get(week)
            if hit and not refresh and (time.time() - hit[0]) < self.ROSTER_TTL:
                return hit[1]
        payload = self.client.rosters(week)
        shape = self.shape()
        teams: dict[int, dict] = {}
        for t in payload.get("teams") or []:
            rec = (t.get("record") or {}).get("overall") or {}
            block = (t.get("tradeBlock") or {}).get("players") or {}
            entries = []
            for e in ((t.get("roster") or {}).get("entries") or []):
                ppe = e.get("playerPoolEntry") or {}
                rec_player = None
                if ppe.get("player"):
                    rec_player = normalize_player(ppe, self.cfg.season, shape)
                entries.append({
                    "player_id": int(e["playerId"]),
                    "slot_id": int(e.get("lineupSlotId", 20)),
                    "acquired": e.get("acquisitionType"),
                    "player": rec_player,
                })
            counter = t.get("transactionCounter") or {}
            teams[int(t["id"])] = {
                "team_id": int(t["id"]),
                "name": (t.get("name") or f"{t.get('location','')} {t.get('nickname','')}").strip(),
                "abbrev": t.get("abbrev"),
                "wins": rec.get("wins", 0),
                "losses": rec.get("losses", 0),
                "ties": rec.get("ties", 0),
                "points_for": round(float(rec.get("pointsFor") or 0.0), 1),
                "points_against": round(float(rec.get("pointsAgainst") or 0.0), 1),
                "playoff_seed": t.get("playoffSeed"),
                "waiver_rank": t.get("waiverRank"),
                "faab_spent": counter.get("acquisitionBudgetSpent"),
                "acquisitions": counter.get("acquisitions"),
                "trades": counter.get("trades"),
                "trade_block_ids": [int(pid) for pid, v in block.items() if v == "ON_THE_BLOCK"],
                "entries": entries,
            }
        with self._lock:
            self._rosters[week] = (time.time(), teams)
        return teams

    def invalidate_rosters(self, week: int | None = None) -> None:
        """Forget cached rosters (one week, or all) so the next read refetches."""
        with self._lock:
            if week is None:
                self._rosters.clear()
            else:
                self._rosters.pop(week, None)

    def rostered_ids(self, week: int | None = None) -> set[int]:
        return {e["player_id"] for t in self.league_rosters(week).values() for e in t["entries"]}

    def team_players(self, team_id: int, week: int | None = None) -> list[dict]:
        """A team's players as valued records, each tagged with its lineup slot.

        Records come from the season board; anyone ESPN's pool paged out
        (deep bench) is built from the roster payload instead, so a roster is
        never silently short a player.
        """
        week = week or self.week()
        board = self.season_board(week)
        shape = self.shape()
        team = self.league_rosters(week).get(int(team_id))
        if not team:
            return []
        out = []
        for e in team["entries"]:
            p = board["by_id"].get(e["player_id"])
            if p is None and e["player"] is not None:
                p = e["player"]
                self._enrich_for_week([p], week)
                base = board["replacement_points"].get(p["position"])
                p["vorp"] = round(p["ros_points"] - base, 2) if base is not None else None
                p["tier"] = None
                p["value_basis"] = "vorp" if base is not None else "espn_adp"
                p["late_round_position"] = p["position"] in ("K", "D/ST")
            if p is None:
                continue
            rec = dict(p)
            rec["slot_id"] = e["slot_id"]
            rec["slot"] = SLOT_BY_ID.get(e["slot_id"], str(e["slot_id"]))
            rec["acquired"] = e["acquired"]
            rec["on_trade_block"] = e["player_id"] in team["trade_block_ids"]
            out.append(rec)
        return out

    def season_available(self, week: int | None = None, position: str | None = None) -> list[dict]:
        """Unrostered players by rest-of-season VORP, using the live roster set."""
        week = week or self.week()
        board = self.season_board(week)
        taken = self.rostered_ids(week)
        pool = [p for p in board["players"] if p["player_id"] not in taken]
        if position:
            pool = [p for p in pool if p["position"] == position.upper()]
        return pool

    def matchups(self, week: int | None = None) -> list[dict]:
        week = week or self.week()
        out = []
        for m in self.client.matchups(week):
            # ESPN returns the whole season's schedule regardless of
            # scoringPeriodId; keep only this week's games.
            if int(m.get("matchupPeriodId") or 0) != week:
                continue
            home, away = m.get("home") or {}, m.get("away") or {}
            out.append({
                "week": int(m.get("matchupPeriodId") or 0),
                "home_team_id": int(home.get("teamId") or 0),
                "away_team_id": int(away.get("teamId") or 0),
                "home_points": home.get("totalPoints"),
                "away_points": away.get("totalPoints"),
                "home_espn_proj": home.get("totalProjectedPointsLive") or home.get("totalProjectedPoints"),
                "away_espn_proj": away.get("totalProjectedPointsLive") or away.get("totalProjectedPoints"),
                "home_win_prob": home.get("winProbability"),
                "winner": m.get("winner"),
                "playoff": m.get("playoffTierType") not in (None, "NONE"),
            })
        return out
