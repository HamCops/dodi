"""The ESPN client must never send the login cookies anywhere but ESPN."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.config import Config  # noqa: E402
from espn_mcp.espn import BASE, ESPNClient  # noqa: E402

CFG = Config(
    league_id="1",
    season=2026,
    team_id=None,
    espn_s2="secret-s2",
    swid="{00000000-0000-0000-0000-000000000000}",
    pool_ttl=900,
    state_dir=None,
)


def cookie_header(client: ESPNClient, url: str) -> str | None:
    return client._client.build_request("GET", url).headers.get("cookie")


def test_cookies_sent_to_espn_hosts():
    c = ESPNClient(CFG)
    try:
        for url in (f"{BASE}/seasons/2026", "https://fantasy.espn.com/football/"):
            header = cookie_header(c, url) or ""
            assert "espn_s2=secret-s2" in header
            assert "SWID=" in header
    finally:
        c.close()


def test_cookies_never_leave_espn():
    c = ESPNClient(CFG)
    try:
        for url in ("https://example.com/",
                    "https://espn.com.evil.example/",
                    "https://notespn.com/"):
            assert cookie_header(c, url) is None, url
    finally:
        c.close()


def test_no_cookies_without_auth():
    c = ESPNClient(Config(league_id="1", season=2026, team_id=None, espn_s2=None,
                          swid=None, pool_ttl=900, state_dir=None))
    try:
        assert cookie_header(c, f"{BASE}/seasons/2026") is None
    finally:
        c.close()
