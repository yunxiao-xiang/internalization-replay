"""Replay the trading day end-to-end.

Usage:
    python3 main.py [quotes_csv orders_csv [out_dir]]

Defaults: data/aapl_quotes_20260817.csv, data/client_orders_20260817.csv, out/
"""
from __future__ import annotations

import sys

from internalizer.data import load_orders, load_quotes
from internalizer.engine import Engine
from internalizer.reporting import Reporter
from internalizer.strategy import Config, Strategy


def merge_events(quotes, orders):
    """Single time-ordered stream; a quote ties before an order at the same
    timestamp so the order sees the quote 'at or before now'."""
    qi = oi = 0
    while qi < len(quotes) or oi < len(orders):
        if oi >= len(orders) or (qi < len(quotes) and quotes[qi].ts <= orders[oi].ts):
            yield "Q", quotes[qi]
            qi += 1
        else:
            yield "O", orders[oi]
            oi += 1


def main(argv: list[str]) -> int:
    quotes_path = argv[1] if len(argv) > 1 else "data/aapl_quotes_20260817.csv"
    orders_path = argv[2] if len(argv) > 2 else "data/client_orders_20260817.csv"
    out_dir = argv[3] if len(argv) > 3 else "out"

    quotes = load_quotes(quotes_path)
    orders = load_orders(orders_path)

    reporter = Reporter()
    engine = Engine(Strategy(Config()), reporter)
    for kind, ev in merge_events(quotes, orders):
        if kind == "Q":
            engine.on_quote(ev)
        else:
            engine.on_order(ev)
    engine.on_close()

    summary = reporter.write_outputs(out_dir, engine.position, engine.cash, orders)
    print(summary)
    print(f"Wrote {out_dir}/fills.csv, {out_dir}/firm_trades.csv, {out_dir}/summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
