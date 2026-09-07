"""Production-style order book: price-level dict + order-id index + sorted
price ladder ("三件套"). Same interface as book.OrderBook, plus O(1) cancel.

Structures (stdlib only):
  levels:      dict[price -> PriceLevel]   FIFO doubly-linked queue per price
                                           + incrementally maintained total
  order_index: dict[order_id -> Node]      O(1) hash straight to the node
  prices:      sorted list of active prices (bisect; O(P) insert/remove where
               P = active price levels, P << N orders)

Order ops (add / cancel / consume-at-head) are O(1); only creating or
emptying a price level touches the ladder at O(P).

Consistency with the engine's fill path: the engine decrements
`order.remaining` directly and never notifies the book, and fills only ever
hit the head of the best level. Each node therefore carries `booked` (the
remaining the book last saw) and reconciles lazily whenever its level's head
is observed - the same lazy philosophy as the heap book's tombstones, applied
to quantities.
"""
from __future__ import annotations

from bisect import insort

from .models import Order


class _Node:
    __slots__ = ("order", "booked", "prev", "next", "price")

    def __init__(self, order: Order):
        self.order = order
        self.booked = order.remaining   # remaining as last synced into level.total
        self.prev: _Node | None = None
        self.next: _Node | None = None
        self.price = order.limit


class _PriceLevel:
    __slots__ = ("price", "head", "tail", "total")

    def __init__(self, price: int):
        self.price = price
        self.head: _Node | None = None
        self.tail: _Node | None = None
        self.total = 0                  # sum of booked over queued nodes

    def append(self, node: _Node) -> None:
        if self.tail is None:
            self.head = self.tail = node
        else:
            self.tail.next, node.prev = node, self.tail
            self.tail = node
        self.total += node.booked

    def unlink(self, node: _Node) -> None:   # O(1) pointer surgery
        if node.prev: node.prev.next = node.next
        else:         self.head = node.next
        if node.next: node.next.prev = node.prev
        else:         self.tail = node.prev
        node.prev = node.next = None


class DictOrderBook:
    def __init__(self):
        self._levels: dict[tuple[str, int], _PriceLevel] = {}
        self._prices = {"BUY": [], "SELL": []}       # each kept sorted ascending
        self._index: dict[str, _Node] = {}

    # ---------- writes ----------
    def add(self, order: Order) -> None:
        assert order.limit is not None, "market orders never rest"
        key = (order.side, order.limit)
        level = self._levels.get(key)
        if level is None:
            level = self._levels[key] = _PriceLevel(order.limit)
            insort(self._prices[order.side], order.limit)   # O(P), new level only
        node = _Node(order)
        level.append(node)                                   # O(1), FIFO = time priority
        self._index[order.order_id] = node                   # O(1)

    def cancel(self, order_id: str) -> Order | None:
        """The three-piece showcase: hash to the node, pointer-surgery it out."""
        node = self._index.pop(order_id, None)               # O(1)
        if node is None:
            return None
        side = node.order.side
        level = self._levels[(side, node.price)]
        self._sync(level, node)                              # fold in any unseen fills
        level.total -= node.booked
        level.unlink(node)                                   # O(1)
        if level.head is None:
            self._drop_level(side, node.price)               # O(P), level-empty only
        return node.order

    # ---------- reads ----------
    def best_buy(self) -> Order | None:
        return self._peek("BUY")

    def best_sell(self) -> Order | None:
        return self._peek("SELL")

    def coverage(self, side: str, price: int) -> int:
        """Sum of resting shares on `side` at prices that profitably offset a
        principal fill at `price` - O(levels in range), not O(orders)."""
        prices = self._prices[side]
        total = 0
        it = (p for p in prices if p <= price) if side == "SELL" \
            else (p for p in reversed(prices) if p >= price)
        for p in list(it):
            level = self._levels.get((side, p))
            if level is None:
                continue
            self._reconcile_head(side, level)                # exactness at the head
            if level.head is not None:
                total += level.total
        return total

    def open_orders(self) -> list[Order]:
        return [n.order for n in self._index.values() if n.order.remaining > 0]

    # ---------- internals ----------
    def _peek(self, side: str) -> Order | None:
        prices = self._prices[side]
        while prices:
            p = prices[-1] if side == "BUY" else prices[0]   # best of the ladder
            level = self._levels[(side, p)]
            self._reconcile_head(side, level)
            if level.head is None:                           # level died: drop, retry
                self._drop_level(side, p)
                continue
            return level.head.order
        return None

    def _reconcile_head(self, side: str, level: _PriceLevel) -> None:
        """Fold engine-side fills into the level. Fills only ever consume the
        head of a level (top-of-book execution), so syncing heads is exact."""
        while level.head is not None:
            node = level.head
            self._sync(level, node)
            if node.booked > 0:
                return
            level.unlink(node)                               # dead head: bury it
            self._index.pop(node.order.order_id, None)

    def _sync(self, level: _PriceLevel, node: _Node) -> None:
        if node.booked != node.order.remaining:
            level.total -= node.booked - node.order.remaining
            node.booked = node.order.remaining

    def _drop_level(self, side: str, price: int) -> None:
        self._levels.pop((side, price), None)
        self._prices[side].remove(price)                     # O(P)
