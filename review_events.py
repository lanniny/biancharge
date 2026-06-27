import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_autotrader import decimal_from


PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_EVENT_QUEUE = "logs/realtime-supervisor-events.jsonl"
DEFAULT_REVIEW_LEDGER = "logs/realtime-supervisor-model-reviews.jsonl"


def parse_decimal(value: Any) -> Decimal:
    return decimal_from(value or "0")


def read_jsonl(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def existing_reviewed_ids(path: str) -> set[str]:
    reviewed = set()
    for item in read_jsonl(path):
        event_id = item.get("eventId")
        if event_id:
            reviewed.add(str(event_id))
    return reviewed


def newest_pending_events(event_queue_path: str, review_ledger_path: str, limit: int) -> list[dict[str, Any]]:
    reviewed = existing_reviewed_ids(review_ledger_path)
    pending = [event for event in read_jsonl(event_queue_path) if str(event.get("eventId")) not in reviewed]
    pending.sort(key=lambda event: (PRIORITY_RANK.get(str(event.get("priority")), 0), str(event.get("createdAt", ""))), reverse=True)
    return pending[:limit]


def find_position(event: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    account = event.get("context", {}).get("account", {})
    for position in account.get("futuresNonZeroPositions", []):
        if position.get("symbol") == symbol:
            return position
    return None


def context_account(event: dict[str, Any]) -> dict[str, Any]:
    account = event.get("context", {}).get("account", {})
    return account if isinstance(account, dict) else {}


def context_signal(event: dict[str, Any]) -> dict[str, Any]:
    signal = event.get("context", {}).get("signal", {})
    return signal if isinstance(signal, dict) else {}


def spot_free_balance(event: dict[str, Any], asset: str = "USDT") -> Decimal:
    for balance in context_account(event).get("spotNonZeroBalances", []):
        if balance.get("asset") == asset:
            return parse_decimal(balance.get("free"))
    return Decimal("0")


def futures_available_balance(event: dict[str, Any]) -> Decimal:
    return parse_decimal(context_account(event).get("futuresAccount", {}).get("availableBalance"))


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


def reserve_cash_review(event: dict[str, Any]) -> dict[str, Any]:
    account = event.get("context", {}).get("account", {})
    futures_account = account.get("futuresAccount", {})
    available = parse_decimal(futures_account.get("availableBalance"))
    margin_balance = parse_decimal(futures_account.get("totalMarginBalance"))
    unrealized = parse_decimal(futures_account.get("totalUnrealizedProfit"))
    spcx = find_position(event, "SPCXUSDT")
    mark = parse_decimal(spcx.get("markPrice")) if spcx else Decimal("0")
    liquidation = parse_decimal(spcx.get("liquidationPrice")) if spcx else Decimal("0")
    distance_to_liq = (mark - liquidation) / mark if mark else Decimal("0")

    if available < Decimal("30"):
        verdict = "confirm"
        user_message = (
            "合约可用保证金低于 30 USDT 储备线，当前不适合新开任何杠杆仓；"
            "优先动作是停止加仓，保留保证金缓冲。"
        )
    else:
        verdict = "downgrade"
        user_message = "合约可用保证金已回到储备线以上，这条警报降级为观察。"

    if spcx and distance_to_liq < Decimal("0.12"):
        user_message += " SPCXUSDT 距离强平缓冲偏窄，应准备手动减仓方案。"

    return {
        "verdict": verdict,
        "userMessage": user_message,
        "evidence": {
            "availableBalanceUSDT": str(available),
            "totalMarginBalanceUSDT": str(margin_balance),
            "totalUnrealizedProfitUSDT": str(unrealized),
            "spcxMarkPrice": str(mark),
            "spcxLiquidationPrice": str(liquidation),
            "spcxDistanceToLiqPct": str(distance_to_liq),
        },
        "manualPlan": [
            "Do not open new leveraged positions.",
            "Do not add to SPCXUSDT while available futures balance remains below reserve.",
            "If SPCXUSDT drops toward the configured danger/reduce zone, prepare a manual partial reduction instead of averaging down.",
        ],
    }


def data_unavailable_review(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}
    return {
        "verdict": "block",
        "userMessage": "账户或行情数据不完整，本轮所有新开仓/加仓/买入候选都必须拦截；先恢复只读数据，再重新复核。",
        "evidence": {
            "accountDataError": details.get("accountDataError"),
            "signalErrors": details.get("signalErrors", []),
        },
        "manualOrderPlan": {
            "venue": "none",
            "symbol": event.get("symbol"),
            "side": "不下单",
            "orderType": "none",
            "realOrderAllowed": False,
        },
        "manualPlan": [
            "Do not place new spot or futures orders while account/market data is incomplete.",
            "Check network/API availability and run a clean read-only supervisor cycle.",
            "Only review manual trades after fresh account balance, open orders, positions, and market signals are available.",
        ],
    }


def reduce_position_review(event: dict[str, Any]) -> dict[str, Any]:
    symbol = str(event.get("symbol") or "")
    position = find_position(event, symbol)
    available = futures_available_balance(event)
    if not position:
        return {
            "verdict": "downgrade",
            "userMessage": f"{symbol} 当前未在账户快照里发现有效持仓，这条减仓警报降级为观察。",
            "evidence": {"symbol": symbol, "availableBalanceUSDT": str(available)},
            "manualPlan": ["Do not submit a close order unless the Binance UI still shows an open position."],
        }

    amount = parse_decimal(position.get("positionAmt"))
    mark = parse_decimal(position.get("markPrice"))
    liquidation = parse_decimal(position.get("liquidationPrice"))
    entry = parse_decimal(position.get("entryPrice"))
    unrealized = parse_decimal(position.get("unRealizedProfit") or position.get("unrealizedProfit"))
    distance_to_liq = (mark - liquidation) / mark if mark and liquidation else parse_decimal(event.get("details", {}).get("distanceToLiqPct"))
    abs_amount = abs(amount)
    suggested_reduce_qty = abs_amount * Decimal("0.5")
    close_side = close_side_for_position(amount)

    if amount == 0:
        verdict = "downgrade"
        user_message = f"{symbol} 持仓数量为 0，不能按减仓执行，保持观察。"
    else:
        verdict = "confirm"
        user_message = (
            f"{symbol} 减仓警报确认：当前不是开新杠杆仓的环境，优先把风险降下来。"
            f"手动执行时只允许 {close_side}，不要反向加仓。"
        )
        if available < Decimal("30"):
            user_message += " 合约可用保证金低于 30 USDT，减仓优先级更高。"
        if distance_to_liq and distance_to_liq < Decimal("0.12"):
            user_message += " 距强平缓冲偏窄，市价/贴近标记价限价都应以降低爆仓风险为目标。"

    return {
        "verdict": verdict,
        "userMessage": user_message,
        "evidence": {
            "symbol": symbol,
            "positionAmt": str(amount),
            "markPrice": str(mark),
            "entryPrice": str(entry),
            "liquidationPrice": str(liquidation),
            "distanceToLiqPct": str(distance_to_liq),
            "unrealizedProfitUSDT": str(unrealized),
            "availableBalanceUSDT": str(available),
        },
        "manualOrderPlan": {
            "venue": "Binance U本位合约",
            "symbol": symbol,
            "side": close_side,
            "quantityHint": format_decimal(suggested_reduce_qty) if suggested_reduce_qty else "",
            "orderType": "市价快速降风险，或限价贴近标记价并设置 reduce-only",
            "timeInForce": "GTC for limit orders",
            "realOrderAllowed": False,
        },
        "manualPlan": [
            f"Open Binance U本位合约 {symbol}; choose {close_side}.",
            f"Candidate first reduction: about 50% of the current position ({format_decimal(suggested_reduce_qty)}), adjusted to Binance precision.",
            "Use reduce-only. Do not open a new leveraged position while this alert is active.",
            "After reducing, confirm available futures balance is back above the reserve line.",
        ],
    }


def spot_buy_approval_review(event: dict[str, Any]) -> dict[str, Any]:
    symbol = str(event.get("symbol") or "")
    details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}
    signal = context_signal(event)
    free_usdt = spot_free_balance(event, "USDT")
    available = futures_available_balance(event)
    max_quote = parse_decimal(details.get("maxQuoteUSDT"))
    price = parse_decimal(details.get("price") or signal.get("price"))
    confidence = parse_decimal(signal.get("confidence"))
    warnings = signal.get("warnings") or details.get("warnings") or []
    quote_cap = min(max_quote, free_usdt) if max_quote else free_usdt
    blockers: list[str] = []

    if max_quote and free_usdt < max_quote:
        blockers.append("spot USDT is below the proposed max quote size")
    if available and available < Decimal("30"):
        blockers.append("futures available balance is below the 30 USDT reserve line")
    if confidence and confidence < Decimal("0.70"):
        blockers.append("signal confidence fell below the 0.70 approval threshold")
    if warnings:
        blockers.append("signal contains warnings that need manual inspection")

    if blockers:
        verdict = "block"
        user_message = f"{symbol} 现货买入审批被拦截：{'；'.join(blockers)}。当前不应该自动买入。"
    else:
        verdict = "confirm"
        user_message = (
            f"{symbol} 现货买入候选通过复核，但仍是人工审批方案：只考虑小额现货，不开杠杆，不追价。"
        )

    return {
        "verdict": verdict,
        "userMessage": user_message,
        "evidence": {
            "symbol": symbol,
            "freeSpotUSDT": str(free_usdt),
            "futuresAvailableBalanceUSDT": str(available),
            "maxQuoteUSDT": str(max_quote),
            "quoteCapUSDT": str(quote_cap),
            "latestPrice": str(price),
            "signalConfidence": str(confidence),
            "warnings": warnings,
            "blockers": blockers,
        },
        "manualOrderPlan": {
            "venue": "Binance 现货",
            "symbol": symbol,
            "side": "买入",
            "quoteAmountUSDT": format_decimal(quote_cap) if verdict == "confirm" else "",
            "orderType": "限价",
            "limitPriceHint": str(price) if price else "",
            "timeInForce": "GTC",
            "realOrderAllowed": False,
        },
        "manualPlan": [
            "Use Binance spot only; do not use margin or futures for this candidate.",
            "Use a limit order near the latest reviewed price and cancel/review again if price moves before fill.",
            "Keep quote size at or below the reviewed quote cap.",
            "If any blocker is present, do not place the order.",
        ],
    }


def reassess_take_profit_or_hold_review(event: dict[str, Any]) -> dict[str, Any]:
    symbol = str(event.get("symbol") or "")
    position = find_position(event, symbol)
    available = futures_available_balance(event)
    if not position:
        return {
            "verdict": "downgrade",
            "userMessage": f"{symbol} 未发现有效持仓，止盈/继续持有复核降级为观察。",
            "evidence": {"symbol": symbol, "availableBalanceUSDT": str(available)},
            "manualPlan": ["Do not submit any futures order unless the position is visible in Binance UI."],
        }

    amount = parse_decimal(position.get("positionAmt"))
    mark = parse_decimal(position.get("markPrice"))
    entry = parse_decimal(position.get("entryPrice"))
    break_even = parse_decimal(position.get("breakEvenPrice"))
    unrealized = parse_decimal(position.get("unRealizedProfit") or position.get("unrealizedProfit"))
    close_side = close_side_for_position(amount)
    in_strength_zone = mark >= entry or (break_even and mark >= break_even)

    if amount == 0:
        verdict = "downgrade"
        user_message = f"{symbol} 持仓数量为 0，继续观察。"
    elif in_strength_zone:
        verdict = "confirm"
        user_message = (
            f"{symbol} 已到入场价/盈亏平衡附近或上方，适合做止盈/减风险复核；"
            "这不是加仓信号。"
        )
    else:
        verdict = "downgrade"
        user_message = f"{symbol} 还未回到关键复核价上方，当前更像持有观察，不适合主动加仓。"

    return {
        "verdict": verdict,
        "userMessage": user_message,
        "evidence": {
            "symbol": symbol,
            "positionAmt": str(amount),
            "markPrice": str(mark),
            "entryPrice": str(entry),
            "breakEvenPrice": str(break_even),
            "unrealizedProfitUSDT": str(unrealized),
            "availableBalanceUSDT": str(available),
        },
        "manualOrderPlan": {
            "venue": "Binance U本位合约",
            "symbol": symbol,
            "side": close_side,
            "quantityHint": format_decimal(abs(amount) * Decimal("0.25")) if amount else "",
            "orderType": "reduce-only partial close if manually approved",
            "timeInForce": "GTC for limit orders",
            "realOrderAllowed": False,
        },
        "manualPlan": [
            "Do not add to the futures position from this alert.",
            "If manually approved, consider a partial reduce-only close into strength.",
            "If price fails the reviewed zone again, keep cash reserved and wait for the next cycle.",
        ],
    }


def generic_review(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "needs_manual_model_review",
        "userMessage": "事件已进入复核队列，需要结合最新账户和市场状态再次判断。",
        "evidence": {"action": event.get("action"), "symbol": event.get("symbol"), "reason": event.get("reason")},
        "manualPlan": ["Review the event context before taking any manual action."],
    }


def review_event(event: dict[str, Any]) -> dict[str, Any]:
    action = event.get("action")
    if action == "DATA_UNAVAILABLE":
        result = data_unavailable_review(event)
    elif action == "RESERVE_CASH":
        result = reserve_cash_review(event)
    elif action == "REDUCE_POSITION":
        result = reduce_position_review(event)
    elif action == "SPOT_BUY_APPROVAL":
        result = spot_buy_approval_review(event)
    elif action == "REASSESS_TAKE_PROFIT_OR_HOLD":
        result = reassess_take_profit_or_hold_review(event)
    else:
        result = generic_review(event)
    return {
        "reviewedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "eventId": event.get("eventId"),
        "eventCreatedAt": event.get("createdAt"),
        "action": event.get("action"),
        "symbol": event.get("symbol"),
        "market": event.get("market"),
        "priority": event.get("priority"),
        "requiresModelReview": event.get("requiresModelReview"),
        "realOrderAllowed": False,
        "sourceReason": event.get("reason"),
        **result,
    }


def append_reviews(path: str, reviews: list[dict[str, Any]]) -> None:
    if not reviews:
        return
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with open(review_path, "a", encoding="utf-8") as file:
        for review in reviews:
            file.write(json.dumps(review, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review realtime supervisor notification events.")
    parser.add_argument("--events", default=DEFAULT_EVENT_QUEUE)
    parser.add_argument("--reviews", default=DEFAULT_REVIEW_LEDGER)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    events = newest_pending_events(args.events, args.reviews, args.limit)
    reviews = [review_event(event) for event in events]
    if args.write:
        append_reviews(args.reviews, reviews)
    print(json.dumps({"pendingCount": len(events), "reviews": reviews}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())