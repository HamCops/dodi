"""Push notifications through ntfy, with the approve and reject buttons.

Notifications are a convenience, never a dependency: a failed push is
reported to the caller as data and must not stop the move it describes.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config


def approval_actions(cfg: Config, proposal: dict) -> list[dict[str, Any]]:
    """The buttons under a proposal: approve, reject, and the details page.

    Approve and reject are POSTs the phone makes itself, carrying the
    proposal's own token. The token only unlocks this one proposal, and the
    approval service is meant to be reachable from the tailnet alone, so a
    notification read by someone else does not let them act on it.
    """
    if not cfg.approve_base_url:
        return []
    base = f"{cfg.approve_base_url.rstrip('/')}/p/{proposal['id']}"
    auth = {"Authorization": f"Bearer {proposal['token']}"}
    return [
        {"action": "http", "label": "Approve", "url": f"{base}/approve",
         "method": "POST", "headers": auth, "clear": True},
        {"action": "http", "label": "Reject", "url": f"{base}/reject",
         "method": "POST", "headers": auth, "clear": True},
        {"action": "view", "label": "Details", "url": f"{base}?t={proposal['token']}"},
    ]


def push(cfg: Config, title: str, message: str, *, actions: list[dict] | None = None,
         priority: int = 3, tags: list[str] | None = None) -> dict[str, Any]:
    if not (cfg.ntfy_url and cfg.ntfy_topic):
        return {"sent": False, "reason": "ntfy is not configured"}
    body: dict[str, Any] = {
        "topic": cfg.ntfy_topic,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if tags:
        body["tags"] = tags
    if actions:
        body["actions"] = actions
    headers = {"Authorization": f"Bearer {cfg.ntfy_token}"} if cfg.ntfy_token else {}
    try:
        resp = httpx.post(cfg.ntfy_url.rstrip("/") + "/", json=body, headers=headers,
                          timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"sent": True}


def deadline(cfg: Config, proposal: dict, now: float | None = None) -> dict[str, Any]:
    """When a proposal closes, in the manager's time, and whether that is
    less notice than he asked for."""
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = time.time() if now is None else now
    minutes = int((proposal["expires_at"] - now) // 60)
    when = datetime.fromtimestamp(proposal["expires_at"], ZoneInfo(cfg.timezone))
    same_day = when.date() == datetime.fromtimestamp(now, ZoneInfo(cfg.timezone)).date()
    text = when.strftime("%-I:%M %p" if same_day else "%a %-I:%M %p")
    if minutes < 120:
        text += f" ({minutes} min)"
    return {"text": text, "minutes": minutes,
            "short_notice": minutes < cfg.approval_lead_minutes}


def push_proposal(cfg: Config, proposal: dict) -> dict[str, Any]:
    actions = approval_actions(cfg, proposal)
    by = deadline(cfg, proposal)
    # The summary is built in code from ESPN's data. The reasoning is the
    # agent's own words. They are labelled so the two are never confused.
    message = f"Decide by {by['text']}.\n\nTHE MOVE\n" + proposal["summary"]
    if proposal.get("reasoning"):
        message += "\n\nDODI'S VIEW\n" + proposal["reasoning"]
    if not actions:
        message += f"\n\n(No approval link configured; proposal {proposal['id']}.)"
    title = proposal["title"]
    if by["short_notice"]:
        # Less time than he asked for. Still worth sending: he may be there.
        title = f"{by['minutes']} MIN: " + title
    return push(cfg, title, message, actions=actions,
                priority=5 if by["short_notice"] else 4,
                tags=["rotating_light" if by["short_notice"] else "football"])
