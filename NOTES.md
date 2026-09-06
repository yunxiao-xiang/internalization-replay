#

# Orderbook 


# No-lookahead 体现在两处，本质是结构性保证，不是某个 if 判断：

1. main.py:merge_events 的归并顺序 —— 这是事件进入引擎的唯一入口。条件 quotes[qi].ts <= orders[oi].ts 保证：dispatch 一个 order 之前，所有 ts ≤ order.ts 的 quote 已经全部喂给了引擎，而 ts 更大的 quote 还没被读到。
2. Engine 只持有单个当前行情 —— on_quote 里 self.quote = q（engine.py:44 附近），引擎从头到尾拿不到整条 quote tape（quotes 列表根本没传进 Engine）。on_order 决策时只能读 self.quote。想偷看未来行情在结构上就不可能——未来的 quote 此刻还没进入引擎。(quote snapshot)


# Strategy
# 1.什么情况下需要internalize？

我已经梳理出内部化(internalize)的三种触发情形——新增风险型(点差≥2¢、未近收盘、有仓位容量)、减仓型(无条件欢迎，帮公司出库存)、公司主动发起型(超软限时找客户限价单减仓)，其余情况一律路由到市场(route)。接下来我会基于这个规则框架继续分析代码逻辑。

# principal fill at what price? 
我已经整理出三种principal fill的定价规则（新增风险/减仓改善价、1¢点差按touch价、rebalance按客户限价成交），并明确了统一的NBBO约束断言，核心结论是客户成交价永远不劣于route，公司利润来自免对冲成本而非压价。

# risk policy
only internalize when reducing risk?
on close 我们需要end the day flat
收盘归零的可行性来自市场流动性假设而非book深度——即便book为空，市场单也能强制轧平，book只是省成本的优化；真正需要保证的是成本有界，靠±10k硬限和15:55起reduce-only把最坏残量压到很小。我已整理好这套答辩逻辑及生产环境下（MOC/LOC、限额线性收缩、TWAP分批）的应对方案

# future
1. principal book risk policy: (1) limit should shrink proportional to time left on that trading day (2)use markout analysis to rank client toxicity / market regime, can incorporate own prediction model to avoid adverse selection

2. principal fill at what price? - swtich to principal book risk adjusted price later
3. OMS?
4. no impact / fee accounted for principal fill vs internalization, actually we should count that in before make decision to fill in market
5. quote book can hold all data if we have tick data instead of orderbook snapshot - sometimes client can fill at not only NBBO （not needed as we made assumption "Assume routed orders fill immediately and completely at the prevailing market quote" and "All executions must occur at or within the current market best bid/ask (NBBO) at the time of the fill."




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