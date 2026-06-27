# VPS 一分钟速查

> 完整说明见 [DEVELOPMENT.md](./DEVELOPMENT.md)

## 登录

```bash
ssh -p 58231 -i ~/.ssh/vps_154 deploy@154.219.123.15
cd /home/deploy/market-autotrader
```

## 改代码后

```bash
python3 -m pytest tests/ -q --tb=line -x   # 可选
python3 market_autotrader.py --config market_autotrader.vps.json --once | tail -3
sudo systemctl restart market-autotrader
tail -f logs/market-autotrader-live-decisions.jsonl
```

## 闸门

| 操作 | 命令 |
|------|------|
| 允许下单 | `pwsh ./arm_live_trading.ps1 -Hours 8` |
| 禁止新开 | `rm logs/live-trading.armed` |
| 紧急停 | `touch logs/live-trading.kill` |

`arm_live_trading.ps1` writes a JSON arm file with an expiry timestamp; expired arm files block new live orders.

## 密钥

文件：`/home/deploy/market-autotrader/.env`（`chmod 600`）  
改后：`sudo systemctl restart market-autotrader`

## 服务

```bash
sudo systemctl status market-autotrader
sudo journalctl -u market-autotrader -f
```
