# espn-mcp

An MCP server that exposes your ESPN fantasy football league so an AI assistant
can help you draft — live, during a snake draft, on the clock.

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

The player pool is cached for `ESPN_POOL_TTL` seconds (default 15 min) because
it is slow and changes slowly. Draft picks are never cached.

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

61 tests, no network or credentials required — the ESPN client is stubbed with
real-shaped payloads, so the value math, snake pick ordering, board assembly and
tool wiring are all verified offline.

## Layout

```
src/espn_mcp/
  config.py      env loading
  espn.py        HTTP client, auth, error messages
  constants.py   ESPN's position/slot/team id maps
  scoring.py     league shape parsing, league-scored projections
  value.py       replacement level, VORP, tiers  (pure, unit tested)
  board.py       caching layer, draft state, snake pick math
  server.py      MCP tool definitions
scripts/
  doctor.py      pre-draft credential and access check
  snapshot.py    offline fallback board
```
