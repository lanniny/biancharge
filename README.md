# Binance Paper Monitor

一个安全版 Binance 行情监控和纸上交易工具。

**开发与 VPS 运维文档**：见 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)（完整）与 [`docs/VPS_QUICKREF.md`](docs/VPS_QUICKREF.md)（速查）。

它只访问 Binance 公开行情接口，不需要 API Key，不包含真实下单、撤单、转账或提现逻辑。你之前泄露过的 API Key / Secret 不会被写入本项目。

## 功能

- 查询现货公开价格
- 查询公开交易所信息和 24h 行情
- 使用本机环境变量做只读账户余额查询
- Binance 主站行情不可访问时，自动尝试 Binance.US 公开行情
- 根据阈值打印价格提醒
- 用虚拟余额做纸上买入 / 卖出
- 使用 `market_autotrader.py` 做多市场自动监控、纸上交易、风险闸门和理由账本
- 单元测试覆盖核心逻辑

## 快速开始

```powershell
python .\binance_paper_monitor.py --config .\config.example.json --once
```

持续监控：

```powershell
python .\binance_paper_monitor.py --config .\config.example.json
```

查询公开信息：

```powershell
python .\binance_paper_monitor.py --config .\config.example.json --public-info
```

查询账户只读信息：

```powershell
$env:BINANCE_API_KEY = "你的新API Key"
$env:BINANCE_API_SECRET = "你的新Secret"
python .\binance_paper_monitor.py --config .\config.example.json --account
```

运行测试：

```powershell
python -m unittest discover -s tests
```

运行多市场自动交易智能体的一次决策：

```powershell
python .\market_autotrader.py --config .\market_autotrader.example.json --once
```

这个默认示例使用本地合成行情，不需要联网或 API Key；它用于验证完整链路是否正常。要切到公开 Binance K 线和 Alpaca 美股数据，可参考 `market_autotrader.live-data.example.json`，其中 Alpaca Key 只从本机环境变量读取。

全天候纸上交易循环：

```powershell
python .\market_autotrader.py --config .\market_autotrader.example.json
```

实时监督型智能体：

```powershell
python .\realtime_supervisor.py --config .\realtime_supervisor.example.json --once
python .\realtime_supervisor.py --config .\realtime_supervisor.example.json
```

`realtime_supervisor.py` 会实时轮询账户、合约持仓、现货/合约行情，输出 `NO_TRADE`、`HOLD_WITH_BUFFER`、`REDUCE_POSITION`、`SPOT_BUY_APPROVAL` 等建议，并写入 `logs/realtime-supervisor.jsonl`。触发操作时会在 `approvals/` 生成审批票据，并把需要提醒的事项写入 `logs/realtime-supervisor-events.jsonl`，但不会发真实订单。

当前 supervisor 的自主模式是 `autonomous_analysis_approval_required`：

- 自动完成账户/市场轮询、信号打分、风险闸门、模型复核、审批票据、桌面/文件通知和 JSONL 决策账本。
- 自动给出 `manualOrderPlan`，包括 Binance 现货或 U 本位合约页面可参考的方向、订单类型、数量/金额上限和限制条件。
- 真实撮合下单、撤单、调杠杆和转账必须由人确认后在交易所完成；程序里的 `realOrderAllowed` 始终为 `false`。
- 如果账户或行情数据不完整，会触发 `DATA_UNAVAILABLE => block`，本轮所有新开仓、加仓、买入候选都会被拦截，避免把网络/API 故障误判成交易机会。

警报/买入候选通知：

- `logs/realtime-supervisor-events.jsonl`: 通知级事件队列，每行一个 JSON，包含账户快照、行情信号、持仓风险、审批票据路径和完整理由。
- `logs/realtime-supervisor-latest-alert.txt`: 最新一条警报和模型复核结果的人类可读摘要，方便快速查看。
- `logs/realtime-supervisor-notify-state.json`: 去重冷却状态，默认 15 分钟内同类事件只提醒一次。
- 每个事件都有 `requiresModelReview: true` 和 `realOrderAllowed: false`。后台会在事件产生后自动写入 `modelReviewStatus`、`modelReviewMessage`，并追加到 `logs/realtime-supervisor-model-reviews.jsonl`；它不是自动真实下单指令。
- 默认只通知 `DATA_UNAVAILABLE`、`REDUCE_POSITION`、`SPOT_BUY_APPROVAL`、`REASSESS_TAKE_PROFIT_OR_HOLD`、`RESERVE_CASH`，并且最低优先级是 `medium`。
- `desktop_notifications: true` 时，通知级事件会尝试触发 Windows 托盘气泡提醒；提醒会显示模型复核 verdict，不会给出自动真实下单指令。

查看最新通知事件：

```powershell
Get-Content .\logs\realtime-supervisor-events.jsonl -Tail 3
Get-Content .\logs\realtime-supervisor-latest-alert.txt
```

本地 Web 控制台：

```powershell
.\start_trading_dashboard.ps1
```

打开 `http://127.0.0.1:8765`。控制台提供：

- 账户、持仓、信号、建议、最新模型复核和审批单列表。
- `运行一次复核`、`启动盯盘`、`停止盯盘` 操作按钮。
- 红色 `真实下单审批` 操作台：填写交易对、方向、数量/金额、限价和备注后，会生成 `approvals/live_order_requests/` 下的 `LIVE_ORDER_REQUEST` 审批单。
- 这个按钮不会调用 Binance 真实下单接口；审批单会保留 `realOrderAllowed: false`、风险闸门状态、拦截原因和 Binance 手动检查项。

停止控制台：

```powershell
.\stop_trading_dashboard.ps1
```

生成模型复核记录：

```powershell
python .\review_events.py --write --limit 3
```

复核记录会写入 `logs/realtime-supervisor-model-reviews.jsonl`。这一步会把原始警报转成 `confirm`、`downgrade` 或 `needs_manual_model_review` 等结论，并保留证据字段。只有复核后的结论才适合作为给人的操作建议。

生成人工审批票据：

```powershell
python .\market_autotrader.py --config .\market_autotrader.approval.example.json --once
```

这会在 `approvals/` 下生成一份订单计划 JSON，包含订单方向、金额、指标、理由和人工检查项。它不会发真实订单。

验证 Binance 测试下单接口：

```powershell
$env:BINANCE_API_KEY = "你的本机 API Key"
$env:BINANCE_API_SECRET = "你的本机 Secret"
python .\market_autotrader.py --config .\market_autotrader.binance-order-test.example.json --once
```

这个模式只调用 Binance Spot 的 `/api/v3/order/test`，用于验证签名和订单参数，不会调用真实 `/api/v3/order` 撮合端点。

`market_autotrader.py` 默认只做纸上交易。每个资产每轮会输出一条 JSON 记录，包含：

- `action`: `BUY` / `SELL` / `HOLD` / `BLOCKED`
- `reasons`: 趋势、动量、RSI、量能、波动率等交易理由
- `blocked_reasons`: 风控拒绝原因，例如冷却期、波动过高、仓位过大、实盘未授权
- `indicators`: 本轮计算出的市场指标
- `portfolio`: 纸上账户状态

若配置了 `ledger_path`，所有决策会追加写入 JSONL 账本，便于复盘每笔交易或每次不交易的原因。

如果运行时看到 `HTTP 451`，说明当前网络环境无法访问 Binance 主站公开行情；脚本会继续尝试 `config.example.json` 里的下一个公开行情端点。你也可以把 `price_endpoints` 改成你能访问的行情代理或测试网端点。

## 研究回测：BTC 永续与美股/ETF 现金（只做纸面）

`research_backtest.py` 是单独的研究命令，用来核对 **BTCUSDT U 本位永续** 和 **美股/ETF 现金** 的回测或纸面账本。它不改 `market_autotrader.py` 的原有策略默认值，也不打开实盘闸门。`mode` 只能是 `backtest` 或 `paper`；写成 `live`、带上下单开关或凭证字段会直接退出。这条命令不读 `.env`、不连旧的 VPS、不发订单、不改交易所杠杆。

`docs/DEVELOPMENT.md` 里的 VPS 运维流程和这条研究命令无关。

### 产品矩阵

| 产品 | `product` | 这条路径做什么 |
| --- | --- | --- |
| BTCUSDT U 本位永续 | `binance_usdt_perp` | 模拟逐仓保证金、手续费、滑点、止损跳空。标记价有则用于盯市/强平，没有就标明回退到成交价。指数价不进盈亏。资金费只有 CSV 提供 `funding_rate` 才入账，否则为 `unavailable`，净盈亏不含资金费。 |
| 美股，例如 AAPL | `us_cash_equity` | 现金买入股票。杠杆拒绝为非 1。无资金费。开仓限制在 `America/New_York` 常规时段 09:30–16:00。 |
| ETF，例如 SPY、QQQ | `us_cash_etf` | 同上，但是现金 ETF，不是期货或期权的替代品。 |
| 美股/ETF 期权 | 不支持 | 配置该产品会失败。 |
| CME 期货 | 不支持 | 配置该产品会失败。 |
| Binance `AAPLUSDT` 一类代码 | 不在这里模拟 | 那是交易所合成行情，不是纳斯达克股票，也不是上市 ETF。 |

现金账本和永续账本分开，起始资金不共用。信号只用已收盘 K 线，下一根开盘价成交；止损若被跳空穿过，按开盘价而不是更优的止损价成交。策略是固定参数的 `baseline_sma_momentum`，示例窗口很短，只为了让合成夹具能走出信号，**不是寻优，也不保证交易频率或收益**。

### 命令

在仓库根目录、不需要 API Key：

```powershell
python research_backtest.py --config research_backtest.example.json
python research_backtest.py --config research_backtest.example.json --mode paper
python -m unittest tests.test_research_backtest
```

示例配置 `research_backtest.example.json` 读取 `fixtures/research/` 里标明 `SYNTHETIC FIXTURE` 的玩具 K 线。纸面模式和回测用同一套账本规则，末根 K 线产生的信号如果没有下一根 K 线就保持未成交。

### 输出指标

终端和 `logs/research-backtest-report.json` 会给出：

- 净盈亏 `net_pnl`（是否已含资金费看 `net_pnl_includes_funding` / `funding_status`）
- 手续费 `fees`
- 资金费 `funding`（没有序列时为 `null`）
- 最大回撤 `max_drawdown`（按永续账本或现金账本，不是单只股票各自假装一套组合）
- 成交笔数 `fills`，明细在 JSONL
- 样本区间 `sample_period`，以及 `sample_split_timestamp` 两侧的 `in_sample` / `out_of_sample`

这些数字只核对账本。合成夹具上的盈亏不是真实业绩。

### 公开历史数据

另一条配置 `research_historical.example.json` 使用公开历史，而不是上面的合成夹具。参数在文件里事先锁定，样本外从 `2024-01-01` 起，程序不会按样本外结果改参数。

```powershell
python research_data.py fetch --config research_historical.example.json
python research_backtest.py --config research_historical.example.json
```

- BTCUSDT 来自 [Binance 公开数据归档](https://data.binance.vision) 的 U 本位永续日线、标记价、指数价和 8 小时资金费。这里访问 FAPI 会得到 HTTP 451，所以不调用 FAPI，也不用代理绕过。
- AAPL、SPY、QQQ 来自 Yahoo 公开日线。OHLC 按拆股调整；股息在除息日以现金计一次。提前收市日保留日线，不单独模拟 13:00 的盘中路径。
- 原始压缩包缓存在 `data/research-cache/`，不提交。报告和成交写到 `artifacts/research/`。
- 每个账本 10000 名义本金。三只现金标的按权重分账，不能共用同一笔现金。永续杠杆只出现在研究账本的保证金里。

已跑过的锁定参数结果写在 `artifacts/research/historical-study.json`。精确小数以该文件为准。下面是四舍五入后的样本外账本（2024-01-01 至样本结束），只说明这组规则的记录，不是收益承诺。

| 账本 | 基线净盈亏 | 收益率 | 手续费 | 资金费 | 回撤 | 成交 | 买入持有净盈亏 | 费用×2 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSDT 永续 10000 USDT | 147.04 | 1.47% | 67.41 | -197.56，状态 incomplete | 6.71% | 90 | 6832.96 | 54.11 |
| 现金账本 10000 USD | 364.47 | 3.64% | 0 | 不适用 | 1.30% | 155 | 4151.78 | 327.80 |

现金手续费按 0 bps 假设；滑点已含在净盈亏里。样本外单票净盈亏大约是 AAPL 108.24、SPY 106.14、QQQ 150.10，各自只用约 3333.33 的切片。样本内永续基线高于买入持有，样本外则明显低于买入持有。参数没有按这个差别再调整。

数据范围：BTCUSDT 日线 2496 根，2020-01-01 至 2026-10-31，日历缺口 0，资金费结算 7488 次。其中 27 次没有 8 小时标记价，这些次数没有记成 0，所以资金费后盈亏标成不完整。另有 9 根日线没有标记价 OHLC，那些天的止损/盯市回退到成交价；13 根没有指数价，指数不进盈亏。AAPL/SPY/QQQ 各 1717 根日线，2020-01-02 至 2026-10-30。2025-01-09 全国哀悼日已作为休市日，不补价格。AAPL 的拆股已经在价格里，没有再乘一次股数；股息按除息日现金计入。

历史结果可能落后于简单持有。那只说明这组固定规则在这段公开数据上的账本，不是对未来收益的判断。

### BTCUSDT 前向纸面

`paper_forward.py` 是给后续常驻机器用的前向纸面程序，不是上面的离线回放。默认只做 BTCUSDT U 本位永续，虚拟资金 10000 USDT，模拟杠杆 1 倍。信号仍是日线 MA20/50 和 8% 止损。历史日线只预热均线，不记成前向成交。已收盘日线才出信号；成交用信号之后的第一笔新鲜买卖报价，或下一根日线开盘。资金费按 8 小时结算点入账，缺标记或费率时记缺失，不记 0。

```powershell
python paper_forward.py preflight --config paper_forward.example.json
python paper_forward.py start --config paper_forward.example.json
python paper_forward.py status --config paper_forward.example.json
python paper_forward.py stop --config paper_forward.example.json
```

`preflight` 只读连接 `wss://fstream.binance.com` 的公开行情。FAPI REST 在开发环境返回 HTTP 451，程序不调用它，也不用代理。打包文件是 `paper-forward-btcusdt.tar.gz`，里面同时有日线 BTC 配置和四币配置。行情断开会写带时间戳的日志并按指数退避重连，不会因为一次套接字异常退出。行情过期时 `status` 显示 `stale`。常驻机器要先停掉旧的 BTC 进程，保留原来的状态文件，再用新包续跑；四币进程另开，不替换日线进程。步骤写在 `packaging/README-PAPER.md`。本仓库的云端检查不能代替常驻机器上的持续纸面进程。

动态杠杆研究在 `leverage_study.example.json`，结果在 `artifacts/research/leverage-study.json`。8% 止损配合 1% 风险预算时，规划器在真实 BTC 日线上始终停在 1 倍，没有为了用满 50 倍而放大仓位。

### 四币独立验证：预先标准通过，保留段有赢有亏

BTC 上入选的非对称慢速时间序列动量被原样拿去跑 ETHUSDT、SOLUSDT、BNBUSDT、XRPUSDT，没有在这些币上重选参数。空头仍只在已完成的 20 周均线之下，多头用日线 SMA(50)，连续 3 根 4 小时 K 线确认，最少持有 30 根，1% 风险，1–50 倍规划器，强平必须留在止损外面。评分截止到 2026-08-01；更早的 K 线只给指标预热。四币都是正的净值，2 倍手续费和滑点后仍为正，强平都是 0，等权组合夏普约 1.00，达到事先写的「至少 3/4、组合夏普不低于 0.5、无强平」。因此对 BTC 和这四币的 2026-08-01 至 2026-10-31 各看了一次，并加了第二套纸面配置 `paper_forward.alts.example.json`。它不改、也不替换正在运行的 1 倍日线进程。

这仍是局限很大的结果。规则本身来自 BTC 上的第二次迭代，而这四只永续和 BTC 高度同向，不是另一套经济环境。SOL 和 BNB 的策略净值远低于同期买入持有。K 线月份没有内部缺口。SOL 有 8 个资金费月份文件缺失，持仓期间还有一些资金费因为没有标记价而没有入账（ETH 6 次、SOL 14 次、BNB 16 次、XRP 17 次），这些都没有记成 0。开仓合约杠杆全部是 1 倍，因为 1% 风险和 ATR 止损放不下更高杠杆。保留段只有约三个月、笔数很少：BTC 约 -218.75，XRP 约 -145.78，ETH 约 +191.66，SOL 约 +305.36，BNB 约 +286.09，强平仍是 0。

| 币 | 净值 | 年化 | 夏普 | 最大回撤 | 笔数 | 多头已平仓 | 空头已平仓 | 2 倍成本后 | 买入持有 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ETHUSDT | +5031.46 | 7.6% | 0.73 | 16.4% | 87 | +4858.77 | +1457.13 | +4503.84 | -6896.15 |
| SOLUSDT | +8281.21 | 11.4% | 0.87 | 16.8% | 101 | +8434.43 | +970.06 | +7949.11 | +225403.48 |
| BNBUSDT | +12587.47 | 15.7% | 0.87 | 28.6% | 120 | +14519.87 | -701.83 | +11718.88 | +151369.20 |
| XRPUSDT | +2911.93 | 4.7% | 0.40 | 29.3% | 116 | +2638.10 | +726.93 | +2451.63 | +8817.19 |

等权组合起始 40000，净值约 +28812.07，最大回撤约 11.4%。多头已平仓合计约 +30451.16，空头合计约 +2452.29。按年净值大约是：ETH 2021 +1546、2022 +68、2023 +523、2024 +744、2025 +1889、2026 至 8 月前 +258；SOL +3477、+1121、+3005、-920、+968、+544；BNB +5192、+469、-364、+3763、+3257、+251；XRP +302、-343、-639、+2793、-268、+1094。某一年的净值可以和该年已平仓多空之和不同，资金费和跨年持仓算在净值里。账本在 `artifacts/research/alt-momentum-validation.json`。

### 中短线多空第二轮：仍不推荐

这是在同一段 2021-01-01 至 2026-08-01 拼接样本外上的第二次迭代。慢速参数、周线过滤，以及不再优化 ATR 突破，都是看过第一轮结果之后才定的。这段区间因此带有选择偏差，即使某条规则过门槛，也不能把它当成没见过的样本。2026-08-01 以后的保留段这次没有评分。`paper_forward.example.json` 仍是 1 倍、只做多的日线配置，没有第二套纸面进程，也没有更换正在运行的进程。

第二轮只有 6 个写死候选，都允许做多做空。空头必须处在已完成的 20 周均线之下。对称版的多头还要求周线向上；非对称版的多头仍用日线 SMA(50)。信号要连续 3 根 4 小时 K 线一致，新开仓后至少持有 30 根（约 5 天），止损和强平不受这个持仓时间限制。主线是慢速时间序列动量（48/180/540 根，权重 0.2/0.3/0.5，EMA 180）、慢速唐奇安通道（120 进、40 出），以及两者同向才开仓。止损都是 3 倍 ATR。

门槛和第一轮相同：夏普不低于 0.5、最大回撤不超过 35%、至少 20 笔、强平为 0、手续费和滑点放大到 2 倍后净值仍为正。没有任何一族同时通过。

| 策略 | 拼接样本外 | 说明 |
| --- | --- | --- |
| 慢速时间序列动量 | 净值 +2665.29，年化约 4.3%，夏普 0.448，回撤 16.8%，109 笔，强平 0 | 12 个训练窗都选了非对称过滤。多头已平仓 +3317.18（51 笔），空头 -1722.54（58 笔）。2 倍成本后净值仍约 +2200.85，但夏普不够。开仓全部是 1 倍，平均有效杠杆约 0.25。资金费缺 12 次，状态不完整。 |
| 慢速唐奇安通道 | 没有成交 | 全样本只有很少的做多/做空信号。每个训练窗都凑不满 8 笔合格交易，按规则该段样本外空仓。 |
| 两者同向 | 没有成交 | 同向信号更少，同样没有训练窗入选。 |

按年看，入选的非对称动量净值大约是 2021 +236、2022 -509、2023 +226、2024 +2242、2025 +660、2026 至 8 月前 -204。某一年的净值变化和该年已平仓多空之和可以不同，因为资金费和跨年持仓算在净值里。同一窗口的 1 倍买入持有净值大约 +154.94。账本在 `artifacts/research/swing-walkforward-round2.json`。

### 中短线多空第一轮：不推荐

4 小时动量/趋势网格都允许做多和做空（`allow_short=True`）。事先写死的门槛是：拼接样本外夏普不低于 0.5、最大回撤不超过 35%、至少 20 笔、强平为 0，并且手续费和滑点放大到 2 倍后净值仍为正。没有任何一条同时通过，所以不推荐，也不另开第二套纸面进程。2024 年以后的行情在更早的研究里看过，第一轮走步验证里的对应窗口仍算复用样本。

虚拟资金 10000 USDT，拼接样本外是 2021-01-01 至 2026-08-01。下面的多头/空头数字是已平仓交易的盈亏贡献，净值还包含资金费和未平仓盯市，两者不必相等。

| 策略 | 净值盈亏 | 年化 | 夏普 | 最大回撤 | 笔数 | 多头已平仓 | 空头已平仓 | 2 倍成本后净值 | 强平 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ATR 突破 | -904.23 | -1.7% | -1.07 | 10.2% | 185 | +26.16（125 笔） | -690.35（60 笔） | -1470.19 | 0 |
| 时间序列动量 | +4647.31 | +7.1% | 0.497 | 18.9% | 840 | +8231.10（422 笔） | -1450.61（418 笔） | -668.07 | 0 |
| 唐奇安通道 | +1230.49 | +2.1% | 0.32 | 11.3% | 139 | +1520.57（78 笔） | +416.28（61 笔） | +686.64 | 0 |

时间序列动量差 0.003 夏普未过门槛，2 倍成本后也转为亏损，资金费有 18 次缺失，状态是不完整。唐奇安通道多空都为正，但夏普不够。ATR 突破的亏损主要来自空头。同一窗口的 1 倍买入持有净值大约 +154.94，其中 27 次资金费没有标记价，没有记成 0。完整账本在 `artifacts/research/swing-walkforward.json`。

### 明确限制

- 不模拟期权、CME 期货，也不把现金 ETF 说成合约适配器。
- 示例没有真实资金费和独立标记/指数序列；报告会写明不可用，而不是把缺失记成 0。
- 永续杠杆只存在于研究账本里的保证金占用，不会向任何账户发送调杠杆请求。
- 旧的 Alpaca 示例 `market_autotrader.live-data.example.json` 需要本机密钥，研究命令不调用它。没有密钥时，现金股/ETF 使用自备 CSV。
- 样本切分只用于报告，不用来挑选参数。

## 配置说明

复制 `config.example.json` 后按需修改。示例里的纸上交易策略含义：

- 当 `BTCUSDT` 价格小于等于 `buy_below` 时，用虚拟 USDT 买入。
- 当 `BTCUSDT` 价格大于等于 `sell_above` 时，卖出虚拟 BTC。
- 每次买入最多使用 `trade_quote_amount` 指定的虚拟 USDT。

## 安全边界

请不要把真实 API Key 写进本项目。若你确实要做自动化交易，建议另建项目，并至少满足：

- API Key 绑定 IP 白名单
- 禁止提现权限
- 明确单笔和每日损失上限
- 每次真实下单前需要人工确认，或者至少使用独立审批开关
- 先在测试网或纸上交易环境长期验证

本项目不会执行无人值守真实下单。`market_autotrader.py` 的 `live` 模式会被硬拦截；当前仅支持纸上交易、人工审批票据和 Binance `/api/v3/order/test` 测试下单验证。真正接入券商或交易所真实撮合端点前，需要单独审查密钥权限、测试网结果、订单类型、异常处理和最大损失规则。