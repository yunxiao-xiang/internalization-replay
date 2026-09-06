# Design Write-up

## Architecture

The system is an event replay over a single merged, time-ordered stream of ~52k
quotes and 500 orders. A quote that shares a timestamp with an order is applied
first, so every decision uses the most recent quote at or before "now"; nothing
ever reads ahead. Concerns are split into five small modules:

- **data** — parsing only (the input files wrap each line in quotes; this is
  absorbed here so nothing downstream knows about it). All prices become
  integer cents immediately: no float comparisons anywhere in the engine.
- **book** — resting client limit orders in price-time priority (a heap per
  side, lazily pruned). It knows nothing about strategy.
- **strategy** — every pricing and risk *decision* (cross windows, principal
  quotes, position limits) as pure functions of (order, position, time, NBBO).
  This is the file to read to understand the business logic, and the only file
  to touch to change it.
- **engine** — execution *mechanics*: the decision waterfall, position and cash
  tracking, and a single `_fill()` chokepoint through which every client fill
  passes. That chokepoint enforces the hard constraints — price within NBBO,
  price consistent with the client's limit — so no strategy bug can produce a
  non-compliant fill; it would crash the run instead.
- **reporting** — fill history, firm blotter, EOD summary.

`validate.py` is a deliberately independent audit: it re-reads the raw CSVs and
the outputs and re-checks NBBO compliance against the quote tape, limit-price
compliance, chronology, over-fills, that firm trades hit the touch, and that
the reconstructed firm position ends flat with P&L matching the summary.

## Internalization strategy

The decision waterfall for every order (and for resting orders re-evaluated on
every quote tick) is: **cross → internalize → route → rest/cancel**.

**Cross first.** A client-to-client cross is riskless for the firm and usually
improves both sides, so it always wins when limits overlap inside the NBBO.
Price: the cent-rounded midpoint, clamped into the window that satisfies both
limits and the NBBO. Crosses are rare on this flow (~1,600 shares) because
marketable orders are handled immediately rather than left to accumulate — a
deliberate choice: holding a marketable client order back to fish for a cross
would trade the client's execution quality for the firm's.

**Internalize when the spread pays for the risk.** With a quoted spread ≥ 2¢,
the firm fills the client at the midpoint rounded a half-cent in the firm's
favor. This guarantees the client ≥ 1¢ of price improvement over the touch
while the firm keeps at least half the spread as edge against its eventual
hedge. At a 1¢ spread there is no room to both improve the client and earn
anything, so new-risk flow routes out instead. The engine therefore
internalizes *more* exactly when spreads widen (the volatile open, the midday
selloff) — which is when the edge is largest — and routes when the market is
tight. One asymmetry: flow that *reduces* existing inventory is always
accepted, at the improved midpoint when the spread allows, otherwise at the
touch (the client does no worse than routing; the firm exits without paying
the spread). Client flow is roughly two-sided, so most inventory exits this
way rather than through hedges (20 hedge trades against 243k internalized
shares).

**The break-even condition, made explicit.** Internalizing a fill is worth it
only when: *expected spread captured from a future offsetting client order >
probability-weighted cost of having to hedge instead (half the spread at exit
+ the expected adverse move while holding)*. Midpoint pricing sets the
first term to roughly zero by construction — each leg trades at mid, so a
netted pair captures only the half-cent rounding, and the hedge term is pure
cost. The day's P&L attribution proves it: of the +$14,396, only **+$160** is
edge versus the contemporaneous midpoint; **+$14,235 is inventory drift** —
the position happening to be short into the midday selloff ($11.4k of it in
the 11:00 hour alone). On a day engineered with a selloff, a pop, and a
closing rally, that sign is luck: the same policy with the flow imbalance
reversed loses a similar amount. What the risk framework actually bounds is
the magnitude — the ±6k band caps exposure at roughly band × move, and the
hard cap, reduce-only close, and forced flatten keep the tail finite.

**Wide spreads cut both ways.** The 7¢ spreads of the midday selloff make
internalizing look most attractive (most spread to capture) exactly when it
is most dangerous: the market is trending against whoever holds inventory,
and exiting costs half of that same wide spread. This engine deliberately
does not condition on the trend — a live trend estimate from the tape is a
prediction, and mispredicting it is worse than not using it — so the
protection is the inventory band, not a directional signal. A production
policy would tighten the band or go reduce-only when the spread is wide *and*
the tape is moving directionally; see the production section.

**Inventory risk.** Two limits: a hard cap of ±10,000 shares that principal
fills are sized against (breach is impossible, the residual routes), and a
soft band of ±6,000; when a fill pushes the position outside it, the firm
immediately reduces back to the band. Reduction prefers resting client limits
priced inside the spread over hedging at the touch — a resting buy at
244.80 with the market 244.78/244.82 is a strictly better exit for a long than
selling at 244.78, and the client gets filled at their limit. Only the
remainder hedges in the market. From 15:55 the firm stops building new
inventory (risk-reducing fills only), and at 16:00 it flattens whatever is
left the same way: resting orders first, then the touch. It ended this day
flat with only 20 hedge trades (21,300 shares) against 243,300 internalized.

**Resting orders.** Non-marketable DAY limits rest and are re-evaluated on
every quote: they cross if the book comes to overlap inside the NBBO, execute
through the same waterfall the moment they become marketable, and expire at
the close (routing an unmarketable limit would violate the client's price, so
expiry is the only compliant disposition). Non-marketable IOC remainders
cancel immediately. Partial fills occur when crossing consumes part of an
order or a principal fill is capped by the position limit; the summary proves
every share of every order is filled, cancelled, or expired.

**What's optimized, and the trade-offs accepted.** The objective is spread
capture net of hedging costs, subject to never disadvantaging a client versus
routing (every internal fill is at or better than the client's routing
alternative) and hard risk bounds. Accepted trade-offs: no attempt to predict
short-term price moves (positions are inventory, not bets — hence the tight
band); no queueing of marketable flow to manufacture crosses; the spread
threshold and bands are fixed constants rather than fitted to volatility.
Simple, explainable rules were preferred over tuned ones throughout.

## Discoveries during implementation

These were found empirically while building against the data — none were known
ahead of time, and none required changing the code structure; each was absorbed
locally in the module that owns it:

- **Input format quirk:** both CSVs wrap every physical line in double quotes
  and use CRLF endings, so a naive `csv.reader` sees one giant field per row.
  Handled entirely inside `data.py`'s loader; nothing downstream is aware.
- **Sub-millisecond quote bursts:** the tape prints multiple quotes within the
  same millisecond. The engine simply applies them in file order and each fill
  records the exact quote it executed against; the validator accepts any quote
  sharing the fill's exact timestamp when checking NBBO consistency.
- **Half-cent midpoints:** odd spreads (3¢, 5¢, 7¢) put the true midpoint on a
  half-cent. This forced an explicit rounding rule — round the half-cent in the
  firm's favor — which still leaves the client ≥ 1¢ of improvement whenever the
  spread is ≥ 2¢. A one-line decision in `strategy.py`.
- **Crosses are structurally rare (~1,600 shares):** because marketable flow is
  executed immediately, opposite-side limits almost never coexist inside the
  spread. This validated the choice not to queue marketable orders to
  manufacture crosses; the waterfall order stayed as designed.
- **Drift, not spread capture, carries the P&L:** attributing each principal
  trade against the contemporaneous midpoint decomposes the +$14,396 into
  +$160 of trade edge and +$14,235 of inventory drift. Midpoint pricing gives
  the client nearly the entire spread, so the realized profit is the position
  happening to sit on the right side of the day's moves — bounded by the risk
  band, but directional. This was only visible after running the attribution;
  it reframed the P&L claim honestly rather than changing the code.
- **The market moving through a resting limit is a compliance trap:** a
  resting buy at 190.00 when the ask drops to 189.95 must fill at ≤ 189.95
  (price improvement), never at its now-stale limit. The design already
  handles this — marketable resting orders re-price through the same waterfall
  (improved midpoint or the new touch) and the `_fill()` chokepoint asserts
  every price against the current NBBO — and `test_engine.py` pins the case
  in both directions.

## Assumptions

- Routed orders and firm hedges fill fully at the touch regardless of
  displayed size (per the prompt).
- Multiple quotes within one millisecond are applied in file order; fills
  record the exact quote they executed against.
- The close uses the last quote of the session as the prevailing NBBO.

## What I'd change for production

- **Multiple symbols / real-time feeds:** replace list replay with a feed
  handler per symbol pushing into per-symbol engine instances behind a single
  event loop; the engine already only consumes an event stream, so the replay
  harness is the only throwaway part. Sequence numbers and gap/staleness
  detection on the quote feed (a stale NBBO makes "compliant" fills
  non-compliant in reality).
- **Risk:** per-symbol and firm-wide dollar limits instead of share limits,
  volatility-scaled soft bands, and a kill switch that flips the strategy to
  route-everything. Hedging via marketable limit orders with slippage
  modeling instead of assumed fills at the touch.
- **Regime-aware internalization:** with enough history to estimate it
  properly, condition on the joint state of spread and short-horizon drift —
  go reduce-only (or shrink the band) when the spread is wide and the tape is
  trending, since that is precisely when incoming flow is adversely selected
  and the hedge exit is most expensive. Not done here: fitting a trend signal
  to one simulated day would be curve-fitting, and the one-day attribution
  shows the band already bounds the damage.
- **Pricing that prices the risk:** midpoint improvement is a client-friendly
  choice, not an economic one — it leaves the firm almost no edge per fill.
  Production pricing would slide between midpoint and the touch based on the
  break-even condition above (expected offset arrival rate vs hedge cost),
  per client tier.
- **Compliance:** the NBBO check should also run post-trade from an
  independent process (as `validate.py` sketches), with clock-sync tolerance
  around quote changes.
- **Persistence and recovery:** fills and position journaled so the engine can
  restart intraday; currently everything is in memory.
- **Strategy:** adverse-selection measurement per client (markouts) to tier
  who gets midpoint versus touch pricing — the single biggest realism gap in
  treating all flow as equally benign.
