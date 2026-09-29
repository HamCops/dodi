"""The approval queue: roster moves that wait on the manager's yes.

A proposal is a writing tool call (an add, a drop, a trade offer, an answer
to one) stored with its arguments instead of being sent. Approving it replays
the call with apply=true. Kept free of any network or ESPN access so the
state machine can be unit tested: pending -> approved -> applied | failed,
or pending -> rejected | expired.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

# The writing tools a proposal may replay, and the arguments each accepts.
# Anything else is refused at the door: the queue must not become a way to
# call arbitrary functions with stored arguments.
ACTIONS: dict[str, tuple[str, ...]] = {
    "add_player": ("add", "drop"),
    "drop_player": ("player",),
    "propose_trade": ("give", "receive", "partner_team_id"),
    "respond_to_trade": ("trade_id", "action"),
    "start_player": ("player", "over"),
}

DEFAULT_TTL_HOURS = {
    "add_player": 24.0,
    "drop_player": 24.0,
    "propose_trade": 48.0,
    "respond_to_trade": 24.0,
    "start_player": 72.0,
}

# A move the manager turned down is not offered again for this long.
REJECTED_COOLDOWN_HOURS = 72.0

OPEN = ("pending", "approved")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    action      TEXT NOT NULL,
    params      TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    reasoning   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    decided_at  REAL,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals (status);
"""


class ProposalError(ValueError):
    """The request cannot be queued or decided; the message says why."""


def clean_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Only the arguments the action takes, with empty ones dropped."""
    if action not in ACTIONS:
        raise ProposalError(
            f"Unknown action {action!r}. One of: {', '.join(sorted(ACTIONS))}.")
    unknown = sorted(set(params) - set(ACTIONS[action]))
    if unknown:
        raise ProposalError(
            f"{action} does not take {', '.join(unknown)}. "
            f"It takes: {', '.join(ACTIONS[action])}.")
    return {k: params[k] for k in ACTIONS[action] if params.get(k) not in (None, "", [])}


def _fingerprint(action: str, params: dict[str, Any]) -> str:
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        if isinstance(v, (list, tuple)):
            return sorted(norm(x) for x in v)
        return v
    return json.dumps([action, {k: norm(v) for k, v in params.items()}], sort_keys=True)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    out["params"] = json.loads(out["params"])
    out["result"] = json.loads(out["result"]) if out["result"] else None
    return out


def public(p: dict) -> dict:
    """A proposal as it may be shown to the model or logged: never the token."""
    return {k: v for k, v in p.items() if k != "token"}


class ProposalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
        self.path.chmod(0o600)  # holds the approval tokens

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def expire_due(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._db() as db:
            cur = db.execute(
                "UPDATE proposals SET status='expired', decided_at=? "
                "WHERE status='pending' AND expires_at<=?", (now, now))
            return cur.rowcount

    def find_blocking(self, action: str, params: dict[str, Any],
                      now: float | None = None) -> dict | None:
        """The same move already waiting, or turned down recently."""
        now = time.time() if now is None else now
        self.expire_due(now)
        want = _fingerprint(action, params)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM proposals WHERE action=? AND "
                "(status IN ('pending','approved') OR (status='rejected' AND decided_at>?)) "
                "ORDER BY created_at DESC",
                (action, now - REJECTED_COOLDOWN_HOURS * 3600)).fetchall()
        for r in rows:
            p = _row(r)
            if _fingerprint(action, p["params"]) == want:
                return p
        return None

    def create(self, action: str, params: dict[str, Any], *, title: str, summary: str,
               reasoning: str, ttl_hours: float | None = None,
               expires_at: float | None = None, now: float | None = None) -> dict:
        params = clean_params(action, params)
        now = time.time() if now is None else now
        if expires_at is None:
            expires_at = now + (ttl_hours or DEFAULT_TTL_HOURS[action]) * 3600
        if expires_at <= now:
            raise ProposalError("The window for this move has already closed.")
        pid = secrets.token_hex(4)
        with self._db() as db:
            db.execute(
                "INSERT INTO proposals (id, token, action, params, title, summary, "
                "reasoning, status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, secrets.token_urlsafe(32), action, json.dumps(params), title,
                 summary, reasoning, "pending", now, expires_at))
        return self.get(pid)  # type: ignore[return-value]

    def get(self, pid: str) -> dict | None:
        with self._db() as db:
            return _row(db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())

    def list(self, status: str | None = None, limit: int = 20,
             now: float | None = None) -> list[dict]:
        self.expire_due(now)
        q, args = "SELECT * FROM proposals", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._db() as db:
            return [_row(r) for r in db.execute(q, args).fetchall()]  # type: ignore[misc]

    def authorized(self, pid: str, token: str | None) -> dict | None:
        """The proposal, if the token is the one issued for it."""
        # As bytes: compare_digest refuses text that is not ASCII, and what
        # arrives here is whatever a client chose to send.
        given = (token or "").encode("utf-8", "replace")
        p = self.get(pid)
        if p is None or not token:
            # Compare anyway so a wrong id and a wrong token cost the same.
            hmac.compare_digest(given, secrets.token_urlsafe(32).encode())
            return None
        return p if hmac.compare_digest(given, p["token"].encode()) else None

    def decide(self, pid: str, decision: str, now: float | None = None) -> dict:
        """Move a pending proposal to approved or rejected, exactly once.

        The update is conditional on the row still being pending, so two taps
        on the button (or a retry from the phone) cannot apply a move twice.
        """
        if decision not in ("approved", "rejected"):
            raise ProposalError("decision must be approved or rejected.")
        now = time.time() if now is None else now
        self.expire_due(now)
        with self._db() as db:
            cur = db.execute(
                "UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status='pending'",
                (decision, now, pid))
            changed = cur.rowcount == 1
        p = self.get(pid)
        if p is None:
            raise ProposalError("No such proposal.")
        if not changed:
            raise ProposalError(f"Already {p['status']}; nothing to do.")
        return p

    def finish(self, pid: str, ok: bool, result: dict[str, Any]) -> dict:
        with self._db() as db:
            db.execute(
                "UPDATE proposals SET status=?, result=? WHERE id=? AND status='approved'",
                ("applied" if ok else "failed", json.dumps(result, default=str), pid))
        return self.get(pid)  # type: ignore[return-value]
