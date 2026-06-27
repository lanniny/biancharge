import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from market_autotrader import AssetConfig, MarketBar, MarketSnapshot, StrategyConfig, build_signal, decimal_from
from review_events import append_reviews, review_event


SPOT_BASE = "https://api.binance.com"
FAPI_BASE = "https://fapi.binance.com"
PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
APPROVAL_TICKET_ACTIONS = {"SPOT_BUY_APPROVAL", "REDUCE_POSITION", "REASSESS_TAKE_PROFIT_OR_HOLD"}
DEFAULT_NOTIFY_ACTIONS = ["DATA_UNAVAILABLE", "REDUCE_POSITION", "SPOT_BUY_APPROVAL", "REASSESS_TAKE_PROFIT_OR_HOLD", "RESERVE_CASH"]


@dataclass(frozen=True)
class SupervisorConfig:
    poll_seconds: int
    request_timeout_seconds: int
    request_attempts: int
    ledger_path: str
    event_queue_path: str
    notify_dedup_path: str
    latest_alert_path: str
    model_review_path: str
    approval_dir: str
    recv_window: int
    futures_symbols: list[str]
    spot_symbols: list[str]
    reserve_futures_available_usdt: Decimal
    max_new_spot_quote_usdt: Decimal
    min_spot_cash_usdt: Decimal
    spcx_reduce_below: Decimal
    spcx_reassess_above: Decimal
    spcx_danger_buffer_pct: Decimal
    write_approval_tickets: bool
    notify_actions: list[str]
    notification_cooldown_seconds: int
    min_notify_priority: str
    desktop_notifications: bool
    desktop_notification_seconds: int


@dataclass(frozen=True)
class SupervisorRecommendation:
    action: str
    symbol: str
    market: str
    priority: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def request_json(url: str, headers: dict[str, str] | None = None, timeout_seconds: int = 3, attempts: int = 1) -> Any:
    last_error: str | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers or {"User-Agent": "codex-realtime-supervisor/1.0"})
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = body[:500]
            return {"_error": f"HTTP {exc.code}", "_body": parsed_body}
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1 + attempt)
    return {"_error": last_error or "unknown request error"}


def sign_query(params: dict[str, Any], secret: str) -> str:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def server_time(base_url: str, time_path: str, timeout_seconds: int = 3, attempts: int = 1) -> int:
    payload = request_json(f"{base_url}{time_path}", timeout_seconds=timeout_seconds, attempts=attempts)
    if not isinstance(payload, dict) or "serverTime" not in payload:
        raise RuntimeError(f"Could not read server time from {base_url}{time_path}: {payload}")
    return int(payload["serverTime"])


def signed_get(
    base_url: str,
    path: str,
    params: dict[str, Any] | None,
    time_path: str,
    recv_window: int,
    timeout_seconds: int = 3,
    attempts: int = 1,
) -> Any:
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        return {"_error": "BINANCE_API_KEY/BINANCE_API_SECRET missing"}
    signed_params = dict(params or {})
    signed_params["recvWindow"] = recv_window
    try:
        signed_params["timestamp"] = server_time(base_url, time_path, timeout_seconds=timeout_seconds, attempts=attempts)
    except Exception as exc:
        return {"_error": f"server time unavailable for signed request: {exc}"}
    url = f"{base_url}{path}?{sign_query(signed_params, api_secret)}"
    return request_json(
        url,
        headers={"X-MBX-APIKEY": api_key, "User-Agent": "codex-realtime-supervisor/1.0"},
        timeout_seconds=timeout_seconds,
        attempts=attempts,
    )


def load_config(path: str) -> SupervisorConfig:
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)
    return SupervisorConfig(
        poll_seconds=int(raw.get("poll_seconds", 30)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 3)),
        request_attempts=int(raw.get("request_attempts", 1)),
        ledger_path=raw.get("ledger_path", "logs/realtime-supervisor.jsonl"),
        event_queue_path=raw.get("event_queue_path", "logs/realtime-supervisor-events.jsonl"),
        notify_dedup_path=raw.get("notify_dedup_path", "logs/realtime-supervisor-notify-state.json"),
        latest_alert_path=raw.get("latest_alert_path", "logs/realtime-supervisor-latest-alert.txt"),
        model_review_path=raw.get("model_review_path", "logs/realtime-supervisor-model-reviews.jsonl"),
        approval_dir=raw.get("approval_dir", "approvals"),
        recv_window=int(raw.get("recv_window", 60000)),
        futures_symbols=raw.get("futures_symbols", ["SPCXUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]),
        spot_symbols=raw.get("spot_symbols", ["BTCUSDT", "SOLUSDT"]),
        reserve_futures_available_usdt=decimal_from(raw.get("reserve_futures_available_usdt", "30")),
        max_new_spot_quote_usdt=decimal_from(raw.get("max_new_spot_quote_usdt", "5")),
        min_spot_cash_usdt=decimal_from(raw.get("min_spot_cash_usdt", "5")),
        spcx_reduce_below=decimal_from(raw.get("spcx_reduce_below", "172.5")),
        spcx_reassess_above=decimal_from(raw.get("spcx_reassess_above", "188.5")),
        spcx_danger_buffer_pct=decimal_from(raw.get("spcx_danger_buffer_pct", "0.08")),
        write_approval_tickets=bool(raw.get("write_approval_tickets", True)),
        notify_actions=raw.get("notify_actions", DEFAULT_NOTIFY_ACTIONS),
        notification_cooldown_seconds=int(raw.get("notification_cooldown_seconds", 900)),
        min_notify_priority=raw.get("min_notify_priority", "medium"),
        desktop_notifications=bool(raw.get("desktop_notifications", True)),
        desktop_notification_seconds=int(raw.get("desktop_notification_seconds", 12)),
    )


def parse_decimal(value: Any) -> Decimal:
    return decimal_from(value or "0")


def has_error(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("_error"):
            return True
        return any(has_error(value) for value in payload.values())
    if isinstance(payload, list):
        return any(has_error(item) for item in payload)
    return False


def nonzero_spot_balances(account: Any) -> list[dict[str, str]]:
    balances = []
    if not isinstance(account, dict):
        return balances
    for item in account.get("balances", []):
        free = parse_decimal(item.get("free"))
        locked = parse_decimal(item.get("locked"))
        if free or locked:
            balances.append({"asset": item.get("asset", ""), "free": str(free), "locked": str(locked), "total": str(free + locked)})
    return balances


def public_klines(
    base_url: str,
    path: str,
    symbol: str,
    limit: int = 120,
    timeout_seconds: int = 3,
    attempts: int = 1,
) -> list[MarketBar]:
    query = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": limit})
    payload = request_json(f"{base_url}{path}?{query}", timeout_seconds=timeout_seconds, attempts=attempts)
    if isinstance(payload, dict) and "_error" in payload:
        raise RuntimeError(str(payload))
    return [
        MarketBar(
            timestamp=int(item[0]),
            open=parse_decimal(item[1]),
            high=parse_decimal(item[2]),
            low=parse_decimal(item[3]),
            close=parse_decimal(item[4]),
            volume=parse_decimal(item[5]),
        )
        for item in payload
    ]


def signal_for(symbol: str, market: str, config: SupervisorConfig | None = None) -> dict[str, Any]:
    timeout_seconds = config.request_timeout_seconds if config else 3
    attempts = config.request_attempts if config else 1
    if market == "futures":
        bars = public_klines(FAPI_BASE, "/fapi/v1/klines", symbol, timeout_seconds=timeout_seconds, attempts=attempts)
    else:
        bars = public_klines(SPOT_BASE, "/api/v3/klines", symbol, timeout_seconds=timeout_seconds, attempts=attempts)
    asset = AssetConfig(symbol=symbol, market=market, base_asset=symbol.replace("USDT", ""), quote_asset="USDT", provider={"type": "static"})
    signal = build_signal(MarketSnapshot(asset=asset, bars=bars, observed_at=int(time.time())), StrategyConfig())
    return {
        "symbol": symbol,
        "market": market,
        "price": str(bars[-1].close),
        "action": signal.action,
        "confidence": str(signal.confidence),
        "warnings": signal.warnings,
        "indicators": signal.indicators,
        "reasons": signal.reasons,
    }


def account_snapshot(config: SupervisorConfig) -> dict[str, Any]:
    spot_account = signed_get(
        SPOT_BASE,
        "/api/v3/account",
        {"omitZeroBalances": "false"},
        "/api/v3/time",
        config.recv_window,
        timeout_seconds=config.request_timeout_seconds,
        attempts=config.request_attempts,
    )
    futures_account = signed_get(
        FAPI_BASE,
        "/fapi/v3/account",
        {},
        "/fapi/v1/time",
        config.recv_window,
        timeout_seconds=config.request_timeout_seconds,
        attempts=config.request_attempts,
    )
    futures_positions = signed_get(
        FAPI_BASE,
        "/fapi/v3/positionRisk",
        {},
        "/fapi/v1/time",
        config.recv_window,
        timeout_seconds=config.request_timeout_seconds,
        attempts=config.request_attempts,
    )
    futures_open_orders = signed_get(
        FAPI_BASE,
        "/fapi/v1/openOrders",
        {},
        "/fapi/v1/time",
        config.recv_window,
        timeout_seconds=config.request_timeout_seconds,
        attempts=config.request_attempts,
    )
    nonzero_positions = []
    if isinstance(futures_positions, list):
        for position in futures_positions:
            amount = parse_decimal(position.get("positionAmt"))
            unrealized = parse_decimal(position.get("unRealizedProfit") or position.get("unrealizedProfit"))
            if amount or unrealized:
                nonzero_positions.append(position)
    return {
        "spotNonZeroBalances": nonzero_spot_balances(spot_account),
        "futuresAccount": {
            "totalWalletBalance": futures_account.get("totalWalletBalance") if isinstance(futures_account, dict) else None,
            "totalUnrealizedProfit": futures_account.get("totalUnrealizedProfit") if isinstance(futures_account, dict) else None,
            "totalMarginBalance": futures_account.get("totalMarginBalance") if isinstance(futures_account, dict) else None,
            "availableBalance": futures_account.get("availableBalance") if isinstance(futures_account, dict) else None,
        },
        "futuresNonZeroPositions": nonzero_positions,
        "futuresOpenOrders": futures_open_orders if isinstance(futures_open_orders, list) else futures_open_orders,
    }


def find_position(snapshot: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    for position in snapshot.get("futuresNonZeroPositions", []):
        if position.get("symbol") == symbol:
            return position
    return None


def spot_free_balance(snapshot: dict[str, Any], asset: str) -> Decimal:
    for balance in snapshot.get("spotNonZeroBalances", []):
        if balance.get("asset") == asset:
            return parse_decimal(balance.get("free"))
    return Decimal("0")


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def close_side_for_position(amount: Decimal) -> str:
    if amount > 0:
        return "平多 / 卖出 / reduce-only"
    if amount < 0:
        return "平空 / 买入 / reduce-only"
    return "无持仓"


def manual_order_plan_for(
    config: SupervisorConfig,
    recommendation: SupervisorRecommendation,
    cycle: dict[str, Any],
) -> dict[str, Any]:
    snapshot = cycle.get("account", {})
    if recommendation.action == "SPOT_BUY_APPROVAL":
        free_usdt = spot_free_balance(snapshot, "USDT")
        max_quote = parse_decimal(recommendation.details.get("maxQuoteUSDT"))
        quote_cap = min(free_usdt, max_quote) if max_quote else free_usdt
        return {
            "venue": "Binance 现货",
            "symbol": recommendation.symbol,
            "side": "买入",
            "orderType": "限价",
            "timeInForce": "GTC",
            "quoteAmountUSDT": format_decimal(quote_cap),
            "limitPriceHint": recommendation.details.get("price", ""),
            "constraints": [
                "现货买入，不使用杠杆或借贷",
                f"下单金额不超过 {format_decimal(quote_cap)} USDT",
                "若价格明显偏离复核价，取消并等待下一轮复核",
            ],
            "realOrderAllowed": False,
        }

    if recommendation.action in {"REDUCE_POSITION", "REASSESS_TAKE_PROFIT_OR_HOLD"}:
        position = find_position(snapshot, recommendation.symbol)
        amount = parse_decimal(position.get("positionAmt")) if position else Decimal("0")
        fraction = Decimal("0.5") if recommendation.action == "REDUCE_POSITION" else Decimal("0.25")
        quantity_hint = abs(amount) * fraction
        return {
            "venue": "Binance U本位合约",
            "symbol": recommendation.symbol,
            "side": close_side_for_position(amount),
            "orderType": "reduce-only partial close",
            "timeInForce": "GTC for limit orders",
            "quantityHint": format_decimal(quantity_hint) if quantity_hint else "",
            "constraints": [
                "只允许 reduce-only，禁止反向开仓",
                "不要提高杠杆，不要补保证金后继续加仓",
                "执行后重新确认 availableBalance 与强平距离",
            ],
            "realOrderAllowed": False,
        }

    if recommendation.action == "RESERVE_CASH":
        return {
            "venue": "Binance U本位合约",
            "symbol": recommendation.symbol,
            "side": "不下单",
            "orderType": "none",
            "constraints": [
                f"合约可用保证金低于 {format_decimal(config.reserve_futures_available_usdt)} USDT 储备线时不新开仓",
                "等待资金缓冲恢复或主动降低已有风险",
            ],
            "realOrderAllowed": False,
        }

    return {
        "venue": "none",
        "symbol": recommendation.symbol,
        "side": "不下单",
        "orderType": "none",
        "constraints": ["No approved manual order plan for this recommendation."],
        "realOrderAllowed": False,
    }


def autonomy_summary(recommendations: list[SupervisorRecommendation]) -> dict[str, Any]:
    ranked = sorted(
        recommendations,
        key=lambda item: (PRIORITY_RANK.get(item.priority, 0), item.action != "NO_TRADE"),
        reverse=True,
    )
    primary = ranked[0] if ranked else None
    return {
        "mode": "autonomous_analysis_approval_required",
        "realOrderAllowed": False,
        "liveTradingStatus": "blocked_by_design",
        "primaryAction": primary.action if primary else "NO_TRADE",
        "primarySymbol": primary.symbol if primary else "ALL",
        "primaryReason": primary.reason if primary else "No recommendation was produced.",
        "whatIsAutomatic": [
            "market/account polling",
            "signal scoring",
            "risk gates",
            "model review",
            "approval ticket generation",
            "desktop/file notification",
            "decision ledger",
        ],
        "whatRequiresHuman": ["real matching-engine order placement", "cancel/replace orders", "leverage changes", "transfers"],
    }


def make_recommendations(config: SupervisorConfig, snapshot: dict[str, Any], signals: list[dict[str, Any]]) -> list[SupervisorRecommendation]:
    recommendations: list[SupervisorRecommendation] = []
    futures_account = snapshot.get("futuresAccount", {})
    if has_error(snapshot) or any(signal.get("error") for signal in signals):
        recommendations.append(
            SupervisorRecommendation(
                action="DATA_UNAVAILABLE",
                symbol="ALL",
                market="all",
                priority="high",
                reason="One or more account or market data sources failed; block new trades until a clean read-only cycle succeeds.",
                details={
                    "accountDataError": has_error(snapshot),
                    "signalErrors": [
                        {"symbol": signal.get("symbol"), "market": signal.get("market"), "error": signal.get("error")}
                        for signal in signals
                        if signal.get("error")
                    ],
                },
            )
        )
        recommendations.append(
            SupervisorRecommendation(
                action="NO_TRADE",
                symbol="ALL",
                market="all",
                priority="medium",
                reason="No trade is permitted while supervisor data is incomplete.",
            )
        )
        return recommendations

    available = parse_decimal(futures_account.get("availableBalance"))
    if available < config.reserve_futures_available_usdt:
        recommendations.append(
            SupervisorRecommendation(
                action="RESERVE_CASH",
                symbol="USDT",
                market="futures",
                priority="high",
                reason=f"Futures available balance {available} is below reserve {config.reserve_futures_available_usdt}; do not open new leveraged trades.",
            )
        )

    spcx = find_position(snapshot, "SPCXUSDT")
    if spcx:
        mark_price = parse_decimal(spcx.get("markPrice"))
        liquidation = parse_decimal(spcx.get("liquidationPrice"))
        entry = parse_decimal(spcx.get("entryPrice"))
        distance_to_liq = (mark_price - liquidation) / mark_price if mark_price else Decimal("0")
        if mark_price <= config.spcx_reduce_below or distance_to_liq < config.spcx_danger_buffer_pct:
            recommendations.append(
                SupervisorRecommendation(
                    action="REDUCE_POSITION",
                    symbol="SPCXUSDT",
                    market="futures",
                    priority="critical",
                    reason="SPCXUSDT is near the configured reduce/danger zone.",
                    details={"markPrice": str(mark_price), "liquidationPrice": str(liquidation), "distanceToLiqPct": str(distance_to_liq)},
                )
            )
        elif mark_price >= config.spcx_reassess_above or mark_price >= entry:
            recommendations.append(
                SupervisorRecommendation(
                    action="REASSESS_TAKE_PROFIT_OR_HOLD",
                    symbol="SPCXUSDT",
                    market="futures",
                    priority="medium",
                    reason="SPCXUSDT is near reassessment/entry zone; consider whether to keep risk open or reduce into strength.",
                    details={"markPrice": str(mark_price), "entryPrice": str(entry), "breakEvenPrice": spcx.get("breakEvenPrice")},
                )
            )
        else:
            recommendations.append(
                SupervisorRecommendation(
                    action="HOLD_WITH_BUFFER",
                    symbol="SPCXUSDT",
                    market="futures",
                    priority="medium",
                    reason="SPCXUSDT is not in the danger zone but remains below entry; do not add.",
                    details={"markPrice": str(mark_price), "entryPrice": str(entry), "liquidationPrice": str(liquidation)},
                )
            )

    for signal in signals:
        if signal.get("market") != "spot":
            continue
        if signal.get("action") == "BUY" and parse_decimal(signal.get("confidence")) >= Decimal("0.70"):
            spot_free_usdt = spot_free_balance(snapshot, "USDT")
            if spot_free_usdt < config.min_spot_cash_usdt:
                recommendations.append(
                    SupervisorRecommendation(
                        action="SPOT_BUY_BLOCKED_CASH",
                        symbol=signal["symbol"],
                        market="spot",
                        priority="medium",
                        reason=f"Spot BUY signal exists, but free USDT {spot_free_usdt} is below minimum {config.min_spot_cash_usdt}; do not issue a buy ticket.",
                        details={"freeUSDT": str(spot_free_usdt), "minSpotCashUSDT": str(config.min_spot_cash_usdt), "price": signal.get("price")},
                    )
                )
                continue
            recommendations.append(
                SupervisorRecommendation(
                    action="SPOT_BUY_APPROVAL",
                    symbol=signal["symbol"],
                    market="spot",
                    priority="medium",
                    reason=f"Spot signal is BUY with confidence {signal['confidence']}; approval required before any real order.",
                    details={"maxQuoteUSDT": str(config.max_new_spot_quote_usdt), "price": signal.get("price"), "warnings": signal.get("warnings", [])},
                )
            )

    if not any(item.action.endswith("APPROVAL") or item.action == "REDUCE_POSITION" for item in recommendations):
        recommendations.append(
            SupervisorRecommendation(
                action="NO_TRADE",
                symbol="ALL",
                market="all",
                priority="medium",
                reason="No high-confidence action requires immediate manual approval this cycle.",
            )
        )
    return recommendations


def priority_meets_min(priority: str, minimum: str) -> bool:
    return PRIORITY_RANK.get(priority, 0) >= PRIORITY_RANK.get(minimum, 0)


def notification_candidate(config: SupervisorConfig, recommendation: SupervisorRecommendation) -> bool:
    return recommendation.action in set(config.notify_actions) and priority_meets_min(recommendation.priority, config.min_notify_priority)


def recommendation_event_key(recommendation: SupervisorRecommendation) -> str:
    return f"{recommendation.action}|{recommendation.market}|{recommendation.symbol}"


def load_notify_state(path: str) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {"events": {}}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), dict):
        return {"events": {}}
    return payload


def save_notify_state(path: str, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def relevant_signal(cycle: dict[str, Any], recommendation: SupervisorRecommendation) -> dict[str, Any] | None:
    for signal in cycle.get("signals", []):
        if signal.get("symbol") == recommendation.symbol and signal.get("market") == recommendation.market:
            return signal
    return None


def build_notification_event(
    config: SupervisorConfig,
    recommendation: SupervisorRecommendation,
    cycle: dict[str, Any],
    approval_ticket: str | None,
    now_ts: int,
) -> dict[str, Any]:
    key = recommendation_event_key(recommendation)
    event_id = hashlib.sha256(f"{key}|{now_ts}".encode("utf-8")).hexdigest()[:16]
    position = find_position(cycle.get("account", {}), recommendation.symbol)
    return {
        "eventId": event_id,
        "createdAt": datetime.fromtimestamp(now_ts, timezone.utc).astimezone().isoformat(),
        "dedupKey": key,
        "priority": recommendation.priority,
        "action": recommendation.action,
        "symbol": recommendation.symbol,
        "market": recommendation.market,
        "reason": recommendation.reason,
        "details": recommendation.details,
        "manualOrderPlan": manual_order_plan_for(config, recommendation, cycle),
        "requiresModelReview": True,
        "modelReviewStatus": "pending",
        "realOrderAllowed": False,
        "safetyBoundary": "Read-only supervisor: no real order, cancel, transfer, or leverage change was submitted.",
        "approvalTicket": approval_ticket,
        "reviewPacket": {
            "minimumQuestions": [
                "Is the recommendation still consistent with current account risk and available balance?",
                "Is the position size small enough for the configured max exposure?",
                "Would waiting reduce emotional or leverage-driven risk?",
                "Should this be downgraded to watch-only instead of shown as actionable?",
            ],
            "notifyUserOnlyAfterModelReview": True,
        },
        "context": {
            "cycleCreatedAt": cycle.get("createdAt"),
            "account": cycle.get("account"),
            "signal": relevant_signal(cycle, recommendation),
            "position": position,
        },
        "paths": {
            "ledgerPath": config.ledger_path,
            "eventQueuePath": config.event_queue_path,
        },
    }


def alert_text(event: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    lines = [
        f"[{event.get('createdAt')}] Codex realtime supervisor alert",
        f"priority: {event.get('priority')}",
        f"action: {event.get('action')}",
        f"symbol: {event.get('symbol')}",
        f"market: {event.get('market')}",
        f"reason: {event.get('reason')}",
        f"requiresModelReview: {event.get('requiresModelReview')}",
        f"realOrderAllowed: {event.get('realOrderAllowed')}",
        f"approvalTicket: {event.get('approvalTicket') or ''}",
        f"eventQueuePath: {event.get('paths', {}).get('eventQueuePath')}",
    ]
    if review:
        lines.extend(
            [
                "",
                "[model review]",
                f"reviewedAt: {review.get('reviewedAt')}",
                f"verdict: {review.get('verdict')}",
                f"userMessage: {review.get('userMessage')}",
                f"evidence: {json.dumps(review.get('evidence', {}), ensure_ascii=False)}",
                f"manualOrderPlan: {json.dumps(review.get('manualOrderPlan') or event.get('manualOrderPlan', {}), ensure_ascii=False)}",
                f"manualPlan: {json.dumps(review.get('manualPlan', []), ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_latest_alert(path: str, event: dict[str, Any], review: dict[str, Any] | None = None) -> None:
    alert_path = Path(path)
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    alert_path.write_text(alert_text(event, review), encoding="utf-8")


def ps_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def send_desktop_notification(config: SupervisorConfig, event: dict[str, Any], review: dict[str, Any] | None = None) -> bool:
    if not config.desktop_notifications or os.name != "nt":
        return False
    seconds = max(3, min(config.desktop_notification_seconds, 30))
    title = "Codex 盯盘: 模型复核结果"
    verdict = review.get("verdict") if review else "pending"
    message = f"{event.get('action')} {event.get('symbol')} {event.get('market')} => {verdict} | 查看 {config.latest_alert_path}"
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Warning
$notify.Visible = $true
$notify.ShowBalloonTip({seconds * 1000}, {ps_single_quoted(title)}, {ps_single_quoted(message)}, [System.Windows.Forms.ToolTipIcon]::Warning)
Start-Sleep -Seconds {seconds}
$notify.Dispose()
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def write_notification_events(
    config: SupervisorConfig,
    recommendations: list[SupervisorRecommendation],
    cycle: dict[str, Any],
    approval_tickets_by_key: dict[str, str],
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    now = int(now_ts if now_ts is not None else time.time())
    state = load_notify_state(config.notify_dedup_path)
    state_events = state.setdefault("events", {})
    emitted: list[dict[str, Any]] = []
    queue_path = Path(config.event_queue_path)
    for recommendation in recommendations:
        if not notification_candidate(config, recommendation):
            continue
        key = recommendation_event_key(recommendation)
        last_sent = int(state_events.get(key, 0) or 0)
        if now - last_sent < config.notification_cooldown_seconds:
            continue
        event = build_notification_event(config, recommendation, cycle, approval_tickets_by_key.get(key), now)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with open(queue_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
        review = review_event(event)
        append_reviews(config.model_review_path, [review])
        event["modelReviewStatus"] = review.get("verdict")
        event["modelReview"] = {
            "reviewedAt": review.get("reviewedAt"),
            "verdict": review.get("verdict"),
            "userMessage": review.get("userMessage"),
            "evidence": review.get("evidence"),
            "manualOrderPlan": review.get("manualOrderPlan") or event.get("manualOrderPlan"),
            "manualPlan": review.get("manualPlan"),
            "reviewLedgerPath": config.model_review_path,
        }
        write_latest_alert(config.latest_alert_path, event, review)
        send_desktop_notification(config, event, review)
        state_events[key] = now
        emitted.append(event)
    if emitted:
        save_notify_state(config.notify_dedup_path, state)
    return emitted


def notification_summary(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "eventId": event.get("eventId"),
        "createdAt": event.get("createdAt"),
        "priority": event.get("priority"),
        "action": event.get("action"),
        "symbol": event.get("symbol"),
        "market": event.get("market"),
        "requiresModelReview": event.get("requiresModelReview"),
        "realOrderAllowed": event.get("realOrderAllowed"),
        "approvalTicket": event.get("approvalTicket"),
        "manualOrderPlan": event.get("modelReview", {}).get("manualOrderPlan") or event.get("manualOrderPlan"),
        "modelReviewStatus": event.get("modelReviewStatus"),
        "modelReviewMessage": event.get("modelReview", {}).get("userMessage"),
        "modelReviewPath": event.get("modelReview", {}).get("reviewLedgerPath"),
        "eventQueuePath": event.get("paths", {}).get("eventQueuePath"),
    }


def write_approval_ticket(config: SupervisorConfig, recommendation: SupervisorRecommendation, cycle: dict[str, Any]) -> str:
    Path(config.approval_dir).mkdir(parents=True, exist_ok=True)
    safe_symbol = recommendation.symbol.replace("/", "")
    path = Path(config.approval_dir) / f"{int(time.time())}_{recommendation.action}_{safe_symbol}.json"
    ticket = {
        "status": "awaiting_manual_approval",
        "realOrderAllowed": False,
        "recommendation": asdict(recommendation),
        "manualOrderPlan": manual_order_plan_for(config, recommendation, cycle),
        "manualChecks": [
            "Confirm order manually in Binance UI.",
            "Do not exceed the maxQuoteUSDT or reduction amount in this ticket.",
            "Do not add leverage to an existing stressed position.",
            "This ticket is not an automated real-order instruction.",
        ],
        "cycle": cycle,
    }
    path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def run_cycle(config: SupervisorConfig) -> dict[str, Any]:
    snapshot = account_snapshot(config)
    signals = []
    for symbol in config.futures_symbols:
        try:
            signals.append(signal_for(symbol, "futures", config))
        except Exception as exc:
            signals.append({"symbol": symbol, "market": "futures", "error": str(exc)})
    for symbol in config.spot_symbols:
        try:
            signals.append(signal_for(symbol, "spot", config))
        except Exception as exc:
            signals.append({"symbol": symbol, "market": "spot", "error": str(exc)})
    recommendations = make_recommendations(config, snapshot, signals)
    cycle = {
        "createdAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "mode": "read_only_supervisor",
        "autonomy": autonomy_summary(recommendations),
        "account": snapshot,
        "signals": signals,
        "recommendations": [asdict(item) for item in recommendations],
        "approvalTickets": [],
    }
    if config.write_approval_tickets:
        approval_tickets_by_key = {}
        for recommendation in recommendations:
            if recommendation.action in APPROVAL_TICKET_ACTIONS:
                ticket_path = write_approval_ticket(config, recommendation, cycle)
                cycle["approvalTickets"].append(ticket_path)
                approval_tickets_by_key[recommendation_event_key(recommendation)] = ticket_path
    else:
        approval_tickets_by_key = {}
    events = write_notification_events(config, recommendations, cycle, approval_tickets_by_key)
    cycle["notificationEvents"] = [notification_summary(event) for event in events]
    Path(config.ledger_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config.ledger_path, "a", encoding="utf-8") as file:
        file.write(json.dumps(cycle, ensure_ascii=False) + "\n")
    return cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only realtime market supervisor with approval tickets.")
    parser.add_argument("--config", default="realtime_supervisor.example.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    while True:
        cycle = run_cycle(config)
        print(json.dumps(cycle, ensure_ascii=False, indent=2))
        if args.once:
            return 0
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())