#!/usr/bin/env python3
"""Dump the full value board to snapshots/ as an offline fallback.

ESPN's API is undocumented and can break or rate-limit without warning. Run
this shortly before the draft so a mid-draft outage leaves you with a usable
board instead of nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from espn_mcp.board import DraftBoard  # noqa: E402
from espn_mcp.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    b = DraftBoard(cfg)
    data = b.board(refresh=True)

    out_dir = Path(__file__).resolve().parents[1] / "snapshots"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"board-{cfg.league_id}-{stamp}.json"

    payload = {
        "generated_utc": stamp,
        "league": b.shape().describe(),
        "replacement_ranks": data["replacement_ranks"],
        "replacement_points": data["replacement_points"],
        "players": data["players"],
    }
    path.write_text(json.dumps(payload, indent=2))

    csv_path = path.with_suffix(".csv")
    rows = ["rank,name,pos,team,proj,vorp,tier,adp"]
    for p in data["players"]:
        rows.append(
            f"{p['overall_value_rank']},{p['name']},{p['position']},{p['pro_team']},"
            f"{p['projected_points']},{p['vorp']},{p.get('tier')},{p.get('espn_adp') or ''}"
        )
    csv_path.write_text("\n".join(rows))

    print(f"wrote {path} ({len(data['players'])} players)")
    print(f"wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
