<style>
body { font-size: 11pt; line-height: 1.3; }
h1 { font-size: 15pt; font-weight: 700; margin: 0 0 5pt; line-height: 1.2; }
h2 { font-size: 13pt; font-weight: 700; margin: 10pt 0 4pt; line-height: 1.2; }
p { margin: 0 0 8pt; }
table { font-size: 10pt; margin: 4pt 0 8pt; width: auto; }
th, td { padding: 0.5pt 10pt 0.5pt 0; }
ol, ul { margin: 0 0 4pt; padding-left: 14pt; }
li { margin: 0 0 3pt; line-height: 1.3; }
</style>

# Design Write-up

## Architecture

The system replays one merged, time-ordered stream of ~52k quotes and 500
orders. A quote sharing a timestamp with an order is applied first, so every
decision uses the most recent quote at or before "now"; the engine holds only
the single current quote, never the tape, so lookahead is impossible by
construction. All prices are integer cents. Five modules split the concerns:
**data** (parsing), **book** (resting limits in price-time priority, a heap
per side), **strategy** (all pricing and risk decisions), **engine**
(execution, position, cash), **reporting**; paths and risk parameters live in
`config.py`.

Two choices carry compliance. Every client fill passes through one
chokepoint, `engine._fill()`, which checks NBBO bounds and the client's limit
before booking; a strategy bug crashes the replay instead of printing a
violating fill. This covers the trap where the market moves through a resting
limit: the order re-prices to the new touch or better, never its stale limit
(`test_engine.py` pins both directions). Second, `validate.py` independently
re-reads the raw CSVs and outputs, re-verifies every fill against the tape,
reconstructs the position to zero, and recomputes the P&L.

## Internalization strategy

The waterfall, for incoming orders and for resting orders re-evaluated on
every tick, is cross, then internalize, then route. Crosses are riskless and
improve both sides, so they win whenever limits overlap inside the NBBO; they
are rare (1,600 shares) because marketable flow executes immediately rather
than being held to fish for matches at the client's expense.

**Pricing.** Principal fills price at the midpoint rounded a half-cent in the
firm's favor; at 2¢ this is the exact midpoint. The rule that an internalized
client does strictly better than routing is self-imposed (the prompt requires
only at-or-within NBBO); with whole-cent prices it makes 2¢ the minimum
feasible spread for new risk, and the forgone 1¢ flow costs nothing since its
half-cent edge equals the hedge cost. 2¢ is also the volume-maximizing
choice; rerunning the day at 3¢:

| | 2¢ baseline | 3¢ threshold |
|---|---|---|
| Internalized shares | 243,300 | 97,400 (−60%) |
| Routed shares | 232,200 | 378,100 |
| Firm hedges | 20 (21,300 sh) | 10 (14,600 sh) |
| Realized P&L | $14,396 | $13,499 |
| Client price improvement | $2,216 | $1,119 (−49%) |

Raising the threshold forfeits the 2¢ bucket, the day's largest:
internalization drops 60% and client improvement halves while firm P&L
barely moves. Two exceptions price differently: inventory-reducing flow at a
1¢ spread fills at the touch (client matches routing; firm exits without
paying the spread), and the rebalancer fills resting inside-spread limits at
the client's own price, a cheaper exit than hedging.

**Risk.** A hard limit of ±10,000 shares is enforced by sizing: fills are
truncated to remaining capacity, residual routed, so a breach cannot happen.
A soft band of ±6,000 triggers immediate reduction, resting orders first,
market second. From 15:55 the firm is reduce-only; at 16:00 it flattens (this
day it was flat by 13:45 through client flow; the 17 leftover DAY orders
expired, the only disposition that respects their limits). 10k is ~0.02% of
AAPL's ADV and ~1.6% of last-five-minutes volume, so the forced flatten is
always liquid; the limit comes from the risk budget, not liquidity.

**What is optimized, and the honest accounting.** The objective is spread
capture net of hedging costs, subject to never disadvantaging a client versus
routing and to hard risk bounds. Attribution against the contemporaneous mid
splits the day's +$14,396 exactly: +$160 of execution edge (+$545 from client
fills, −$385 of hedge costs at 1.8¢/share) and +$14,236 of inventory drift,
$11.4k of it in the 11:00 selloff hour when the book happened to be short.
Midpoint pricing hands the client nearly the whole spread, so the profit is
the position sitting on the right side of an engineered day: luck, which the
band only bounds. Wide spreads cut both ways, most attractive to internalize
exactly when the market trends and the exit costs half that same spread.
Break-even: internalize only if expected capture from future offsetting flow
exceeds the probability-weighted hedge cost plus expected adverse move.

**Trade-offs accepted.** No trend prediction (fitting a signal to one
simulated day is curve-fitting; the band bounds the damage). No volatility
estimate: one simulated day gives too few data points to measure it without
forward-looking bias or extra assumptions, so thresholds and bands stay
static. One refinement was tested and rejected: unwinding 500 shares
whenever the spread is 1¢ and the book sits at the soft limit fired only 4
times all day, because pinned inventory and tight spreads anti-correlate —
evidence the hedging problem is procyclical and must be solved before the
book is pinned.

## Iterations after submission

Each version replays the same day. Edge and hedge cost are measured against
the contemporaneous mid; drift is P&L minus edge.

| | v0 | v0.1 | v0.2 |
|---|---|---|---|
| Internalized shares | 243,300 | 222,000 | 218,300 |
| Routed shares | 232,200 | 253,500 | 257,200 |
| Market hedges | 20 (21,300 sh) | 0 | 17 (16,300 sh) |
| Client fill edge | $545.5 | $503.5 | $460.5 |
| Hedge cost | −$385.0 | $0.0 | −$81.5 |
| Execution edge | $160.5 | $503.5 | $379.0 |
| Inventory drift | $14,235.5 | $14,235.5 | $14,699.0 |
| Realized P&L | $14,396 | $14,739 | $15,078 |
| Client price improvement | $2,216 | $1,873 | $1,935 |
| Max abs position | 8,600 | 6,000 | 6,000 |
| Minutes above 4,000 sh | 60 | 60 | 18 |
| Minutes at ±6,000 sh | 15 | 15 | 5 |
| σ(drift) | $3.8k | $3.8k | $3.3k |

**v0.1: client improvement rebalanced to the principal book.** v0 sized new
risk to the hard limit, so a fill that pushed the book past ±6,000 was
unwound at the touch in the same event. On those shares the firm filled the
client at the improved price and exited against the same quote, a round trip
that lost exactly the improvement the client received. v0.1 caps new risk at
the room left to the soft limit and routes the rest. On replay the cap binds
on 20 orders and routes 21,300 shares, matching v0's 20 hedges of 21,300
shares. Firm P&L rises $343 ($385 of hedge cost saved, $42 of fill edge given
up) and client improvement falls by the same $343. The position after every
event is identical, so drift stays at $14,236: the gain is a transfer from
clients to the principal book, and those clients still get the touch, the
price routing would give them. The resting-book `coverage` term that shipped
with v0.1 returned zero on all 457 calls, since cross-first execution
consumes every resting order it looks for; removing it leaves the outputs
byte-identical.

**Open problem: no hedges.** v0.1 never trades with the market. Inventory
leaves only through offsetting client flow or the close, so the book carries
exactly v0's risk: 15 minutes pinned at the band, 60 minutes above 4,000
shares, and σ(drift) of about $3.8k (the mid's realized daily σ of $1.50
times a position RMS of 2,557 shares). v0's hedges only undid same-event
overshoot; neither version reduces inventory that sits inside the band. The
rejected 1¢ unwind above shows that waiting until the book is pinned fails,
because pinned inventory and tight spreads rarely coincide. That sets up
v0.2's bleed target: start hedging from a trigger below the band, at the
cheapest spreads or once inventory has aged, before the book is pinned.

**v0.2: bleed target.** Above a 4,000-share trigger the firm cuts inventory
back to the trigger when (A) the spread is 1¢, where a hedge costs half a
cent a share, or (B) the position has stayed above the trigger for 10
minutes, realized evidence that offsetting flow is not coming. Both triggers
read observable state only; one simulated day cannot identify a volatility
or trend signal without lookahead. The trigger sits below the band so that a
cheap window has time to arrive before the book is pinned. On replay trigger
A fired 17 times and cut 16,300 shares, all hedged in the market at a 1¢
spread with nothing taken from the resting book; trigger B never fired and
stays as the backstop for a trend day with no cheap window. The average
hedge cost falls to 0.50¢ a share from v0's 1.81¢, time above 4,000 from 60
to 18 minutes, time at the band from 15 to 5, and σ(drift) by 14%.

**The cost of v0.2.** Execution edge falls $124.5, in two parts. The hedge
premium is $81.5, 16,300 shares at half a 1¢ spread, fixed once a trigger
fires. The other $43 is client fill edge the smaller book gave up: the
reduce branch is capped at the current position, so less inventory leaves
less room for 1¢ reducing flow. The premium is set by the trigger and the
opportunity cost by the day's flow, so the two are reported separately.
P&L rose $339 on drift: bleeding changed the position path and this day
happened to reward it.

## Assumptions

Routed orders and firm hedges fill fully at the touch regardless of displayed
size (per the prompt). Same-millisecond quotes apply in file order; each fill
records the exact quote it executed against. The close uses the session's
last quote as the prevailing NBBO.

## Production changes

1. **Symbols and feeds:** a feed handler per symbol pushing into per-symbol
   engine instances (the engine already consumes only an event stream);
   sequence numbers and staleness detection, since a stale NBBO silently
   invalidates "compliant" fills.
2. **Risk limits:** (1) capacity that shrinks with time remaining: ADV ×
   (remaining/390) × participation (5–10%) — at 5 minutes and 5% this gives
   ~32,000 shares, so 10,000 is conservative by 3×; (2) volatility-targeted
   bands with hysteresis (constant dollar risk; `position_after` pinned at
   ±6,000 shows the static band becoming a directional position in trends);
   (3) dollar limits, MOC for the flatten, a route-everything kill switch.
3. **Hedge execution:** choose between crossing the spread and posting inside
   it — cross only when a vol-based fair-value gap to the touch exceeds a
   threshold; work the rest passively.
4. **Pricing:** sub-penny improvement (makes 1¢ spreads internalizable);
   inventory-adjusted pricing that skews away from midpoint as the book
   fills; per-client markout tiers scaling the 2¢ threshold — treating all
   flow as equally benign is the biggest realism gap.
5. **Costs:** net exchange fees and market impact into internalize-vs-route.
6. **Data:** with tick/depth data, routed fills could walk the book instead
   of filling at the displayed touch.
7. **Plumbing:** explicit OMS state machine with client execution reports,
   journaled fills/position for intraday restart, and `validate.py` run
   post-trade as an independent compliance process.
