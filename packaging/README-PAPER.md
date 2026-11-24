# BTCUSDT U 本位永续纸面前向

这是虚拟资金的前向纸面程序。它不发订单、不读 API key、不走代理。

默认：

- 品种：BTCUSDT USD-M 永续
- 虚拟资金：10000 USDT
- 模拟杠杆：1 倍
- 信号：日线 MA20/50，动量 20，止损 8%
- 历史日线只预热均线，不回补成前向成交

## 命令

在解压后的目录里：

```powershell
python paper_forward.py preflight --config paper_forward.example.json
python paper_forward.py start --config paper_forward.example.json
python paper_forward.py status --config paper_forward.example.json
python paper_forward.py stop --config paper_forward.example.json
```

`preflight` 只检查公开行情。`status` 在没有新鲜心跳时会显示 `not_started` 或 `not_running`。不要把离线回放或一次预检说成正在跑模拟盘。

行情地址都在 `fstream.binance.com`：

- 日线 K 线和标记价：`/market/stream?streams=btcusdt@markPrice@1s/btcusdt@kline_1d`
- 买卖报价：`/ws/btcusdt@bookTicker`

FAPI REST 若返回 HTTP 451，不要用代理绕过，也不要把现货行情当成永续。预检失败就不要 start。

状态和日志写在 `paper-state/`。停止命令只写 stop 文件，让正在运行的进程自己退出。行情套接字断开或时间戳超过 `stale_mark_seconds` / `stale_book_seconds` 时，进程记一条带时间戳的 `feed_error` 或 `feed_disconnect`，按 1、2、4 秒退避重连，最多 60 秒。单次行情异常不会退出。断流期间不开新仓。`status` 在行情过期时显示 `stale`，不会把过期行情报成 `heartbeat_fresh`。

## 常驻机器切换

旧的 BTC 1 倍进程还在用更早的包。新包同时带 `paper_forward.example.json` 和 `paper_forward.alts.example.json`。不要删现有状态文件。

1. 在旧进程的目录执行 `python paper_forward.py stop --config paper_forward.example.json`，确认那个进程已经退出。保留 `paper-state/btcusdt-paper-state.json`。
2. 把新包解压到新目录，或只替换代码。状态目录继续用原来的 `paper-state/`。
3. 用同一状态文件启动新的 BTC 纸面：`python paper_forward.py start --config paper_forward.example.json`。空仓和现金 10000 会原样读入。
4. 另开一个进程启动四币纸面：`python paper_forward.py start --config paper_forward.alts.example.json`。它写入 `paper-state/alt-slow-momentum-state.json`，不读 BTC 的状态。
5. `python paper_forward.py status --config paper_forward.example.json` 和 `--config paper_forward.alts.example.json`。行情不更新时进程应仍在，状态是 `stale`。

正在运行的进程应继续使用 `paper_forward.example.json`（固定 1 倍）。`paper_forward.dynamic.example.json` 是以后可选的纸面配置：信号仍是同一套日线规则，杠杆由风险预算和强平缓冲决定，不会为了用满 50 倍而放大仓位。重启前要停掉旧进程。已有持仓的保证金和杠杆不会被改写。

正在运行的进程应继续使用 `paper_forward.example.json`。`paper_forward.alts.example.json` 是另一套进程：ETH、SOL、BNB、XRP 各 10000 USDT，规则是已经冻结的非对称慢速动量，状态文件是 `paper-state/alt-slow-momentum-state.json`。不要用它替换正在跑的 1 倍日线进程。

```powershell
python research_leverage_study.py
```

2024 年以后的区间已经看过，不能再当成全新的样本外。2026-08-01 以后的保留段这次没有评分。
