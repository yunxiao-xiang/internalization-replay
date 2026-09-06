"""P&L attribution: trade edge vs contemporaneous mid, vs inventory drift.

Total principal P&L = sum(edge vs mid at each trade) + sum(pos * d(mid)).
Also buckets by hour and shows position/spread behavior in the selloff.
"""
import csv
from bisect import bisect_right
from datetime import datetime

import config
from internalizer.data import load_quotes
from internalizer.models import to_cents

quotes = load_quotes(config.QUOTES_CSV)
qts = [q.ts for q in quotes]

trades = []  # (ts, signed_qty_for_firm, px)  firm buys > 0
with open(config.FILLS_CSV) as f:
    for r in csv.DictReader(f):
        if r["capacity"] != "PRINCIPAL":
            continue
        ts = datetime.fromisoformat(r["timestamp"])
        q = int(r["quantity"])
        firm_qty = -q if r["side"] == "BUY" else q   # client buy = firm sells
        trades.append((ts, firm_qty, to_cents(r["price"])))
with open(config.FIRM_TRADES_CSV) as f:
    for r in csv.DictReader(f):
        ts = datetime.fromisoformat(r["timestamp"])
        q = int(r["quantity"])
        firm_qty = q if r["side"] == "BUY" else -q
        trades.append((ts, firm_qty, to_cents(r["price"])))
trades.sort(key=lambda t: t[0])

def mid_at(ts):
    i = bisect_right(qts, ts)
    q = quotes[i - 1]
    return (q.bid + q.ask) / 2

edge = 0.0          # cents*shares: (mid - px) * firm_qty  (buy below mid = +)
drift = 0.0         # cents*shares: pos * change in mid between trades
pos = 0
prev_mid = None
hourly = {}
for ts, fq, px in trades:
    m = mid_at(ts)
    if prev_mid is not None:
        drift += pos * (m - prev_mid)
    e = (m - px) * fq
    edge += e
    h = ts.hour
    hourly.setdefault(h, [0.0, 0.0])
    hourly[h][0] += e
    pos += fq
    prev_mid = m
# attribute drift to hour of the *later* trade for simplicity
prev_mid = None
pos = 0
for ts, fq, px in trades:
    m = mid_at(ts)
    if prev_mid is not None:
        hourly[ts.hour][1] += pos * (m - prev_mid)
    pos += fq
    prev_mid = m

print(f"trade edge vs mid : ${edge/100:>10,.2f}")
print(f"inventory drift   : ${drift/100:>10,.2f}")
print(f"total             : ${(edge+drift)/100:>10,.2f}")
print()
print("hour  edge($)   drift($)   ")
for h in sorted(hourly):
    e, d = hourly[h]
    print(f"{h:02d}   {e/100:>9,.2f} {d/100:>10,.2f}")

# position stats and selloff behavior
print()
maxlong = maxshort = 0
pos = 0
for ts, fq, px in trades:
    pos += fq
    maxlong = max(maxlong, pos); maxshort = min(maxshort, pos)
print(f"max long {maxlong}, max short {maxshort}, trades {len(trades)}")

# spread regime: internalized shares by spread bucket
buck = {}
with open(config.FILLS_CSV) as f:
    for r in csv.DictReader(f):
        if r["venue"] != "INTERNAL":
            continue
        s = to_cents(r["nbbo_ask"]) - to_cents(r["nbbo_bid"])
        buck[s] = buck.get(s, 0) + int(r["quantity"])
print("internalized shares by spread(c):", dict(sorted(buck.items())))
# day shape
print("mid open/low/high/close:",
      (quotes[0].bid+quotes[0].ask)/200,
      min((q.bid+q.ask)/200 for q in quotes),
      max((q.bid+q.ask)/200 for q in quotes),
      (quotes[-1].bid+quotes[-1].ask)/200)
