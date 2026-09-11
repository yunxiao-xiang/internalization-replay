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


def quote(bid, ask, sec=0):
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


def test_on_cancel_tombstones_resting_order():
    eng = make_engine()
    eng.on_quote(quote(18999, 19002))
    o = Order("O1", D, "C1", "AAPL", "BUY", "LIMIT", 500, 19000, "DAY")
    eng.on_order(o)                            # inside spread: rests
    assert eng.on_cancel("O1", D) is True
    assert o.closed_as == "CANCELLED" and o.remaining == 0
    assert eng.book.best_buy() is None         # tombstone buried at next peek
    assert eng.on_cancel("O1", D) is False     # already dead: cancel loses
    assert eng.on_cancel("NOPE", D) is False   # unknown id
    assert eng.position == 0 and eng.cash == 0


def test_pav_hedges_at_the_touch_when_pav_crosses_the_bid():
    # v0.4, bleed disabled to isolate it: long 5,000 in a 1c market puts PAV under the bid
    eng = Engine(Strategy(Config(bleed_trigger=10**9)), Reporter())
    eng.on_quote(quote(18998, 19002))
    eng.position = 5000
    eng.on_quote(quote(18999, 19000, sec=1))      # PAV 18998.875 < bid 18999
    assert eng.position == 4000                   # sold until PAV reached the bid
    t = eng.rep.firm_trades[-1]
    assert (t["side"], t["quantity"], round(float(t["price"]) * 100)) == ("SELL", 1000, 18999)


def test_pav_internalizes_a_resting_limit_it_crosses():
    # vol 3% quadruples risk: long 4,000 puts PAV 2c under the mid, below a resting buy at 189.99
    eng = Engine(Strategy(Config(daily_vol=0.03, bleed_trigger=10**9)), Reporter())
    eng.on_quote(quote(18998, 19002))
    o = Order("O1", D, "C1", "AAPL", "BUY", "LIMIT", 3000, 18999, "DAY")
    eng.on_order(o)                               # below the 190.00 mid: no midpoint offer, rests
    assert o.remaining == 3000
    eng.position = 4000
    eng.on_quote(quote(18998, 19002, sec=1))      # PAV 18998 < limit 18999: internalize at the limit
    assert eng.position == 2000 and o.remaining == 1000   # 2,000 moves PAV exactly to 189.99
    f = eng.rep.fills[-1]
    assert (f["capacity"], f["venue"], f["quantity"], f["_px"]) == ("PRINCIPAL", "INTERNAL", 2000, 18999)
    assert eng.rep.firm_trades == []              # PAV 18999 is above the 189.98 bid: no market trade


def test_pav_never_flips_the_position():
    # the market drops so a resting buy sits above the mid: its PAV size (9,000) would
    # pass flat, and the fill stops at the 1,000 held
    eng = Engine(Strategy(Config(bleed_trigger=10**9)), Reporter())
    eng.on_quote(quote(18998, 19002))
    o = Order("O1", D, "C1", "AAPL", "BUY", "LIMIT", 5000, 18999, "DAY")
    eng.on_order(o)                               # rests below the 190.00 mid
    eng.position = 1000
    eng.on_quote(quote(18996, 19000, sec=1))      # mid 189.98: the 189.99 limit is now above it
    assert eng.position == 0 and o.remaining == 4000


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK: {len(fns)} tests passed")
