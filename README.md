# espn-mcp

An MCP server that exposes your ESPN fantasy football league so an AI assistant
can help you draft — live, during a snake draft, on the clock — and then manage
the team through the season: start/sit by matchup, waiver claims, and trades.

It does not try to pick for you. It gives the model accurate, league-specific
facts: who is actually available, what your roster still needs, how much value
is left at each position, and how many picks you have before your next turn.
The one thing it computes rather than reports is the value math (VORP,
replacement level, tiers) — deterministic arithmetic that a language model
should not be doing in its head.

## Why VORP and not projected points

Ranking by projected points says take a QB first: the top QB outscores the top
RB outright. That is wrong in a 1-QB league, because the *twelfth* QB also
scores a lot, so the top QB's edge over a replacement-level starter is small.
VORP measures each player against the last startable player at their position,
which is the actual cost of passing on them.

Replacement level is derived from *your* league — team count and starting
lineup — not from a rule of thumb. FLEX slots are allocated empirically: the
server pools every flex-eligible player who is not already a dedicated starter,
takes the best N by projection, and counts what positions they actually are. A
league that starts 3 WR produces different scarcity than one that starts 2, and
the board reflects that automatically.

Scoring is read from the league too, via `scoringSettings.scoringItems`, so PPR,
half-PPR, TE premium and fully custom scoring all work with no configuration.

Tiers come from 1-D k-means (Jenks natural breaks) over each position's
draftable range, not a gap threshold. A global threshold does not work: elite
players are genuinely far apart, so it makes each of them a singleton tier and
dumps everyone else into one blob — which turns "players left in this tier"
into a constant panic signal. Clustering adapts to the local scale. Each
position also reports the *next* tier's size and the VORP drop into it, which
is the number that actually answers "can I wait until my next pick?"

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill it in
```

### Credentials

`ESPN_LEAGUE_ID` is the number in your league URL:
`https://fantasy.espn.com/football/league?leagueId=123456789`

`ESPN_TEAM_ID` is the `teamId` in the URL when you open your own roster.

If the league is private (most are), you also need two cookies from a browser
logged in to ESPN — DevTools → Application → Cookies → `fantasy.espn.com`:

- `espn_s2` — a long URL-encoded string, copy the whole value
- `SWID` — a UUID **including** the surrounding braces

**These are session credentials. Treat them like a password.** They grant access
to your ESPN account's fantasy data. `.env` is gitignored; keep it that way, and
do not paste these into a chat, an issue, or a shared log. They also expire — if
tools start returning auth errors mid-season, re-copy them.

### Verify before draft day

```bash
./.venv/bin/python scripts/doctor.py
```

Prints your league format, replacement levels and a top-10 board. If that
works, the server works. Run it well before the draft, not ten minutes prior.

### Register with Claude Code

```bash
claude mcp add espn-fantasy -- "$(pwd)/.venv/bin/python" -m espn_mcp.server
```

## Tools

| Tool | Purpose |
|---|---|
| `get_league_settings` | Team count, scoring, starting lineup, draft date and slot. Call once. |
| `get_draft_context` | **The on-the-clock call.** State + your needs + best available + tier depth + position runs, in one round-trip. |
| `get_draft_state` | Picks made, who's on the clock, your next picks. Never cached. |
| `get_available_players` | Undrafted players ranked by VORP, projection, or ADP. |
| `get_value_board` | Replacement levels and tier structure — the *why* behind the rankings. |
| `get_roster` | Any team's roster and unfilled starting slots. |
| `get_player` | One player's projection, VORP, tier, ADP, injury status. |
| `next_pick` | **The on-the-clock call.** Compact: candidates that fill a real hole, tier cliffs, picks until your turn. ~1KB. |
| `record_picks` | Batch-record picks by name. Persisted, so you only ever send what's new. |
| `record_pick` / `undo_pick` / `reset_draft` | Single pick, undo, and clearing state before a new draft. |
| `refresh_draft_order` | Re-read the order and your slot after a randomized draw. |
| `refresh_board` | Force a pool re-fetch (injury news, depth chart change). |

In season:

| Tool | Purpose |
|---|---|
| `get_matchup` | **The weekly call.** Your opponent, both lineups by this week's projection, the exact start/sit swaps and what they gain, holes on either side (bye, OUT, empty slot), ESPN's win probability. Pass `week` to plan ahead. |
| `get_waiver_targets` | Every unrostered player scored by what adding him does to your optimal lineup (rest-of-season and this week), plus drop candidates, waiver clear times and your priority/FAAB. |
| `analyze_trade` | Both sides of a proposed trade, before and after: starting-lineup strength, this week, bench value, roster size and position limits, suggested drops. |
| `get_transactions` | The league's transaction log, newest first: every lineup move (player, from slot, to slot), add, drop, waiver claim and trade, with team and timestamp. Filter by team, week or kind. The only view of history; rosters show the present. |
| `find_trade_partners` | Which teams are weak where you are strong and vice versa, with their tradeable players, your surplus, each team's trade block, and the best 1-for-1 that helps both sides. |
| `get_roster` | In season: a team's starters and bench with lineup slots, this week's and rest-of-season projections, positional strength. |
| `set_lineup` | **Writes to ESPN.** The swaps that turn your set lineup into the best one by this week's projection. Default is a preview; `apply=true` submits them as one ESPN transaction. Players whose game has kicked off are locked and worked around; IR is left alone. |
| `move_player` | **Writes to ESPN.** Put one named player in one slot (`QB`, `RB`, `WR`, `TE`, `FLEX`, `K`, `D/ST`, `BE`, `IR`). A full starting slot swaps out its lowest-projected occupant. The way to activate a player off IR once a bench spot is free. |
| `add_player` | **Writes to ESPN.** Free-agent pickup (immediate) or waiver claim (queued for the next run), with the drop in the same transaction. Preview shows the lineup before/after, roster legality and suggested drops; `apply=true` submits. |
| `drop_player` | **Writes to ESPN.** Cut one player. Preview shows what the lineup loses. |
| `propose_trade` | **Writes to ESPN.** Preview is `analyze_trade`; `apply=true` sends the offer to the other manager. |
| `get_pending_trades` | Offers waiting on you and offers you sent, with ids and the same evaluation `analyze_trade` gives. |
| `respond_to_trade` | **Writes to ESPN.** Accept or decline an offer made to you, or withdraw one you sent. |

The player pool is cached for `ESPN_POOL_TTL` seconds (default 15 min) because
it is slow and changes slowly. Draft picks are never cached; rosters are cached
for 60 seconds because waivers and trades move them.

## During the draft

Ask in plain language — "I'm on the clock, what should I take?" — and the model
will call `get_draft_context` and reason over it. Useful follow-ups:

- "How much RB value is left before the tier breaks?"
- "Can I wait on TE until my next pick, or does the tier empty first?"
- "Who's fallen furthest below their ADP?"
- "What does team 7 still need?" (they pick right before you)

`get_draft_context` includes `picks_between_this_and_next`, which is the number
that actually matters at a turn: how many players come off the board before you
choose again.

**Speed.** Since ESPN publishes no picks until a draft ends, the picks you
report *are* the draft. They persist to `state/`, so each turn only needs the
picks made since the last one -- never the whole board. That, not the API, was
the real clock cost: re-stating 100+ names per turn. Recording 8 picks takes
~380ms (was ~540ms *each*), and `next_pick` answers in ~90ms with a ~1KB
payload. Server-side compute is 0.3ms; everything else is one ESPN round-trip.

## ESPN's stat columns, and which matter for a draft

Mapping ESPN's glossary onto what this server exposes:

| ESPN | Exposed as | Draft relevance |
|---|---|---|
| PROJ | `proj` | Yes — but the **season** total, not ESPN's weekly "upcoming game" figure |
| %ROST | `percent_owned` | Weak. Popularity, already priced into ADP |
| +/- | `rostered_change` | Moderate — a week's move in rostered % |
| %ST | `percent_started` | Weak pre-draft; no lineups have been set |
| PRK | `position_value_rank` | Recomputed by VORP, not ESPN's ranking |
| OPRK | not exposed | **No** — a single-week matchup rating |
| PVO | not exposed | **No** — position vs a specific week's opponent |
| LAST | not exposed | **No** — last game's score; no games played yet |

OPRK, PVO and LAST are in-season lineup tools. They describe one week against
one opponent and say nothing about a player's season value, which is what a
draft is buying.

The most useful field is not in the glossary: `averageDraftPositionPercentChange`,
exposed as `adp_moving` (`"earlier"` / `"later"`) plus `adp_change_pct`. **ADP is
a lagging average** — it is computed over drafts that already happened, so a
player who just won a starting job still carries a stale, too-late ADP. That
makes him look like a bargain in a value-vs-ADP comparison right up until the
moment he does not last to your pick. Direction of travel is what separates a
real value from a stale number, so every `falling_below_adp` entry is annotated
with it.

Note the sign convention: the change applies to the ADP *number*, so positive
means being drafted **later** (cooling off), not hotter. The server reports a
direction word rather than a bare signed float for exactly that reason.

`get_draft_context` also reports `adp_as_of` and `adp_age_hours`, since the
whole "will he last?" question rests on how current the market data is.


## During the season

Two projections matter in season and they answer different questions, so
every in-season record carries both:

- **`week_proj`** — ESPN's projection for one week. It already reflects the
  NFL opponent, the injury designation and the bye, which is why `get_matchup`
  uses it for start/sit: a player's season value is irrelevant to whether he
  should start *this* week against *that* defense.
- **`ros_pg`** — rest-of-season points per remaining game. ESPN publishes a
  full-season projection and season-to-date actuals but no rest-of-season
  figure, so ROS is the difference, divided by the games the player has left
  (a bye still ahead counts against him). This is what a roster spot is worth
  from here on, and it is the basis for in-season VORP, waiver value and trade
  value. In week 1 it equals the draft board.

Everything is measured as a change to your **optimal starting lineup**, because
that is the only thing that scores. A waiver pickup who sits behind what you
already have gains 0 no matter how good his projection looks; a trade is
judged by what each side's lineup projects to before and after. The lineup is
solved the same way for every team — dedicated slots first, then flex — so
team strength is comparable across the league, which is how
`find_trade_partners` spots a team weak at WR and deep at RB.

Typical asks:

- "Who should I start this week?" → `get_matchup`. Lists the swaps, with the
  gain; `holes` flags a starter on bye or OUT before kickoff does.
- "Anyone worth a claim?" → `get_waiver_targets`. `targets` is ranked by
  lasting lineup gain, `streamers_this_week` by this week only (D/ST and K
  live here), `best_depth_by_ros_vorp` is the stash list, and
  `drop_candidates` is who to cut for him. `waivers_clear` says when a claim
  processes.
- "Is this trade good for me?" → `analyze_trade`. Give names, get both sides'
  before/after. The partner is inferred from the players you receive.
- "Who should I be trading with?" → `find_trade_partners`, optionally for one
  position. `best_1_for_1` is a concrete opener that helps both lineups;
  `mutual: false` means every fit found is lopsided.
- "Next week I have three guys on bye" → `get_matchup(week=N)`.
- "Set my best lineup" → `set_lineup`, then `set_lineup(apply=true)` once the
  preview looks right. It uses the same projection `get_matchup` scores, so
  the swaps match. A tiny `gain` (a few hundredths) is projection noise, not a
  reason to bench a Sunday player for a Thursday one.
- "Bowers is off IR, put him on the bench" → `move_player("Bowers", "BE")`.
  Needs a free bench spot first; ESPN will not do the drop for you.

- "Grab the Saints defense, drop Shakir" → `add_player("Saints", drop="Shakir")`
  to preview, then `apply=true`. A player on waivers becomes a claim that
  processes at `waivers_clear`; a free agent lands immediately.
- "Offer him Judkins for Pickens" → `propose_trade(["Judkins"], ["Pickens"])`
  previews both sides; `apply=true` sends it. `get_pending_trades` tracks it;
  `respond_to_trade(id, "withdraw")` pulls it back.
- "Anyone offered me anything?" → `get_pending_trades`, then
  `respond_to_trade(id, "accept" | "decline")`.

Every writing tool needs `ESPN_S2` and `SWID`; they post to
`lm-api-writes.fantasy.espn.com` with the same cookie scoping as reads, and
every one previews by default and only touches ESPN with `apply=true`. A
player is locked from his kickoff until the week ends, and ESPN rejects a
transaction that touches one, so the tools plan around locked players rather
than submit and fail. Lineup moves and free-agent adds have been exercised
against a live league; the waiver, trade-proposal and trade-response payloads
follow the shapes ESPN's own client sends and are covered by tests, but the
first real use of each is its live check.

`opp_rank_vs_pos` is ESPN's OPRK — points a defense allows to a position, 1 =
softest matchup, 32 = stingiest. It is empty until games have been played, so
it appears from week 2.

## Desktop: live matchup in the Omarchy bar

`scripts/feed_sync.py` bridges this league into the
[Fantasy Feed](https://github.com/HamCops/omarchy-fantasy-feed) Omarchy plugin
(a fork with league support). It writes your and your opponent's starters,
the league's scoring rules, and ESPN's live matchup totals into files the
plugin watches, so the bar shows `ME 41.2 – 37.9 TM2` and the panel shows
both lineups scored the way ESPN scores them. The plugin never sees your
cookies; this script does the fantasy-API reads.

```bash
./.venv/bin/python scripts/feed_sync.py --once            # sync now
./.venv/bin/python scripts/feed_sync.py --install-service # then enable the unit it prints
```

The service re-syncs every 15 minutes while nothing is on. While the plugin
reports games in progress it re-syncs every minute, lineups included, so a
start/sit change on ESPN reaches the bar within a minute.

## ESPN quirks handled

Found by running against a real league; each has a regression test.

- **Placeholder picks, and negative player ids.** An unstarted draft does not
  return an empty pick list. ESPN pre-seeds every slot — all 170 of them in a
  10×17 league — with `playerId: -1`, which at face value reads as a completed
  draft. The obvious filter, `playerId > 0`, then introduces a worse bug:
  **D/ST ids are negative** (`-16001`..`-16034`), so every drafted defense is
  silently dropped, the pick count drifts, and defenses stay in the available
  pool after being taken. `-1` must be matched exactly. Found by simulating a
  full 170-pick draft; invisible to spot checks of the top of the board.
- **No D/ST projections.** Every defense comes back projected at 0.0, so VORP
  for them would be a uniform 0 — which would sort them *above* genuinely
  negative-value players. Positions with no projections are marked
  `ranked_by: espn_adp`, get `vorp: null`, and sort below everything ranked on
  real value while staying ADP-ordered among themselves.
- **Non-contiguous team ids.** A 10-team league can have ids `[1,2,3,6,7,8,9,
  10,12,13]`. Draft slot comes from position in the order, never from the id.
- **Weekly projections are one week per request.** `kona_player_info` returns
  the stat split `11{season}{week}` only when the request's `scoringPeriodId`
  is that same week, whatever the filter asks for. Each week the season tools
  look at is therefore its own pool fetch (about half a second), cached per
  week.
- **Draft time is epoch milliseconds in UTC.** Any US evening draft therefore
  reads as the *next day* in UTC — a draft the league page shows as "Mon Sep 7
  at 8:00 PM" comes back as `2026-09-08T00:00:00Z`. `get_league_settings`
  renders it in the machine's local zone alongside the UTC value, plus a
  countdown, so it matches what the site says.
- **Randomized draft order.** Leagues that draw the order shortly before the
  draft rewrite it late. Two consequences, both handled: league settings are
  TTL-cached rather than cached forever, and the order is read from ESPN's
  published pick schedule — every slot in `draftDetail.picks` carries a
  `teamId` even before anyone picks — which is live, uncached, and updates the
  moment the draw happens. `get_league_settings` sets
  `draft_slot_is_provisional` when the published order is still teams in id
  order, and `refresh_draft_order` forces a re-read after the draw.

  Using the published schedule also means the snake pattern is never assumed:
  formats like third-round reversal come through correctly because ESPN states
  who owns each pick rather than us inferring it.

## Failure modes

This uses ESPN's undocumented v3 API. It can change or rate-limit without
notice.

- **Snapshot first.** Run `scripts/snapshot.py` shortly before the draft. It
  writes a full JSON and CSV board to `snapshots/`, so an outage mid-draft
  leaves you with a usable board rather than nothing.
- **Cookies expire.** Re-copy them if you see auth errors. The server reports
  these as readable messages rather than raising.
- **Picks are not readable during a live draft.** This is the big one, and it
  was verified end to end against a real 10-team league drafted start to
  finish. While the draft was in progress — dozens of picks made, visible in
  the browser — the read API reported `picks_made: 0` and every roster empty,
  across ~60 polls over five minutes, unauthenticated and with cookies alike.
  The moment the draft completed, all 160 picks and all 160 roster entries
  appeared at once.

  ESPN's draft room is a separate real-time system; the league API is only
  written at completion. **So polling cannot drive a live draft.** Use
  `record_pick` to enter picks by name as they happen — that path carried an
  entire real draft and is what draft day should rely on.

  A practice draft is worse still: it never persists at all, so it cannot even
  be used to test this.
- **Offline drafts, or a live draft that stops reporting.** Use `record_pick`
  to enter picks by name — "Gibbs" is enough. The team defaults to whoever the
  schedule says is on the clock, ambiguous names return candidates instead of
  guessing, and already-drafted players are refused. Manual picks merge with
  anything ESPN does report, so the pool stays correct either way. This is the
  insurance policy for draft night; `undo_pick` reverses mistakes.
- **Run it locally.** ESPN blocks some datacenter IP ranges.
- **Projections are ESPN's.** They are mediocre in absolute terms. The value
  math and ADP-vs-value gaps are where the edge is, since ESPN's ADP reflects
  what your ESPN leaguemates will actually do.

## Browser scripts (live draft room)

`scripts/live_draft.py`, `autodraft.py`, `draft_player.py` and `sync_from_room.py`
drive a real browser because ESPN has no live-draft API. Two things to know
before using them:

- **`--attach` uses Chrome's remote debugging port.** Starting your browser
  with `--remote-debugging-port=9222` lets *any* local process read every tab,
  cookie and session in that browser, not just ESPN. Do it on a machine you
  control, close that browser instance when the draft ends, and never expose
  the port beyond `127.0.0.1`.
- **`state/` holds session material.** `state/browser-profile/` is a full
  logged-in browser profile (cookies, saved logins) and `state/discover.log`
  captures raw draft-room traffic including every manager's member GUID.
  Both are gitignored. Do not copy or share them.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

83 tests, no network or credentials required — the ESPN client is stubbed with
real-shaped payloads, so the value math, snake pick ordering, board assembly,
lineup solving, trade/waiver arithmetic and tool wiring are all verified
offline.

## Layout

```
src/espn_mcp/
  config.py      env loading
  espn.py        HTTP client, auth, error messages
  constants.py   ESPN's position/slot/team id maps
  scoring.py     league shape parsing, league-scored projections
  value.py       replacement level, VORP, tiers  (pure, unit tested)
  season.py      rest-of-season, optimal lineups, trade/waiver deltas  (pure, unit tested)
  board.py       caching layer, draft state, snake pick math, in-season rosters/matchups
  server.py      MCP tool definitions
scripts/
  doctor.py      pre-draft credential and access check
  snapshot.py    offline fallback board
  feed_sync.py   bridge to the Omarchy Fantasy Feed plugin (matchup + scoring)
```
