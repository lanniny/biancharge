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

状态和日志写在 `paper-state/`。停止命令只写 stop 文件，让正在运行的进程自己退出。
