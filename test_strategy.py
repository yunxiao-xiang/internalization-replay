"""Unit tests for the pricing/policy layer. Run: python3 test_strategy.py"""
from datetime import datetime, time

from internalizer.models import Order
from internalizer.strategy import Config, Strategy

S = Strategy(Config())
TS = datetime(2026, 8, 17, 10, 0)
LATE = datetime(2026, 8, 17, 15, 56)


def order(side, qty=1000, limit=None, otype=None):
    return Order("T", TS, "C", "AAPL", side, otype or ("LIMIT" if limit else "MARKET"),
                 qty, limit, "DAY")


def test_improved_price():
    # spread 3c: mid 24479.5 -> buy pays 24480 (1c inside ask), sell gets 24479 (1c above bid)
    assert S.improved_price("BUY", 24478, 24481) == 24480
    assert S.improved_price("SELL", 24478, 24481) == 24479
    # spread 2c: both sides get the exact midpoint
    assert S.improved_price("BUY", 24478, 24480) == 24479
    assert S.improved_price("SELL", 24478, 24480) == 24479
    # spread 7c: midpoint pricing splits the wide spread with the client
    assert S.improved_price("BUY", 24478, 24485) == 24482
    assert S.improved_price("SELL", 24478, 24485) == 24481


def test_cross_window_clamps_to_limits_and_nbbo():
    assert S.cross_window(24480, 24479, 24478, 24482) == (24479, 24480)
    assert S.cross_window(None, None, 24478, 24482) == (24478, 24482)
    assert S.cross_window(24478, 24481, 24478, 24482) is None  # limits don't overlap
    assert S.cross_window(24477, None, 24478, 24482) is None   # buy limit below bid


def test_principal_quote_wide_spread_internalizes():
    qty, px = S.principal_quote(order("BUY"), 0, TS, 24478, 24481)
    assert (qty, px) == (1000, 24480)


def test_principal_quote_tight_spread_declines_new_risk():
    qty, _ = S.principal_quote(order("BUY"), 0, TS, 24478, 24479)
    assert qty == 0


def test_principal_quote_tight_spread_reduces_inventory_at_touch():
    # firm long 500, client buys: unwind at the ask, capped at the position
    qty, px = S.principal_quote(order("BUY"), 500, TS, 24478, 24479)
    assert (qty, px) == (500, 24479)


def test_principal_quote_soft_limit_gates_new_risk():
    # v0.1: new risk is sized to room-to-the-soft-limit, not the hard limit
    # firm short 9,500, already past the soft band -> decline entirely
    qty, _ = S.principal_quote(order("BUY"), -9500, TS, 24478, 24481)
    assert qty == 0
    # short 5,500: only the 500 that fits inside the band is internalized
    qty, px = S.principal_quote(order("BUY", 2000), -5500, TS, 24478, 24481)
    assert (qty, px) == (500, 24480)


def test_principal_quote_pinned_at_soft_declines():
    # pinned at -6,000, client BUY 2,000, spread 3c: no room -> route everything
    qty, _ = S.principal_quote(order("BUY", 2000), -6000, TS, 24478, 24481)
    assert qty == 0


def test_principal_quote_no_new_risk_late():
    qty, _ = S.principal_quote(order("BUY"), 0, LATE, 24478, 24481)
    assert qty == 0
    # but risk-reducing flow is still welcome late
    qty, px = S.principal_quote(order("BUY"), 2000, LATE, 24478, 24481)
    assert (qty, px) == (1000, 24480)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK: {len(fns)} tests passed")
