#!/usr/bin/env bash
# Download two seasons of ESPN weekly projections and nflverse results into ./data.
# Public endpoints, no credentials. About 60 MB.
set -euo pipefail
cd "$(dirname "$0")"; mkdir -p data; cd data
for y in 2024 2025; do
  for w in $(seq 1 17); do
    f=espn_${y}_$w.json
    [ -s "$f" ] || curl -sf -m 60 -o "$f" \
      "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/$y/segments/0/leaguedefaults/1?view=kona_player_info&scoringPeriodId=$w" \
      -H "X-Fantasy-Filter: {\"players\":{\"limit\":500,\"sortPercOwned\":{\"sortPriority\":1,\"sortAsc\":false},\"filterStatsForTopScoringPeriodIds\":{\"value\":1,\"additionalValue\":[\"11$y$w\",\"01$y$w\"]}}}"
    sleep 0.3
  done
  for n in stats_player/stats_player_week_$y.csv snap_counts/snap_counts_$y.csv; do
    [ -s "$(basename $n)" ] || curl -sfL -m 120 -O "https://github.com/nflverse/nflverse-data/releases/download/$n"
  done
done
[ -s players.csv ] || curl -sfL -m 120 -O https://github.com/nflverse/nflverse-data/releases/download/players/players.csv
[ -s games.csv ] || curl -sfL -m 120 -O https://github.com/nflverse/nfldata/raw/master/data/games.csv
echo "done: run the scripts from inside data/ (python ../build.py, then the others)"
