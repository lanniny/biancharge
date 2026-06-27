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