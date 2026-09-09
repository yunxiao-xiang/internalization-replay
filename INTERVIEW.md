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

---

## Q1-追问: 为什么允许在 −5,000 空头时继续接 2,000 股同向新风险？3¢ 够吗？为什么不把软限当 internalize 上限？

**结合后的答案**（我的要点 + 修正）：

- 机制层（我的回答，正确但只是描述）：−5,000 未触软限、硬限容量足够，所以能接。
- 设计层（必须补的一层）：**软限从来不是接单闸门，是事后目标带**——sizing 只看硬限，
  "先接单后管理" vs "在门口拒单"才是面试官要的对比。
- 3¢ 够不够：策略里确实没定价这件事（诚实承认✓）；生产答案是 vol + fair-value-vs-mid
  决定意愿，用 **skew quoting** 从 50-50 mid 向公司侧偏移拿 edge，约束 = 不出
  bid/ask/客户限价（否则 agency 严格占优）。
- 埋着的经济学陷阱：超软限的边际 1,000 股若立即市场对冲——edge 0.5¢ < 对冲 1.5¢，
  净 −$10，不如 route。辩护三层：rebalance 先吃 resting book（成本≤0）；netting 消化
  为主（当天仅 20 笔市场对冲）；真正的修法是 inventory-skewed pricing 把 1.5¢ 边际
  对冲成本定价进报价，让不划算的流量被价格劝退，而非数量一刀切。

**英文背诵版**："The soft limit is deliberately a target band, not a gate... on the
shares that push past the band I capture ~0.5c of edge while hedging at the touch
costs 1.5c — negative expectancy if hedged immediately. Three defenses: the
rebalancer exits through resting client limits first; two-sided flow nets most
inventory off for free (twenty market hedges all day); and the real fix is price,
not quantity — skew the quote with inventory, constrained never to go outside the
NBBO or through the client's limit, so marginally unprofitable flow prices itself
out instead of a hard cutoff rejecting profitable flow along with it."

## Q1-追问 2: 钉在 −6,000 时来 BUY 2,000 还接吗？scan-resting-book 提议 / 软限会否变硬限？

- 代码答案：接，全额——`principal_quote` 不读软限，cap = −6,000+10,000 = 4,000 ≥ 2,000；
  成交后 −8,000，rebalance 立即削回。这是 EV 最薄的动作（赌削仓走 resting/netting）。
- 我的提议（被采纳为 v0.1）：先 scan resting book，internalize qty = room-to-soft +
  **有利可图的** coverage（resting sell limit ≤ 成交价），其余 route——"直接接再 rebalance
  的 net benefit 不变、client 吃掉 firm P&L" 的判断数学上成立（同 tick 零和转移 1¢/股）。
- 实证：v0→v0.1 内部化 −21,300 股 = v0 对冲股数（精确对账）；对冲 20→0 笔；
  P&L +$343 = 客户改善 −$343。
- 软限变硬限？模拟器"即时对冲"假设下：是，10k 沦为摆设；生产里对冲需要时间，
  6k–10k 是 hedge-in-flight 工作区，硬限重新必要——同一参数在不同假设下语义会变。

## Q2: 生产化（500 symbol、真实 feed）——第一个断掉的是什么？

**结合后的答案**（我的三点 + 修正与补充）：

- 我的三点：① per-symbol 参数（6k/10k 是 AAPL 专属，应 ADV/vol 驱动 notional limit）——
  对但属参数问题排不到第一；② infinite-size-at-touch 假设坍塌——对，且连带击穿收盘
  必平保证和 bleed 成本模型；③ 每 tick 的 ToB 更新——方向对但 **heapq 是 O(log n)
  不是 O(1)**（peek 才 O(1)），且真正的热点不在这。
- 第一个断的（漏答）：**`book.coverage()` 的 O(N) 线性扫描**，每次 marketable 执行
  调一次——500 symbol × 千级 resting × 万级 tick/s 的第一个 CPU 悬崖。修法：传 cap
  early-exit（一行）；根治用价格档聚合/Fenwick O(log P)。
- 架构上第一个断的假设：**事件处理零耗时**——单线程同步循环积压 → NBBO 陈旧 →
  "合规"成交变违规。
- Keep / throw：保留 strategy（纯函数）、book、engine 机制（每 symbol 一实例，
  零共享状态，多进程绕 GIL）；扔 data.py 整表加载和 merge_events（回放专用），
  Reporter 改异步 journal。

**追问三连（我的问题 + 确认的答案）**：
- sweep 用 peek：堆顶最优单大概率不可执行必须留在书里，消耗只走 _fill 扣量 +
  懒删除；安静 tick 摊还 O(1)，全天 O(M log M) 与 tick 数解耦。
- coverage 时机：每次 marketable 执行（非每 tick）；early-exit 要传 cap（heap 底层
  list 无序，不能按价升序白嫖）；v0 不用 coverage 确实省调用——但正解是
  "先用 O(N) 买正确性，再用聚合结构买回成本"。
- Reporter 异步：有界队列 + 独立 writer 批量落盘（WAL + seq no，重启重放恢复）；
  quote 用 **conflation**（1-slot mailbox，latest wins，决策对 quote 是 Markov 的所以
  合法）；**order 一条不能丢**——双通道：quote conflated、order 无损队列优先。

## Q3: validate.py 证明了什么、抓不到什么？三类全绿放行的 bug

**模范答案**（本题未作答）：

它证明"所有已发生的成交合法且自洽"——审计 commission，且假设解析正确。三类盲区：

1. **不作为之罪**：删掉 route 残量的两行 / sweep 的 >= 写成 > ——该成交的没成交，
   已记录的行全部合规 → 全绿。修法：完备性不变量审计（独立 oracle 重放
   "到达时 marketable 必须全量成交"）。
2. **共模失效**：validator 与引擎共享 data.py——loader 把 bid/ask 读反，两边错得
   一致 → 全绿。真独立要求从字节开始的第二套解析。
3. **合法但错误**：improved_price 改成恒 at ask（客户零改善）/ reduces 符号写反
   （重仓方向加仓）/ IOC 忘取消稍后合法成交——全部在 NBBO 内 → 全绿。
   合规边界 ≠ 策略正确性 ≠ 客户指令忠实执行。

**收尾句**：validate.py 证明"没有一笔违法成交"，对漏做的事、共享代码的共同错觉、
合法区间内的错误决策三者天然失明——防线的价值恰恰在于知道每道防线不防什么。

## Q4: Cross 定价——resting SELL 244.79 × incoming BUY 244.83，NBBO 244.78/244.82

**结合后的答案**（我的回答 + 修正）：

1. **成交价 = 244.80，不是我算的 244.81**：`cross_price` 锚定 **NBBO mid**
   `(bid+ask)//2 = 244.80`，不是两个限价的 mid；窗口 lo = max(bid, 244.79) = 244.79，
   hi = min(**ask**, 244.83) = 244.82（上界被 ask 卡住不是买方限价）。244.80 在窗口内，
   免 clamp。教训：报数字前先跑一遍自己的代码。
2. 改善分配：vs touch 双方各 +2¢（对称）——锚定市场公允价平均分，刻意无视先来后到
   与限价激进度；限价只在 clamp 时起作用。
3. 行为分析（我答"maker 不会变"，漏了本质）：**我们的 cross 就是 midpoint dark pool**。
   交易所惯例 maker 只拿自己的限价（改善全归 taker）；midpoint 规则给 maker 优于
   限价的价——对 maker 慷慨，代价由 taker 让渡，公平性无碍。真正的扭曲：卖方限价
   ≤ mid 时收到的都是 mid → **激进化报价免费**（提高被 cross 概率、不损价格）→
   理性 maker 压价到 mid 附近；随之而来 dark pool 的已知弊病：**quote fade / NBBO mid
   操纵**可以移动内部 cross 成交价。我答对的部分：更 passive（卖方抬价）降低 fill
   likelihood；MM 在交易所同样无 improvement、只有 fee 差异。

**收尾句**：midpoint cross 的公平性没问题——它比交易所惯例对 maker 更慷慨；
要审视的是激励（免费激进化）与可操纵性（mid 依赖），这正是真实 midpoint venue
挂 anti-gaming 逻辑的原因。

## Q5: Desk head 问"明天的期望 P&L 是多少？数、区间、置信来源"

**结合后的答案**（我的洞察 + 补上的报数纪律）：

- **我的亮点（保留）**：库存方向不是巧合——仓位 ≡ −净客户流，客户流是市场压力
  缩影，策略结构性站在压力对面；压力冲击若暂时则 drift 期望为正。**引用自己的
  markout 证据**：internalized flow 10min markout −4.25¢/股（价格向客户反方向回归）。
- **模范报数**：
  - Edge：E ≈ +$400/天，σ ±$150（今天 $379 = 客户侧 +$460 − 对冲 $82；驱动 =
    volume × spread 分布，日间稳定）——策略的"工资"；
  - Drift：E ≈ 0 到小正（martingale 下 0；uninformed-flow 下为正，markout 支持但
    n=1 不许入账）；σ ≈ 时间加权 |pos| 1,456 × 日内路径 $2–3 ≈ ±$3–6k；
    尾部被 band 封顶 ≈ ±$15k；
  - **合计：E ≈ +$400，1σ ±$5k，尾部 ±$15k；今天 +$15k 是一次 +3σ 实现。**
- **口径纠错**：对 mid 的 edge 是 0–0.5¢/股（+touch 半点差），"1–3¢"是对 touch 的
  客户改善口径——别混。
- **Run/no-run（别答成改进清单）**：跑——edge 期望独立为正、drift 尾部有界；
  预算与考核锚定 edge（~$400/天），drift 记噪声；优先级 = 压缩 drift 方差
  （skew、vol-band），并积累多日 markout，显著后才把回归溢价计入期望。

**模板**：E(edge) + E(drift) ± σ(drift)，尾部 = band × 极端路径，最后一句 run/no-run。

## Q5-补充: σ(drift) 的推导与精确计算

**公式**：drift = ∫pos·dP；鞅 + 仓位与未来增量独立的假设下
σ(drift) = σ_P(daily, RV口径) × pos_RMS，其中 pos_RMS = √(∫pos²dt / T)。

**今天的数据（v0.2）**：
- σ_P：tape 逐 tick mid 增量平方和开根（realized variance）= $1.50/天；
- pos_RMS = 2,203 股（高于时间加权平均 |pos| 1,456——RMS 加重 4–6k 时段）；
- **σ(drift) = $3,311**。我口算的 ±$5k 用了 1%×$244≈$2.5 的年化口径猜日波动，
  RV 口径只有 $1.50（当天 $3.2 高低差里有趋势段，非扩散）→ 报数应用 RV 口径。

**关键读数**：今天 drift $14,699 = **4.4σ**。两种解释并列：
1. 真尾部（被设计的日子，band 钉满恰逢单边）；
2. 公式的独立性假设被违反——markout 证据（internalized −4.25¢）表明仓位与
   后续回归正相关，该相关既抬高 E[drift] 也使 "4.4σ" 高估异常度。

**金句**：同一个相关性，在期望里叫流动性提供溢价，在方差公式里叫假设失效。

**修正后的 Q5 报数**：E ≈ +$400，1σ ≈ ±$3.3k，尾部 ≈ band 6,000 × 极端路径 ≈ ±$15k。
