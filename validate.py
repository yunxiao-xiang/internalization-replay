"""Independent compliance and accounting audit of the engine's outputs.

Re-reads the raw inputs and the produced CSVs (no engine code paths besides
the loaders) and verifies:
  1. Every client fill is priced at or within the prevailing NBBO at fill time,
     and never violates the client's limit price.
  2. The recorded NBBO on each fill matches the quote tape.
  3. Fills are chronological and no share of any order is over-filled.
  4. Reconstructed firm position (principal fills + firm trades) ends at zero,
     and the recomputed principal P&L matches the summary.
  5. Firm trades hit the touch: buys at the ask, sells at the bid.

Usage: python3 validate.py  (after python3 main.py)
"""
from __future__ import annotations

import csv
import sys
from bisect import bisect_right
from datetime import datetime

import config
from internalizer.data import load_orders, load_quotes
from internalizer.models import to_cents


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    quotes = load_quotes(config.QUOTES_CSV)      # re-read the raw tape, not the engine's copy
    orders = {o.order_id: o for o in load_orders(config.ORDERS_CSV)}   # id -> original order
    qts = [q.ts for q in quotes]                 # ascending -> bisect key
    # all quotes sharing an exact timestamp (engine may act on any of them)
    at_ts: dict[datetime, list] = {}
    for q in quotes:
        at_ts.setdefault(q.ts, []).append(q)

    with open(config.FILLS_CSV) as f:
        fills = list(csv.DictReader(f))
    with open(config.FIRM_TRADES_CSV) as f:
        firm = list(csv.DictReader(f))

    position = 0
    cash = 0
    filled: dict[str, int] = {}
    prev_ts = None
    for row in fills:
        ts = datetime.fromisoformat(row["timestamp"])
        px = to_cents(row["price"])
        qty = int(row["quantity"])
        rb, ra = to_cents(row["nbbo_bid"]), to_cents(row["nbbo_ask"])
        o = orders[row["order_id"]]

        if prev_ts and ts < prev_ts:             # replay must never go backwards
            fail(f"{row['fill_id']} out of time order")
        prev_ts = ts

        # 1. price within the recorded NBBO and the client's limit
        if not (rb <= px <= ra):                 # the hard regulatory constraint
            fail(f"{row['fill_id']} price {px} outside recorded NBBO {rb}/{ra}")
        if o.limit is not None:
            if o.side == "BUY" and px > o.limit:
                fail(f"{row['fill_id']} buy above limit")
            if o.side == "SELL" and px < o.limit:
                fail(f"{row['fill_id']} sell below limit")

        # 2. recorded NBBO must be the prevailing quote (the last at or before
        #    the fill time, or any quote sharing its exact timestamp when the
        #    tape prints several in one millisecond)
        i = bisect_right(qts, ts)                # last quote at or before the fill
        if i == 0:
            fail(f"{row['fill_id']} before first quote")
        prevailing = quotes[i - 1]
        # several quotes can share one millisecond; any of them is a legitimate match
        candidates = [prevailing] + at_ts.get(ts, [])
        if not any(q.bid == rb and q.ask == ra for q in candidates):
            fail(f"{row['fill_id']} recorded NBBO {rb}/{ra} not on tape at {ts}")

        # 3. accounting
        filled[o.order_id] = filled.get(o.order_id, 0) + qty
        if filled[o.order_id] > o.quantity:      # more shares printed than the client asked for
            fail(f"{o.order_id} over-filled")

        # 4. firm side of principal fills
        if row["capacity"] == "PRINCIPAL":       # rebuild the firm's book independently
            if o.side == "BUY":                  # client buys -> firm sells
                position -= qty
                cash += qty * px
            else:
                position += qty
                cash -= qty * px

    for row in firm:
        ts = datetime.fromisoformat(row["timestamp"])
        px = to_cents(row["price"])
        qty = int(row["quantity"])
        i = bisect_right(qts, ts)
        prevailing = quotes[i - 1]
        candidates = [prevailing] + at_ts.get(ts, [])
        # 5. firm trades pay the spread: buy at ask, sell at bid
        if row["side"] == "BUY":                 # hedges pay the touch, never inside it
            if not any(q.ask == px for q in candidates):
                fail(f"{row['trade_id']} firm buy not at prevailing ask")
            position += qty
            cash -= qty * px
        else:
            if not any(q.bid == px for q in candidates):
                fail(f"{row['trade_id']} firm sell not at prevailing bid")
            position -= qty
            cash += qty * px

    if position != 0:                            # independent proof the firm ended flat
        fail(f"reconstructed EOD position {position} != 0")

    with open(config.SUMMARY_TXT) as f:
        summary = f.read()
    pnl = f"${cash / 100:,.2f}"                  # recomputed from the CSVs alone
    if pnl not in summary:                       # must agree with what the engine reported
        fail(f"recomputed P&L {pnl} not found in summary")

    cross = [r for r in fills if r["venue"] == "CROSS"]   # every cross prints two legs
    if len(cross) % 2 != 0:
        fail("cross fills must come in pairs")

    print(f"OK: {len(fills)} fills, {len(firm)} firm trades all compliant; "
          f"EOD position 0; P&L {pnl} matches summary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
