"""Set the best lineup without a model in the loop.

For the runs that matter most and need no judgement: after inactives are
announced, shortly before each kickoff window. Prints a short report when it
changed something, and nothing when the lineup was already right, so a
scheduler that stays silent on empty output stays silent.
"""

from __future__ import annotations

import logging
import sys

from .notify import push


def run() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .server import board, set_lineup

    preview = set_lineup(apply=False)
    if "error" in preview:
        print(f"Dodi lineup check failed: {preview['error']}")
        return 1
    _record(board())
    if not preview["moves"]:
        _close_calls(preview)
        return 0
    result = set_lineup(apply=True)
    if "error" in result or not result.get("applied"):
        msg = result.get("error", "ESPN did not apply the lineup.")
        print(f"Dodi lineup change failed: {msg}")
        push(board().cfg, "Dodi: lineup change failed", msg, priority=4, tags=["x"])
        return 1
    lines = [f"{m['player']}: {m['from']} -> {m['to']}" for m in result["moves"]]
    lines.append(f"Projected {result['set_total']} -> {result['optimal_total']} "
                 f"({result['gain']:+})")
    if result.get("questionable"):
        lines.append("Questionable starters: " + "; ".join(result["questionable"]))
    report = "\n".join(lines)
    print(f"DODI LINEUP — Week {result['week']}\n{report}")
    push(board().cfg, f"Dodi: lineup set, week {result['week']}", report,
         tags=["football"])
    _close_calls(result)
    return 0


def _close_calls(lineup: dict) -> None:
    """Send any start/sit call too close for the projection to the manager."""
    from .server import board, queue_close_calls
    if not board().cfg.require_approval:
        return
    for p in queue_close_calls(calls=lineup.get("close_calls") or []):
        print(f"Asked: {p['title']}")


def _record(b) -> None:
    """Log what every source said before kickoff, to score them afterwards."""
    from .tracking import record_snapshot
    try:
        record_snapshot(b)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop the lineup
        print(f"(snapshot not recorded: {exc})", file=sys.stderr)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
