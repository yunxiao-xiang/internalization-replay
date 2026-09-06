# Interview Q&A — annotated with function calls

## Q1: A client market order arrives at 10:15. Walk through it end to end.

Scenario: market BUY 2,000 shares · NBBO 3¢ wide (bid B / ask B+3) · firm
position −5,000 (short).

### Step 0 — the order gets its turn (`main.merge_events`)

Quotes and orders are one merged stream; the `<=` tie-break dispatches every
quote with ts ≤ 10:15 **before** this order. By the time `engine.on_order(o)`
is called, `engine.quote` is the prevailing NBBO and nothing after 10:15 has
been read (no lookahead by construction — the engine never holds the tape).

### Step 1 — cross first (`Engine._try_cross` → `Strategy.cross_window`)

Loop over the best opposite side of the internal book (`book.best_sell()`).
For each candidate, `cross_window(buy_limit=None, sell_limit, bid, ask)`
computes the price range satisfying both clients **and** the NBBO; if
non-empty, both legs fill at the clamped midpoint (`cross_price`), two
AGENCY/CROSS rows via `_fill()`. Assume no overlap here → 2,000 remain.

### Step 2 — marketability (`Engine._marketable`)

`order_type == "MARKET"` → always marketable (a market order has no limit;
the limit check applies only to LIMIT orders). Enter `_execute_marketable`.

### Step 3 — principal decision (`Strategy.principal_quote(order, position=−5000, ts, bid, ask)`)

All four inputs matter:
- spread = 3 ≥ `min_internalize_spread` (2) ✓
- ts < `no_new_risk_after` (15:55) ✓
- **direction**: client BUY ⇒ firm SELLS ⇒ position goes MORE short.
  This is *new risk* (branch A), not inventory-reducing — reduces would need
  `position > 0` for a BUY.
- **capacity check**: cap = position + hard = −5,000 + 10,000 = 5,000 ≥ 2,000
  ⇒ internalize in full. (At −8,500 it would fill 1,500 and route 500 —
  the hard limit is enforced by sizing, so a breach cannot happen.)

Price = `improved_price("BUY", bid, ask)` = ceil(mid) = B+2 = **ask − 1¢**
(client saves 1¢ vs routing; odd spread → the half-cent rounds to the firm).

### Step 4 — compliance chokepoint (`Engine._fill`)

Every fill passes here before booking:
- `bid ≤ px ≤ ask` (else `ComplianceError`, run dies)
- buy px ≤ client limit / sell px ≥ client limit (limit orders)
- `0 < qty ≤ remaining`
Then bookkeeping: `position −= 2,000 → −7,000`, `cash += 2,000 × px`, and
`Reporter.record_fill(...)` writes the row with `nbbo_bid/nbbo_ask` columns
(self-documenting compliance evidence).

### Step 5 — post-trade risk check (`Engine._rebalance(ts, soft=6,000)`)

|−7,000| > 6,000 ⇒ reduce 1,000, in cost order:
1. **Resting book first**: firm is short, needs to BUY → consume resting
   SELL limits with `limit ≤ ask` at the client's own price
   (`_fill(..., PRINCIPAL, INTERNAL)`) — a cheaper cover than the ask, and a
   fill that client wasn't otherwise getting.
2. **Market for the remainder**: `_firm_trade(ts, "BUY", residual, ask)` —
   buy at the ask (side and price matter: covering a short, paying the
   touch), recorded in the firm blotter with `position_after = −6,000`.

### One-sentence close

"2,000 shares fill PRINCIPAL/INTERNAL at ask−1¢ through the `_fill()`
assertion, position −5,000 → −7,000 breaches the soft band, `_rebalance`
covers 1,000 via resting sells then BUY @ask, ending at −6,000 — every row in
`fills.csv` / `firm_trades.csv` via `Reporter`."

### Gotchas the interviewer will probe

1. **Sign of the position**: client BUY deepens a short (−5,000 → −7,000).
   Saying "7,000" without the sign reads as a direction error.
2. **Capacity check before internalizing** — the hard limit is a sizing rule,
   not an after-the-fact alarm.
3. **Rebalance is resting-book-first**, market second; the hedge has a side
   and a price (BUY @ask), not just "execute in market".
4. **Market orders have no limit price** — marketability is trivially true;
   don't reach for a limit check.

### Full call trace

```
merge_events                      # quotes ≤ 10:15 already applied
└─ Engine.on_order(o)
   ├─ Engine._try_cross(o)        # book.best_sell / Strategy.cross_window/cross_price
   │    └─ Engine._fill ×2        #   (if overlap: AGENCY/CROSS both legs)
   ├─ Engine._marketable(o)       # MARKET → True
   ├─ Engine._execute_marketable(o)
   │    ├─ Strategy.principal_quote(o, −5000, ts, bid, ask) → (2000, ask−1¢)
   │    ├─ Engine._fill(PRINCIPAL, INTERNAL)   # NBBO+limit asserts, pos −7,000
   │    │    └─ Reporter.record_fill
   │    └─ (residual would route: _fill(AGENCY, MARKET) @ask)
   └─ Engine._rebalance(ts, 6000)
        ├─ book.best_sell → _fill(PRINCIPAL, INTERNAL) @client limit  # cheaper cover
        └─ Engine._firm_trade("BUY", residual, ask)                   # → pos −6,000
             └─ Reporter.record_firm_trade
```
