"""Binance futures funding fees, commissions, and cost snapshots."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_autotrader import (
    LIVE,
    ExecutionConfig,
    PaperPortfolio,
    TradingMemory,
    atomic_write_json,
    decimal_from,
    execution_from_config,
    normalize_symbol,
    risk_from_config,
    signed_binance_request,
)

COST_INCOME_TYPES = frozenset({"FUNDING_FEE", "COMMISSION"})
DEFAULT_SNAPSHOT_PATH = "logs/trading-costs-latest.json"
DEFAULT_STATE_PATH = "logs/trading-costs-state.json"


@dataclass(frozen=True)
class TradingCostsConfig:
    enabled: bool = True
    lookback_hours: int = 24
    snapshot_path: str = DEFAULT_SNAPSHOT_PATH
    state_path: str = DEFAULT_STATE_PATH


def costs_from_config(raw: dict[str, Any] | None) -> TradingCostsConfig:
    raw = raw or {}
    return TradingCostsConfig(
        enabled=bool(raw.get("enabled", True)),
        lookback_hours=int(raw.get("lookback_hours", 24)),
        snapshot_path=str(raw.get("snapshot_path", DEFAULT_SNAPSHOT_PATH)),
        state_path=str(raw.get("state_path", DEFAULT_STATE_PATH)),
    )


def fetch_futures_income(
    execution: ExecutionConfig,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if start_time_ms is not None:
        params["startTime"] = start_time_ms
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    payload = signed_binance_request(execution, "GET", "/fapi/v1/income", params, market="binance_futures")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected income payload: {payload!r}")
    return payload


def fetch_premium_index(execution: ExecutionConfig, symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    payload = signed_binance_request(
        execution,
        "GET",
        "/fapi/v1/premiumIndex",
        {"symbol": normalized},
        market="binance_futures",
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected premiumIndex payload for {normalized}: {payload!r}")
    return payload


def _day_key_from_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def aggregate_income_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    today_utc = datetime.now(timezone.utc).date().isoformat()
    totals_by_type: dict[str, Decimal] = {}
    today_by_type: dict[str, Decimal] = {}
    by_symbol_funding: dict[str, Decimal] = {}
    by_symbol_commission: dict[str, Decimal] = {}
    recent: list[dict[str, Any]] = []

    for row in sorted(rows, key=lambda item: int(item.get("time", 0)), reverse=True):
        income_type = str(row.get("incomeType", "")).upper()
        amount = decimal_from(row.get("income", "0"))
        symbol = normalize_symbol(str(row.get("symbol", ""))) if row.get("symbol") else ""
        timestamp_ms = int(row.get("time", 0))
        totals_by_type[income_type] = totals_by_type.get(income_type, Decimal("0")) + amount
        if _day_key_from_ms(timestamp_ms) == today_utc:
            today_by_type[income_type] = today_by_type.get(income_type, Decimal("0")) + amount
        if income_type == "FUNDING_FEE" and symbol:
            by_symbol_funding[symbol] = by_symbol_funding.get(symbol, Decimal("0")) + amount
        if income_type == "COMMISSION" and symbol:
            by_symbol_commission[symbol] = by_symbol_commission.get(symbol, Decimal("0")) + amount
        if income_type in COST_INCOME_TYPES and len(recent) < 12:
            recent.append(
                {
                    "time": timestamp_ms,
                    "timeIso": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "symbol": symbol or None,
                    "incomeType": income_type,
                    "income": str(amount),
                    "asset": row.get("asset", "USDT"),
                    "tranId": row.get("tranId"),
                }
            )

    def cost_total(mapping: dict[str, Decimal]) -> Decimal:
        funding = mapping.get("FUNDING_FEE", Decimal("0"))
        commission = mapping.get("COMMISSION", Decimal("0"))
        # Binance reports costs as negative numbers.
        return (-funding if funding < 0 else Decimal("0")) + (-commission if commission < 0 else Decimal("0"))

    return {
        "lookbackRows": len(rows),
        "totalsByType": {key: str(value) for key, value in totals_by_type.items()},
        "todayByType": {key: str(value) for key, value in today_by_type.items()},
        "lookbackCostQuote": str(cost_total(totals_by_type)),
        "todayCostQuote": str(cost_total(today_by_type)),
        "todayFundingQuote": str(-today_by_type.get("FUNDING_FEE", Decimal("0")) if today_by_type.get("FUNDING_FEE", Decimal("0")) < 0 else Decimal("0")),
        "todayCommissionQuote": str(
            -today_by_type.get("COMMISSION", Decimal("0")) if today_by_type.get("COMMISSION", Decimal("0")) < 0 else Decimal("0")
        ),
        "fundingBySymbol": {symbol: str(amount) for symbol, amount in sorted(by_symbol_funding.items())},
        "commissionBySymbol": {symbol: str(amount) for symbol, amount in sorted(by_symbol_commission.items())},
        "recentCosts": recent,
    }


def load_cost_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {"seenTranIds": []}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seenTranIds": []}
    if not isinstance(payload, dict):
        return {"seenTranIds": []}
    return payload


def save_cost_state(path: str | Path, state: dict[str, Any]) -> None:
    # Atomic: corruption here would lose seenTranIds dedup and double-count costs/PnL.
    atomic_write_json(path, state)


def apply_new_income_rows(memory: TradingMemory, rows: list[dict[str, Any]], seen_tran_ids: set[str]) -> set[str]:
    updated = set(seen_tran_ids)
    for row in rows:
        income_type = str(row.get("incomeType", "")).upper()
        # REALIZED_PNL feeds the daily-loss circuit breaker (exchange-authoritative);
        # FUNDING_FEE/COMMISSION feed the daily cost/drag accounting. Anything else
        # (TRANSFER, INSURANCE_CLEAR, etc.) is ignored here.
        if income_type not in COST_INCOME_TYPES and income_type != "REALIZED_PNL":
            continue
        tran_id = str(row.get("tranId", ""))
        if not tran_id or tran_id in updated:
            continue
        amount = decimal_from(row.get("income", "0"))
        timestamp_ms = int(row.get("time", 0))
        day_key = (
            _day_key_from_ms(timestamp_ms)
            if timestamp_ms
            else datetime.now(timezone.utc).date().isoformat()
        )
        if income_type == "REALIZED_PNL":
            memory.record_realized_pnl(amount, day_key)
        else:
            memory.record_income(income_type, amount, day_key)
        updated.add(tran_id)
    if len(updated) > 5000:
        updated = set(list(updated)[-5000:])
    return updated


def funding_outlook_for_symbols(
    execution: ExecutionConfig,
    symbols: list[str],
) -> list[dict[str, Any]]:
    outlook: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            payload = fetch_premium_index(execution, symbol)
        except Exception as exc:
            outlook.append({"symbol": normalize_symbol(symbol), "error": str(exc)})
            continue
        next_funding_ms = int(payload.get("nextFundingTime", 0))
        outlook.append(
            {
                "symbol": normalize_symbol(symbol),
                "markPrice": payload.get("markPrice"),
                "lastFundingRate": payload.get("lastFundingRate"),
                "nextFundingTime": next_funding_ms,
                "nextFundingTimeIso": datetime.fromtimestamp(next_funding_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if next_funding_ms
                else None,
            }
        )
    return outlook


def refresh_trading_costs(
    execution: ExecutionConfig,
    memory: TradingMemory,
    portfolio: PaperPortfolio | None = None,
    *,
    cfg: TradingCostsConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or TradingCostsConfig()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - cfg.lookback_hours * 3600 * 1000
    rows = fetch_futures_income(execution, start_time_ms=start_ms, end_time_ms=now_ms)
    state = load_cost_state(cfg.state_path)
    seen = {str(item) for item in state.get("seenTranIds", [])}
    seen = apply_new_income_rows(memory, rows, seen)
    state["seenTranIds"] = sorted(seen)
    state["lastSyncAt"] = now_ms
    state["lastSyncAtIso"] = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_cost_state(cfg.state_path, state)

    account_summary: dict[str, Any] = {}
    try:
        account = signed_binance_request(execution, "GET", "/fapi/v3/account", {}, market="binance_futures")
        if isinstance(account, dict):
            account_summary = {
                "walletBalance": account.get("totalWalletBalance"),
                "unrealizedProfit": account.get("totalUnrealizedProfit"),
                "marginBalance": account.get("totalMarginBalance"),
                "availableBalance": account.get("availableBalance"),
            }
    except Exception as exc:
        account_summary = {"error": str(exc)}

    held_symbols = [symbol for symbol, position in (portfolio.positions.items() if portfolio else []) if position.quantity != 0]
    funding_outlook = funding_outlook_for_symbols(execution, held_symbols[:12])

    snapshot = {
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "lookbackHours": cfg.lookback_hours,
        "accountSummary": account_summary,
        "memoryToday": {
            "fundingFee": str(memory.daily_funding_fee_today()),
            "commission": str(memory.daily_commission_today()),
            "priceLoss": str(memory.daily_loss_today()),
            "totalDrag": str(memory.total_daily_drag_today()),
        },
        **aggregate_income_rows(rows),
        "fundingOutlook": funding_outlook,
    }
    snapshot_path = Path(cfg.snapshot_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def load_trading_costs_snapshot(path: str | Path = DEFAULT_SNAPSHOT_PATH) -> dict[str, Any]:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return {}
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"error": "invalid json"}


def maybe_refresh_trading_costs(
    config: dict[str, Any],
    memory: TradingMemory,
    portfolio: PaperPortfolio | None = None,
) -> dict[str, Any]:
    execution = execution_from_config(config.get("execution", {}))
    risk = risk_from_config(config.get("risk", {}))
    if execution.mode != LIVE and risk.mode != LIVE:
        return {}
    cfg = costs_from_config(config.get("trading_costs", {}))
    if not cfg.enabled:
        return {}
    try:
        return refresh_trading_costs(execution, memory, portfolio, cfg=cfg)
    except Exception as exc:
        snapshot = {
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
            "error": str(exc),
            "memoryToday": {
                "fundingFee": str(memory.daily_funding_fee_today()),
                "commission": str(memory.daily_commission_today()),
                "priceLoss": str(memory.daily_loss_today()),
                "totalDrag": str(memory.total_daily_drag_today()),
            },
        }
        snapshot_path = Path(cfg.snapshot_path)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return snapshot
