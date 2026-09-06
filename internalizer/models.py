"""Core domain objects. Prices are integer cents throughout the engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def to_cents(s: str) -> int:
    return round(float(s) * 100)


def fmt_price(cents: int) -> str:
    return f"{cents / 100:.2f}"


def fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


@dataclass(frozen=True)
class Quote:
    ts: datetime
    symbol: str
    bid: int
    bid_size: int
    ask: int
    ask_size: int

    @property
    def spread(self) -> int:
        return self.ask - self.bid


@dataclass
class Order:
    order_id: str
    ts: datetime
    client_id: str
    symbol: str
    side: str                 # BUY / SELL
    order_type: str           # MARKET / LIMIT
    quantity: int
    limit: Optional[int]      # cents; None for market orders
    tif: str                  # DAY / IOC
    remaining: int = 0
    closed_as: Optional[str] = None   # CANCELLED / EXPIRED once terminally closed

    def __post_init__(self):
        self.remaining = self.quantity
