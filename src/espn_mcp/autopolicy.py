"""Which queued moves Dodi may make on his own, and which still wait on a yes.

With ESPN_AUTO_APPLY on, a move that passes this policy is applied the moment
it is requested and the manager is told afterwards. Anything else is queued
for approval exactly as before. Pure functions over a move's preview, so the
rules can be tested without ESPN.

The line is drawn where a mistake is cheap to undo:
  - adds that make the lineup better, and D/ST or K swapped like for like
  - trade offers that raise my starters and the other side should accept
    (an offer is only an offer; he still has to say yes)
  - declining an offer made to me, withdrawing one I sent
and where it is not:
  - accepting a trade (players leave for good)
  - a bare drop (nobody replaces him)
  - an add that cuts a QB, RB, WR or TE for less than he gives
  - close start/sit calls (a coin flip; the manager's preference)
"""

from __future__ import annotations

STREAM_POSITIONS = ("D/ST", "K")


def auto_ok(action: str, params: dict, preview: dict) -> tuple[bool, str]:
    """(may apply without asking, why) for a previewed move."""
    rule = _RULES.get(action)
    if rule is None:
        return False, f"{action} always waits on the manager."
    return rule(params, preview)


def _add(params: dict, preview: dict) -> tuple[bool, str]:
    delta = preview.get("delta") or {}
    ros = delta.get("starters_ros_per_game") or 0
    week = delta.get("starters_this_week") or 0
    pos = (preview.get("add") or {}).get("pos")
    drops = preview.get("drop") or []
    if pos in STREAM_POSITIONS and drops and all(d.get("pos") == pos for d in drops):
        if week > 0:
            return True, f"{pos} swapped for {pos}, better this week."
        return False, f"{pos} swap does not help this week."
    if ros > 0:
        return True, "Raises the lineup rest of season."
    return False, "Does not raise the lineup rest of season."


def _trade(params: dict, preview: dict) -> tuple[bool, str]:
    mine = ((preview.get("me") or {}).get("delta") or {}).get("starters_ros_per_game") or 0
    if mine <= 0:
        return False, "Does not raise my starters."
    if preview.get("worth_offering") is not True:
        why = "; ".join(preview.get("not_worth_because") or []) or "not checked"
        return False, f"Not a trade I would make on my own: {why}."
    if preview.get("likely_accepted") is not True:
        return False, "Not likely to be accepted."
    if (preview.get("usage") or {}).get("warning"):
        return False, "Usage warning: buys a player running hot or sells one running cold."
    return True, "Clearly raises my starters, fair on value, and should be accepted."


def _respond(params: dict, preview: dict) -> tuple[bool, str]:
    action = str(params.get("action") or preview.get("action") or "").lower()
    if action in ("decline", "withdraw", "cancel"):
        return True, f"{action.capitalize()} costs nothing."
    return False, "Accepting a trade always waits on the manager."


_RULES = {
    "add_player": _add,
    "propose_trade": _trade,
    "respond_to_trade": _respond,
}
