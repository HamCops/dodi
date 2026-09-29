"""The one place outside hosts are called from."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

ALLOWED_HOSTS = frozenset({"api.sleeper.app", "api.fantasycalc.com", "site.api.espn.com",
                           "api.open-meteo.com", "geocoding-api.open-meteo.com"})

USER_AGENT = "espn-mcp (+https://github.com/HamCops/espn-mcp)"


class SourceError(RuntimeError):
    """A source could not be read; the message says which and why."""


# Files on GitHub releases are served from a storage host after a redirect.
TEXT_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                        "objects.githubusercontent.com"})


def fetch_text(url: str, timeout: float = 60.0) -> str:
    """GET a text file, following redirects only between allowed hosts."""
    for _ in range(5):
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "") not in TEXT_HOSTS:
            raise SourceError(f"{parts.hostname or url!r} is not an allowed data source.")
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=False,
                             headers={"User-Agent": USER_AGENT})
            if resp.is_redirect:
                url = str(resp.next_request.url)
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            raise SourceError(f"{parts.hostname}: {type(exc).__name__}: {exc}") from exc
    raise SourceError("Too many redirects.")


def fetch_json(url: str, params: list[tuple[str, Any]] | None = None,
               timeout: float = 20.0) -> Any:
    """GET a JSON document from an allowed host. No credentials are ever sent."""
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise SourceError(f"{host or url!r} is not an allowed data source.")
    try:
        resp = httpx.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"{host}: {type(exc).__name__}: {exc}") from exc
