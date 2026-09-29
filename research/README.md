# Does anything beat ESPN's weekly projection?

The lineup is set by ESPN's projection for the week. Before adding other
signals to that number, each was tested on two full seasons. Most did not
survive. This directory is the test, so it can be rerun and argued with.

## Method

- **Data**: ESPN's weekly projection for the 500 most-rostered players, every
  week of 2024 and 2025 (standard scoring, `leaguedefaults/1`), joined to what
  each player actually scored (nflverse), the closing betting line, the roof,
  wind and temperature (nflverse `games.csv`), and snaps, targets and carries
  from the weeks before.
- **Sample**: QB, RB, WR and TE projected for 4 or more points. 5,871
  player-weeks.
- **Out of sample**: every model is fit on one season and scored on the
  other, both ways round. A signal has to help in both.
- **What is scored**: not only error, but decisions. For every pair of
  same-position players in the same week projected within 3 points of each
  other, which one does the model start, and how many points does that
  choice gain. That is the start/sit question as it is actually faced.

## Results

ESPN alone picks the higher scorer in a close pair **57%** of the time. That
is the ceiling being worked against: weekly scoring is mostly noise.

| Signal | Fit 2024, score 2025 | Fit 2025, score 2024 | Verdict |
|---|---|---|---|
| ESPN alone | 57.47%, +1.365 pts | 56.91%, +1.418 pts | baseline |
| + betting line (implied team total) | 57.55%, +1.397 | 56.98%, +1.444 | helps both ways, slightly |
| + betting line and wind | 57.64%, +1.417 | 56.86%, +1.424 | wind helps one year, hurts the other |
| + snap, target, carry trends | 57.25%, +1.310 | | worse |
| + recent scoring vs projection | 57.27%, +1.282 | | worse |
| + everything | 57.63%, +1.376 | 56.42%, +1.363 | overfit |

Tiebreaks, tested on pairs projected within 1.5 points, starting the
lower-projected player when:

| Condition | Lower-projected player wins | Swing |
|---|---|---|
| (any pair) | 45.7% | -0.73 |
| his preseason rank is half the starter's or better | 48.6% | -0.29 |
| ...and projections are within 0.5 | 50.6% | +0.03 |
| his season average is 3+ points higher | 47.7% | -0.62 |
| his team's implied total is 4+ higher | 48.3% | -0.16 |

Players coming back:

| Group | n | Beat projection by |
|---|---|---|
| Everyone | 5,871 | -0.33 |
| Back after missing 2+ weeks | 135 | -0.70 ± 0.57 |
| Season debut in week 3 or later | 64 | -3.82 ± 0.48 |

## What was built from it

- **The betting line is in the lineup number**, as a small adjustment
  (`VEGAS_PER_POINT` in `sources/signals.py`). Worth about 0.03 points per
  close decision. Small, but it points the same way in both seasons and the
  mechanism is plain.
- **Wind, rain and temperature are shown, not used.** A person can weigh a
  30 mph gust; the average effect is not reliable enough to automate.
- **Usage trends and recent form are not used.** ESPN's projection already
  has them, and adding them again makes it worse.
- **Close calls are narrow (0.5 points) and go to the manager.** History says
  they are a coin flip, so they are a preference, not an edge.
- **No boost for returning players.** They underperform their projection.

## Limits

- Two seasons. The line's effect is small enough that a third could erase it.
- ESPN's stored projection for a past week is its last one before kickoff,
  with inactive players already zeroed. That flatters ESPN a little and is
  the same number the post-inactives lineup run sees.
- The 500 players are the most rostered as of when the data was pulled.
- Standard scoring only. PPR leagues should refit.
- Preseason rank stands in for "the better player". Live trade value, which
  the close-call rule uses, could not be tested: there is no history of it.
  `tracking.py` now records it before every kickoff so that it can be.

## Rerun

    ./fetch.sh
    cd data
    python -m venv .v && .v/bin/pip install numpy pandas
    .v/bin/python ../build.py        # joins everything into bt.pkl
    .v/bin/python ../signals.py      # each signal against ESPN's error
    .v/bin/python ../vegas.py        # the adjustment that was kept
    .v/bin/python ../tiebreak.py     # better-player tiebreaks
    .v/bin/python ../returning.py    # players back from injury

# Does workload predict the rest of the season?

The lineup question above is nearly closed: ESPN's weekly number is hard to
beat. The roster question is not. Which players to pick up and trade for
depends on what they will score from here on, and the obvious guide, points
so far, is mostly touchdowns and long plays.

## Method

- nflverse game logs, 2024 and 2025, RB, WR and TE.
- At each of weeks 3 to 10: everything known so far, against points per
  game over the rest of the season (players with 2+ games before and 4+
  after).
- **Expected points** are fit from volume alone: carries, targets and air
  yards. No yards gained, no touchdowns.
- Fit on one season, scored on the other, both ways.

## Results

Correlation with rest-of-season points per game:

| | Points so far | Last 3 games | Workload | Workload, last 3 |
|---|---|---|---|---|
| RB | 0.81 / 0.78 | 0.74 / 0.71 | 0.82 / 0.76 | 0.79 / 0.72 |
| WR | 0.68 / 0.73 | 0.63 / 0.62 | 0.75 / 0.71 | 0.70 / 0.61 |
| TE | 0.65 / 0.68 | 0.52 / 0.60 | 0.77 / 0.61 | 0.66 / 0.57 |
| QB | 0.35 / 0.59 | | 0.35 / 0.49 | |

Points and workload together beat points alone for RB, WR and TE in both
directions (error down 2% to 10%). Not for QB.

Players whose scoring and workload disagree by 3+ points a game (3+ games,
a real role):

| | n | So far | Afterwards | Change |
|---|---|---|---|---|
| Running hot, scored on 2025 | 75 | 14.7 | 10.5 | -4.2 |
| Running hot, scored on 2024 | 96 | 14.9 | 12.3 | -2.6 |
| Running cold, scored on 2025 | 66 | 6.6 | 9.0 | +2.4 |
| Running cold, scored on 2024 | 59 | 5.5 | 7.7 | +2.2 |

Trading a hot player for a cold one at the same position, within 1.5
points a game of each other so far: +3.9 a game afterwards (78% right, 45
pairs) and +1.8 (56%, 52 pairs).

The waiver wire (players under 7 a game), top tenth by each measure, points
per game afterwards:

| Picked by | 2025 | 2024 |
|---|---|---|
| Workload, season | 7.6 | 7.1 |
| Points so far | 7.1 | 6.7 |
| Targets | 6.9 | 6.8 |
| Snap share, last 3 | 6.6 | 6.2 |
| Points, last 3 | 6.2 | 6.0 |
| Jump in snap share | 3.6 | 3.5 |

## What was built from it

- `usage.py`: expected points and a rest-of-season outlook for every RB, WR
  and TE, in standard, half and full PPR.
- `sell_high` and `buy_low` in `find_trade_partners`; a `usage` block and a
  buying-high warning in `analyze_trade`; `workload_targets` in
  `get_waiver_targets`.

## What was not

- Recency. The last three games predict worse than the whole season, for
  points and for workload. No "hot hand" weighting.
- A jump in snap share. Chasing it found nothing.
- Anything for quarterbacks.

## Limits

- This beats points so far. Whether it beats ESPN's own rest-of-season
  projection is not known: ESPN keeps no history of those. It is shown next
  to ESPN's number, not in place of it, and `tracking.py` records both from
  here on.
- The pairs in the trade test overlap, so its 78% and 56% are less certain
  than they look. The direction agrees in both seasons; the size does not.
- Survivors only: a player had to play 4+ more games to be counted, so
  injuries are not in these numbers.

Rerun: `python ../usage.py` and `python ../usage_fit.py` from `data/`.

# Defenses and kickers

Same method, same two seasons: ESPN's weekly projection for every defense
and kicker against what they scored, with the betting line and the roof.

## Defenses

ESPN's projection moves with the matchup, and not far enough.

| Opponent expected to score | n | ESPN said | Scored |
|---|---|---|---|
| Under 17 | 89 | 7.2 | 10.1 |
| 17 to 20 | 196 | 6.4 | 8.5 |
| 20 to 23 | 304 | 5.5 | 5.8 |
| 23 to 26 | 293 | 4.4 | 4.0 |
| 26 or more | 142 | 3.3 | 2.3 |

Alongside ESPN's number, each point of opponent implied total was worth
-0.52 ± 0.12 (2024) and -0.49 ± 0.13 (2025). Adding it cut the error in both
directions and raised the score of the top three picks each week by 0.9
and 0.2 points, though with 17 weeks a season that gain is inside the
noise. Being favored, and playing at home, added nothing beyond it.

## Kickers

| | n | ESPN said | Scored |
|---|---|---|---|
| Outdoors | 637 | 7.9 | 7.9 |
| Indoors | 328 | 7.9 | 9.0 |

+1.06 ± 0.46 in 2024, +1.02 ± 0.45 in 2025. The kicker's own team total
added nothing (+0.03 ± 0.06). Wind of 15 mph or more pointed the right way
and was too rare to measure.

Kicker scoring is close to random: ESPN's projection correlates 0.19 with
the result. A point is a large share of what can be known.

## Built

`DEFENSE_PER_OPPONENT_POINT` and `KICKER_INDOORS` in `sources/signals.py`,
and `streaming` in `get_waiver_targets`.

Rerun: `python ../defense_kicker.py`, `python ../defense_kicker_stability.py`.
