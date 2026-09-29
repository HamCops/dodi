"""Checks what the model writes against what the tools returned.

The numbers behind a move are computed in code. The sentence explaining it
is written by a language model, and a model will now and then write a
number that is close to the right one, or belongs to another player. That
sentence is what the manager reads when he decides.

So a number in the explanation has to be a number from the preview of the
move. One that is not is sent back to be corrected or left out. Words are
not checked; only what can be.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBER = re.compile(r"(?<![\w.])([+-]?\d[\d,]*(?:\.\d+)?)([kKmM])?(?![\w])")
_SCALE = {"k": 1_000.0, "m": 1_000_000.0}


def _written(text: str) -> list[tuple[str, float, float]]:
    """Numbers in a text: (as written, value, how far off rounding allows)."""
    out = []
    for m in _NUMBER.finditer(text):
        raw, suffix = m.group(1).replace(",", ""), (m.group(2) or "").lower()
        try:
            value = abs(float(raw))
        except ValueError:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        scale = _SCALE.get(suffix, 1.0)
        out.append((m.group(0), value * scale, 0.5 * 10 ** -decimals * scale))
    return out


def _everyday(value: float, slack: float) -> bool:
    """Counts, dates and years: whole numbers that are not statistics."""
    whole = slack == 0.5 and float(value).is_integer()
    return whole and (value <= 31 or 2000 <= value <= 2100)


def known_numbers(data: Any) -> list[float]:
    """Every number anywhere in a tool result, including inside its text."""
    out: list[float] = []
    if isinstance(data, bool) or data is None:
        return out
    if isinstance(data, (int, float)):
        out.append(abs(float(data)))
    elif isinstance(data, str):
        out.extend(v for _, v, _ in _written(data))
    elif isinstance(data, dict):
        for v in data.values():
            out.extend(known_numbers(v))
    elif isinstance(data, (list, tuple)):
        for v in data:
            out.extend(known_numbers(v))
    return out


def unsupported_numbers(text: str, *sources: Any) -> list[str]:
    """Numbers in `text` that appear in none of the sources.

    A written number matches a known one if rounding the known one could
    have produced it: "8.4" matches 8.39, "17" matches 17.47, "3.99M"
    matches 3,994,120.
    """
    known = [v for s in sources for v in known_numbers(s)]
    missing = []
    for shown, value, slack in _written(text or ""):
        if _everyday(value, slack):
            continue
        if any(abs(k - value) <= slack + 1e-9 for k in known):
            continue
        if shown not in missing:
            missing.append(shown)
    return missing
