"""The ESPN client must never send the login cookies anywhere but ESPN."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import traceback

import httpx
import pytest

from espn_mcp.config import Config  # noqa: E402
from espn_mcp.espn import BASE, ESPNClient, ESPNError, host_allowed  # noqa: E402

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


def test_host_allowlist():
    assert host_allowed(f"{BASE}/seasons/2026")
    assert host_allowed("https://fantasy.espn.com/x")
    assert host_allowed("https://espn.com/")
    assert not host_allowed("http://lm-api-reads.fantasy.espn.com/x")  # not https
    assert not host_allowed("https://example.com/")
    assert not host_allowed("https://espn.com.evil.example/")
    assert not host_allowed("https://notespn.com/")


def test_client_refuses_non_espn_urls_before_sending():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    c = ESPNClient(CFG)
    c._client = httpx.Client(transport=httpx.MockTransport(handler), cookies=c._client.cookies)
    try:
        with pytest.raises(ESPNError, match="non-ESPN"):
            c._get("https://example.com/leagues/1", views=["mSettings"])
        assert calls == []
        c._get(f"{BASE}/seasons/2026", views=["mSettings"])
        assert len(calls) == 1
    finally:
        c.close()


def test_error_messages_never_contain_the_cookies():
    def failing(request):
        # A hostile or chatty transport that echoes what it was sent.
        cookie = request.headers.get("cookie", "")
        raise httpx.ConnectError(f"boom while sending {cookie}", request=request)

    def rejecting(request):
        cookie = request.headers.get("cookie", "")
        return httpx.Response(500, text=f"server saw {cookie}")

    for transport in (failing, rejecting):
        c = ESPNClient(CFG)
        c._client = httpx.Client(transport=httpx.MockTransport(transport), cookies=c._client.cookies)
        try:
            with pytest.raises(ESPNError) as info:
                c._get(f"{BASE}/seasons/2026", views=["mSettings"])
            text = str(info.value)
            assert "secret-s2" not in text
            assert "{00000000-0000-0000-0000-000000000000}" not in text
            assert "***" in text
        finally:
            c.close()


def test_cookies_are_secure_only():
    # A redirect from https to http inside espn.com must not carry the session.
    c = ESPNClient(CFG)
    try:
        assert cookie_header(c, f"{BASE}/seasons/2026")
        assert cookie_header(c, "http://lm-api-reads.fantasy.espn.com/x") is None
    finally:
        c.close()


def test_scrub_covers_truncated_bodies_and_the_exception_cause():
    secret = "secret-s2"

    def long_body(request):
        # The secret straddles the 300-character cut the message applies.
        return httpx.Response(500, text="x" * 295 + secret + "y" * 50)

    def failing(request):
        raise httpx.ConnectError(f"refused with {secret} in hand", request=request)

    for transport in (long_body, failing):
        c = ESPNClient(CFG)
        c._client = httpx.Client(transport=httpx.MockTransport(transport), cookies=c._client.cookies)
        try:
            with pytest.raises(ESPNError) as info:
                c._get(f"{BASE}/seasons/2026", views=["mSettings"])
            # Both the message and the chained cause, as a traceback prints them.
            lines = traceback.format_exception_only(info.value)
            if info.value.__cause__ is not None:
                lines += traceback.format_exception_only(info.value.__cause__)
            formatted = "".join(lines)
            assert secret not in formatted
            assert "***" in formatted
        finally:
            c.close()


class _Recorder(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"status": "EXECUTED", "id": "abc"})


def test_set_lineup_posts_to_the_writes_host_with_cookies():
    c = ESPNClient(CFG)
    rec = _Recorder()
    c._client._transport = rec
    try:
        out = c.set_lineup(12, 3, [{"player_id": 1, "from_slot_id": 21, "to_slot_id": 20}])
    finally:
        c.close()
    assert out["status"] == "EXECUTED"
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.host == "lm-api-writes.fantasy.espn.com"
    assert "espn_s2=secret-s2" in req.headers.get("cookie", "")
    import json
    body = json.loads(req.content)
    assert body["type"] == "ROSTER" and body["teamId"] == 12 and body["scoringPeriodId"] == 3
    assert body["memberId"] == CFG.swid
    assert body["items"] == [{"playerId": 1, "type": "LINEUP",
                              "fromLineupSlotId": 21, "toLineupSlotId": 20}]


def test_set_lineup_with_no_moves_does_not_call_espn():
    c = ESPNClient(CFG)
    rec = _Recorder()
    c._client._transport = rec
    try:
        assert c.set_lineup(12, 3, [])["status"] == "NOOP"
    finally:
        c.close()
    assert rec.requests == []


def test_writes_refused_without_auth():
    c = ESPNClient(Config(league_id="1", season=2026, team_id=None, espn_s2=None,
                          swid=None, pool_ttl=900, state_dir=None))
    try:
        with pytest.raises(ESPNError, match="ESPN_S2 and SWID"):
            c.set_lineup(12, 3, [{"player_id": 1, "from_slot_id": 21, "to_slot_id": 20}])
    finally:
        c.close()


def test_unexecuted_transaction_is_an_error():
    class Refuse(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"status": "INVALID"})

    c = ESPNClient(CFG)
    c._client._transport = Refuse()
    try:
        with pytest.raises(ESPNError, match="INVALID"):
            c.set_lineup(12, 3, [{"player_id": 1, "from_slot_id": 21, "to_slot_id": 20}])
    finally:
        c.close()
