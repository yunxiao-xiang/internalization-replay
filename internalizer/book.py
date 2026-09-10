"""Resting client limit orders in price-time priority, one heap per side.

Filled/closed orders are removed lazily: entries whose order has no remaining
quantity are discarded on the next peek.
"""
from __future__ import annotations

import heapq

from .models import Order


class OrderBook:
    def __init__(self):
        self._buys: list = []   # (-limit, seq, order): highest limit first
        self._sells: list = []  # (limit, seq, order): lowest limit first
        self._seq = 0
        self._open: dict[str, Order] = {}

    def add(self, order: Order) -> None:
        assert order.limit is not None, "market orders never rest"
        self._seq += 1
        if order.side == "BUY":
            heapq.heappush(self._buys, (-order.limit, self._seq, order))
        else:
            heapq.heappush(self._sells, (order.limit, self._seq, order))
        self._open[order.order_id] = order

    def _peek(self, heap: list) -> Order | None:
        # delete orders that remaining = 0
        while heap and heap[0][2].remaining == 0:
            _, _, o = heapq.heappop(heap)
            self._open.pop(o.order_id, None)
        # return the top of the list
        return heap[0][2] if heap else None

    def best_buy(self) -> Order | None:
        return self._peek(self._buys)

    def best_sell(self) -> Order | None:
        return self._peek(self._sells)

    def find(self, order_id: str) -> Order | None:
        """Live resting order by id, or None. O(1) via the _open dict; the
        heap entry itself is untouched (cancellation just tombstones it)."""
        o = self._open.get(order_id)
        return o if o is not None and o.remaining > 0 else None

    def open_orders(self) -> list[Order]:
        return [o for o in self._open.values() if o.remaining > 0]
