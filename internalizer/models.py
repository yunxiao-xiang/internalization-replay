"""Core domain objects. Prices are integer cents throughout the engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def to_cents(s: str) -> int:
    return round(float(s) * 100)          # parse once at the edge; no floats inside the engine


def fmt_price(cents: int) -> str:
    return f"{cents / 100:.2f}"           # cents -> the dollars the CSV reports


def fmt_ts(ts: datetime) -> str:
    # isoformat() would print microseconds; the data set is millisecond-resolution
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


@dataclass(frozen=True)                   # frozen: a quote is a snapshot, never edited
class Quote:
    ts: datetime
    symbol: str
    bid: int                              # cents
    bid_size: int                         # displayed size; unused (market absorbs any size)
    ask: int
    ask_size: int

    @property
    def spread(self) -> int:
        return self.ask - self.bid        # cents; drives the internalize/route decision


@dataclass                                # mutable: `remaining` is decremented as it fills
class Order:
    order_id: str
    ts: datetime
    client_id: str
    symbol: str
    side: str                 # BUY / SELL
    order_type: str           # MARKET / LIMIT
    quantity: int             # original size, never changes -> the accounting anchor
    limit: Optional[int]      # cents; None for market orders
    tif: str                  # DAY / IOC
    remaining: int = 0        # set in __post_init__; only _fill() decrements it
    closed_as: Optional[str] = None   # CANCELLED / EXPIRED once terminally closed

    def __post_init__(self):
        self.remaining = self.quantity    # invariant: quantity == filled + remaining
