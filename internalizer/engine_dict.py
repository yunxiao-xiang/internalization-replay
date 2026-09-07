"""Engine variant backed by the production-style dict book.

Identical policy and mechanics to Engine - the only change is the resting
book implementation (price-level dict + id index + sorted ladder instead of
heaps with tombstones). Exists to demonstrate that the book is a swappable
component behind the best_buy/best_sell/add/coverage/open_orders interface,
and to add the O(1) cancel path a real system needs.
"""
from __future__ import annotations

from .book_dict import DictOrderBook
from .engine import Engine


class DictEngine(Engine):
    def __init__(self, strategy, reporter):
        super().__init__(strategy, reporter)
        self.book = DictOrderBook()
