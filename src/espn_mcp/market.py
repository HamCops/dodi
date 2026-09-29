"""Trade arithmetic on market values: what the other manager thinks he is getting.

A trade is accepted by someone looking at names and reputations, not at this
server's projections. Market value stands in for that view. The trades worth
offering are the ones that raise my lineup by projection while the other side
comes out ahead by market value.
"""

from __future__ import annotations

# Within this share of the larger side, a trade reads as even.
EVEN_WITHIN = 0.10


def total(players: list[dict]) -> int:
    return sum(int(p.get("market_value") or 0) for p in players)


def trade_view(give: list[dict], receive: list[dict]) -> dict | None:
    """Both sides of a trade in market value, from my point of view.

    None when the market prices nobody involved. Players it does not price
    count as zero, and are named, so a total that leans on them can be
    discounted.
    """
    priced = [p for p in give + receive if p.get("market_value") is not None]
    if not priced:
        return None
    given, received = total(give), total(receive)
    their_gain = given - received
    share = their_gain / max(given, received, 1)
    if share >= EVEN_WITHIN:
        verdict = "they win on market value: the kind of offer that gets accepted"
    elif share <= -EVEN_WITHIN:
        verdict = "I win on market value: expect a decline unless it fixes a need of theirs"
    else:
        verdict = "even on market value"
    out = {
        "value_given": given,
        "value_received": received,
        "their_market_gain": their_gain,
        "their_market_gain_pct": round(share * 100),
        "verdict": verdict,
    }
    unpriced = [p["name"] for p in give + receive if p.get("market_value") is None]
    if unpriced:
        out["unpriced"] = unpriced
    if len(give) != len(receive):
        out["note"] = ("Uneven player counts: the side getting the single best player "
                       "usually has to overpay in total value.")
    return out


def acceptable(their_lineup_change: float, view: dict | None,
               max_lineup_loss: float = 0.25) -> bool:
    """Would the other manager plausibly say yes?

    Yes if he wins on market value without his lineup getting clearly worse,
    or if his lineup improves and he is not giving up much market value.
    """
    if view is None:
        return their_lineup_change > 0
    share = view["their_market_gain_pct"] / 100
    if share >= EVEN_WITHIN:
        return their_lineup_change >= -max_lineup_loss
    if share > -EVEN_WITHIN:
        return their_lineup_change >= 0
    return False
