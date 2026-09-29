"""The approval service: the other end of the buttons in a notification.

A small HTTP server the manager's phone talks to. Approving a proposal
replays its stored tool call against ESPN; rejecting it closes it. Every
request must carry the token issued for that one proposal.

It listens on loopback only. Expose it through something private (a tailnet
reverse proxy), never directly.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .config import load_config
from .notify import push
from .proposals import ProposalError, ProposalStore, public

log = logging.getLogger("espn_mcp.approve")


def _store() -> ProposalStore:
    from .server import proposal_store
    return proposal_store()


async def _token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if request.method == "POST":
        form = await request.form()
        if form.get("t"):
            return str(form["t"])
    return request.query_params.get("t")


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _when(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%a %b %d %I:%M %p %Z")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dodi proposal</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 16px;
       max-width: 640px; margin-inline: auto; }}
h1 {{ font-size: 1.25rem; margin: 0 0 8px; }}
.status {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
          border: 1px solid currentColor; font-size: .85rem; }}
.meta {{ opacity: .7; font-size: .9rem; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px;
      border: 1px solid #8884; border-radius: 8px; font-size: .85rem; }}
form {{ display: flex; gap: 12px; margin: 20px 0; }}
button {{ flex: 1; font: inherit; font-weight: 600; padding: 14px; border-radius: 10px;
         border: 1px solid #8886; cursor: pointer; }}
button.yes {{ background: #1a7f37; color: #fff; border-color: #1a7f37; }}
</style></head><body>
<h1>{title}</h1>
<p><span class="status">{status}</span> <span class="meta">{meta}</span></p>
<p>{summary}</p>
<p>{reasoning}</p>
{buttons}
{result}
</body></html>"""

BUTTONS = """<form method="post">
<input type="hidden" name="t" value="{token}">
<button class="yes" formaction="{id}/approve">Approve</button>
<button formaction="{id}/reject">Reject</button>
</form>"""


def _page(p: dict, message: str | None = None) -> HTMLResponse:
    e = html.escape
    pending = p["status"] == "pending"
    meta = (f"expires {_when(p['expires_at'])}" if pending
            else f"decided {_when(p['decided_at'])}")
    result = ""
    if message:
        result += f"<p><strong>{e(message)}</strong></p>"
    if p.get("result"):
        import json
        result += f"<pre>{e(json.dumps(p['result'], indent=1, default=str)[:4000])}</pre>"
    return HTMLResponse(PAGE.format(
        title=e(p["title"]), status=e(p["status"]), meta=e(meta),
        summary=e(p["summary"]), reasoning=e(p["reasoning"]).replace("\n", "<br>"),
        buttons=BUTTONS.format(token=e(p["token"], quote=True), id=e(p["id"], quote=True))
        if pending else "",
        result=result))


def _reply(request: Request, p: dict, message: str, status: int = 200) -> Response:
    if _wants_html(request):
        return _page(p, message)
    return JSONResponse({"status": p["status"], "message": message,
                         "proposal": public(p)}, status_code=status)


def _denied() -> Response:
    # One answer for a wrong id and a wrong token.
    return JSONResponse({"error": "Not found."}, status_code=404)


def apply_proposal(proposal: dict, *, auto: bool = False, why: str = "") -> dict:
    """Send an approved proposal to ESPN, record the outcome, and report it.

    `auto` marks a move Dodi made on his own under the auto-apply policy:
    the push then says so, with the rule that allowed it and his reasoning,
    since the manager never saw it beforehand.
    """
    from .server import board, execute_proposal
    try:
        result = execute_proposal(proposal)
    except Exception as exc:  # noqa: BLE001 - must always record an outcome
        result = {"error": f"{type(exc).__name__}: {exc}"}
    ok = bool(result.get("applied")) and "error" not in result
    done = _store().finish(proposal["id"], ok, _brief(result))
    log.info("proposal %s %s%s", proposal["id"], done["status"], " (auto)" if auto else "")
    if ok:
        note = result.get("note") or "Sent to ESPN."
        if auto:
            body = f"THE MOVE\n{proposal['summary']}\n\n{note}\nRule: {why}"
            if proposal.get("reasoning"):
                body += f"\n\nDODI'S VIEW\n{proposal['reasoning']}"
            push(board().cfg, "Dodi did it: "
                 + proposal['title'].removeprefix('Dodi: ').rstrip('?'),
                 body, tags=["robot"])
        else:
            push(board().cfg, "Dodi: done", f"{proposal['summary']}\n\n{note}",
                 tags=["white_check_mark"])
    else:
        push(board().cfg, "Dodi: move failed",
             f"{proposal['summary']}\n\n{_plain_error(result)}",
             priority=4, tags=["x"])
    return done


def _plain_error(result: dict[str, Any]) -> str:
    """The reason in a sentence: ESPN's own message, without its JSON around it."""
    import re
    error = str(result.get("error") or "ESPN did not apply it.")
    m = re.search(r'"messages":\["((?:[^"\\]|\\.)*)"', error)
    return f"ESPN: {m.group(1)}" if m else error[:300]


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    keep = ("applied", "error", "note", "espn_status", "transaction_id", "week",
            "transaction", "action", "hint")
    return {k: result[k] for k in keep if k in result}


async def details(request: Request) -> Response:
    p = _store().authorized(request.path_params["pid"], await _token(request))
    if p is None:
        return _denied()
    _store().expire_due()
    p = _store().get(p["id"]) or p
    if _wants_html(request):
        return _page(p)
    return JSONResponse({"proposal": public(p)})


async def _decide(request: Request, decision: str) -> Response:
    store = _store()
    p = store.authorized(request.path_params["pid"], await _token(request))
    if p is None:
        return _denied()
    try:
        p = store.decide(p["id"], decision)
    except ProposalError as exc:
        return _reply(request, store.get(p["id"]) or p, str(exc), status=409)
    log.info("proposal %s %s", p["id"], decision)
    if decision == "rejected":
        return _reply(request, p, "Rejected. Nothing was sent to ESPN.")
    # ESPN can take several seconds; answer the phone now, apply behind it.
    resp = _reply(request, p, "Approved. Sending to ESPN; the result will be pushed.",
                  status=202)
    resp.background = BackgroundTask(anyio.to_thread.run_sync, apply_proposal, p)
    return resp


async def approve(request: Request) -> Response:
    return await _decide(request, "approved")


async def reject(request: Request) -> Response:
    return await _decide(request, "rejected")


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "time": int(time.time())})


def recover_interrupted() -> int:
    """Close proposals left mid-apply by a crash. Never retried automatically:
    ESPN may or may not have taken the move, and sending it twice is worse."""
    n = 0
    for p in _store().list("approved", limit=100):
        _store().finish(p["id"], False, {
            "error": "The service stopped while applying this. Check ESPN, then re-queue."})
        n += 1
    return n


app = Starlette(routes=[
    Route("/healthz", health),
    Route("/p/{pid}", details, methods=["GET"]),
    Route("/p/{pid}/approve", approve, methods=["POST"]),
    Route("/p/{pid}/reject", reject, methods=["POST"]),
])


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs; keep them out of the journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    if (n := recover_interrupted()):
        log.warning("closed %d proposal(s) interrupted mid-apply", n)
    uvicorn.run(app, host="127.0.0.1", port=cfg.approve_port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
