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
    """Yield each CSV line as a list of fields, tolerating the input's quirks."""
    # utf-8-sig strips a BOM if present (a plain reader would corrupt the
    # first header name into '﻿order_id' and break column lookup)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()            # also drops the CRLF the files use
            if not line:
                continue                  # blank trailing line
            # whole-line quoting: "a,b,c" (no per-field quotes inside)
            if line.startswith('"') and line.endswith('"') and '","' not in line:
                line = line[1:-1]
            yield next(csv.reader([line]))   # csv.reader handles per-field quotes


def _indexed(path: str):
    """Return (data rows, field accessor). Columns resolve by NAME, not position,
    so a reordered header in another environment still loads correctly."""
    rows = _rows(path)
    header = next(rows)                   # consumed here: the caller only sees data rows
    idx = {name.strip().strip('"').lstrip("﻿").lower(): i   # normalise: quotes, BOM, case
           for i, name in enumerate(header)}

    def field(row, name):
        if name not in idx:               # name the actual header: next run diagnoses itself
            raise KeyError(
                f"column '{name}' not found in {path}; header has: {sorted(idx)}")
        return row[idx[name]].strip()  # strip stray padding around values

    return rows, field


def load_quotes(path: str) -> list[Quote]:
    rows, f = _indexed(path)
    return [
        Quote(
            ts=datetime.fromisoformat(f(r, "timestamp")),
            symbol=f(r, "symbol"),
            bid=to_cents(f(r, "bid_price")),      # dollars -> integer cents at the edge
            bid_size=int(f(r, "bid_size")),       # carried but unused: size never binds
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
            side=f(r, "side").upper(),            # normalise so the engine can compare ==
            order_type=f(r, "order_type").upper(),
            quantity=int(f(r, "quantity")),
            limit=to_cents(f(r, "limit_price")) if f(r, "limit_price") else None,  # blank = MARKET
            tif=f(r, "tif").upper(),
        )
        for r in rows
    ]
