"""Fill history, firm trade blotter, and end-of-day summary."""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime

from .models import Order, Quote, fmt_price, fmt_ts

FILL_COLUMNS = ["fill_id", "timestamp", "order_id", "client_id", "side", "quantity",
                "price", "capacity", "venue", "nbbo_bid", "nbbo_ask"]
FIRM_COLUMNS = ["trade_id", "timestamp", "side", "quantity", "price",
                "position_after", "nbbo_bid", "nbbo_ask"]


class Reporter:
    def __init__(self):
        self.fills: list[dict] = []                 # client fills, in execution order
        self.firm_trades: list[dict] = []            # the firm's own hedges, kept separate
        self.closes: list[tuple[Order, int]] = []   # (order, unfilled shares)
        self._fill_seq = 0                           # F-00001, F-00002, ...
        self._trade_seq = 0                          # H-0001, H-0002, ...

    def record_fill(self, ts: datetime, order: Order, qty: int, px: int,
                    capacity: str, venue: str, q: Quote) -> None:
        """One row per client fill; the NBBO is stamped on it as evidence."""
        self._fill_seq += 1
        self.fills.append({
            "fill_id": f"F-{self._fill_seq:05d}",
            "timestamp": fmt_ts(ts),
            "order_id": order.order_id,
            "client_id": order.client_id,
            "side": order.side,
            "quantity": qty,
            "price": fmt_price(px),
            "capacity": capacity,
            "venue": venue,
            "nbbo_bid": fmt_price(q.bid),            # compliance evidence on every row
            "nbbo_ask": fmt_price(q.ask),
            "_px": px, "_bid": q.bid, "_ask": q.ask,  # underscore keys: cents, not written to CSV
        })

    def record_firm_trade(self, ts: datetime, side: str, qty: int, px: int,
                          position_after: int, q: Quote) -> None:
        """The firm's own hedges: a separate blotter, never mixed with client fills."""
        self._trade_seq += 1
        self.firm_trades.append({
            "trade_id": f"H-{self._trade_seq:04d}",
            "timestamp": fmt_ts(ts),
            "side": side,
            "quantity": qty,
            "price": fmt_price(px),
            "position_after": position_after,
            "nbbo_bid": fmt_price(q.bid),
            "nbbo_ask": fmt_price(q.ask),
        })

    def record_close(self, order: Order, leaves: int) -> None:
        """Terminal state of an order; leaves = shares that never traded."""
        self.closes.append((order, leaves))

    # ---------- outputs ----------
    def write_outputs(self, fills_csv, firm_csv, summary_txt,
                      position: int, cash: int, orders: list[Order]) -> str:
        """Write the three output files; paths come from the caller (config.py)."""
        for p in (fills_csv, firm_csv, summary_txt):
            os.makedirs(os.path.dirname(os.fspath(p)) or ".", exist_ok=True)   # out/ may not exist
        with open(fills_csv, "w", newline="") as f:
            w = csv.DictWriter(f, FILL_COLUMNS, extrasaction="ignore")   # drops the _px/_bid keys
            w.writeheader()
            w.writerows(self.fills)
        with open(firm_csv, "w", newline="") as f:
            w = csv.DictWriter(f, FIRM_COLUMNS)
            w.writeheader()
            w.writerows(self.firm_trades)
        summary = self.summary(position, cash, orders)
        with open(summary_txt, "w") as f:
            f.write(summary)
        return summary

    def summary(self, position: int, cash: int, orders: list[Order]) -> str:
        shares = defaultdict(int)                    # (capacity, venue) -> shares
        improvement = 0  # cents * shares, client fills vs the same-side touch
        filled_qty: dict[str, int] = defaultdict(int) # per order, to classify dispositions
        for f in self.fills:
            shares[(f["capacity"], f["venue"])] += f["quantity"]
            filled_qty[f["order_id"]] += f["quantity"]
            if f["venue"] in ("INTERNAL", "CROSS"):   # routed fills pay the touch: zero improvement
                if f["side"] == "BUY":
                    improvement += (f["_ask"] - f["_px"]) * f["quantity"]
                else:
                    improvement += (f["_px"] - f["_bid"]) * f["quantity"]

        disposition = defaultdict(int)   # order counts
        unfilled_shares = defaultdict(int)
        for o in orders:
            done = filled_qty.get(o.order_id, 0)
            assert done + (o.quantity - done) == o.quantity
            if o.closed_as is None:                  # never cancelled or expired
                assert done == o.quantity, f"open order at EOD: {o.order_id}"
                disposition["FILLED"] += 1
            else:
                key = o.closed_as + ("_PARTIAL_FILL" if done else "")
                disposition[key] += 1
        for o, leaves in self.closes:
            unfilled_shares[o.closed_as] += leaves   # split EXPIRED vs CANCELLED

        total_order_qty = sum(o.quantity for o in orders)   # the accounting identity below
        total_filled = sum(filled_qty.values())
        total_unfilled = sum(unfilled_shares.values())
        firm_qty = sum(t["quantity"] for t in self.firm_trades)

        lines = [
            "END-OF-DAY SUMMARY — AAPL 2026-08-17",
            "=" * 50,
            f"Client orders:                {len(orders)}",
            f"Client order shares:          {total_order_qty:,}",
            "",
            "Client shares filled by capacity/venue:",
        ]
        for (cap, venue), q in sorted(shares.items()):
            lines.append(f"  {cap:<10} {venue:<9} {q:>10,}")
        lines += [
            f"  {'TOTAL':<20} {total_filled:>10,}",
            "",
            "Order dispositions:",
        ]
        for k, v in sorted(disposition.items()):
            lines.append(f"  {k:<28} {v:>5}")
        lines += [
            "",
            f"Shares expired (DAY):         {unfilled_shares.get('EXPIRED', 0):,}",
            f"Shares cancelled (IOC):       {unfilled_shares.get('CANCELLED', 0):,}",
            f"Accounting check:             filled {total_filled:,} + unfilled {total_unfilled:,} "
            f"= {total_filled + total_unfilled:,} of {total_order_qty:,}",
            "",
            f"Firm hedge/unwind trades:     {len(self.firm_trades)} ({firm_qty:,} shares)",
            f"Firm end-of-day position:     {position} shares",
            f"Firm realized principal P&L:  ${cash / 100:,.2f}",
            f"Client price improvement:     ${improvement / 100:,.2f} "
            "(internal + cross fills vs the touch)",
        ]
        return "\n".join(lines) + "\n"
