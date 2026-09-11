"""Internalization policy: all pricing and risk decisions, no execution mechanics.

Policy summary
--------------
1. Cross first: client-to-client at the cent-rounded midpoint, clamped so the
   price satisfies both clients' limits and sits within the NBBO. Zero firm
   risk, both sides usually improved vs. the touch.
2. Internalize when the quoted spread is >= min_internalize_spread (2c): fill
   the client at the midpoint rounded one half-cent in the firm's favor, which
   still guarantees the client >= 1c improvement over the touch. New-risk size
   is capped at room-to-the-soft-limit (v0.1): beyond that, the fill would be
   hedged at the touch immediately and the hedge cost exceeds the edge, so the
   residual routes. The hard limit still bounds everything.
3. Flow that reduces existing inventory is always welcome: filled at the
   improved midpoint when the spread allows, otherwise at the touch (the
   client does no worse than routing; the firm unwinds without paying the
   spread).
4. After no_new_risk_after (15:55) the firm stops building new inventory:
   only risk-reducing principal fills, everything else routes.
5. Everything else routes to the market at the touch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

from .models import Order


@dataclass(frozen=True)
class Config:
    min_internalize_spread: int = 2       # cents of quoted spread needed to take principal risk
    hard_position_limit: int = 10_000     # shares; firm never exceeds this, long or short
    soft_position_limit: int = 6_000      # hedge back inside this band when breached
    no_new_risk_after: time = time(15, 55)


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---- crossing ----
    @staticmethod
    def cross_window(buy_limit: Optional[int], sell_limit: Optional[int],
                     bid: int, ask: int) -> Optional[tuple[int, int]]:
        """Price range where a cross satisfies both clients and the NBBO."""
        lo = bid if sell_limit is None else max(bid, sell_limit)
        hi = ask if buy_limit is None else min(ask, buy_limit)
        return (lo, hi) if lo <= hi else None

    @staticmethod
    def cross_price(window: tuple[int, int], bid: int, ask: int) -> int:
        lo, hi = window
        mid = (bid + ask) // 2
        return min(max(mid, lo), hi)

    # ---- principal pricing ----
    @staticmethod
    def improved_price(side: str, bid: int, ask: int) -> int:
        """Cent-rounded midpoint, half-cent rounded in the firm's favor.

        With spread >= 2c this always gives the client >= 1c improvement
        over the touch while leaving the firm >= half the spread of edge.
        """
        if side == "BUY":
            return (bid + ask + 1) // 2   # ceil(mid)
        return (bid + ask) // 2           # floor(mid)

    def principal_quote(self, order: Order, position: int, ts: datetime,
                        bid: int, ask: int) -> tuple[int, int]:
        """(qty, price) the firm fills as principal; (0, 0) to decline.

        New risk is sized to room-to-the-soft-limit, not the hard limit: the
        marginal fill beyond the band would be hedged at the touch right away,
        where the half-spread hedge cost exceeds the captured edge, so that
        portion routes instead. The hard limit still bounds everything.
        """
        spread = ask - bid
        buy = order.side == "BUY"
        reduces = (buy and position > 0) or (not buy and position < 0)
        late = ts.time() >= self.cfg.no_new_risk_after

        if spread >= self.cfg.min_internalize_spread and not late:
            hard_cap = (position + self.cfg.hard_position_limit if buy
                        else self.cfg.hard_position_limit - position)
            room = (position + self.cfg.soft_position_limit if buy
                    else self.cfg.soft_position_limit - position)
            qty = min(order.remaining, hard_cap, max(0, room))
            if qty > 0:
                return qty, self.improved_price(order.side, bid, ask)

        if reduces:
            qty = min(order.remaining, abs(position))
            if spread >= self.cfg.min_internalize_spread:
                px = self.improved_price(order.side, bid, ask)
            else:
                px = ask if buy else bid  # touch: client matches routing, firm avoids the spread
            return qty, px

        return 0, 0
