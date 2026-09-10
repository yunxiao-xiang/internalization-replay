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
prompted this work. Put your own files at `data/aapl_quotes_20260817.csv` and
`data/client_orders_20260817.csv`; columns resolve by header name, so order does
not matter. `out/dashboard.html` is excluded for the same reason (it embeds the
full quote tape); regenerate it with `python3 make_dashboard.py`.

This branch is the **submitted snapshot**: the v0 strategy with line-by-line
comments. The `main` branch carries the later v0.1–v0.3 experiments.
