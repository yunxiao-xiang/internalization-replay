"""Parity + cancel tests for the dict-based book. Run: python3 test_book_dict.py

The dict book must be bit-for-bit interchangeable with the heap book: same
fills, same firm trades, same cash over the full replay.
"""
from datetime import datetime

import config
from internalizer.book_dict import DictOrderBook
from internalizer.data import load_orders, load_quotes
from internalizer.engine import Engine
from internalizer.engine_dict import DictEngine
from internalizer.models import Order
from internalizer.reporting import Reporter
from internalizer.strategy import Strategy
from main import merge_events

D = datetime(2026, 8, 17, 10, 0)


def order(oid, side, qty, limit):
    return Order(oid, D, "C1", "AAPL", side, "LIMIT", qty, limit, "DAY")


def test_full_replay_parity():
    quotes = load_quotes(config.QUOTES_CSV)
    results = []
    for engine_cls in (Engine, DictEngine):
        rep = Reporter()
        eng = engine_cls(Strategy(config.STRATEGY), rep)
        for kind, ev in merge_events(quotes, load_orders(config.ORDERS_CSV)):
            (eng.on_quote if kind == "Q" else eng.on_order)(ev)
        eng.on_close()
        strip = [{k: v for k, v in f.items() if k != "fill_id"} for f in rep.fills]
        results.append((strip, rep.firm_trades, eng.cash, eng.position))
    (f1, t1, c1, p1), (f2, t2, c2, p2) = results
    assert f1 == f2, "fills differ between heap and dict books"
    assert t1 == t2 and c1 == c2 and p1 == p2 == 0
    print(f"parity: {len(f1)} fills, {len(t1)} firm trades, cash ${c1/100:,.2f} identical")


def test_priority_and_peek():
    b = DictOrderBook()
    b.add(order("A", "BUY", 100, 24479))
    b.add(order("B", "BUY", 100, 24480))   # better price
    b.add(order("C", "BUY", 100, 24480))   # same price, later -> behind B
    assert b.best_buy().order_id == "B"
    b.best_buy().remaining = 0             # engine-style fill of the head
    assert b.best_buy().order_id == "C"    # lazy reconcile buries B
    assert b.coverage("BUY", 24479) == 200


def test_engine_on_cancel():
    from internalizer.models import Quote
    rep = Reporter()
    eng = DictEngine(Strategy(config.STRATEGY), rep)
    eng.on_quote(Quote(D, "AAPL", 24478, 100, 24481, 100))
    o = order("R1", "BUY", 500, 24479)      # inside spread: rests
    eng.on_order(o)
    assert eng.book.best_buy().order_id == "R1"
    assert eng.on_cancel("R1", D) is True   # O(1) removal + terminal transition
    assert o.closed_as == "CANCELLED" and o.remaining == 0
    assert eng.book.best_buy() is None
    assert eng.on_cancel("R1", D) is False  # cancel racing a fill / unknown: no-op
    assert eng.position == 0 and eng.cash == 0


def test_cancel_o1_path():
    b = DictOrderBook()
    b.add(order("A", "SELL", 100, 24481))
    b.add(order("B", "SELL", 200, 24481))  # cancel from mid-queue, no search
    b.add(order("C", "SELL", 300, 24482))
    assert b.cancel("B").order_id == "B"
    assert b.coverage("SELL", 24482) == 400          # 100 + 300 left
    assert b.best_sell().order_id == "A"
    assert b.cancel("A").order_id == "A"             # empties the 24481 level
    assert b.best_sell().order_id == "C"             # ladder advanced
    assert b.cancel("ZZ") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK: {len(fns)} tests passed")
