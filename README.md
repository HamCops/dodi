# espn-mcp

An MCP server that puts your ESPN fantasy football league in front of an AI
assistant for the whole season: the draft, the weekly start/sit call, waivers,
trades, and the roster moves themselves. Ask "who should I start?", "anyone
worth a claim?", "is this trade good for me?", "set my best lineup", "grab the
Saints defense and drop Shakir", and the assistant reads the league, does the
arithmetic, shows you the answer, and, when you say so, makes the change on
ESPN.

Two rules shape it:

- **Facts, not opinions.** Tools return what is true about *your* league: who
  is available, what your lineup projects to, what a swap is worth. The one
  thing the server computes rather than reports is the value math (VORP,
  replacement level, optimal lineups), which is deterministic arithmetic a
  language model should not be doing in its head. Judgement stays with the
  model and with you.
- **Nothing touches ESPN without a preview.** Every writing tool returns the
  exact change and what it is worth by default, and only submits with
  `apply=true`. The assistant is instructed to confirm with you first.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill it in
./.venv/bin/python scripts/doctor.py     # prints league format + a top-10 board if it works
claude mcp add espn-fantasy -- "$(pwd)/.venv/bin/python" -m espn_mcp.server
```

### Credentials

`ESPN_LEAGUE_ID` is the number in your league URL
(`fantasy.espn.com/football/league?leagueId=123456789`). `ESPN_TEAM_ID` is the
`teamId` in the URL of your own roster page.

A private league (most are) also needs two cookies from a browser logged in to
ESPN, under DevTools → Application → Cookies → `fantasy.espn.com`:

- `espn_s2`: a long URL-encoded string, the whole value
- `SWID`: a UUID **including** the braces

**These are session credentials. Treat them like a password.** With them the
server can read your league and, through the writing tools, change your
roster. Only `.env` in the project root is read, never one in the working
directory the server happens to launch from; it is gitignored; keep it out of
chats and logs. The cookies are pinned to `.espn.com` over https and are never
sent anywhere else, including across a redirect. They expire: an auth error
mid-season means re-copy them.

## What the assistant can do

### The draft

| Tool | Purpose |
|---|---|
| `get_league_settings` | Team count, scoring, starting lineup, roster limits, draft date and your slot. Call once. |
| `get_draft_context` | **The on-the-clock call.** Draft state, your roster needs, best available by VORP, tier depth per position, position runs, picks until your next turn. One round-trip. |
| `next_pick` | The same decision in ~1KB: candidates that fill a real hole, tier cliffs, picks until your turn. |
| `get_draft_state` | Picks made, who is on the clock, your upcoming picks. Never cached. |
| `get_available_players` | Undrafted players by VORP, projection or ADP. |
| `get_value_board` | Replacement levels and tier structure, the *why* behind the rankings. |
| `get_player` | One player: projection, VORP, tier, ADP and its direction, injury status. |
| `record_picks` / `record_pick` / `undo_pick` / `reset_draft` | Enter picks by name as they happen. Persisted, so each turn only sends what is new. |
| `refresh_draft_order` / `refresh_board` | Re-read the order after a randomized draw; force a pool re-fetch after news. |

### The week

| Tool | Purpose |
|---|---|
| `get_matchup` | **The weekly call.** Your opponent, both lineups by this week's projection, the exact start/sit swaps and their gain, holes on either side (bye, OUT, empty slot), questionable starters, ESPN's win probability. `week=N` plans ahead. |
| `set_lineup` | **Writes.** The swaps that turn your set lineup into the best one, previewed; `apply=true` submits them as one transaction. Players whose game has kicked off are locked and planned around; IR is left alone. |
| `move_player` | **Writes.** One named player into one slot (`QB`, `RB`, `WR`, `TE`, `FLEX`, `K`, `D/ST`, `BE`, `IR`). A full starting slot swaps out its lowest-projected occupant. How you activate a player off IR once a bench spot is free. |
| `get_roster` | Any team's starters and bench with slots, this week's and rest-of-season projections, positional strength. |

### Roster and trades

| Tool | Purpose |
|---|---|
| `get_waiver_targets` | Every unrostered player scored by what adding him does to your optimal lineup, rest-of-season and this week, plus drop candidates, waiver clear times and your priority or FAAB. |
| `add_player` | **Writes.** Free-agent pickup (immediate) or waiver claim (queued for the next run), with the drop in the same transaction. Preview: lineup before/after, roster legality, suggested drops. |
| `drop_player` | **Writes.** Cut one player. Preview shows what the lineup loses. |
| `analyze_trade` | Both sides of a proposed trade, before and after: starting-lineup strength, this week, bench value, roster size and position limits. |
| `find_trade_partners` | Teams weak where you are strong and vice versa, their tradeable players, your surplus, trade blocks, and the best 1-for-1 that helps both sides. |
| `propose_trade` | **Writes.** Preview is `analyze_trade`; `apply=true` sends the offer to the other manager. |
| `get_pending_trades` | Offers waiting on you and offers you sent, with ids and the same evaluation. |
| `respond_to_trade` | **Writes.** Accept or decline an offer made to you, or withdraw one you sent. |
| `get_transactions` | The league's log, newest first: every lineup move, add, drop, claim and trade, with team and time. The only view of history; rosters show the present. |

Typical asks:

- "Who should I start?" → `get_matchup`, then `set_lineup` once you agree. A
  `gain` of a few hundredths is projection noise, not a reason to bench a
  Sunday player for a Thursday one.
- "Anyone worth a claim?" → `get_waiver_targets`: `targets` by lasting lineup
  gain, `streamers_this_week` by this week only (D/ST and K live here),
  `drop_candidates` for who to cut. Then `add_player("Saints", drop="Shakir")`.
- "Is this trade good?" → `analyze_trade`; the partner is inferred from the
  players you receive. "Who should I be trading with?" → `find_trade_partners`.
- "Offer him Judkins for Pickens" → `propose_trade`, preview, then send.
  "Anyone offered me anything?" → `get_pending_trades`, then `respond_to_trade`.
- "Has my opponent set his lineup?" → `get_transactions(team_id=..., week=...)`.

## How it decides

**VORP, not projected points.** Ranking by projection says take a QB first:
the top QB outscores the top RB. That is wrong in a 1-QB league, because the
twelfth QB also scores a lot, so the top QB's edge over a replacement-level
starter is small. VORP measures each player against the last startable player
at his position, which is the actual cost of passing on him. Replacement level
comes from *your* league: team count, starting lineup, and an empirical FLEX
allocation (pool every flex-eligible non-starter, take the best N, count what
positions they are). Scoring is read from the league too, so PPR, half-PPR, TE
premium and custom rules need no configuration. Tiers are 1-D k-means over
each position's draftable range, so "players left in this tier" adapts to the
local scale instead of panicking at every gap.

**Two projections in season.** `week_proj` is ESPN's projection for one week;
it already reflects the NFL opponent, injury designation and bye, so it decides
start/sit. `ros_pg` is rest-of-season points per remaining game (ESPN's season
projection minus actuals, over the games left, a bye ahead counting against
him); it is what a roster spot is worth from here on, and the basis for
in-season VORP, waiver value and trade value. `opp_rank_vs_pos` is ESPN's
OPRK: 1 = the defense that allows the most to that position, 32 = stingiest;
empty until games have been played.

**Everything is a change to the optimal starting lineup**, because that is
the only thing that scores. A pickup who sits behind what you have gains 0
whatever his projection; a trade is judged by what each side's lineup projects
to before and after. The lineup is solved the same way for every team,
dedicated slots first, then flex, so strength is comparable across the league.

## Writing to ESPN

The writing tools post transactions to `lm-api-writes.fantasy.espn.com` with
the same cookie scoping as reads. What they guarantee:

- **Preview by default.** Nothing is sent until `apply=true`. The preview is
  the full change: every slot move, the drop that goes with an add, both sides
  of an offer, and what the lineup gains or loses.
- **Locked players are planned around.** ESPN locks a player from his kickoff
  to the end of the week and rejects any transaction touching one. The tools
  know each starter's kickoff, pin locked players where they are, and refuse a
  move that would fail rather than submit it.
- **One transaction per change.** A start/sit swap, an add with its drop, a
  proposal with both sides: each is a single ESPN transaction, so the roster
  is never over the limit or short a starter in between.
- **A sent offer is a message to a real person.** `propose_trade` and
  `respond_to_trade(accept)` are the two calls the assistant is told to
  confirm before applying.

Lineup moves and free-agent adds have been exercised against a live league.
Waiver claims, trade proposals and trade responses follow the payload shapes
ESPN's own client sends and are covered by tests; the first real use of each
is its live check, and a refusal comes back as a readable error, not a
half-applied change.

## Draft day

**ESPN publishes no picks until a draft ends.** Verified against real drafts:
while the room was in progress the read API reported zero picks and empty
rosters, then everything at once on completion. Polling cannot drive a live
draft, so the picks you enter *are* the draft:

- `record_picks(["Gibbs", "Chase", ...])` each turn with only the new picks.
  Names resolve like the draft room does; ambiguous names return candidates
  instead of guessing; already-drafted players are refused. Picks persist to
  `state/`, so a restart loses nothing.
- Or let a browser do it. `scripts/live_draft.py` watches the real draft room
  and records picks as they land, `sync_from_room.py` seeds the board from the
  room's pick history if you join late, `draft_player.py` clicks DRAFT for
  you, and `autodraft.py` runs the whole draft on the value board. These need
  `pip install -e ".[browser]"` and Chrome started with
  `--remote-debugging-port=9222`. That port lets any local process read every
  tab and cookie in that browser: use it on a machine you control, bind it to
  `127.0.0.1` only, and close that browser when the draft ends.
- `scripts/snapshot.py` shortly before the draft writes the full board to
  `snapshots/` as JSON and CSV, so an ESPN outage mid-draft leaves you a
  board.

On the clock, `get_draft_context` carries `picks_between_this_and_next`, the
number that decides whether a player will last. ADP is a lagging average, so
every value-vs-ADP entry also carries `adp_moving` (`"earlier"` or `"later"`):
a player who just won a job looks like a bargain against a stale ADP right up
until he does not last to your pick. `adp_age_hours` says how current the
market is.

Offline drafts, mock drafts and grading: `scripts/simulate_draft.py` runs a
full mock against the real board, `simulate_autodraft.py` the autodraft scorer,
and `grade_external.py` grades finished rosters against an outside ranking so
the scorer is not marking its own work.

## ESPN quirks handled

Each found against a real league, each with a regression test.

- **Placeholder picks and negative ids.** An unstarted draft pre-seeds every
  slot with `playerId: -1`, which reads as a completed draft. Filtering
  `playerId > 0` is worse: D/ST ids are negative, so every drafted defense
  vanishes and the pick count drifts. `-1` is matched exactly.
- **No D/ST projections.** Defenses project at 0.0, so VORP would be a
  uniform 0 and sort them above negative-value players. Positions without
  projections are marked `ranked_by: espn_adp`, get `vorp: null`, and sort
  below everything ranked on real value.
- **Non-contiguous team ids.** A 10-team league can have ids `[1,2,3,6,7,8,9,
  10,12,13]`. Draft slot comes from position in the order, never from the id.
- **Weekly stats are one week per request.** The player pool returns the
  split for week N only when the request's `scoringPeriodId` is N, so each
  week the season tools look at is its own fetch, cached per week.
- **Mid-week scores live in the `*Live` fields.** During a week ESPN leaves
  `totalPoints` at 0 and reports the running score in `totalPointsLive`; the
  plain field fills in when the week is final.
- **Draft time is epoch milliseconds in UTC**, so any US evening draft reads
  as the next day. Rendered in local time alongside a countdown.
- **Randomized draft order** is rewritten late. Settings are TTL-cached, the
  order is read from ESPN's published pick schedule (live, uncached), and
  `draft_slot_is_provisional` says when the draw has not happened yet. Reading
  the schedule also means third-round reversal and other snake variants come
  through correctly.

Other failure modes: cookies expire (readable error, re-copy them); ESPN
blocks some datacenter IP ranges (run it locally); projections are ESPN's and
mediocre in absolute terms (the value math and ADP-vs-value gaps are the edge,
since ESPN's ADP reflects what your ESPN leaguemates will actually do).

## Caching

The player pool is cached for `ESPN_POOL_TTL` seconds (default 15 minutes)
because it is slow and changes slowly. Rosters are cached for 60 seconds and
refreshed after every write. Draft state and the transaction log are never
cached.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

118 tests, no network or credentials. The ESPN client is stubbed with
real-shaped payloads, so the value math, snake pick ordering, board assembly,
lineup solving, trade and waiver arithmetic, every write's transaction shape,
and the tool wiring are verified offline. The client tests also prove the
cookies never leave `.espn.com`.

## Layout

```
src/espn_mcp/
  config.py      .env loading, secret masking
  espn.py        HTTP client: host pin, cookie scoping, reads and writes
  constants.py   ESPN's position / slot / pro-team id maps
  scoring.py     league shape parsing, league-scored projections
  value.py       replacement level, VORP, tiers            (pure, unit tested)
  season.py      rest-of-season, optimal lineups, lineup plans,
                 trade / waiver deltas, transaction log     (pure, unit tested)
  board.py       caching layer, draft state, in-season rosters and matchups
  server.py      the MCP tools
scripts/
  doctor.py              credential and access check
  snapshot.py            offline fallback board
  live_draft.py          watch the draft room, record picks      (browser)
  sync_from_room.py      seed the board from the room's history  (browser)
  draft_player.py        click DRAFT in the room                 (browser)
  autodraft.py           draft on the value board                (browser)
  watch_draft.py         poll the read API during a draft (proves it lags)
  simulate_draft.py      offline mock draft on the real board
  simulate_autodraft.py  offline run of the autodraft scorer
  grade_external.py      grade rosters against an outside ranking
state/                   picks, browser profile, notes (gitignored)
```

The Omarchy desktop widget that shows this matchup in the bar is a separate
project, [omarchy-fantasy-feed](https://github.com/HamCops/omarchy-fantasy-feed);
it reads the league on its own and shares no code with this one.
