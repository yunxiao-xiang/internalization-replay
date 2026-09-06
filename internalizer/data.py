"""CSV loading.

The provided files wrap every physical line in double quotes and use CRLF
endings, so each line is unwrapped before splitting. Neither file contains
embedded commas inside fields.
"""
from __future__ import annotations

from datetime import datetime

from .models import Order, Quote, to_cents


def _rows(path: str):
    with open(path, newline="") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('"') and line.endswith('"'):
                line = line[1:-1]
            yield line.split(",")


def load_quotes(path: str) -> list[Quote]:
    rows = _rows(path)
    next(rows)  # header
    return [
        Quote(
            ts=datetime.fromisoformat(r[0]),
            symbol=r[1],
            bid=to_cents(r[2]),
            bid_size=int(r[3]),
            ask=to_cents(r[4]),
            ask_size=int(r[5]),
        )
        for r in rows
    ]


def load_orders(path: str) -> list[Order]:
    rows = _rows(path)
    next(rows)  # header
    return [
        Order(
            order_id=r[0],
            ts=datetime.fromisoformat(r[1]),
            client_id=r[2],
            symbol=r[3],
            side=r[4],
            order_type=r[5],
            quantity=int(r[6]),
            limit=to_cents(r[7]) if r[7] else None,
            tif=r[8],
        )
        for r in rows
    ]
