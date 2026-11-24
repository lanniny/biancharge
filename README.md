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