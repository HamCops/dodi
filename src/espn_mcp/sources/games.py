"""This week's NFL games: the betting line, the venue, and the forecast.

The line comes from ESPN's public scoreboard, which carries one sportsbook's
spread and total for every game. A team's implied total is what the market
expects it to score: half the game total, shifted by half the spread.

The forecast comes from Open-Meteo for the venue's city at the hour of
kickoff. Indoor games have no weather.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

Fetch = Callable[..., Any]


def week_games(fetch: Fetch, season: int, week: int) -> dict[str, dict]:
    """Team abbreviation -> its game this week."""
    data = fetch(SCOREBOARD, params=[("seasontype", 2), ("week", int(week)),
                                     ("dates", int(season))])
    out: dict[str, dict] = {}
    for event in (data or {}).get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation")
                 for c in comp.get("competitors") or []}
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        venue = comp.get("venue") or {}
        address = venue.get("address") or {}
        game = {
            "kickoff": event.get("date"),
            "indoor": bool(venue.get("indoor")),
            "city": address.get("city"),
            "state": address.get("state"),
            "country": address.get("country"),
        }
        line = _line(comp.get("odds") or [], home, away)
        for team, opp, is_home in ((home, away, True), (away, home, False)):
            rec = {**game, "opponent": opp, "home": is_home}
            if line:
                margin = line["home_margin"] if is_home else -line["home_margin"]
                rec["over_under"] = line["total"]
                rec["favored_by"] = round(margin, 1)
                rec["implied_total"] = round((line["total"] + margin) / 2, 2)
            out[team] = rec
    return out


def _line(odds: list[dict], home: str, away: str) -> dict | None:
    """The game total and the margin the home team is expected to win by."""
    for o in odds:
        total, spread = o.get("overUnder"), o.get("spread")
        if total is None or spread is None:
            continue
        size = abs(float(spread))
        # `spread` has no fixed sign across ESPN's feeds; the favorite flag does.
        if (o.get("homeTeamOdds") or {}).get("favorite"):
            margin = size
        elif (o.get("awayTeamOdds") or {}).get("favorite"):
            margin = -size
        else:
            margin = 0.0 if size == 0 else None
        if margin is None:
            continue
        return {"total": float(total), "home_margin": margin}
    return None


def locate(fetch: Fetch, city: str, state: str | None, country: str | None) -> dict | None:
    """Coordinates of a venue's city."""
    rows = (fetch(GEOCODE, params=[("name", city), ("count", 10)]) or {}).get("results") or []
    want_us = (country or "USA").upper() in ("USA", "US", "UNITED STATES")
    for r in rows:
        if want_us and r.get("country_code") != "US":
            continue
        if not want_us and r.get("country_code") == "US":
            continue
        if want_us and state and _STATES.get(state.upper()) not in (None, r.get("admin1")):
            continue
        return {"lat": r["latitude"], "lon": r["longitude"]}
    return None


def forecast(fetch: Fetch, lat: float, lon: float, kickoff: str) -> dict | None:
    """Wind, rain and temperature at the hour of kickoff."""
    when = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(timezone.utc)
    day = when.strftime("%Y-%m-%d")
    data = fetch(FORECAST, params=[
        ("latitude", lat), ("longitude", lon), ("timezone", "UTC"),
        ("start_date", day), ("end_date", day),
        ("hourly", "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m"),
        ("wind_speed_unit", "mph"), ("temperature_unit", "fahrenheit")])
    hourly = (data or {}).get("hourly") or {}
    try:
        i = hourly["time"].index(when.strftime("%Y-%m-%dT%H:00"))
    except (KeyError, ValueError):
        return None

    def at(key):
        values = hourly.get(key) or []
        return values[i] if i < len(values) else None

    out = {"wind_mph": at("wind_speed_10m"), "gusts_mph": at("wind_gusts_10m"),
           "rain_pct": at("precipitation_probability"), "temp_f": at("temperature_2m")}
    return {k: round(v) for k, v in out.items() if v is not None} or None


_STATES = {
    "AZ": "Arizona", "CA": "California", "CO": "Colorado", "FL": "Florida",
    "GA": "Georgia", "IL": "Illinois", "IN": "Indiana", "LA": "Louisiana",
    "MA": "Massachusetts", "MD": "Maryland", "MI": "Michigan", "MN": "Minnesota",
    "MO": "Missouri", "NC": "North Carolina", "NJ": "New Jersey", "NV": "Nevada",
    "NY": "New York", "OH": "Ohio", "PA": "Pennsylvania", "TN": "Tennessee",
    "TX": "Texas", "WA": "Washington", "WI": "Wisconsin",
}
