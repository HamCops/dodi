"""Data from outside ESPN: a second opinion on every player.

ESPN's own projections are what every manager in the league already sees.
These adapters add what they do not: what the wider market pays for a player
in trades, who is being picked up right now, and another projection to check
ESPN's against. Every source is optional and fails soft -- a source that is
down leaves its fields off the player, and the tools carry on with ESPN alone.
"""

from .signals import Signals, build_signals

__all__ = ["Signals", "build_signals"]
