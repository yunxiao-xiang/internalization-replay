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

### Q4-补充: clamp 的含义与 mid 出窗的情形

`cross_price` 一行：`px = min(max(mid, lo), hi)` —— 把 NBBO mid "夹"进窗口，
只挪到最近的合法边界为止。

例：NBBO 244.88/244.92（mid 244.90），sell limit 244.91，buy limit 244.93：
lo = max(244.88, 244.91) = 244.91；hi = min(**244.92**, 244.93) = 244.92（ask 卡上界）；
mid 244.90 < lo → clamp 上抬 → **成交 244.91**。

| mid 位置 | 成交价 | 改善分配 |
|---|---|---|
| 窗口内 | mid | 双方对称 |
| mid < lo（卖方限价/bid 绑定） | lo | 卖方恰拿限价，改善倾斜给买方 |
| mid > hi（买方限价/ask 绑定） | hi | 买方恰拿限价，改善倾斜给卖方 |
| lo > hi | 不 cross | window 为 None → internalize/route |

被 clamp 的一方拿到恰好自己的限价（对比 route 仍更优：本例卖方 vs bid +3¢）；
剩余改善全部归对方（买方付 244.91，比限价好 2¢、比 ask 好 1¢）。
细节：本例买方 limit ≥ ask 本身 marketable，但 `_try_cross` 排在 marketability
判断之前 → 先 cross 在 244.91，比 route 还好 1¢——"cross 优先"价值的具体展示。

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

## Q6: sweep 里的 resting-vs-resting cross 分支——我们还有机会 cross 吗？

**我的直觉（正确）**：客户单进来时没 cross 就已经 route 走了；行情移动后变
marketable 的单被 sweep 的 marketable 分支吃掉——所以 sweep 的 cross 分支似乎轮不到。

**升级为证明（当前规则下该分支不可达）**：
设两张 resting 单限价重叠（bL ≥ sL），看后到的那张（设为卖单）到达时刻：
1. 它没被 `_try_cross` 撮合 ⇒ 窗口空：max(bid, sL) > min(ask, bL)；
2. 它挂进了 book ⇒ 非 marketable：sL > bid；
3. 由 1+2 得 lo = sL 且 sL > min(ask, bL)；又 sL ≤ bL ⇒ **sL > ask** ⇒
   bL ≥ sL > ask ⇒ 先到的买单早已 marketable——但 sweep 每 tick 跑且 quote 先于
   order 处理，marketable 的单不可能还挂着。矛盾。买卖对调对称。
⇒ 重叠的 resting 对从不共存；限价静态 ⇒ 之后的 NBBO 移动也造不出重叠。

**实证**：当天 7 组 cross（14 行）的成交时间戳全部 == 后到那条腿的 arrival 时刻，
sweep 的 cross 分支全天零触发。这也回答了"cross 为何只有 1,600 股"的另一半：
不是撮合不积极，而是结构上 cross 的唯一窗口就是订单到达那一瞬。

**不可达性依赖四个前提**（任一松动它就复活）：cross-at-arrival、每 tick sweep、
**限价静态**、quote 先于 order。三个复活场景：
1. **amend/replace**（最现实）：#A SELL 244.83 与 #B BUY 244.81 各自挂着（窗口空），
   客户把 #A 改到 244.80 → 下个 quote 的 sweep 得到窗口 [244.80, 244.81] → cross。
2. **sweep 漏跑/conflation**（生产必然）：积压导致 marketable 单没被处理，书里出现
   重叠——但注意此时 NBBO 往往已跑到两个限价之外，窗口仍可能是空的，说明该分支
   在生产里也是低频兜底而非主路径。
3. **改设计**（speed bump / conditional order）：marketable 单停留 200ms 等对手方——
   打破 cross-at-arrival。代价正是我们当初拒绝的理由（扣留客户可执行订单），
   除非像 IEX 那样把 speed bump 作为公开披露的场地规则。

### Q6-追问 1: amend 应该在 on_order 里处理吗？

**不应该——独立入口 `on_amend(order_id, new_limit, new_qty, ts)`，逻辑复用。**
`on_order` 假设"从未见过的新单"：跑完 waterfall 然后 `book.add()`。amend 面对已在册
订单，必须先取出旧状态，且要显式实现**队列优先级惯例**：改价或增量 → 失去时间
优先级、按新价排队尾；仅减量 → 保留原位。这条规则在 `on_order` 里没有位置放。
替代方案是拆成 cancel + new（FIX 常见），代价是中间有个"两边都没挂"的窗口。
两种做法改完都要立刻重跑 waterfall（新限价可能当场 marketable 或可 cross）——
**共享的是执行逻辑，不是入口**。已写入 DESIGN production changes 第 7 条。

### Q6-追问 2: sweep 可能跑不完时，删掉 cross 检查能否降延迟？

**不能删——但直觉指向的优化方向对，只是位置错了。**
1. **它已近乎免费**：两个 `best_*` peek（O(1)，且本来就要取来做 marketable 判断）
   加一次纯算术 `cross_window`；删掉省几十纳秒。同一函数里真正贵的是
   `_execute_marketable` → `coverage()` 的 O(N) 扫书——**要砍延迟砍那个**。
2. **逻辑自相矛盾**：它不可达恰恰依赖"每 tick sweep 完整跑"；一旦假设 sweep 会
   漏跑（正是删它的理由），重叠就可能出现，此时被删的分支正是最后的守卫。
3. **正解是分层不是删除**：快路径只做 NBBO 更新 + top-of-book 可执行性判断；
   cross 检查/coverage/bleed 用脏标志（`crossable_dirty`）或定时器移出快路径，
   让 99% 的安静 tick 走最短路径。

**金句**：删除一个不可达分支的收益是纳秒级，代价是把隐式不变量变成隐患——
尤其当你删它的理由，恰好也是它可能变得可达的理由。

## Q7: coverage 项是怎么变成死代码的（自省案例）

**需求来源**：v0.1 讨论中我提的复合需求——"resting book 没有 offsetting order
是否不接？或者书里有多少接多少"。它含两个独立判断，被合并成了一条规则：
- **判断 A**：钉在软限之上接单 = 用 1.5¢ 对冲成本换 0.5¢ edge → 该 route。
  实现为 `room = 到软限的余量`——**$343 的改善全部来自它**。
- **判断 B**：书里有便宜对手方就多接（退出免费）。实现为 `+ coverage`——**从未开火**。

**为什么不可达**：cross 优先的规则保证——只要 resting 单便宜到能对冲，来单一定
先把它 cross 掉；coverage 找的集合（限价优于成交价）是 cross 可吃集合的子集。
sweep 路径同理：它先跑 cross 分支，进入 marketable 分支时"存在可 cross 对手方"
已被否证。

**实证（插桩计数）**：全天 457 次 `_execute_marketable`（on_order 299 / sweep 158），
**coverage > 0 出现 0 次**。删除后 P&L、fills、hedges 完全不变（$15,078）。

**漏检的根因**：v0.1 当时只验证了**总量结果**（P&L、对冲笔数、±$343 精确对账），
没有验证**机制归因**。一句"这 $343 里 room 和 coverage 各占多少"就能当场暴露。

**教训（已写入 workspace 与 _template 的 CLAUDE.md）**：新增 function/branch 时，
在测试运行里统计其**实际调用次数**并分析是否真的需要——总量对账证明结果，
不证明机制；零调用就删掉，或写明为何不可达、什么条件下会复活。

**处理**：删除 `coverage` 参数、`book.coverage()`（heap 版）与相关调用；
`book_dict.coverage()` 保留为深度查询，docstring 标注"引擎不使用，实测 0/457"。
顺带消灭了 Q2 里点名的那个 O(N) CPU 悬崖——那个悬崖本来就是为一个死项付的钱。

## Q8: 非 marketable 的盘中限价单能否 internalize？（v0.3）

**问题**：NBBO 244.40/244.44，客户 BUY @244.43 → `_marketable` 为假（< ask），
直接挂 book。但 `improved_price` = mid **244.42 ≤ 244.43**，合法且对客户严格更优——
我们放过了一笔双赢成交。

**为什么原设计漏掉**："marketable" 被我借用得过窄：交易所语义是"能立刻与外部
成交"，我用它当成了"客户是否急于成交"的代理，把盘中限价单默认解读为耐心挂单者。
但客户的限价 244.43 已明确表达"244.43 及更好都接受"，244.42 严格更好——**用"他
可能想等更好的价"拒绝一个严格更优的成交，是替客户猜意图**。真实 midpoint venue
（IEX D-Peg 等）正是靠给盘中挂单提供 mid 成交获客。

**原路径的补偿**：这类单多数最终仍被 `_rebalance` 吃掉（公司减仓时按**客户限价**
成交），但价格是 244.43 而非 244.42，且要等到公司恰好想减仓。

**v0.3 实现**：`on_order` 的 `_marketable` 分支加 `elif` → `_offer_midpoint()`：
若 `improved_price` 满足客户限价 ∧ `principal_quote` 给出的正是该改善价（排除
touch 分支）→ 成交；否则照常挂单。无 route 腿。

**调用计数（按 CLAUDE.md 新规矩必做）**：199 次调用 → **13 笔成交、9,000 股**。
关键的诚实发现：**13 笔全部是 2¢ 点差、客户限价恰好等于 mid** ⇒ 相对客户自己
限价的改善是 **$0.00**，客户得到的是"立刻成交"而非更好的价；相对 touch 则是 +1¢。
我举的 4¢ 例子（limit 244.43 / mid 244.42）当天一次也没出现。

**全天影响（v0.2 → v0.3）**：P&L $15,078 → **$15,307**；客户改善 $1,935 → **$2,013**；
成交股数 477,100 → 478,100（IOC 取消 −1,000）；对冲 17 → 19 笔。

**风险（必须一并说）**：这会放大内部化量与逆向选择敞口——耐心挂单者往往比
marketable 单更 informed（他们在选价而不是抢时间）。生产上应配 markout 分层而非
无差别打开。与 v0.1 的方向恰好相反：v0.1 收紧接单，v0.3 放宽接单。

### 具体数据例子：一个 order 事件与一个 quote 事件的完整触发链

**A. Order 事件——ORD-00004（真实数据，v0.3 新增路径）**
到达 09:31:20.121，BUY 100 LIMIT @244.85；此刻 NBBO **244.84/244.86**（2¢），
公司仓位 **−300**。

```
merge_events                     # ≤09:31:20.121 的 quote 已全部先行处理
└─ Engine.on_order(o)
   ├─ _try_cross(o, ts)          # book.best_sell() → None（无对手方）→ 立即返回
   ├─ _marketable(o)             # 244.85 < ask 244.86 → False
   ├─ _offer_midpoint(o, ts)     # ← v0.3 新分支
   │   ├─ improved_price("BUY", 24484, 24486) = ceil(mid) = 244.85
   │   ├─ 244.85 ≤ limit 244.85 ✓（恰好等于，非严格更优）
   │   ├─ principal_quote(o, −300, ts, ...) → (100, 244.85)
   │   │     spread 2 ≥ 2 ✓ / 未过 15:55 ✓
   │   │     hard_cap = −300+10,000 = 9,700；room = −300+6,000 = 5,700
   │   │     qty = min(100, 9,700, 5,700) = 100
   │   └─ _fill(ts, o, 100, 244.85, PRINCIPAL, INTERNAL)
   │        ├─ 断言 24484 ≤ 24485 ≤ 24486 ✓；买价 ≤ 限价 ✓；0 < 100 ≤ 100 ✓
   │        ├─ position −300 → −400；cash += 100 × 244.85
   │        └─ Reporter.record_fill(... nbbo_bid=244.84, nbbo_ask=244.86)
   ├─ o.remaining == 0 → 不挂 book、不取消
   ├─ _rebalance(ts, 6000)       # |−400| ≤ 6,000 → 首行 return（空转）
   └─ _track_inventory_age(ts)   # |−400| ≤ bleed_trigger 4,000 → _over_since = None
```
客户结果：立刻全成 @244.85，比 route（ask 244.86）好 1¢；原版本下这单会挂进
book 等行情。

**B. Quote 事件——09:45:00.001 的 tick（触发 bleed hedge）**
新 NBBO **244.51/244.52**（1¢），进入前公司仓位 **−5,900**（上午空头累积）。

```
Engine.on_quote(q)
├─ self.quote = q               # NBBO 快照替换（O(1)，引擎只持有这一条）
├─ _sweep(q.ts)                 # while: best_buy/best_sell peek
│    ├─ cross 分支：无重叠（Q6 证明其不可达）
│    ├─ b.limit ≥ ask? / s.limit ≤ bid? 均否
│    └─ return（安静 tick，摊还 O(1)）
├─ _rebalance(q.ts, 6000)       # |−5,900| ≤ 6,000 → return（未破软限）
├─ bleed_target(spread=1, pos=−5900, over_secs=…)
│    ├─ |−5,900| > bleed_trigger 4,000 ✓
│    └─ spread 1 ≤ cheap_spread_max 1 ✓ → 返回 target = 4,000（触发器 A）
├─ _rebalance(q.ts, 4000)       # excess = 5,900 − 4,000 = 1,900
│    ├─ 先找 resting SELL：best_sell().limit ≤ ask? 否（无便宜卖单）→ break
│    └─ _firm_trade(ts, "BUY", 1900, 244.52)   # 市场回补，付半点差 0.5¢/股
│         ├─ position −5,900 → −4,000；cash −= 1,900 × 244.52
│         └─ Reporter.record_firm_trade(... position_after=−4,000)
└─ _track_inventory_age(q.ts)   # |−4,000| ≤ 4,000 → _over_since = None（计时器复位）
```
这是当天 19 笔对冲中最大的一笔，也是"挑最便宜的窗口动手"的实证：spread 恰为 1¢
（全天最低），对冲成本 0.5¢/股 vs v0 时代被迫对冲的 1.8¢/股。

## Q9: bleed 的双面性——降低漂移方差的同时，牺牲了减仓型 internalization 的 edge

**核心发现**：库存在这套策略里有**双重身份**——既是风险敞口（bleed 想消灭它），
也是**接收 1¢ 免费流量的产能**（edge 的主力来源）。压低库存等于同时压低这项产能。

**edge 的结构（实证分桶）**：客户成交的每股 edge 只有两个取值：
- **偶数点差（2/4/6¢）：edge ≡ 0** —— mid 是整数，改善价 = 精确 mid，公司零边际。
  当天 2¢ 桶超过 10 万股，一分不赚；
- **奇数点差（1/3/5¢）：edge = 0.5¢/股** —— mid 落在半美分，取整判给公司。
  **全部 edge 来自这里**，其中 1¢ 桶（$334 / 全部 $503）是主力。

**1¢ 桶是什么**（这里修正一个容易犯的错）：2¢ 门槛**只管新增风险**——`principal_quote`
的 reduce 分支独立于那个 `if`，无点差门槛。实证：v0.1 中 1¢ 点差的 INTERNAL 成交
涉及 92 张订单，**100% 在 touch 成交**（买 @ask / 卖 @bid），全部是减仓分支。
其上限硬编码为 `min(order.remaining, abs(position))`——**库存有多少，才能接多少**。

**v0.1 → v0.2 的代价拆解（−$124.5）**：
- **保费 −$81.5（结构性确定）**：16,300 股 × 0.5¢ 半点差；触发器 A 保证每笔都在
  1¢ 窗口成交，是理论最低单位成本（对比 v0 被迫对冲的 1.81¢/股）。
- **机会成本 −$43（路径相关）**：残差 $503.5 − $460.5，主项在 1¢ 桶
  （−9,900 股 × 0.5¢ = −$49.5，被 3¢ 桶 +$6.5 抵消）。因果链：bleed 把 |pos|
  从常驻 4k–6k 压到 4k 以下 → reduce 分支上限变小 → 少接 1¢ 免费流量。

**受影响订单（精确定位）**：15 张订单少接 10,000 股、**全部是 1¢ 点差**，其中
ORD-00053 (−1,700)、ORD-00297 (−1,500，完全归零)、ORD-00285 (−1,200) 为最大三笔；
另有 8 张因 bleed 改变了后续仓位轨迹而**多接 6,300 股**——所以
**bleed 不是单向削减，而是重新分配了全天的接单容量**，$43 是净额不是毛额。

**买到了什么**：时间加权 |pos| 1,685 → 1,456 股；σ(drift) ∝ pos_RMS，敞口下降
同比例压缩漂移方差。这是用确定的小额支出换不确定的大额损失概率——保险的结构。

**答辩表述**："bleed 花了 $82 的确定保费和约 $43 的随机机会成本，买到约 14% 的
敞口方差缩减。值得强调的是这两项性质不同：保费 = 对冲量 × 半点差，触发即发生；
机会成本取决于当天 1¢ 时段来了多少减仓流量，换一天可能是 $20 或 $80。所以我分开
报，不合成一个'总成本 $125'。"

**由此得出的优化方向**：数量类改动（v0.1/v0.2/v0.3）动不了 edge 的天花板——
偶数点差 edge ≡ 0 是**定价规则**决定的，而它们占内部化量一半以上。要提升 edge
只能改定价（偶数点差向公司偏移半美分，代价是客户改善从 1¢ 降到 spread/2 − 0.5¢），
或按客户毒性分层定价。
