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

    # ---------- event handlers ----------
    def on_quote(self, q: Quote) -> None:
        self.quote = q
        self._sweep(q.ts)
        self._rebalance(q.ts, self.cfg.soft_position_limit)

    def on_order(self, o: Order) -> None:
        assert self.quote is not None, "order arrived before first quote"
        self._try_cross(o, o.ts)
        if o.remaining and self._marketable(o):
            self._execute_marketable(o, o.ts)
        if o.remaining:
            if o.tif == "IOC":
                self._close_order(o, "CANCELLED")
            else:
                self.book.add(o)
        self._rebalance(o.ts, self.cfg.soft_position_limit)

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
        qty, px = self.strat.principal_quote(o, self.position, ts, q.bid, q.ask)
        if qty:
            self._fill(ts, o, qty, px, "PRINCIPAL", "INTERNAL")
        if o.remaining:
            px = q.ask if o.side == "BUY" else q.bid
            self._fill(ts, o, o.remaining, px, "AGENCY", "MARKET")

    def _sweep(self, ts: datetime) -> None:
        """Re-evaluate resting orders against the new NBBO: cross resting
        orders whose limits now overlap inside the market, then execute any
        that became marketable."""
        q = self.quote
        while True:
            b, s = self.book.best_buy(), self.book.best_sell()
            if b and s:
                win = self.strat.cross_window(b.limit, s.limit, q.bid, q.ask)
                if win:
                    px = self.strat.cross_price(win, q.bid, q.ask)
                    qty = min(b.remaining, s.remaining)
                    self._fill(ts, b, qty, px, "AGENCY", "CROSS")
                    self._fill(ts, s, qty, px, "AGENCY", "CROSS")
                    continue
            if b and b.limit >= q.ask:
                self._execute_marketable(b, ts)
                continue
            if s and s.limit <= q.bid:
                self._execute_marketable(s, ts)
                continue
            return

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

    # ---------- booking ----------
    def _fill(self, ts: datetime, order: Order, qty: int, px: int,
              capacity: str, venue: str) -> None:
        q = self.quote
        if not (q.bid <= px <= q.ask):
            raise ComplianceError(
                f"fill {fmt_price(px)} outside NBBO {fmt_price(q.bid)}/{fmt_price(q.ask)} "
                f"for {order.order_id} at {fmt_ts(ts)}")
        if order.limit is not None:
            if order.side == "BUY":
                assert px <= order.limit, f"buy filled above limit: {order.order_id}"
            else:
                assert px >= order.limit, f"sell filled below limit: {order.order_id}"
        assert 0 < qty <= order.remaining
        order.remaining -= qty
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
