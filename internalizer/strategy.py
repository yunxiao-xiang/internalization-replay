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
6. Position-adjusted value (v0.4, the Delta One PAV): PAV = mid - risk *
   position, with risk normalized as in d1's baseline covariance scaling. When
   PAV is beyond a price the firm can exit at (a resting client limit, else the
   opposite touch), the book trades there, sized to move PAV to that price and
   never past flat.
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
    # proactive bleed (v0.2): hedge band inventory down to bleed_trigger when
    # (A) the spread is cheap, or (B) the position has aged past age_limit
    bleed_trigger: int = 4_000            # shares; bleed fires above this, reduces to it
    cheap_spread_max: int = 1             # cents; trigger A when spread <= this
    age_limit_secs: int = 600             # trigger B when |pos| > trigger this long
    # position-adjusted value (v0.4, Delta One PAV): PAV = mid - risk * position.
    # risk is normalized like d1's baseline covariance scaling: at risk_norm_vol,
    # holding risk_norm_qty shares shifts PAV by risk_norm_edge_c, and it scales
    # with (daily_vol / risk_norm_vol) squared
    daily_vol: float = 0.015              # daily return vol input (1.5%)
    risk_norm_vol: float = 0.015          # the vol the normalization is defined at
    risk_norm_qty: int = 4_000            # shares that shift PAV by risk_norm_edge_c
    risk_norm_edge_c: float = 0.5         # cents of PAV shift at risk_norm_qty


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

    def bleed_target(self, spread: int, position: int,
                     over_secs: float | None) -> int | None:
        """Proactive hedge of band inventory: target |position|, or None.

        Fires only above bleed_trigger, via two observable triggers (no
        forecasts): (A) the spread is at its cheapest, so the half-spread
        hedge cost is minimal; (B) the position has sat above the trigger for
        age_limit_secs — realized evidence that offsetting flow is not
        coming, so the netting option has decayed.
        """
        if abs(position) <= self.cfg.bleed_trigger:
            return None
        if spread <= self.cfg.cheap_spread_max:
            return self.cfg.bleed_trigger
        if over_secs is not None and over_secs >= self.cfg.age_limit_secs:
            return self.cfg.bleed_trigger
        return None

    # ---- position-adjusted value (v0.4) ----
    def risk_value(self) -> float:
        """Cents of PAV shift per share held: d1's risk value for a single name.

        d1 scales the covariance so the baseline symbol's variance maps to a
        target edge at a target quantity; here the baseline is risk_norm_vol,
        so risk grows with the square of daily_vol relative to it.
        """
        c = self.cfg
        return c.risk_norm_edge_c / c.risk_norm_qty * (c.daily_vol / c.risk_norm_vol) ** 2

    def pav(self, bid: int, ask: int, position: int) -> float:
        """Position-adjusted value in cents: the mid shifted against inventory,
        so a long book values the stock lower and a short book higher."""
        return (bid + ask) / 2 - self.risk_value() * position

    def pav_qty(self, px: int, pav: float, position: int) -> int:
        """Shares to trade at px toward flat: d1 sizing (edge / risk), which
        moves PAV exactly to px. Capped at the position, so it never flips."""
        edge = px - pav if position > 0 else pav - px   # long sells above PAV, short buys below
        return max(0, min(abs(position), int(edge / self.risk_value() + 1e-6)))
