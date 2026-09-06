"""CSV loading.

Robust to formatting differences between environments: columns are resolved
by header name (any column order works), and both quoting styles are handled
— files that wrap each whole line in double quotes (the original data set)
and standard per-field CSV quoting.
"""
from __future__ import annotations

import csv
from datetime import datetime

from .models import Order, Quote, to_cents


def _rows(path: str):
    with open(path, newline="") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            # whole-line quoting: "a,b,c" (no per-field quotes inside)
            if line.startswith('"') and line.endswith('"') and '","' not in line:
                line = line[1:-1]
            yield next(csv.reader([line]))


def _indexed(path: str):
    rows = _rows(path)
    header = next(rows)
    idx = {name.strip().lower(): i for i, name in enumerate(header)}

    def field(row, name):
        return row[idx[name]].strip()

    return rows, field


def load_quotes(path: str) -> list[Quote]:
    rows, f = _indexed(path)
    return [
        Quote(
            ts=datetime.fromisoformat(f(r, "timestamp")),
            symbol=f(r, "symbol"),
            bid=to_cents(f(r, "bid_price")),
            bid_size=int(f(r, "bid_size")),
            ask=to_cents(f(r, "ask_price")),
            ask_size=int(f(r, "ask_size")),
        )
        for r in rows
    ]


def load_orders(path: str) -> list[Order]:
    rows, f = _indexed(path)
    return [
        Order(
            order_id=f(r, "order_id"),
            ts=datetime.fromisoformat(f(r, "timestamp")),
            client_id=f(r, "client_id"),
            symbol=f(r, "symbol"),
            side=f(r, "side").upper(),
            order_type=f(r, "order_type").upper(),
            quantity=int(f(r, "quantity")),
            limit=to_cents(f(r, "limit_price")) if f(r, "limit_price") else None,
            tif=f(r, "tif").upper(),
        )
        for r in rows
    ]
