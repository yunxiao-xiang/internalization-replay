"""Standalone engine backed by the production-style dict book.

Same policy and mechanics as engine.Engine (kept in lockstep - the parity
test replays the full day on both and asserts identical fills, hedges, and
cash), written out in full rather than inherited, plus the one capability the
dict book exists for: `on_cancel`, the O(1) client-cancel path.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

from .book_dict import DictOrderBook
from .engine import ComplianceError
from .models import Order, Quote, fmt_price, fmt_ts
from .strategy import Strategy

CLOSE = dtime(16, 0)


class DictEngine:
    def __init__(self, strategy: Strategy, reporter):
        self.strat = strategy
        self.cfg = strategy.cfg
        self.rep = reporter
        self.book = DictOrderBook()
        self.quote: Quote | None = None
        self.position = 0     # shares, signed
        self.cash = 0         # cents, from principal trading only
        self._over_since: datetime | None = None   # when |pos| first exceeded bleed_trigger

    # ---------- event handlers ----------
    def on_quote(self, q: Quote) -> None:
        self.quote = q
        self._sweep(q.ts)
        self._rebalance(q.ts, self.cfg.soft_position_limit)
        over = ((q.ts - self._over_since).total_seconds()
                if self._over_since else None)
        target = self.strat.bleed_target(q.spread, self.position, over)
        if target is not None:
            self._rebalance(q.ts, target)
        self._track_inventory_age(q.ts)

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
        self._track_inventory_age(o.ts)

    def on_cancel(self, order_id: str, ts: datetime) -> bool:
        """Client cancel of a resting order: O(1) removal via the id index,
        then the standard terminal transition. Position and cash untouched -
        nothing traded. Returns False for an unknown/already-dead order (a
        cancel racing a fill loses; the fill stands)."""
        order = self.book.cancel(order_id)
        if order is None or order.remaining == 0:
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
