"""Engine-level regression tests. Run: python3 test_engine.py

Covers the compliance case where the market moves through a resting order's
limit: the fill must come at or inside the new NBBO (price improvement),
never at the now-outside limit price.
"""
from datetime import datetime

from internalizer.engine import Engine
from internalizer.models import Order, Quote
from internalizer.reporting import Reporter
from internalizer.strategy import Config, Strategy

D = datetime(2026, 8, 17, 10, 0, 0)


def make_engine():
    return Engine(Strategy(Config()), Reporter())


def quote(bid, ask, sec=0):        # sec advances the clock between ticks        # sec advances the clock between ticks
    return Quote(D.replace(second=sec), "AAPL", bid, 100, ask, 100)


def test_market_through_resting_buy_limit_fills_inside_new_nbbo():
    eng = make_engine()
    eng.on_quote(quote(18999, 19002))
    o = Order("O1", D, "C1", "AAPL", "BUY", "LIMIT", 500, 19000, "DAY")
    eng.on_order(o)                       # 190.00 < ask 190.02: rests
    assert o.remaining == 500
    eng.on_quote(quote(18992, 18995, sec=5))   # ask drops through the limit
    assert o.remaining == 0
    fill = eng.rep.fills[0]
    px = round(float(fill["price"]) * 100)
    assert px <= 18995, f"filled at {px}, above the new ask"   # never 190.00
    assert px <= 19000 and px >= 18992


def test_market_through_resting_buy_routes_at_new_ask_when_firm_declines():
    # position already at the short hard limit: firm cannot internalize,
    # so the resting buy routes at the new ask -- still <= its stale limit
    # (soft limit pinned to the hard limit so the rebalancer stays out of the way)
    eng = Engine(Strategy(Config(soft_position_limit=10_000)), Reporter())
    eng.on_quote(quote(18999, 19002))
    eng.position = -10_000
    o = Order("O1", D, "C1", "AAPL", "BUY", "LIMIT", 500, 19000, "DAY")
    eng.on_order(o)
    eng.on_quote(quote(18992, 18995, sec=5))
    fill = next(f for f in eng.rep.fills if f["order_id"] == "O1")
    assert fill["venue"] == "MARKET"
    assert round(float(fill["price"]) * 100) == 18995


def test_market_through_resting_sell_limit_fills_inside_new_nbbo():
    eng = make_engine()
    eng.on_quote(quote(18999, 19002))
    o = Order("O2", D, "C1", "AAPL", "SELL", "LIMIT", 300, 19001, "DAY")
    eng.on_order(o)                       # 190.01 > bid 189.99: rests
    eng.on_quote(quote(19005, 19008, sec=5))   # bid gaps up through the limit
    assert o.remaining == 0
    px = round(float(eng.rep.fills[0]["price"]) * 100)
    assert px >= 19005, f"filled at {px}, below the new bid"   # never 190.01


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK: {len(fns)} tests passed")
