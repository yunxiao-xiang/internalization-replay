#

# Orderbook 


# No-lookahead 体现在两处，本质是结构性保证，不是某个 if 判断：

1. main.py:merge_events 的归并顺序 —— 这是事件进入引擎的唯一入口。条件 quotes[qi].ts <= orders[oi].ts 保证：dispatch 一个 order 之前，所有 ts ≤ order.ts 的 quote 已经全部喂给了引擎，而 ts 更大的 quote 还没被读到。
2. Engine 只持有单个当前行情 —— on_quote 里 self.quote = q（engine.py:44 附近），引擎从头到尾拿不到整条 quote tape（quotes 列表根本没传进 Engine）。on_order 决策时只能读 self.quote。想偷看未来行情在结构上就不可能——未来的 quote 此刻还没进入引擎。(quote snapshot)


# Strategy
# 1.什么情况下需要internalize？

我已经梳理出内部化(internalize)的三种触发情形——新增风险型(点差≥2¢、未近收盘、有仓位容量)、减仓型(无条件欢迎，帮公司出库存)、公司主动发起型(超软限时找客户限价单减仓)，其余情况一律路由到市场(route)。接下来我会基于这个规则框架继续分析代码逻辑。
why 2 cents spread:
┌──────────────────────────┬────────────────┬────────────────┐
│                          │  2¢ baseline   │  3¢ threshold  │
├──────────────────────────┼────────────────┼────────────────┤
│ Internalized shares      │ 243,300        │ 97,400 (−60%)  │
├──────────────────────────┼────────────────┼────────────────┤
│ Routed shares            │ 232,200        │ 378,100        │
├──────────────────────────┼────────────────┼────────────────┤
│ Firm hedges              │ 20 (21,300 sh) │ 10 (14,600 sh) │
├──────────────────────────┼────────────────┼────────────────┤
│ Realized P&L             │ $14,396        │ $13,499        │
├──────────────────────────┼────────────────┼────────────────┤
│ Client price improvement │ $2,216         │ $1,119 (−49%)  │
└──────────────────────────┴────────────────┴────────────────┘

# principal fill at what price? 
我已经整理出三种principal fill的定价规则（新增风险/减仓改善价、1¢点差按touch价、rebalance按客户限价成交），并明确了统一的NBBO约束断言，核心结论是客户成交价永远不劣于route，公司利润来自免对冲成本而非压价。

# risk policy
only internalize when reducing risk?
on close 我们需要end the day flat
收盘归零的可行性来自市场流动性假设而非book深度——即便book为空，市场单也能强制轧平，book只是省成本的优化；真正需要保证的是成本有界，靠±10k硬限和15:55起reduce-only把最坏残量压到很小。我已整理好这套答辩逻辑及生产环境下（MOC/LOC、限额线性收缩、TWAP分批）的应对方案

hard stop on 15:55 (configurable) to take increasing risk, and by 16:00 MOC all remaining quantity on principal book

# future
1. principal book risk policy: 
(1) book position limit should shrink proportional to time left on that trading day, we can set the config risk limits as sth like: ADV * (remaining time / 390) * participation rate (~ 5%-10%), the number I hard-coded in config is much smaller thank the number calculated from the formula - 10,000 vs 32,000 (5mins & 5% participation)
(2)use markout analysis to rank client toxicity: can apply per-client mulitplier to the "2 cents internalization spread", or can incorporate own prediction model to avoid adverse selection
(3) internalization: we want to client get strictly better than touch (at least 1 cent), so we trade with 
(4) firm hedge flow - we assumed we can execute all at top book (opposite level), in reality we need to decide execute how much aggressively (cross book at what limit price) vs passively (stay on limit and wait to be hit). we can calculate a PAV based on vol and its difference with bid / ask, set a threshold on how large this gap would be and decide whether execute aggressively / passively
(5) one "drawback" of applying soft limit and hard limit on position - after hitting the soft limit, the strategy will only execute the qty above limit and passively wait for another offsetting client order coming in. However i tried to let principal book liquidate some shares when it stucks in soft limit (6000) and spread is 1 cent wide (idea is to aggressively execute some when its quiet), but it doesnt make too much difference as we get most flows in volatile times and spreads are wide at that time. One intuition here is we hold maximum of 6000 shares directional risk and resist taking more in volatile times.

2. principal fill at mid rounded towards principal book's side, later can swtich to principal book risk adjusted price later
3. no impact / fee accounted for execution vs internalization, actually we should count that in before make decision to fill in market
4. quote book can hold all data if we have tick data instead of orderbook snapshot - sometimes client can fill at not only NBBO （not needed as we made assumption "Assume routed orders fill immediately and completely at the prevailing market quote" and "All executions must occur at or within the current market best bid/ask (NBBO) at the time of the fill."

# 在刚刚提到的-6000 加入2000全额接的情况下，是否可以先scan resting book再决定是否internalize，直接接并且触发rebalance的话net benefit不变，但是client eat掉了一部分firm pnl，resting book没有offseting order的话是否应该考虑不接？或者resting book里面有多少接多少，其余的直接agency execution

这个提议是对的——而且在模型假设下严格占优。逐层拆：

1. 你的"net benefit 不变、client 吃掉 firm P&L"判断，数学上成立。
钉在 −6,000 时接 BUY 2,000 @ B+2（ask−1），rebalance 立即市场对冲残量 @ ask=B+3：公x司每股 −1¢，客户每股 +1¢（相对 route）——同 tick、同 NBBO 下这是精确的零和转移，公司白送。因为超软限部分是"即时对冲"，连"等未来反向流免费消化"的期权价值都不存在（那是 band 以内仓位的辩护，不适用于超限增量）。

2. 所以正确的接单量公式是：

▎ internalize qty = (到软限的余量) + (resting book 里有利可图的对手方深度)，其余 route

两个细节：
- "有利可图" = resting sell 的 limit ≤ 成交价 B+2。买回价高于你卖出价的 cover 是亏的——L ∈ (B+2, ask] 的挂单不该算进 coverage，那部分也 route；
- "完全不接"不存在——永远不是拒单，是 route（客户拿 touch，和你不接他一样，没人受损）。书里空、又钉在软限 → internalize qty = 0，全 route，等价于你说的"不接"。

3. 和 skew quoting 的关系——这是答辩的升华点：
两者是同一个思想的离散版和连续版：都是把边际对冲成本定价进接单决策。
- Book-scan gating（你的方案）：价格不动（仍给 mid 改善），数量上只接对冲成本 ≤ 0 的部分——离散、二值；
- Skew pricing：数量不动，价格随仓位从 mid 滑向 touch，让边际不划算的流量被价格劝退——连续、平滑。
- 生产里两个都要：skew 做常态调节，book-scan 做钉仓时的硬保护。你的方案还有个实现优势：resting book 是当前状态，扫它不违反 no-lookahead，且复用 rebalance 已有的遍历逻辑。

4. 要主动交代的两个 caveat：
- 当天实证影响很小——只有钉仓时刻受影响，全天市场对冲仅 20 笔 / $385，这是个尾部优化不是主收益源；
- 客户体验不对称：同样的单，book 深时拿 mid 改善、book 空时被 route——对客户仍然"永不差于 route"（底线不破），但 improvement 变得依赖他人挂单，best-ex 报告里要能解释这个条件性。

一句话收尾："超软限的增量流量，接单的 EV 完全等于 rebalance 的出口质量——所以决策应该先看出口（scan book）再接单，接多少由有利出口的深度决定；这和 skew pricing 是同一枚硬币的数量面和价格面。"

why 2 cents spread:
┌──────────────────────────┬────────────────┬────────────────┐
│                          │  2¢ baseline   │  3¢ threshold  │
├──────────────────────────┼────────────────┼────────────────┤
│ Internalized shares      │ 243,300        │ 97,400 (−60%)  │
├──────────────────────────┼────────────────┼────────────────┤
│ Routed shares            │ 232,200        │ 378,100        │
├──────────────────────────┼────────────────┼────────────────┤
│ Firm hedges              │ 20 (21,300 sh) │ 10 (14,600 sh) │
├──────────────────────────┼────────────────┼────────────────┤
│ Realized P&L             │ $14,396        │ $13,499        │
├──────────────────────────┼────────────────┼────────────────┤
│ Client price improvement │ $2,216         │ $1,119 (−49%)  │
└──────────────────────────┴────────────────┴────────────────┘


Makes an execution decision for every order (and re-evaluates resting orders as the market moves). Your strategy decides, at minimum:

when to fill a client on a principal basis (internalize) and at what price within the spread;
when to cross two client orders against each other, and at what price;
how long to let a limit order rest, and what triggers its execution, routing, or expiry;
when to route out to the market;
how to handle partial fills, if your design uses them (allowed, not required);
what happens to still-open orders at the 16:00 close.

一句话原则


▎ Internalize 的本质是用公司资产负债表卖流动性。应该 internalize 当且仅当：这笔风险有人付钱（点差补偿足够）、或这笔成交本身在降风险（对冲库存）、且任何时候都留得住退路（限额内、来得及出）。

判断框架 —— 四个风险维度

1. 补偿维度：点差是否付得起风险？
每笔 internalize 的期望收益 = 保留的点差边际；期望成本 = 概率加权的对冲成本（半点差）+ 持仓期间的预期不利变动。Break-even：

▎ E[未来反向客户流捕获的点差] > P(被迫对冲) × 半点差 + E[持仓期漂移损失]

点差越宽左边越大——但注意右边的半点差也是同一个宽点差，所以宽点差不是无条件的绿灯（见第 4 条）。

2. 仓位维度：这笔成交让风险变大还是变小？
- 减仓方向的流量永远接——它是免费的对冲，连 1¢ 点差都值得（客户按 touch 不吃亏，公司省过桥成本）。这是整个 policy 里唯一无条件的规则；
- 增仓方向的流量只在容量内接，超出部分 route——风险上限必须是结构性的（成交前截断），不是事后报警。

3. 时间维度：还来得及消化吗？
持仓的风险 ∝ 预期持有时间。临近收盘，"等反向流量免费消化"的时间窗归零，剩下的只有强制按 touch 倾销一条路——所以尾盘只接减仓流量。推广到生产：限额应随剩余交易时间收缩。

4. 对手方/市场状态维度：这笔流量是不是毒的？
Marketable 流量在单边行情里是逆向选择——客户在跌势里砸给你的卖单，大概率你接完还继续跌。宽点差 + 趋势同时出现时最危险：进场补偿看着高，出场成本（半个宽点差）和漂移损失更高。我们当天的归因就是证据：$14,396 里只有 $160 是边际，其余全是漂移——方向碰巧站对了。生产环境按客户分层（markout 统计）+ 行情 regime 调整；单日数据上我们拒绝拟合趋势信号，用仓位带宽兜底。

落到我们的参数

┌──────────┬──────────────────────────────────────────────────────────────┐
│ 框架维度 │                          我们的规则                          │
├──────────┼──────────────────────────────────────────────────────────────┤
│ 补偿     │ 点差 ≥ 2¢ 才接新风险；1¢ 只接减仓                            │
├──────────┼──────────────────────────────────────────────────────────────┤
│ 仓位     │ 硬限 ±10k 成交前截断；软限 ±6k 超限即减                      │
├──────────┼──────────────────────────────────────────────────────────────┤
│ 时间     │ 15:55 起 reduce-only；16:00 强平                             │
├──────────┼──────────────────────────────────────────────────────────────┤
│ 逆向选择 │ 不预测趋势，靠带宽限损；写入 write-up 作为 production 改进项 │
└──────────┴──────────────────────────────────────────────────────────────┘

答辩收尾句："我的 policy 把'该不该 internalize'拆成四个可独立辩护的判断——每个参数都能说出它防的是哪种损失，以及放宽它会在哪一天亏钱。"

有想深入的维度，或者要我模拟面试官对这套框架追问吗？


on_order 的实际次序：

1. 先尝试 cross（只需限价窗口与 NBBO 重叠，不要求 marketable）；
2. 判断 marketable（买限价 ≥ ask / 卖限价 ≤ bid）——只有 marketable 的订单才进入 principal_quote 定价；
3. 非 marketable 的残量：DAY → rest 进 book，IOC → 取消。
regen cmd: pdf generate --no-confidential --no-chapter-breaks --margins 0.6in --title "Internalization Engine Design" DESIGN.md DESIGN.pdf
