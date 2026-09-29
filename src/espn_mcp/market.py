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


# --- Is a trade worth offering at all? ---------------------------------------
#
# "Raises my lineup and he would say yes" is not enough: a trade he says yes
# to because it is a steal for him is a bad trade for me. On 2026-09-29 the
# old test passed Christian Watson (market 3653) for Michael Wilson (1362):
# +0.25 a game for me, -0.82 this week, 63% of the value handed over.

MIN_TRADE_GAIN = 0.5    # my starters, rest-of-season points per game
MAX_OVERPAY = 0.25      # share of the larger side's market value I may give away
MAX_WEEK_COST = 0.25    # points this week; less than this is projection noise
MAX_OUTLOOK_LOSS = 1.0  # points a game by workload (usage outlook), net


def worth_offering(my_gain: float, my_week_change: float, view: dict | None,
                   usage: dict | None = None) -> dict:
    """Would I make this trade, whatever the other side thinks?

    Returns {"ok": bool, "reasons": [...]}: every rule it breaks, so a
    report can say exactly why it was passed over.
    """
    reasons = []
    if my_gain < MIN_TRADE_GAIN:
        reasons.append(f"my starters gain {my_gain:+.2f} a game; the bar is "
                       f"+{MIN_TRADE_GAIN}")
    if my_week_change < -MAX_WEEK_COST:
        reasons.append(f"costs {my_week_change:+.2f} this week")
    if view is not None and view["their_market_gain_pct"] / 100 > MAX_OVERPAY:
        reasons.append(f"gives away {view['their_market_gain_pct']}% of the market value "
                       f"({view['value_given']} for {view['value_received']}); the cap "
                       f"is {round(MAX_OVERPAY * 100)}%")
    if usage and usage.get("outlook_change") is not None \
            and usage["outlook_change"] < -MAX_OUTLOOK_LOSS:
        reasons.append(f"workload outlook {usage['outlook_change']:+.2f} a game: "
                       "the player going out is the better bet")
    return {"ok": not reasons, "reasons": reasons}
