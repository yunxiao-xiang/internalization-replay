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


def test_bleed_target_dual_trigger():
    # below trigger: never fires
    assert S.bleed_target(1, 3000, over_secs=9999) is None
    # above trigger + cheap spread (A): reduce to trigger
    assert S.bleed_target(1, 5000, over_secs=None) == 4000
    assert S.bleed_target(1, -5000, over_secs=None) == 4000
    # above trigger + wide spread + young position: hold
    assert S.bleed_target(3, 5000, over_secs=120) is None
    # above trigger + wide spread + aged past 10 min (B): reduce anyway
    assert S.bleed_target(3, 5000, over_secs=600) == 4000


def close(a, b):
    return abs(a - b) < 1e-9


def test_risk_value_d1_normalization():
    # v0.4: at the 1.5% baseline, 4,000 shares shift PAV by 0.5c; risk scales with vol squared
    assert close(S.risk_value(), 0.5 / 4000)
    assert close(Strategy(Config(daily_vol=0.03)).risk_value(), 4 * 0.5 / 4000)


def test_pav_shifts_against_inventory():
    # NBBO 244.79/244.80, mid 244.795: long 4,000 -> PAV at the bid, short 4,000 -> at the ask
    assert close(S.pav(24479, 24480, 4000), 24479)
    assert close(S.pav(24479, 24480, -4000), 24480)
    assert close(S.pav(24479, 24480, 0), 24479.5)


def test_pav_qty_moves_pav_to_the_price_and_never_flips():
    # long 6,000 in a 1c market: PAV 24478.75 sits 0.25c under the bid -> sell 2,000
    pav = S.pav(24479, 24480, 6000)
    assert S.pav_qty(24479, pav, 6000) == 2000
    assert close(S.pav(24479, 24480, 6000 - 2000), 24479)   # PAV lands exactly on the bid
    # short 6,000: symmetric, buy 2,000 at the ask
    assert S.pav_qty(24480, S.pav(24479, 24480, -6000), -6000) == 2000
    # a price 1.25c above PAV would size at 10,000: capped at the 6,000 held
    pav = S.pav(24478, 24481, 6000)                          # 24479.5 - 0.75
    assert S.pav_qty(24480, pav, 6000) == 6000
    # a price on the wrong side of PAV: nothing
    assert S.pav_qty(24478, pav, 6000) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK: {len(fns)} tests passed")
