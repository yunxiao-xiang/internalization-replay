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
        self._seq = 0           # monotonic arrival counter -> time priority within a price
        self._open: dict[str, Order] = {}   # id -> order, for O(1) lookup and close-out

    def add(self, order: Order) -> None:
        assert order.limit is not None, "market orders never rest"
        self._seq += 1                                  # later arrivals sort behind at same price
        if order.side == "BUY":
            heapq.heappush(self._buys, (-order.limit, self._seq, order))  # negate: max-heap on price
        else:
            heapq.heappush(self._sells, (order.limit, self._seq, order))  # min-heap: cheapest first
        self._open[order.order_id] = order              # second index, same object

    def _peek(self, heap: list) -> Order | None:
        # lazy delete: heapq cannot remove from the middle, so dead entries are
        # buried here, one pop per order over its lifetime (amortised O(log n))
        while heap and heap[0][2].remaining == 0:       # filled/cancelled/expired = tombstone
            _, _, o = heapq.heappop(heap)               # only dead entries are ever popped
            self._open.pop(o.order_id, None)            # keep the two structures consistent
        return heap[0][2] if heap else None             # read the top, never pop a live order

    def best_buy(self) -> Order | None:
        return self._peek(self._buys)                   # highest limit, earliest at that price

    def best_sell(self) -> Order | None:
        return self._peek(self._sells)                  # lowest limit, earliest at that price

    def open_orders(self) -> list[Order]:
        return [o for o in self._open.values() if o.remaining > 0]   # for the 16:00 expiry sweep
