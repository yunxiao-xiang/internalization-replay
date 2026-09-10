"""Replay the trading day end-to-end.

Usage:
    python3 main.py [quotes_csv orders_csv [out_dir]]

Defaults come from config.py.
"""
from __future__ import annotations

import sys

import config
from internalizer.data import load_orders, load_quotes
from internalizer.engine import Engine
from internalizer.reporting import Reporter
from internalizer.strategy import Strategy


def merge_events(quotes, orders):
    """Single time-ordered stream; a quote ties before an order at the same
    timestamp so the order sees the quote 'at or before now'."""
    qi = oi = 0                                  # both inputs are already sorted -> O(n) merge
    while qi < len(quotes) or oi < len(orders):
        # `<=` is the tie-break that makes lookahead impossible: every quote at or
        # before an order's timestamp is dispatched before that order
        if oi >= len(orders) or (qi < len(quotes) and quotes[qi].ts <= orders[oi].ts):
            yield "Q", quotes[qi]
            qi += 1
        else:
            yield "O", orders[oi]
            oi += 1


def _sniff(path) -> str:
    """'quotes' or 'orders', judged by the file's header line."""
    with open(path, encoding="utf-8-sig") as f:
        head = f.readline().lower()
    return "quotes" if "bid_price" in head else "orders"


def main(argv: list[str]) -> int:
    quotes_path = argv[1] if len(argv) > 1 else config.QUOTES_CSV
    orders_path = argv[2] if len(argv) > 2 else config.ORDERS_CSV
    if _sniff(quotes_path) == "orders" and _sniff(orders_path) == "quotes":
        print("note: input files look swapped; swapping them")
        quotes_path, orders_path = orders_path, quotes_path
    if len(argv) > 3:
        fills_csv, firm_csv, summary_txt = config.out_paths(argv[3])
    else:
        fills_csv, firm_csv, summary_txt = (
            config.FILLS_CSV, config.FIRM_TRADES_CSV, config.SUMMARY_TXT)

    quotes = load_quotes(quotes_path)            # ~52k ticks
    orders = load_orders(orders_path)            # 500 client orders

    reporter = Reporter()                        # collects fills, hedges, terminal states
    engine = Engine(Strategy(config.STRATEGY), reporter)   # policy injected, not hardcoded
    for kind, ev in merge_events(quotes, orders):
        if kind == "Q":
            engine.on_quote(ev)                  # refresh NBBO, re-evaluate the book
        else:
            engine.on_order(ev)                  # run the decision waterfall
    engine.on_close()                            # 16:00: flatten, expire, assert flat

    summary = reporter.write_outputs(fills_csv, firm_csv, summary_txt,
                                     engine.position, engine.cash, orders)
    print(summary)
    print(f"Wrote {fills_csv}, {firm_csv}, {summary_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
