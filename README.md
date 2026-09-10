# Equity Order Internalization Engine — AAPL, 2026-08-17

Python 3.10+, standard library only.

## Run

```bash
python3 main.py                 # reads data/*.csv, writes out/
python3 validate.py             # independent audit of the outputs
python3 test_strategy.py        # unit tests for the pricing/policy layer
```

`main.py` accepts explicit paths: `python3 main.py <quotes.csv> <orders.csv> <out_dir>`.

## Outputs (in `out/`)

- `fills.csv` — one row per client fill, with capacity, venue, and the prevailing NBBO at fill time.
- `firm_trades.csv` — the firm's own hedge/unwind trades (not client fills).
- `summary.txt` — end-of-day summary: shares by capacity/venue, order dispositions, share accounting, EOD position (zero), realized principal P&L, client price improvement.

## Layout

```
internalizer/
  data.py        CSV loaders (handles the quoted-line/CRLF format of the inputs)
  models.py      Quote / Order dataclasses; integer-cent price handling
  book.py        resting limit orders, price-time priority per side
  strategy.py    all policy: crossing, principal pricing, risk limits
  engine.py      event replay, execution mechanics, position & cash
  reporting.py   fill history, firm blotter, EOD summary
main.py          entry point: merge quotes+orders in time order, run, report
validate.py      re-audits outputs from the raw CSVs (compliance + accounting)
test_strategy.py unit tests for the policy layer
```

See `DESIGN.md` for the architecture and internalization strategy write-up.

## Data

The two input CSVs are not in this repository — they belong to the exercise that
prompted this work. To run it, put your own files at:

```
data/aapl_quotes_20260817.csv    timestamp,symbol,bid_price,bid_size,ask_price,ask_size
data/client_orders_20260817.csv  order_id,timestamp,client_id,symbol,side,order_type,quantity,limit_price,tif
```

Columns resolve by header name, so column order does not matter, and both
whole-line-quoted and standard CSV quoting are handled. `out/dashboard.html` is
also excluded because it embeds the full quote tape; regenerate it with
`python3 make_dashboard.py` once your data is in place.

## Versions

| tag | change |
|---|---|
| `v0` | baseline: new risk sized against the hard limit; soft-band breaches hedged at the touch |
| `v0.1-scan_resting_book` | new risk sized to room-to-the-soft-limit; the residual routes |
| `v0.2-bleed_hedging` | proactive bleed to 4,000 sh on 1c spreads or 10-minute aged inventory |
| `v0.3-midpoint_offer` | inside-the-spread limits internalized when the improved price meets their limit |

`test.ipynb` replays all four and compares P&L attribution, hedging, and exposure.
