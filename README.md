# D.O.D.I. Draft Optimization Dynasty Intelligence 

An MCP server that gives your AI agent full control of an ESPN fantasy football
league — draft picks, weekly lineups, waivers, trades, and roster moves. Wire
it into any MCP-compatible harness (Claude Code, Hermes, your own), give the
agent a persona and a cron schedule, and let it run your season.

We call ours **Dodi** — **D**raft **O**ptimization & **D**ynasty
**I**ntelligence. Dodi checks the waiver wire every afternoon, optimizes
the lineup before kickoff, scouts trade partners, fires off offers, responds to
incoming trades, and pings us on the phone when something happens. Dodi doesn't
ask permission to set the best lineup. Dodi does ask before trading away your
RB1. You decide where that line is.

## What this is for

You have an AI agent (LLM + tool loop). You want it to manage an ESPN fantasy
football team — not answer trivia about one, but actually read the league state,
do the math, and submit roster changes. This server is the tool layer. It
handles:

- **The draft.** Live pick tracking, VORP-ranked boards, on-the-clock
  recommendations, and optional browser automation to click DRAFT for you.
- **Weekly lineups.** Start/sit optimization against ESPN's weekly projections,
  with one call to preview and one to apply.
- **Waivers and free agents.** Every unrostered player scored by what he does to
  your starting lineup, with drop candidates ranked by expendability.
- **Trades.** League-wide partner scouting, both-sides-win analysis, proposal
  submission, and inbox management.
- **Transactions.** Full league history — who moved whom, when — so your agent
  can detect when opponents are active or asleep.

The server never guesses. Every writing tool previews the exact change by
default and only submits with `apply=true`. Where you draw the autonomy line —
auto-apply lineups, require confirmation for trades, let it YOLO everything —
is up to your agent's prompt.

## Quick start

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # fill in your credentials (see below)
./.venv/bin/python scripts/doctor.py   # validates creds, prints league info + top-10 board
```

### Register with your harness

**Claude Code / Hermes:**
```bash
claude mcp add espn-fantasy -- "$(pwd)/.venv/bin/python" -m espn_mcp.server
```

**Generic MCP (stdio transport):**
```json
{
  "command": "/path/to/espn-mcp/.venv/bin/python",
  "args": ["-m", "espn_mcp.server"],
  "transport": "stdio"
}
```

The server exposes ~25 tools. Your agent discovers them via MCP's `tools/list`.
No tool configuration needed on the harness side.

## Credentials

`ESPN_LEAGUE_ID` — the number in `fantasy.espn.com/football/league?leagueId=...`

`ESPN_TEAM_ID` — the `teamId=` in your roster page URL.

Private leagues (most are) also need two cookies from a browser session logged
into ESPN. DevTools → Application → Cookies → `fantasy.espn.com`:

- `espn_s2` — long URL-encoded string, full value
- `SWID` — UUID **with** the braces: `{F64EC866-...}`

Put all four in `.env`. The file is gitignored. These are session credentials —
treat them like a password. They're scoped to `.espn.com` over HTTPS and never
sent elsewhere. They expire mid-season; an auth error means re-copy them.

## Tools

### Draft

| Tool | What it does |
|---|---|
| `get_draft_context` | **The on-the-clock call.** Board state, roster needs, VORP-ranked candidates, tier depth, position runs, picks until your next turn. One round-trip. |
| `next_pick` | Same decision in ~1KB — candidates that fill a hole, tier cliffs, time pressure. |
| `get_draft_state` | Picks made, who's on the clock, your upcoming slots. Never cached. |
| `get_available_players` | Undrafted pool by VORP, projection, or ADP. |
| `get_value_board` | Replacement levels and tier structure — the math behind the rankings. |
| `get_player` | One player in detail: projection, VORP, tier, ADP, injury. |
| `record_picks` / `record_pick` | Enter picks by name as they happen. Persisted to `state/`. |
| `undo_pick` / `reset_draft` | Fix mistakes or start fresh. |
| `refresh_draft_order` | Re-read after a randomized draw. |
| `refresh_board` | Force pool re-fetch after breaking news. |

### Weekly lineup

| Tool | What it does |
|---|---|
| `get_matchup` | Both lineups, start/sit swaps with point values, opponent holes, win probability. `week=N` for lookahead. |
| `set_lineup` | Preview the optimal swaps; `apply=true` submits them. Locked players (game started) are planned around. |
| `move_player` | One player → one slot. Swaps out the weakest occupant if the slot is full. |
| `get_roster` | Any team's full roster with projections and positional strength. |

### Waivers and free agents

| Tool | What it does |
|---|---|
| `get_waiver_targets` | Best pickups by lineup impact (ROS and this-week), drop candidates, waiver timing. |
| `add_player` | Free-agent add (instant) or waiver claim (queued), with optional drop in the same transaction. |
| `drop_player` | Cut a player. Preview shows what the lineup loses. |

### Trades

| Tool | What it does |
|---|---|
| `find_trade_partners` | League-wide scan: who's weak where you're strong, mutual-fit candidates, trade blocks, records. |
| `analyze_trade` | Both sides before/after: starter strength, bench value, roster legality. |
| `propose_trade` | Preview or send (`apply=true`) an offer to another manager. |
| `get_pending_trades` | Inbox: offers waiting on you and offers you sent. |
| `respond_to_trade` | Accept, decline, or withdraw. |

### League info

| Tool | What it does |
|---|---|
| `get_league_settings` | Scoring, roster slots, team count, draft type. Call once per session. |
| `get_transactions` | Full transaction log — lineup moves, adds, drops, claims, trades — by team and week. |

### Approvals

| Tool | What it does |
|---|---|
| `request_approval` | Queue an add, drop, trade offer, trade answer or start/sit call for the manager. Previewed first; sent to ESPN only when approved. |
| `get_proposals` | What is waiting, and what was approved, rejected or expired. |

## Running it hands-off

Everything in this section is optional and off by default. Each part is
switched on in `.env`; `.env.example` documents the settings.

### Approve from your phone

With `ESPN_REQUIRE_APPROVAL=1`, roster moves and trades can no longer be
sent directly: `add_player`, `drop_player`, `propose_trade` and
`respond_to_trade` refuse `apply=true`, in the server, whatever the prompt
says. The agent queues them with `request_approval`, and you get an
[ntfy](https://ntfy.sh) notification with Approve, Reject and Details
buttons. Lineup changes stay direct.

`espn-mcp-approve` is the service behind the buttons. It listens on
loopback only. Put it behind something private (a tailnet, a VPN), never
the open internet: approving a proposal makes a real roster move. Each
proposal has its own token, a decision can be made once, and a proposal
interrupted mid-send is closed, never retried.

### Decisions by the kickoff clock

Run `espn-mcp-tick` every five minutes. For each group of games on your
roster it counts back from the real kickoff:

| When | What |
|---|---|
| Kickoff − 65 min | Calls `GAMETIME_HOOK`, to start your agent |
| Kickoff − 45 min | Sets the lineup (`espn-mcp-lineup`, no model) and sends close calls |
| Kickoff − 5 min | Approval requests for that game expire |

Both runs come after inactives are announced, and leave
`APPROVAL_LEAD_MINUTES` (default 30) to answer. `espn-mcp-tick status`
prints the week's plan.

### Outside data

With `ESPN_EXTERNAL_SOURCES=1`, player records also carry:

| Field | Source | What it is |
|---|---|---|
| `market` | FantasyCalc | Trade value from completed trades |
| `usage` | nflverse | Points scored against what carries and targets say he should score |
| `game` | ESPN scoreboard, Open-Meteo | Betting line, roof, forecast at kickoff |
| `adj_week_proj` | derived | ESPN's projection moved by the betting line; the lineup is set by it |
| `adds_24h`, `alt_week_proj`, `injury_alt` | Sleeper | Pickup trend, a second projection, a second injury report |

All public, no keys. Every source is cached and fails soft: one that is
down leaves its fields off and the tools carry on with ESPN alone.

What is used and how much it counts was decided by testing each signal on
two past seasons, fit on one and scored on the other. Most did not survive.
[`research/README.md`](research/README.md) has the method, the results,
what was thrown out, and the scripts to rerun it.

### Weekly review

`espn-mcp-report` prints last week's result, the points left on the bench,
how each projection did against what was recorded before kickoff, and how
the approved and rejected moves turned out.

## Building an agent on top (the Dodi pattern)

The MCP server is the tool layer. Your agent is the brain. Here's how Dodi
works as a reference architecture:

### 1. Write a system prompt

Give the agent a persona, decision framework, and autonomy rules. Dodi's:
- Aggressive, data-driven, no sentimentality
- Auto-applies lineup optimizations (no confirmation needed)
- Reports trade recommendations but waits for approval (or not — your call)
- Returns `[SILENT]` when there's nothing to report, so the harness skips delivery

### 2. Schedule recurring runs

Dodi runs as four cron jobs in Hermes, each loading the same skill/prompt:

| Schedule | Purpose |
|---|---|
| Sunday 01:00 UTC | Full pre-game: matchup analysis, lineup optimization, trade scan, waiver check |
| Thursday 01:00 UTC | TNF lineup lock — make sure Thursday starters are set |
| Tue–Sat 20:00 UTC | Daily trade inbox + waiver wire scan |
| Monday 22:00 UTC | MNF late injury check |

Each run delivers a report to Discord and a short push notification to the
phone via ntfy, so you know to check it without staring at Discord all day.

### 3. Let it write

The tools default to `apply=false` (preview only). Your prompt decides when
the agent calls with `apply=true`. Dodi auto-applies `set_lineup` but previews
trades. A more aggressive agent could auto-send trades where both sides gain.
A cautious one could preview everything. The server doesn't care — it enforces
the preview/apply split, your prompt enforces policy.

## How it decides

**VORP, not projected points.** Raw projections say draft a QB first — the top
QB outscores the top RB. That's wrong in a 1-QB league because the 12th QB
also scores a lot, making the top QB's edge over replacement small. VORP
measures each player against the last startable player at his position in
*your* league: team count, starting slots, and an empirical FLEX allocation.
Scoring rules (PPR, half-PPR, TE premium, custom) are read from the league —
no configuration needed.

**Two projections in season.** `week_proj` is ESPN's weekly number (reflects
opponent, injury, bye) — decides start/sit. `ros_pg` is rest-of-season points
per remaining game — decides roster value, trade worth, waiver priority.

**Everything is a change to the optimal starting lineup.** A pickup who sits
behind what you have gains 0 whatever his projection. A trade is judged by
what each side's starters project before and after. The lineup solver runs the
same way for every team, so strength is comparable league-wide.

**Tiers are 1-D k-means** over each position's draftable range. "Players left
in this tier" adapts to the local distribution instead of panicking at every
gap.

## Draft day

ESPN publishes no picks until a draft ends — verified against real drafts. The
read API reports zero picks and empty rosters while the room is live, then
everything at once on completion. Your agent needs picks fed in as they happen:

- `record_picks(["Gibbs", "Chase", ...])` each turn, only the new names.
  Persisted to `state/`, survives restarts, deduplicates on re-send.
- Or use the browser scripts: `live_draft.py` watches the room and records
  automatically, `sync_from_room.py` seeds from room history if you join late,
  `draft_player.py` clicks DRAFT, and `autodraft.py` runs the whole draft on
  the value board. These need `pip install -e ".[browser]"` and Chrome with
  `--remote-debugging-port=9222` (bind to 127.0.0.1, close after).
- `snapshot.py` saves the full board to `snapshots/` as JSON and CSV before the
  draft, so an ESPN outage doesn't leave you blind.

On the clock, `get_draft_context` returns `picks_between_this_and_next` — the
number that decides if a player will last. ADP entries include `adp_moving`
(`"earlier"` / `"later"`) and `adp_age_hours` so your agent can discount stale
market data.

## ESPN quirks handled

Each found against a real league, each with a regression test.

- **Placeholder picks and negative IDs.** Unstarted drafts pre-seed every slot
  with `playerId: -1`. D/ST IDs are also negative. Exact `-1` match, not
  `> 0` filtering.
- **No D/ST projections.** Defenses project 0.0; they're marked
  `ranked_by: espn_adp` with `vorp: null` and sort below real-value players.
- **Non-contiguous team IDs.** A 10-team league can have IDs
  `[1,2,3,6,7,8,9,10,12,13]`. Draft slot comes from position in the order.
- **Weekly stats require per-week requests.** One `scoringPeriodId` per fetch.
- **Mid-week scores use `*Live` fields.** `totalPoints` stays 0 until the week
  is final; `totalPointsLive` has the running score.
- **Draft time is epoch ms in UTC.** Rendered in local time with countdown.
- **Randomized draft order** rewrites late. Read from ESPN's pick schedule
  (live, uncached), not settings. `draft_slot_is_provisional` flags pre-draw.

Cookie expiry, datacenter IP blocks, and ESPN's mediocre projections are the
other failure modes. The value math and ADP-vs-value gaps are the edge — ESPN's
ADP reflects what your ESPN leaguemates will actually do.

## Caching

Player pool: `ESPN_POOL_TTL` seconds (default 900). Rosters: 60s, refreshed
after writes. Draft state and transaction log: never cached.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

118 tests, no network, no credentials. ESPN client stubbed with real payloads.
Covers value math, snake ordering, board assembly, lineup solving, trade/waiver
arithmetic, every write's transaction shape, and cookie scoping.

## Layout

```
src/espn_mcp/
  config.py      .env loading, secret masking
  espn.py        HTTP client: host pin, cookie scoping, reads and writes
  constants.py   ESPN position / slot / pro-team ID maps
  scoring.py     league shape parsing, league-scored projections
  value.py       replacement level, VORP, tiers            (pure, tested)
  season.py      ROS, optimal lineups, lineup plans,
                 trade / waiver deltas, transaction log     (pure, tested)
  board.py       caching layer, draft state, in-season rosters and matchups
  server.py      the MCP tools
  proposals.py   the approval queue                         (pure, tested)
  notify.py      ntfy pushes and the approval buttons
  approve.py     the service behind the buttons
  gametime.py    scheduling counted back from kickoff
  autolineup.py  set the lineup without a model
  usage.py       expected points from workload              (pure, tested)
  market.py      trade arithmetic on market values          (pure, tested)
  tracking.py    what was known before kickoff, for scoring later
  report.py      the weekly review
  sources/       Sleeper, FantasyCalc, nflverse, betting lines, weather
research/        the backtests behind every outside signal
scripts/
  doctor.py              credential + access check
  snapshot.py            offline fallback board
  live_draft.py          watch the draft room, record picks      (browser)
  sync_from_room.py      seed board from room history            (browser)
  draft_player.py        click DRAFT in the room                 (browser)
  autodraft.py           draft on the value board                (browser)
  watch_draft.py         poll read API during draft (proves it lags)
  simulate_draft.py      offline mock draft
  simulate_autodraft.py  offline autodraft scorer
  grade_external.py      grade rosters against outside ranking
state/                   picks, browser profile, notes (gitignored)
```
