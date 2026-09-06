# Design Write-up

## Architecture

The system replays one merged, time-ordered stream of ~52k quotes and 500
orders. A quote sharing a timestamp with an order is applied first, so every
decision uses the most recent quote at or before "now". The engine holds only
the single current quote and never sees the tape, which makes lookahead
impossible by construction. All prices are integer cents; no float
comparisons exist anywhere.

Five small modules split the concerns: **data** (parsing only; the input
files wrap each line in quotes, and nothing downstream knows), **book**
(resting client limits in price-time priority, a lazily-pruned heap per
side), **strategy** (every pricing and risk decision, as pure functions of
order, position, time, and NBBO), **engine** (execution mechanics, position,
cash), and **reporting**. All I/O paths and risk parameters live in
`config.py`.

Two choices carry the compliance story. Every client fill passes through a
single chokepoint, `engine._fill()`, which checks NBBO bounds and the
client's limit before booking. Decisions and constraints are separated, so a
strategy bug crashes the replay instead of printing a violating fill. This
covers the classic trap where the market moves through a resting limit: the
order re-prices to the new touch or better, never its stale limit, and
`test_engine.py` pins both directions. The second is `validate.py`, an
independent audit that re-reads the raw CSVs and outputs, re-verifies every
fill against the quote tape, reconstructs the firm's position to zero, and
recomputes the P&L.

## Internalization strategy

The waterfall for every order, and for resting orders re-evaluated on every
tick, is cross, then internalize, then route. Crosses are riskless and
improve both sides, so they always win when limits overlap inside the NBBO.
They are rare (1,600 shares) because marketable flow executes immediately
rather than being held back to manufacture matches; holding a client's order
to fish for a cross trades their execution quality for the firm's.

**Pricing.** Principal fills price at the midpoint rounded a half-cent in the
firm's favor; at a 2¢ spread this is the exact midpoint. The rule that an
internalized client always does strictly better than routing is a
self-imposed best-execution principle (the prompt requires only at-or-within
NBBO), and with whole-cent prices it makes 2¢ the minimum feasible spread for
new risk. The principle costs nothing: at 1¢ the firm's half-cent edge at the
touch equals its hedge cost, so that flow is zero-expectation anyway. Two
exceptions fill at other prices. Inventory-reducing flow at a 1¢ spread fills
at the touch, where the client matches routing and the firm exits without
paying the spread. The rebalancer fills resting inside-spread limits at the
client's own price, a cheaper exit than hedging at the touch and a fill the
client wasn't otherwise getting.

**Risk.** A hard limit of ±10,000 shares is enforced by sizing: every
principal fill is truncated to remaining capacity and the residual routes, so
a breach cannot happen. A soft band of ±6,000 triggers immediate reduction,
resting orders first, market second. From 15:55 the firm is reduce-only; at
16:00 it flattens. This day it was already flat by 13:45 through client flow,
and the 17 leftover DAY orders expired. Routing a non-marketable limit would
violate the client's price, so expiry is the only compliant disposition. On
sizing: 10k shares is about 0.02% of AAPL's real-world ADV and 1.6% of
typical last-five-minutes volume, so the forced flatten is always liquid. The
limit itself comes from the risk budget rather than liquidity.

**What is optimized, and the honest accounting.** The objective is spread
capture net of hedging costs, subject to never disadvantaging a client versus
routing and to hard risk bounds. Attribution against the contemporaneous mid
decomposes the day's +$14,396 exactly: +$160 of execution edge (+$545 from
client fills through odd-spread rounding and touch exits, minus $385 of
hedge-crossing costs at 1.8¢/share) and +$14,236 of inventory drift, $11.4k
of it in the 11:00 selloff hour when the book happened to be short. Midpoint
pricing hands the client nearly the whole spread, so the realized profit is
the position sitting on the right side of an engineered day. That is luck.
The band only bounds it. The same trap runs the other way: wide spreads make
internalizing look most attractive exactly when the market is trending and
the exit costs half of that same wide spread. The break-even condition is
explicit: internalize only if expected capture from future offsetting flow
exceeds the probability-weighted hedge cost plus the expected adverse move
while holding.

**Trade-offs accepted.** No trend prediction, since fitting a signal to one
simulated day is curve-fitting; the band bounds the damage instead. Static
thresholds and limits. The 1¢ new-risk flow forgone. One refinement was
tested and rejected: unwinding 500 shares whenever the spread is 1¢ and the
book sits pinned at the soft limit fired only 4 times all day, because pinned
inventory and tight spreads anti-correlate. That is direct evidence the
hedging problem is procyclical and has to be solved before the book is
pinned.

## Assumptions

Routed orders and firm hedges fill fully at the touch regardless of displayed
size (per the prompt). Multiple quotes within a millisecond apply in file
order; each fill records the exact quote it executed against. The close uses
the last quote of the session as the prevailing NBBO.

## Production changes

**Symbols and feeds:** a feed handler per symbol pushing into per-symbol
engine instances; the engine already consumes only an event stream, so the
replay harness is the disposable part. Sequence numbers and staleness
detection on quotes, since a stale NBBO silently invalidates "compliant"
fills. **Risk:** dollar limits, volatility-targeted soft bands with
hysteresis (constant dollar risk instead of constant shares; the hedge
blotter's `position_after` column pinned at ±6,000 shows the static band
degenerating into a full-band directional position during trends), and
position limits that shrink with time remaining: capacity ≈ ADV × (remaining
/ 390) × participation rate (5–10%). At 5 minutes and 5% participation that
formula gives ~32,000 shares, so the hard-coded 10,000 is conservative by 3×.
MOC orders for the flatten, and a kill switch to route-everything.
**Hedge execution:** the model assumes hedges fill fully at the touch; in
production the firm chooses between crossing the spread and posting inside
it. Compare a vol-based fair-value estimate against the touch and cross only
when the gap exceeds a threshold, working the rest passively. **Pricing:**
sub-penny price improvement (making 1¢ spreads internalizable);
inventory-risk-adjusted principal pricing that skews the fill price away from
firm-favorable midpoint as the book fills up; and per-client markout tiers, a
toxicity multiplier on the 2¢ spread threshold per client. Treating all flow
as equally benign is the biggest realism gap. **Costs:** internalize-vs-route
decisions should net out exchange fees and market impact, both ignored here.
**Data:** with full tick and depth data instead of L1 snapshots, routed fills
could walk the book realistically rather than filling at the displayed touch
(unneeded here by the prompt's assumptions). **Plumbing:** explicit OMS
order-state machine with client execution reports, journaled fills and
position for intraday restart, and the `validate.py` audit run post-trade as
an independent compliance process.
