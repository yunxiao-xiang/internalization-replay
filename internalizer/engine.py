"""Event replay and execution mechanics.

Events (quotes + orders) arrive strictly in timestamp order; a quote with the
same timestamp as an order is applied first, so every decision uses the most
recent quote at or before "now". Every client fill passes through _fill(),
which enforces the NBBO and limit-price constraints before booking anything.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

from .book import OrderBook
from .models import Order, Quote, fmt_price, fmt_ts
from .strategy import Strategy

CLOSE = dtime(16, 0)


class ComplianceError(AssertionError):
    pass


class Engine:
    def __init__(self, strategy: Strategy, reporter):
        self.strat = strategy
        self.cfg = strategy.cfg
        self.rep = reporter
        self.book = OrderBook()
        self.quote: Quote | None = None
        self.position = 0     # shares, signed: negative = short
        self.cash = 0         # cents, from principal trading only

    # ---------- event handlers ----------
    def on_quote(self, q: Quote) -> None:
        self.quote = q                  # the only market state the engine holds: no lookahead
        self._sweep(q.ts)               # resting orders may have become executable
        self._rebalance(q.ts, self.cfg.soft_position_limit)  # sweep may have moved the position

    def on_order(self, o: Order) -> None:
        assert self.quote is not None, "order arrived before first quote"
        self._try_cross(o, o.ts)                    # riskless and improves both sides: try first
        if o.remaining and self._marketable(o):
            self._execute_marketable(o, o.ts)       # internalize what we want, route the rest
        elif o.remaining:
            self._offer_midpoint(o, o.ts)           # limit inside the spread: mid may already satisfy it
        if o.remaining:                             # nothing above could execute it
            if o.tif == "IOC":
                self._close_order(o, "CANCELLED")   # IOC: immediate or nothing
            else:
                self.book.add(o)                    # DAY: rest and wait for the market
        self._rebalance(o.ts, self.cfg.soft_position_limit)  # any new inventory checked at once

    def on_close(self) -> None:
        ts = datetime.combine(self.quote.ts.date(), CLOSE)   # stamp everything at 16:00
        self._sweep(ts)                # last chance to execute against final NBBO
        self._rebalance(ts, 0)         # flatten, using resting client orders first
        for o in self.book.open_orders():
            self._close_order(o, "EXPIRED")   # routing these would break their limit price
        assert self.position == 0, "firm must end the day flat"

    # ---------- decision waterfall ----------
    def _marketable(self, o: Order) -> bool:
        if o.order_type == "MARKET":
            return True                 # market orders have no limit to compare against
        # a buy is marketable at or above the ask, a sell at or below the bid
        return o.limit >= self.quote.ask if o.side == "BUY" else o.limit <= self.quote.bid

    def _try_cross(self, o: Order, ts: datetime) -> None:
        q = self.quote
        while o.remaining:                          # keep matching until size or prices run out
            opp = self.book.best_sell() if o.side == "BUY" else self.book.best_buy()
            if opp is None:
                return                              # nothing resting on the other side
            buy_o, sell_o = (o, opp) if o.side == "BUY" else (opp, o)   # normalise the pair
            win = self.strat.cross_window(buy_o.limit, sell_o.limit, q.bid, q.ask)
            if win is None:
                return                              # limits do not overlap inside the NBBO
            px = self.strat.cross_price(win, q.bid, q.ask)   # NBBO mid, clamped into the window
            qty = min(o.remaining, opp.remaining)            # the smaller side sets the size
            self._fill(ts, buy_o, qty, px, "AGENCY", "CROSS")   # two rows, same price and time
            self._fill(ts, sell_o, qty, px, "AGENCY", "CROSS")

    def _execute_marketable(self, o: Order, ts: datetime) -> None:
        q = self.quote
        qty, px = self.strat.principal_quote(o, self.position, ts, q.bid, q.ask)  # policy decides
        if qty:
            self._fill(ts, o, qty, px, "PRINCIPAL", "INTERNAL")   # the firm takes the other side
        if o.remaining:                             # declined or truncated by the position limit
            px = q.ask if o.side == "BUY" else q.bid              # routing pays the touch
            self._fill(ts, o, o.remaining, px, "AGENCY", "MARKET")

    def _offer_midpoint(self, o: Order, ts: datetime) -> None:
        """A limit resting inside the spread can still be internalized on
        arrival when the firm's improved price satisfies it: buy limit 244.43
        with NBBO 244.40/244.44 fills at ceil(mid) 244.42. No route leg; any
        residual rests as before."""
        q = self.quote
        qty, px = self.strat.principal_quote(o, self.position, ts, q.bid, q.ask)  # same policy as marketable flow
        # the limit check alone keeps this off the touch: a non-marketable buy's limit is below the ask
        if qty and (px <= o.limit if o.side == "BUY" else px >= o.limit):
            self._fill(ts, o, qty, px, "PRINCIPAL", "INTERNAL")

    def _sweep(self, ts: datetime) -> None:
        """Re-evaluate resting orders against the new NBBO: cross resting
        orders whose limits now overlap inside the market, then execute any
        that became marketable."""
        q = self.quote
        while True:                                 # one pass can unlock the next
            b, s = self.book.best_buy(), self.book.best_sell()
            if b and s:
                win = self.strat.cross_window(b.limit, s.limit, q.bid, q.ask)
                if win:                             # two resting orders now overlap
                    px = self.strat.cross_price(win, q.bid, q.ask)
                    qty = min(b.remaining, s.remaining)
                    self._fill(ts, b, qty, px, "AGENCY", "CROSS")
                    self._fill(ts, s, qty, px, "AGENCY", "CROSS")
                    continue                        # book changed: restart the scan
            if b and b.limit >= q.ask:              # the market came down to a resting buy
                self._execute_marketable(b, ts)     # re-priced at the NEW touch, not its stale limit
                continue
            if s and s.limit <= q.bid:              # the market came up to a resting sell
                self._execute_marketable(s, ts)
                continue
            return                                  # nothing left to do at this quote

    # ---------- inventory management ----------
    def _rebalance(self, ts: datetime, target: int) -> None:
        """Bring |position| down to target. Resting client limits inside the
        spread are a better exit than the touch, so use them first; hedge the
        remainder in the market."""
        q = self.quote
        excess = abs(self.position) - target
        if excess <= 0:
            return                                  # inside the band: nothing to do
        if self.position > 0:                       # long -> need to sell
            while excess > 0:
                b = self.book.best_buy()
                if b is None or b.limit < q.bid:    # below the bid is worse than just hedging
                    break
                qty = min(excess, b.remaining)
                # sell at the client's own limit, capped at the ask so the fill stays inside NBBO
                self._fill(ts, b, qty, min(b.limit, q.ask), "PRINCIPAL", "INTERNAL")
                excess -= qty
            if excess > 0:
                self._firm_trade(ts, "SELL", excess, q.bid)   # residual: pay the touch
        else:                                       # short -> need to buy (mirror image)
            while excess > 0:
                s = self.book.best_sell()
                if s is None or s.limit > q.ask:
                    break
                qty = min(excess, s.remaining)
                self._fill(ts, s, qty, max(s.limit, q.bid), "PRINCIPAL", "INTERNAL")
                excess -= qty
            if excess > 0:
                self._firm_trade(ts, "BUY", excess, q.ask)

    # ---------- booking ----------
    def _fill(self, ts: datetime, order: Order, qty: int, px: int,
              capacity: str, venue: str) -> None:
        # single chokepoint: every client fill in the system passes through here,
        # so the compliance checks below cannot be bypassed by a strategy bug
        q = self.quote
        if not (q.bid <= px <= q.ask):              # the hard regulatory constraint
            raise ComplianceError(
                f"fill {fmt_price(px)} outside NBBO {fmt_price(q.bid)}/{fmt_price(q.ask)} "
                f"for {order.order_id} at {fmt_ts(ts)}")
        if order.limit is not None:                 # market orders have no limit to breach
            if order.side == "BUY":
                assert px <= order.limit, f"buy filled above limit: {order.order_id}"
            else:
                assert px >= order.limit, f"sell filled below limit: {order.order_id}"
        assert 0 < qty <= order.remaining           # no over-fill, no zero-size print
        order.remaining -= qty                      # the only place remaining is decremented
        if capacity == "PRINCIPAL":                 # agency fills leave the firm flat
            if order.side == "BUY":     # firm sells to the client
                self.position -= qty                # position goes shorter
                self.cash += qty * px               # cash in
            else:                       # firm buys from the client
                self.position += qty
                self.cash -= qty * px
        self.rep.record_fill(ts, order, qty, px, capacity, venue, q)   # NBBO stamped on the row

    def _firm_trade(self, ts: datetime, side: str, qty: int, px: int) -> None:
        # the firm's own hedge: not a client fill, so it goes to a separate blotter
        if side == "BUY":
            self.position += qty
            self.cash -= qty * px
        else:
            self.position -= qty
            self.cash += qty * px
        self.rep.record_firm_trade(ts, side, qty, px, self.position, self.quote)

    def _close_order(self, o: Order, status: str) -> None:
        # the only terminal transition: CANCELLED (IOC) or EXPIRED (DAY at the close)
        o.closed_as = status
        self.rep.record_close(o, o.remaining)       # record leaves before zeroing them
        o.remaining = 0                             # also tombstones the heap entry
