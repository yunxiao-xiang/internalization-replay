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
        self.position = 0     # shares, signed
        self.cash = 0         # cents, from principal trading only
        self._over_since: datetime | None = None   # when |pos| first exceeded bleed_trigger

    # ---------- event handlers ----------
    def on_quote(self, q: Quote) -> None:
        self.quote = q
        self._sweep(q.ts) # sweep order book
        self._rebalance(q.ts, self.cfg.soft_position_limit)  # hedge book, needed if we hedge based on spread width
        over = ((q.ts - self._over_since).total_seconds()
                if self._over_since else None) # record how long position has been over bleed trigger
        # addtional bleeding target - if position is too old or spread is cheap (1c)
        target = self.strat.bleed_target(q.spread, self.position, over)
        if target is not None:
            self._rebalance(q.ts, target)
        self._pav_adjust(q.ts)   # v0.4: trade toward flat where PAV is beyond an exit price
        self._track_inventory_age(q.ts)

    def on_order(self, o: Order) -> None:
        assert self.quote is not None, "order arrived before first quote"
        self._try_cross(o, o.ts) # try cross with resting book first
        # execute the marketable portion
        if o.remaining and self._marketable(o):
            self._execute_marketable(o, o.ts)
        elif o.remaining:
            self._offer_midpoint(o, o.ts)   # inside-the-spread limit: mid may beat it
        if o.remaining:
            if o.tif == "IOC":
                self._close_order(o, "CANCELLED")
            else:
                self.book.add(o)
        self._rebalance(o.ts, self.cfg.soft_position_limit)
        self._pav_adjust(o.ts)
        self._track_inventory_age(o.ts)

    def on_cancel(self, order_id: str, ts: datetime) -> bool:
        """Client cancel of a resting order, heap-book style: O(1) lookup via
        the book's id dict, then the standard CANCELLED transition. The heap
        entry is left behind as a tombstone (remaining == 0) and buried by
        the next peek - no heap surgery. A cancel racing a fill returns False
        and the fill stands. Position and cash are untouched."""
        order = self.book.find(order_id)
        if order is None:
            return False
        self._close_order(order, "CANCELLED")
        return True

    def on_close(self) -> None:
        ts = datetime.combine(self.quote.ts.date(), CLOSE)
        self._sweep(ts)                # last chance to execute against final NBBO
        self._rebalance(ts, 0)         # flatten, using resting client orders first
        for o in self.book.open_orders():
            self._close_order(o, "EXPIRED")
        assert self.position == 0, "firm must end the day flat"

    # ---------- decision waterfall ----------
    def _marketable(self, o: Order) -> bool:
        if o.order_type == "MARKET":
            return True
        return o.limit >= self.quote.ask if o.side == "BUY" else o.limit <= self.quote.bid

    def _try_cross(self, o: Order, ts: datetime) -> None:
        """try to cross for incoming order"""
        q = self.quote
        while o.remaining:
            opp = self.book.best_sell() if o.side == "BUY" else self.book.best_buy()
            if opp is None:
                return
            buy_o, sell_o = (o, opp) if o.side == "BUY" else (opp, o)
            win = self.strat.cross_window(buy_o.limit, sell_o.limit, q.bid, q.ask)
            if win is None:
                return
            px = self.strat.cross_price(win, q.bid, q.ask)
            qty = min(o.remaining, opp.remaining)
            self._fill(ts, buy_o, qty, px, "AGENCY", "CROSS")
            self._fill(ts, sell_o, qty, px, "AGENCY", "CROSS")

    def _execute_marketable(self, o: Order, ts: datetime) -> None:
        q = self.quote
        # check with principal book for internalization firt(offloading principal risk)
        qty, px = self.strat.principal_quote(o, self.position, ts, q.bid, q.ask)
        if qty:
            self._fill(ts, o, qty, px, "PRINCIPAL", "INTERNAL")
        if o.remaining:
            px = q.ask if o.side == "BUY" else q.bid
            self._fill(ts, o, o.remaining, px, "AGENCY", "MARKET")

    def _offer_midpoint(self, o: Order, ts: datetime) -> None:
        """v0.3: a non-marketable limit resting inside the spread can still be
        filled as principal when the improved price beats its own limit (buy
        limit 244.43 with NBBO 244.40/244.44 fills at mid 244.42). Strictly
        better than the client's own instruction, and better than the fill they
        would otherwise wait for. No route leg: the residual rests as before."""
        q = self.quote
        px = self.strat.improved_price(o.side, q.bid, q.ask)
        if (px > o.limit) if o.side == "BUY" else (px < o.limit):
            return                       # mid is worse than the client's limit
        qty, quoted = self.strat.principal_quote(o, self.position, ts, q.bid, q.ask)
        if qty and quoted == px:         # only the improved-price branch, never touch
            self._fill(ts, o, qty, px, "PRINCIPAL", "INTERNAL")

    def _sweep(self, ts: datetime) -> None:
        """Re-evaluate resting orders against the new NBBO: cross resting
        orders whose limits now overlap inside the market, then execute any
        that became marketable."""
        q = self.quote
        while True:
            b, s = self.book.best_buy(), self.book.best_sell()
            # try cross first? -- cross never happened in on_quote->sweep in backtesting, but kept because its nearly
            if b and s:
                # window of cross
                win = self.strat.cross_window(b.limit, s.limit, q.bid, q.ask)
                if win:
                    # cross price determined by mkt mid - clamped within the window, both side still get improvement
                    px = self.strat.cross_price(win, q.bid, q.ask)
                    qty = min(b.remaining, s.remaining)
                    self._fill(ts, b, qty, px, "AGENCY", "CROSS")
                    self._fill(ts, s, qty, px, "AGENCY", "CROSS")
                    continue
            # after cross see if anything is executable in market (if the order is already in market prob do this first)
            if b and b.limit >= q.ask:
                self._execute_marketable(b, ts)
                continue
            if s and s.limit <= q.bid:
                self._execute_marketable(s, ts)
                continue
            return

    def _track_inventory_age(self, ts: datetime) -> None:
        """Clock for bleed trigger B: how long |pos| has sat above the trigger."""
        if abs(self.position) > self.cfg.bleed_trigger:
            if self._over_since is None:
                self._over_since = ts
        else:
            self._over_since = None

    # ---------- inventory management ----------
    def _rebalance(self, ts: datetime, target: int) -> None:
        """Bring |position| down to target. Resting client limits inside the
        spread are a better exit than the touch, so use them first; hedge the
        remainder in the market."""
        q = self.quote
        excess = abs(self.position) - target
        if excess <= 0:
            return
        if self.position > 0:
            while excess > 0:
                b = self.book.best_buy()
                if b is None or b.limit < q.bid:
                    break
                qty = min(excess, b.remaining)
                self._fill(ts, b, qty, min(b.limit, q.ask), "PRINCIPAL", "INTERNAL")
                excess -= qty
            if excess > 0:
                self._firm_trade(ts, "SELL", excess, q.bid)
        else:
            while excess > 0:
                s = self.book.best_sell()
                if s is None or s.limit > q.ask:
                    break
                qty = min(excess, s.remaining)
                self._fill(ts, s, qty, max(s.limit, q.bid), "PRINCIPAL", "INTERNAL")
                excess -= qty
            if excess > 0:
                self._firm_trade(ts, "BUY", excess, q.ask)

    def _pav_adjust(self, ts: datetime) -> None:
        """v0.4: trade the book toward flat whenever its position-adjusted value
        (Delta One PAV) is beyond a price it can exit at. A resting client limit
        inside the NBBO comes first (internalize at the client's limit), then
        the opposite touch (execute in the market). Each trade moves PAV exactly
        to its price, so a crossed PAV is cured with the least size and the
        position never flips."""
        q = self.quote
        while self.position:
            long = self.position > 0
            pav = self.strat.pav(q.bid, q.ask, self.position)
            o = self.book.best_buy() if long else self.book.best_sell()
            if o is not None and q.bid <= o.limit <= q.ask:   # fillable at its own limit
                qty = min(o.remaining, self.strat.pav_qty(o.limit, pav, self.position))
                if qty:
                    self._fill(ts, o, qty, o.limit, "PRINCIPAL", "INTERNAL")
                    continue
            touch = q.bid if long else q.ask
            qty = self.strat.pav_qty(touch, pav, self.position)
            if qty:
                self._firm_trade(ts, "SELL" if long else "BUY", qty, touch)
            return

    # ---------- booking ----------
    def _fill(self, ts: datetime, order: Order, qty: int, px: int,
              capacity: str, venue: str) -> None:
        q = self.quote
        # check price within NBBO
        if not (q.bid <= px <= q.ask):
            raise ComplianceError(
                f"fill {fmt_price(px)} outside NBBO {fmt_price(q.bid)}/{fmt_price(q.ask)} "
                f"for {order.order_id} at {fmt_ts(ts)}")
        # check price with limit
        if order.limit is not None:
            if order.side == "BUY":
                assert px <= order.limit, f"buy filled above limit: {order.order_id}"
            else:
                assert px >= order.limit, f"sell filled below limit: {order.order_id}"
        assert 0 < qty <= order.remaining
        order.remaining -= qty
        # modify static variable if it goes from principal book
        if capacity == "PRINCIPAL":
            if order.side == "BUY":     # firm sells to the client
                self.position -= qty
                self.cash += qty * px
            else:                       # firm buys from the client
                self.position += qty
                self.cash -= qty * px
        self.rep.record_fill(ts, order, qty, px, capacity, venue, q)

    def _firm_trade(self, ts: datetime, side: str, qty: int, px: int) -> None:
        if side == "BUY":
            self.position += qty
            self.cash -= qty * px
        else:
            self.position -= qty
            self.cash += qty * px
        self.rep.record_firm_trade(ts, side, qty, px, self.position, self.quote)

    def _close_order(self, o: Order, status: str) -> None:
        o.closed_as = status
        self.rep.record_close(o, o.remaining)
        o.remaining = 0
