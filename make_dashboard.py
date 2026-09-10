"""Build the replay visualization dashboard.

Re-runs the engine (instrumenting OrderBook.add to capture when each order
starts resting), then injects a compact JSON payload into
dashboard_template.html and writes out/dashboard.html.

Usage: python3 make_dashboard.py
"""
from __future__ import annotations

import json
from datetime import datetime

import config
from internalizer.book import OrderBook
from internalizer.data import load_orders, load_quotes
from internalizer.engine import Engine
from internalizer.reporting import Reporter
from internalizer.strategy import Strategy
from main import merge_events


def ms(ts: datetime) -> int:
    """Milliseconds since 09:30 — the dashboard's single time axis."""
    return int((ts - config.SESSION_OPEN).total_seconds() * 1000)


def main() -> None:
    quotes = load_quotes(config.QUOTES_CSV)
    orders = load_orders(config.ORDERS_CSV)

    # instrument the book so the dashboard knows when each order started resting;
    # the engine itself never records this, and patching beats changing the engine
    rest_start: dict[str, int] = {}
    orig_add = OrderBook.add

    def add_logged(self, order):
        rest_start.setdefault(order.order_id, ms(order.ts))
        return orig_add(self, order)

    OrderBook.add = add_logged
    try:
        reporter = Reporter()
        engine = Engine(Strategy(config.STRATEGY), reporter)
        for kind, ev in merge_events(quotes, orders):
            (engine.on_quote if kind == "Q" else engine.on_order)(ev)
        engine.on_close()
    finally:
        OrderBook.add = orig_add        # always restore, even if the replay raised

    fills = [
        [ms(datetime.fromisoformat(f["timestamp"])), f["order_id"], f["client_id"],
         f["side"][0], f["quantity"], f["_px"], f["capacity"][0],
         {"INTERNAL": "I", "CROSS": "C", "MARKET": "M"}[f["venue"]],
         f["_bid"], f["_ask"]]
        for f in reporter.fills
    ]

    # when each rested order left the book: fully filled, or expired at close
    filled_at: dict[str, tuple[int, int]] = {}  # oid -> (cum qty, last fill ms)
    rest_end: dict[str, int] = {}
    for f in fills:
        oid = f[1]
        cum, _ = filled_at.get(oid, (0, 0))
        filled_at[oid] = (cum + f[4], f[0])
    by_id = {o.order_id: o for o in orders}
    for oid, t0 in rest_start.items():
        o = by_id[oid]
        cum, last = filled_at.get(oid, (0, 0))
        rest_end[oid] = last if cum >= o.quantity else 23_400_000   # else: alive to the close

    orders_js = [
        [o.order_id, ms(o.ts), o.client_id, o.side[0], o.order_type[0],
         o.quantity, o.limit, o.tif,
         rest_start.get(o.order_id), rest_end.get(o.order_id)]
        for o in orders
    ]
    firm_js = [
        [ms(datetime.fromisoformat(t["timestamp"])), t["side"][0], t["quantity"],
         round(float(t["price"]) * 100), t["position_after"]]
        for t in reporter.firm_trades
    ]
    quotes_js = [[ms(q.ts), q.bid, q.ask, q.bid_size, q.ask_size] for q in quotes]

    payload = json.dumps(          # arrays not objects: ~3x smaller for 52k quotes
        {"quotes": quotes_js, "orders": orders_js, "fills": fills, "firm": firm_js},
        separators=(",", ":"))

    with open(config.DASHBOARD_TEMPLATE) as f:
        html = f.read()
    out = html.replace("__DATA_JSON__", payload)   # single-file page, no fetch at runtime
    config.DASHBOARD_HTML.parent.mkdir(exist_ok=True)
    with open(config.DASHBOARD_HTML, "w") as f:
        f.write(out)
    print(f"{config.DASHBOARD_HTML} written ({len(out)/1e6:.1f} MB, "
          f"{len(quotes_js)} quotes, {len(fills)} fills, {len(firm_js)} firm trades)")


if __name__ == "__main__":
    main()
