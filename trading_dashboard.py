import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = "realtime_supervisor.example.json"


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>交易智能体控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #172033;
      --muted: #667085;
      --green: #157347;
      --red: #b42318;
      --amber: #a15c07;
      --blue: #175cd3;
      --shadow: 0 1px 2px rgba(16, 24, 40, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { font-size: 18px; margin: 0; font-weight: 700; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
    main { padding: 18px 22px 28px; max-width: 1500px; margin: 0 auto; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    button, input, select, textarea {
      font: inherit;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
    }
    button {
      height: 34px;
      padding: 0 12px;
      cursor: pointer;
      box-shadow: var(--shadow);
    }
    button:hover { border-color: #98a2b3; }
    button.primary { background: #175cd3; border-color: #175cd3; color: #fff; }
    button.danger { background: var(--red); border-color: var(--red); color: #fff; }
    button.ghost { box-shadow: none; background: #f8fafc; }
    button:disabled { opacity: .55; cursor: wait; }
    .grid { display: grid; gap: 14px; }
    .cards { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; }
    .layout { grid-template-columns: minmax(0, 1.35fr) minmax(360px, .65fr); align-items: start; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel h2 {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 0;
      padding: 12px 14px;
      font-size: 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }
    .panel-body { padding: 12px 14px; }
    .metric { padding: 12px 14px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { margin-top: 5px; font-size: 20px; font-weight: 700; word-break: break-word; }
    .metric .hint { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #f8fafc;
      font-size: 12px;
      white-space: nowrap;
    }
    .pill.green { color: var(--green); border-color: #abefc6; background: #ecfdf3; }
    .pill.red { color: var(--red); border-color: #fecdca; background: #fef3f2; }
    .pill.amber { color: var(--amber); border-color: #fedf89; background: #fffaeb; }
    .pill.blue { color: var(--blue); border-color: #b2ddff; background: #eff8ff; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 9px 10px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; background: #fbfcfe; }
    td { font-size: 13px; }
    .mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }
    .muted { color: var(--muted); }
    .stack { display: grid; gap: 12px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    label { display: grid; gap: 4px; color: var(--muted); font-size: 12px; }
    input, select { height: 34px; padding: 0 9px; min-width: 0; }
    textarea { min-height: 72px; padding: 8px 9px; resize: vertical; }
    .wide { grid-column: 1 / -1; }
    .alert-text {
      white-space: pre-wrap;
      max-height: 330px;
      overflow: auto;
      background: #0f172a;
      color: #dbeafe;
      border-radius: 6px;
      padding: 12px;
    }
    .notice {
      border: 1px solid #fedf89;
      background: #fffaeb;
      color: #7a4100;
      border-radius: 6px;
      padding: 10px 12px;
    }
    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: min(520px, calc(100vw - 36px));
      background: #101828;
      color: #fff;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: 0 12px 28px rgba(16, 24, 40, .25);
      display: none;
      z-index: 5;
    }
    .split { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    @media (max-width: 1000px) {
      header { align-items: flex-start; flex-direction: column; }
      .cards, .layout { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>交易智能体控制台</h1>
      <div class="sub">本地只读监督、模型复核、审批单和手动真实下单准备</div>
    </div>
    <div class="toolbar">
      <button class="ghost" id="refreshBtn">刷新</button>
      <button class="primary" id="runOnceBtn">运行一次复核</button>
      <button id="startBtn">启动盯盘</button>
      <button id="stopBtn">停止盯盘</button>
      <button class="danger" id="ticketBtn">真实下单审批</button>
    </div>
  </header>

  <main>
    <section class="grid cards">
      <div class="panel metric"><div class="label">后台盯盘</div><div class="value" id="runnerState">-</div><div class="hint" id="runnerHint"></div></div>
      <div class="panel metric"><div class="label">主决策</div><div class="value" id="primaryAction">-</div><div class="hint" id="primaryReason"></div></div>
      <div class="panel metric"><div class="label">模型复核</div><div class="value" id="reviewVerdict">-</div><div class="hint" id="reviewMessage"></div></div>
      <div class="panel metric"><div class="label">真实下单状态</div><div class="value" id="liveStatus">审批模式</div><div class="hint">按钮只生成审批单，不直连交易所</div></div>
    </section>

    <section class="grid layout">
      <div class="stack">
        <div class="panel">
          <h2>账户与持仓 <span class="pill" id="cycleTime">-</span></h2>
          <div class="panel-body" id="accountView"></div>
        </div>
        <div class="panel">
          <h2>市场信号 <span class="pill blue" id="signalCount">0</span></h2>
          <div class="panel-body" id="signalsView"></div>
        </div>
        <div class="panel">
          <h2>建议与风控</h2>
          <div class="panel-body" id="recommendationsView"></div>
        </div>
      </div>

      <div class="stack">
        <div class="panel">
          <h2>真实下单审批台 <span class="pill red">不自动提交</span></h2>
          <div class="panel-body stack">
            <div class="notice">这里的红色按钮用于生成真实交易审批单和 Binance 手动填单参数。系统不会调用真实下单、撤单、转账或调杠杆接口。</div>
            <div class="form-grid">
              <label>市场
                <select id="orderMarket"><option value="spot">现货</option><option value="futures">U本位合约</option></select>
              </label>
              <label>交易对
                <input id="orderSymbol" value="BTCUSDT" autocomplete="off">
              </label>
              <label>方向
                <select id="orderSide"><option value="买入">买入</option><option value="卖出">卖出</option><option value="平多 / 卖出 / reduce-only">平多 / 卖出 / reduce-only</option><option value="平空 / 买入 / reduce-only">平空 / 买入 / reduce-only</option></select>
              </label>
              <label>订单类型
                <select id="orderType"><option value="限价">限价</option><option value="市价">市价</option><option value="reduce-only partial close">reduce-only partial close</option></select>
              </label>
              <label>数量
                <input id="orderQty" placeholder="例如 0.01" autocomplete="off">
              </label>
              <label>金额 USDT
                <input id="orderQuote" placeholder="例如 5" autocomplete="off">
              </label>
              <label>限价
                <input id="orderPrice" placeholder="可空" autocomplete="off">
              </label>
              <label>有效期
                <select id="orderTif"><option value="GTC">GTC</option><option value="IOC">IOC</option><option value="FOK">FOK</option></select>
              </label>
              <label class="wide">备注
                <textarea id="orderNotes" placeholder="写下为什么要交易、止损/止盈、最大亏损"></textarea>
              </label>
            </div>
            <div class="split">
              <button id="fillFromPlanBtn">从最新复核填入</button>
              <button class="danger" id="createTicketBtn">创建真实下单审批单</button>
            </div>
            <div class="muted mono" id="ticketResult"></div>
          </div>
        </div>

        <div class="panel">
          <h2>最新告警</h2>
          <div class="panel-body"><div class="alert-text mono" id="latestAlert"></div></div>
        </div>
        <div class="panel">
          <h2>审批单</h2>
          <div class="panel-body" id="approvalsView"></div>
        </div>
      </div>
    </section>
  </main>
  <div class="toast" id="toast"></div>
  <script>
    let state = null;
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const pill = (text, cls = "") => `<span class="pill ${cls}">${esc(text)}</span>`;

    function verdictClass(value) {
      if (value === "confirm") return "green";
      if (value === "block" || value === "critical") return "red";
      if (value === "downgrade" || value === "high") return "amber";
      return "blue";
    }

    function toast(message) {
      const el = $("toast");
      el.textContent = message;
      el.style.display = "block";
      setTimeout(() => { el.style.display = "none"; }, 4200);
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {"Content-Type": "application/json"},
        ...options
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || res.statusText);
      return payload;
    }

    function renderAccount(cycle) {
      const account = cycle?.account || {};
      const futures = account.futuresAccount || {};
      const balances = account.spotNonZeroBalances || [];
      const positions = account.futuresNonZeroPositions || [];
      const balanceRows = balances.length ? balances.map(b => `<tr><td>${esc(b.asset)}</td><td class="mono">${esc(b.free)}</td><td class="mono">${esc(b.locked)}</td><td class="mono">${esc(b.total)}</td></tr>`).join("") : `<tr><td colspan="4" class="muted">无现货非零余额或账户不可用</td></tr>`;
      const posRows = positions.length ? positions.map(p => `<tr><td>${esc(p.symbol)}</td><td>${esc(p.positionSide || "")}</td><td class="mono">${esc(p.positionAmt)}</td><td class="mono">${esc(p.entryPrice)}</td><td class="mono">${esc(p.markPrice)}</td><td class="mono">${esc(p.unRealizedProfit || p.unrealizedProfit)}</td><td class="mono">${esc(p.liquidationPrice)}</td></tr>`).join("") : `<tr><td colspan="7" class="muted">无合约持仓或账户不可用</td></tr>`;
      $("accountView").innerHTML = `
        <div class="split">
          ${pill("钱包 " + (futures.totalWalletBalance ?? "-"))}
          ${pill("可用 " + (futures.availableBalance ?? "-"), futures.availableBalance ? "blue" : "amber")}
          ${pill("未实现盈亏 " + (futures.totalUnrealizedProfit ?? "-"))}
          ${pill("保证金余额 " + (futures.totalMarginBalance ?? "-"))}
        </div>
        <h3>现货余额</h3>
        <table><thead><tr><th>资产</th><th>可用</th><th>锁定</th><th>合计</th></tr></thead><tbody>${balanceRows}</tbody></table>
        <h3>合约持仓</h3>
        <table><thead><tr><th>合约</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>未实现</th><th>强平价</th></tr></thead><tbody>${posRows}</tbody></table>
      `;
    }

    function renderSignals(signals) {
      $("signalCount").textContent = signals.length;
      const rows = signals.length ? signals.map(s => `
        <tr>
          <td>${esc(s.symbol)}</td>
          <td>${esc(s.market)}</td>
          <td>${s.error ? pill("ERROR", "red") : pill(s.action || "-", verdictClass(s.action === "BUY" ? "confirm" : ""))}</td>
          <td class="mono">${esc(s.price || "-")}</td>
          <td class="mono">${esc(s.confidence || "-")}</td>
          <td>${esc((s.warnings || []).join(", ") || s.error || "")}</td>
        </tr>`).join("") : `<tr><td colspan="6" class="muted">暂无信号</td></tr>`;
      $("signalsView").innerHTML = `<table><thead><tr><th>交易对</th><th>市场</th><th>动作</th><th>价格</th><th>置信度</th><th>警告/错误</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    function renderRecommendations(items) {
      const rows = items.length ? items.map(r => `<tr><td>${pill(r.action, verdictClass(r.priority))}</td><td>${esc(r.symbol)}</td><td>${esc(r.market)}</td><td>${esc(r.priority)}</td><td>${esc(r.reason)}</td></tr>`).join("") : `<tr><td colspan="5" class="muted">暂无建议</td></tr>`;
      $("recommendationsView").innerHTML = `<table><thead><tr><th>动作</th><th>标的</th><th>市场</th><th>优先级</th><th>理由</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    function renderApprovals(items) {
      const rows = items.length ? items.map(a => `<tr><td class="mono">${esc(a.name)}</td><td>${esc(a.modifiedAt)}</td><td class="mono">${esc(a.size)}</td></tr>`).join("") : `<tr><td colspan="3" class="muted">暂无审批单</td></tr>`;
      $("approvalsView").innerHTML = `<table><thead><tr><th>文件</th><th>时间</th><th>大小</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    function latestPlan() {
      const review = state?.latestReview || {};
      const eventPlan = state?.latestEvent?.manualOrderPlan || {};
      return review.manualOrderPlan || eventPlan || {};
    }

    function fillFromPlan() {
      const plan = latestPlan();
      if (!plan || !Object.keys(plan).length) {
        toast("没有可填入的最新订单方案");
        return;
      }
      $("orderSymbol").value = plan.symbol || "BTCUSDT";
      $("orderMarket").value = String(plan.venue || "").includes("合约") ? "futures" : "spot";
      $("orderSide").value = plan.side || "买入";
      $("orderType").value = plan.orderType || "限价";
      $("orderQuote").value = plan.quoteAmountUSDT || "";
      $("orderQty").value = plan.quantityHint || "";
      $("orderPrice").value = plan.limitPriceHint || "";
      $("orderTif").value = plan.timeInForce && String(plan.timeInForce).includes("GTC") ? "GTC" : "GTC";
      toast("已从最新复核填入表单");
    }

    async function refresh() {
      state = await api("/api/status");
      const cycle = state.latestCycle || {};
      const autonomy = cycle.autonomy || {};
      const review = state.latestReview || {};
      $("runnerState").innerHTML = state.supervisor?.running ? "运行中" : "未运行";
      $("runnerHint").textContent = state.supervisor?.pid ? `PID ${state.supervisor.pid}` : "";
      $("primaryAction").textContent = autonomy.primaryAction || "-";
      $("primaryReason").textContent = autonomy.primaryReason || "";
      $("reviewVerdict").innerHTML = review.verdict ? `<span class="pill ${verdictClass(review.verdict)}">${esc(review.verdict)}</span>` : "-";
      $("reviewMessage").textContent = review.userMessage || "";
      $("liveStatus").textContent = "审批模式";
      $("cycleTime").textContent = cycle.createdAt || "-";
      $("latestAlert").textContent = state.latestAlertText || "";
      renderAccount(cycle);
      renderSignals(cycle.signals || []);
      renderRecommendations(cycle.recommendations || []);
      renderApprovals(state.approvals || []);
    }

    async function action(path, label) {
      const buttons = document.querySelectorAll("button");
      buttons.forEach(b => b.disabled = true);
      try {
        const res = await api(path, {method: "POST", body: "{}"});
        toast(res.message || `${label}完成`);
        await refresh();
      } catch (err) {
        toast(`${label}失败: ${err.message}`);
      } finally {
        buttons.forEach(b => b.disabled = false);
      }
    }

    async function createTicket() {
      const payload = {
        market: $("orderMarket").value,
        symbol: $("orderSymbol").value.trim(),
        side: $("orderSide").value,
        orderType: $("orderType").value,
        quantity: $("orderQty").value.trim(),
        quoteAmountUSDT: $("orderQuote").value.trim(),
        limitPrice: $("orderPrice").value.trim(),
        timeInForce: $("orderTif").value,
        notes: $("orderNotes").value.trim()
      };
      try {
        const res = await api("/api/live-order-request", {method: "POST", body: JSON.stringify(payload)});
        $("ticketResult").textContent = `${res.ticket.status}: ${res.path}`;
        toast("真实下单审批单已生成");
        await refresh();
      } catch (err) {
        toast(`审批单生成失败: ${err.message}`);
      }
    }

    $("refreshBtn").onclick = refresh;
    $("runOnceBtn").onclick = () => action("/api/run-once", "运行一次复核");
    $("startBtn").onclick = () => action("/api/start", "启动盯盘");
    $("stopBtn").onclick = () => action("/api/stop", "停止盯盘");
    $("ticketBtn").onclick = () => document.getElementById("createTicketBtn").scrollIntoView({behavior: "smooth", block: "center"});
    $("fillFromPlanBtn").onclick = fillFromPlan;
    $("createTicketBtn").onclick = createTicket;
    refresh().catch(err => toast(err.message));
    setInterval(() => refresh().catch(() => {}), 15000);
  </script>
</body>
</html>
"""


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def read_jsonl_tail(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def latest_jsonl(path: Path) -> dict[str, Any]:
    rows = read_jsonl_tail(path, 1)
    return rows[-1] if rows else {}


def process_running(pid: str | int | None) -> bool:
    if not pid:
        return False
    try:
        numeric_pid = int(str(pid).strip())
    except ValueError:
        return False
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {numeric_pid} -ErrorAction SilentlyContinue) {{ 'true' }} else {{ 'false' }}",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        return result.stdout.strip().lower() == "true"
    try:
        os.kill(numeric_pid, 0)
    except OSError:
        return False
    return True


def approval_files(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    approvals = root / "approvals"
    if not approvals.exists():
        return []
    files = [item for item in approvals.rglob("*.json") if item.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    result = []
    for item in files[:limit]:
        stat = item.stat()
        result.append(
            {
                "name": str(item.relative_to(root)),
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    return result


def load_status(root: Path = ROOT) -> dict[str, Any]:
    logs = root / "logs"
    pid_text = read_text(logs / "realtime-supervisor.pid").strip()
    latest_cycle = latest_jsonl(logs / "realtime-supervisor.jsonl")
    latest_review = latest_jsonl(logs / "realtime-supervisor-model-reviews.jsonl")
    latest_event = latest_jsonl(logs / "realtime-supervisor-events.jsonl")
    return {
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "supervisor": {
            "pid": pid_text,
            "running": process_running(pid_text),
            "heartbeat": read_text(logs / "realtime-supervisor-heartbeat.txt").strip(),
            "runnerLogTail": read_text(logs / "realtime-supervisor-runner.log").splitlines()[-8:],
        },
        "latestCycle": latest_cycle,
        "latestReview": latest_review,
        "latestEvent": latest_event,
        "latestAlertText": read_text(logs / "realtime-supervisor-latest-alert.txt"),
        "approvals": approval_files(root),
    }


def run_command(command: list[str], cwd: Path = ROOT, timeout: int = 120) -> dict[str, Any]:
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, errors="replace")
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    }


def live_order_status(latest_cycle: dict[str, Any], latest_review: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    primary_action = latest_cycle.get("autonomy", {}).get("primaryAction")
    verdict = latest_review.get("verdict")
    if primary_action == "DATA_UNAVAILABLE":
        reasons.append("DATA_UNAVAILABLE: account or market data is incomplete")
    if verdict == "block":
        reasons.append("latest model review is block")
    if not latest_cycle:
        reasons.append("no supervisor cycle is available")
    if reasons:
        return "blocked_by_risk_gate", reasons
    return "awaiting_manual_exchange_entry", ["manual approval required before any exchange order"]


def create_live_order_request(payload: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    status = load_status(root)
    latest_cycle = status.get("latestCycle", {})
    latest_review = status.get("latestReview", {})
    ticket_status, blockers = live_order_status(latest_cycle, latest_review)
    symbol = str(payload.get("symbol") or "UNKNOWN").upper().replace("/", "")
    timestamp = int(time.time())
    ticket = {
        "kind": "LIVE_ORDER_REQUEST",
        "status": ticket_status,
        "createdAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "realOrderAllowed": False,
        "notSubmittedToExchange": True,
        "requestedOrder": {
            "market": payload.get("market"),
            "symbol": symbol,
            "side": payload.get("side"),
            "orderType": payload.get("orderType"),
            "quantity": payload.get("quantity"),
            "quoteAmountUSDT": payload.get("quoteAmountUSDT"),
            "limitPrice": payload.get("limitPrice"),
            "timeInForce": payload.get("timeInForce"),
            "notes": payload.get("notes"),
        },
        "riskGate": {
            "status": ticket_status,
            "blockers": blockers,
            "latestPrimaryAction": latest_cycle.get("autonomy", {}).get("primaryAction"),
            "latestReviewVerdict": latest_review.get("verdict"),
            "latestReviewMessage": latest_review.get("userMessage"),
        },
        "manualChecks": [
            "Confirm the same symbol, side, quantity, order type, and price in Binance UI.",
            "Do not use this ticket if the latest review verdict is block or data is unavailable.",
            "Use reduce-only for futures close orders and do not increase leverage from this workflow.",
            "Record the Binance order id manually after exchange-side submission.",
        ],
        "source": {
            "latestCycleCreatedAt": latest_cycle.get("createdAt"),
            "latestReviewEventId": latest_review.get("eventId"),
        },
    }
    out_dir = root / "approvals" / "live_order_requests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{timestamp}_LIVE_ORDER_REQUEST_{symbol}.json"
    path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "ticket": ticket}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TradingDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        if path == "/":
            raw = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/status":
            self.send_json(load_status(ROOT))
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        try:
            if path == "/api/run-once":
                result = run_command(["python", "realtime_supervisor.py", "--config", DEFAULT_CONFIG, "--once"], timeout=120)
                self.send_json({"message": "run-once finished", "result": result, "status": load_status(ROOT)})
                return
            if path == "/api/start":
                result = run_command(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "start_realtime_supervisor.ps1"], timeout=30)
                self.send_json({"message": "supervisor start requested", "result": result, "status": load_status(ROOT)})
                return
            if path == "/api/stop":
                result = run_command(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "stop_realtime_supervisor.ps1"], timeout=30)
                self.send_json({"message": "supervisor stop requested", "result": result, "status": load_status(ROOT)})
                return
            if path == "/api/live-order-request":
                payload = self.read_body()
                ticket = create_live_order_request(payload, ROOT)
                self.send_json(ticket, HTTPStatus.CREATED)
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local UI for the trading supervisor.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Trading dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())