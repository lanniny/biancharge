"""Qbot-style pipeline: MarketPoller → DecisionWorker → ExecutionWorker."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from benchmark_gate import benchmark_gate_from_config, evaluate_benchmark_gate
from market_autotrader import (
    LIVE,
    BLOCKED,
    AssetConfig,
    ExecutionRecord,
    MarketBar,
    MarketSnapshot,
    OrderIntent,
    PaperPortfolio,
    ProfitAdaptiveCapConfig,
    RiskConfig,
    RiskDecision,
    Signal,
    TradingMemory,
    append_ledger,
    apply_holding_priority_signal,
    apply_risk_controls,
    assets_from_config,
    auto_execution_from_config,
    build_provider,
    build_signal,
    build_trade_rationale,
    decimal_from,
    execute_decision,
    validate_risk_execution_mode_alignment,
    execution_from_config,
    fetch_live_portfolio,
    normalize_symbol,
    normalize_price_change_pct_24h,
    process_live_position_closures_at_cycle_start,
    process_live_position_tracking_at_cycle_end,
    reset_server_time_offset,
    risk_from_config,
    strategy_from_config,
    sync_position_peak,
)
from market_discovery import discovery_from_config, resolve_trading_universe


@dataclass(frozen=True)
class PipelineConfig:
    enabled: bool = False
    handoff_dir: str = "logs/pipeline"
    persist_handoffs: bool = True
    state_path: str = "logs/pipeline/pipeline-state.json"


@dataclass
class PollerHandoff:
    cycle_id: str
    polled_at: int
    config_path: str = ""
    discovery_scan: dict[str, Any] = field(default_factory=dict)
    watchlist_by_symbol: dict[str, Any] = field(default_factory=dict)
    benchmark_gate: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    trade_learning: dict[str, Any] = field(default_factory=dict)
    trade_lessons: dict[str, Any] = field(default_factory=dict)
    account_error: str | None = None
    portfolio: dict[str, Any] = field(default_factory=dict)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    asset_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionHandoff:
    cycle_id: str
    decided_at: int
    portfolio: dict[str, Any] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pipeline_from_config(raw: dict[str, Any] | None) -> PipelineConfig:
    raw = raw or {}
    return PipelineConfig(
        enabled=bool(raw.get("enabled", False)),
        handoff_dir=str(raw.get("handoff_dir", "logs/pipeline")),
        persist_handoffs=bool(raw.get("persist_handoffs", True)),
        state_path=str(raw.get("state_path", "logs/pipeline/pipeline-state.json")),
    )


def bar_to_dict(bar: MarketBar) -> dict[str, str]:
    return {
        "timestamp": str(bar.timestamp),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def bar_from_dict(raw: dict[str, Any]) -> MarketBar:
    return MarketBar(
        timestamp=int(raw["timestamp"]),
        open=decimal_from(raw["open"]),
        high=decimal_from(raw["high"]),
        low=decimal_from(raw["low"]),
        close=decimal_from(raw["close"]),
        volume=decimal_from(raw["volume"]),
    )


def asset_to_dict(asset: AssetConfig) -> dict[str, Any]:
    return {
        "symbol": asset.symbol,
        "market": asset.market,
        "base_asset": asset.base_asset,
        "quote_asset": asset.quote_asset,
        "provider": asset.provider,
    }


def asset_from_dict(raw: dict[str, Any]) -> AssetConfig:
    return AssetConfig(
        symbol=normalize_symbol(raw["symbol"]),
        market=raw["market"],
        base_asset=raw["base_asset"],
        quote_asset=raw["quote_asset"],
        provider=dict(raw.get("provider", {})),
    )


def snapshot_to_dict(snapshot: MarketSnapshot) -> dict[str, Any]:
    return {
        "asset": asset_to_dict(snapshot.asset),
        "bars": [bar_to_dict(bar) for bar in snapshot.bars],
        "observed_at": snapshot.observed_at,
    }


def snapshot_from_dict(raw: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        asset=asset_from_dict(raw["asset"]),
        bars=[bar_from_dict(item) for item in raw.get("bars", [])],
        observed_at=int(raw.get("observed_at", int(time.time()))),
    )


def signal_to_dict(signal: Signal) -> dict[str, Any]:
    return {
        "action": signal.action,
        "confidence": str(signal.confidence),
        "reasons": list(signal.reasons),
        "warnings": list(signal.warnings),
        "indicators": dict(signal.indicators),
    }


def signal_from_dict(raw: dict[str, Any]) -> Signal:
    return Signal(
        action=raw["action"],
        confidence=decimal_from(raw.get("confidence", "0")),
        reasons=list(raw.get("reasons", [])),
        warnings=list(raw.get("warnings", [])),
        indicators={str(k): str(v) for k, v in (raw.get("indicators") or {}).items()},
    )


def order_to_dict(order: OrderIntent | None) -> dict[str, Any] | None:
    if order is None:
        return None
    return {
        "action": order.action,
        "symbol": order.symbol,
        "market": order.market,
        "quote_asset": order.quote_asset,
        "quote_amount": str(order.quote_amount),
        "estimated_price": str(order.estimated_price),
        "reduce_only": order.reduce_only,
        "quantity": str(order.quantity) if order.quantity is not None else None,
        "order_type": order.order_type,
        "limit_price": str(order.limit_price) if order.limit_price is not None else None,
        "stop_price": str(order.stop_price) if order.stop_price is not None else None,
        "take_profit_price": str(order.take_profit_price) if order.take_profit_price is not None else None,
        "leverage": order.leverage,
        "intent_kind": order.intent_kind,
        "time_in_force": order.time_in_force,
    }


def order_from_dict(raw: dict[str, Any] | None) -> OrderIntent | None:
    if not raw:
        return None
    return OrderIntent(
        action=raw["action"],
        symbol=raw["symbol"],
        market=raw["market"],
        quote_asset=raw["quote_asset"],
        quote_amount=decimal_from(raw.get("quote_amount", "0")),
        estimated_price=decimal_from(raw.get("estimated_price", "0")),
        reduce_only=bool(raw.get("reduce_only", False)),
        quantity=decimal_from(raw["quantity"]) if raw.get("quantity") else None,
        order_type=str(raw.get("order_type", "MARKET")),
        limit_price=decimal_from(raw["limit_price"]) if raw.get("limit_price") else None,
        stop_price=decimal_from(raw["stop_price"]) if raw.get("stop_price") else None,
        take_profit_price=decimal_from(raw["take_profit_price"]) if raw.get("take_profit_price") else None,
        leverage=int(raw["leverage"]) if raw.get("leverage") is not None else None,
        intent_kind=str(raw.get("intent_kind", "entry")),
        time_in_force=str(raw.get("time_in_force", "GTC")),
    )


def profit_adaptive_cap_to_dict(cfg: ProfitAdaptiveCapConfig) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "step_usdt": str(cfg.step_usdt),
        "extra_trades_per_step": cfg.extra_trades_per_step,
        "hard_ceiling": cfg.hard_ceiling,
    }


def risk_to_dict(risk: RiskConfig | None) -> dict[str, Any] | None:
    if risk is None:
        return None
    return {
        "mode": risk.mode,
        "allow_live_trading": risk.allow_live_trading,
        "max_trade_quote": str(risk.max_trade_quote),
        "max_position_quote": str(risk.max_position_quote),
        "min_confidence": str(risk.min_confidence),
        "max_volatility": str(risk.max_volatility),
        "max_drawdown": str(risk.max_drawdown),
        "max_daily_trades": risk.max_daily_trades,
        "max_daily_loss_quote": str(risk.max_daily_loss_quote),
        "cooldown_seconds": risk.cooldown_seconds,
        "require_reason_count": risk.require_reason_count,
        "kill_switch_path": risk.kill_switch_path,
        "live_arm_path": risk.live_arm_path,
        "reserve_futures_available_usdt": str(risk.reserve_futures_available_usdt),
        "allow_futures_open": risk.allow_futures_open,
        "allowed_live_markets": list(risk.allowed_live_markets),
        "risk_per_trade_pct": str(risk.risk_per_trade_pct),
        "scale_sizing_with_equity": risk.scale_sizing_with_equity,
        "min_trade_quote": str(risk.min_trade_quote),
        "max_portfolio_heat_pct": str(risk.max_portfolio_heat_pct),
        "target_equity_quote": str(risk.target_equity_quote),
        "max_daily_loss_pct": str(risk.max_daily_loss_pct),
        "min_daily_loss_cap_quote": str(risk.min_daily_loss_cap_quote),
        "daily_drag_scope": risk.daily_drag_scope,
        "daily_drag_blocks": risk.daily_drag_blocks,
        "max_concurrent_positions": risk.max_concurrent_positions,
        "min_margin_per_trade": str(risk.min_margin_per_trade),
        "max_position_loss_pct": str(risk.max_position_loss_pct),
        "profit_adaptive_daily_cap": profit_adaptive_cap_to_dict(risk.profit_adaptive_daily_cap),
    }


def decision_to_dict(decision: RiskDecision) -> dict[str, Any]:
    return {
        "approved": decision.approved,
        "action": decision.action,
        "reasons": list(decision.reasons),
        "blocked_reasons": list(decision.blocked_reasons),
        "order": order_to_dict(decision.order),
        "effective_risk": risk_to_dict(decision.effective_risk),
    }


def decision_from_dict(raw: dict[str, Any]) -> RiskDecision:
    return RiskDecision(
        approved=bool(raw.get("approved", False)),
        action=raw.get("action", "HOLD"),
        reasons=list(raw.get("reasons", [])),
        blocked_reasons=list(raw.get("blocked_reasons", [])),
        order=order_from_dict(raw.get("order")),
        effective_risk=risk_from_config(raw.get("effective_risk") or {}) if raw.get("effective_risk") else None,
    )


def should_override_discovery_gate(
    *,
    decision: RiskDecision,
    signal: Signal,
    watch_entry: dict[str, Any],
    trade_learning: dict[str, Any] | None,
    min_confidence: Decimal = Decimal("0.95"),
) -> bool:
    """Allow a strong discovered setup through analysis-only discovery gates.

    This does not override risk, timing, lesson, sizing, or shadow-first blocks.
    It only prevents the discovery regime/score gate from being the sole reason a
    high-conviction executable order is downgraded to analysis-only.
    """
    if not decision.approved or decision.order is None or decision.order.reduce_only:
        return False
    if signal.confidence < min_confidence:
        return False
    if not str(watch_entry.get("source") or "").startswith("discovery:"):
        return False
    if str(watch_entry.get("block_reason") or "") == "min_notional_exceeds_max_trade_quote":
        return False
    timing_override = str(signal.indicators.get("entry_timing_strong_continuation") or "").lower() == "true"
    if not bool(watch_entry.get("regime_pass", True)) and signal.confidence < Decimal("0.98") and not timing_override:
        return False
    bucket = str(watch_entry.get("bucket") or "")
    learning = trade_learning or {}
    canary_buckets = set(learning.get("shadowCanaryBuckets") or [])
    canary_factor = decimal_from((learning.get("bucketCanaryFactors") or {}).get(bucket, "0")) if bucket else Decimal("0")
    valid_canary = bucket in canary_buckets and Decimal("0") < canary_factor < Decimal("1")
    if bucket and bucket in set(learning.get("shadowFirstBuckets") or []) and not valid_canary:
        return False
    bucket_modes = learning.get("bucketLiveModes") if isinstance(learning.get("bucketLiveModes"), dict) else {}
    if bucket and bucket_modes.get(bucket) == "shadow_first" and not valid_canary:
        return False
    return True


def portfolio_to_dict(portfolio: PaperPortfolio) -> dict[str, Any]:
    return {
        "cash": {asset: str(amount) for asset, amount in portfolio.cash.items()},
        "positions": {
            symbol: {"quantity": str(pos.quantity), "average_price": str(pos.average_price)}
            for symbol, pos in portfolio.positions.items()
        },
    }


def portfolio_from_dict(raw: dict[str, Any]) -> PaperPortfolio:
    return PaperPortfolio.from_config(raw)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def update_pipeline_state(cfg: PipelineConfig, cycle_id: str, phase: str) -> None:
    state_path = Path(cfg.state_path)
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = read_json(state_path)
        except json.JSONDecodeError:
            state = {}
    state["latestCycleId"] = cycle_id
    state[f"{phase}CompletedAt"] = int(time.time())
    state[f"{phase}CompletedAtIso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(state_path, state)


class MarketPoller:
    def __init__(
        self,
        config: dict[str, Any],
        pipeline_cfg: PipelineConfig,
        cycle_id: str | None = None,
        memory: TradingMemory | None = None,
    ) -> None:
        self.config = config
        self.pipeline_cfg = pipeline_cfg
        self.cycle_id = cycle_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.memory = memory

    def run(self) -> PollerHandoff:
        static_assets = assets_from_config(self.config.get("assets", []))
        risk = risk_from_config(self.config.get("risk", {}))
        execution = execution_from_config(self.config.get("execution", {}), fallback_mode=risk.mode)
        validate_risk_execution_mode_alignment(risk, execution)
        is_live = execution.mode == LIVE or risk.mode == LIVE
        if is_live:
            reset_server_time_offset()

        account_error: str | None = None
        if is_live:
            try:
                portfolio = fetch_live_portfolio(execution, static_assets)
            except Exception as exc:
                account_error = str(exc)
                portfolio = PaperPortfolio.from_config(self.config.get("portfolio", {}))
        else:
            portfolio = PaperPortfolio.from_config(self.config.get("portfolio", {}))

        if is_live and self.memory is not None:
            from trading_costs import maybe_refresh_trading_costs
            from trade_outcomes import compute_trade_learning_snapshot, trade_learning_from_config
            from trade_lessons import refresh_trade_lessons, trade_lessons_from_config

            maybe_refresh_trading_costs(self.config, self.memory, portfolio)
            learning_cfg = trade_learning_from_config(self.config.get("trade_learning"))
            lessons_cfg = trade_lessons_from_config(self.config.get("trade_lessons"))
            external_outcomes = process_live_position_closures_at_cycle_start(
                self.memory, portfolio, learning_cfg, execution, lessons_cfg=lessons_cfg
            )
            if external_outcomes:
                compute_trade_learning_snapshot(learning_cfg)
                refresh_trade_lessons(lessons_cfg, learning_cfg.outcomes_path)
            self.memory.save(execution.state_path)

        assets, discovery_scan = resolve_trading_universe(self.config, portfolio, static_assets)
        watchlist_by_symbol = {
            f"{entry['symbol']}|{entry['market']}": entry for entry in discovery_scan.get("watchlist", [])
        }
        priority_index = {
            (entry["symbol"], entry["market"]): index for index, entry in enumerate(discovery_scan.get("watchlist", []))
        }
        assets = sorted(assets, key=lambda asset: priority_index.get((normalize_symbol(asset.symbol), asset.market), 999))

        benchmark_state = evaluate_benchmark_gate(
            benchmark_gate_from_config(self.config.get("risk", {}).get("benchmark_gate"))
        )

        from market_context import evaluate_market_context, market_context_from_config, save_market_context_snapshot
        from trade_outcomes import compute_trade_learning_snapshot, trade_learning_from_config
        from trade_lessons import load_lessons_document, refresh_trade_lessons, trade_lessons_from_config

        ctx_cfg = market_context_from_config(self.config.get("market_context"))
        learning_cfg = trade_learning_from_config(self.config.get("trade_learning"))
        lessons_cfg = trade_lessons_from_config(self.config.get("trade_lessons"))
        market_ctx = evaluate_market_context(ctx_cfg)
        if ctx_cfg.enabled:
            save_market_context_snapshot(ctx_cfg.snapshot_path, market_ctx)
        trade_learning = compute_trade_learning_snapshot(learning_cfg)
        trade_lessons = refresh_trade_lessons(lessons_cfg, learning_cfg.outcomes_path)
        if not trade_lessons.get("losses") and not trade_lessons.get("wins"):
            trade_lessons = load_lessons_document(lessons_cfg.lessons_path)

        snapshots: list[dict[str, Any]] = []
        for asset in assets:
            provider = build_provider(asset)
            snapshot = MarketSnapshot(asset=asset, bars=provider.get_bars(asset), observed_at=int(time.time()))
            snapshots.append(snapshot_to_dict(snapshot))

        return PollerHandoff(
            cycle_id=self.cycle_id,
            polled_at=int(time.time()),
            discovery_scan=discovery_scan,
            watchlist_by_symbol=watchlist_by_symbol,
            benchmark_gate=benchmark_state,
            market_context=market_ctx,
            trade_learning=trade_learning,
            trade_lessons=trade_lessons,
            account_error=account_error,
            portfolio=portfolio_to_dict(portfolio),
            snapshots=snapshots,
            asset_count=len(snapshots),
        )

    def persist(self, handoff: PollerHandoff) -> Path:
        base = Path(self.pipeline_cfg.handoff_dir)
        path = base / f"{handoff.cycle_id}-poller.json"
        write_json(path, handoff.to_dict())
        write_json(base / "latest-poller.json", handoff.to_dict())
        update_pipeline_state(self.pipeline_cfg, handoff.cycle_id, "poller")
        return path


class DecisionWorker:
    def __init__(self, config: dict[str, Any], memory: TradingMemory) -> None:
        self.config = config
        self.memory = memory
        self.strategy = strategy_from_config(config.get("strategy", {}))
        self.risk = risk_from_config(config.get("risk", {}))
        self.execution = execution_from_config(config.get("execution", {}), fallback_mode=self.risk.mode)
        self.auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
        self.discovery_cfg = discovery_from_config(config.get("market_discovery", {}))
        from market_context import market_context_from_config
        from trade_outcomes import trade_learning_from_config
        from trade_lessons import trade_lessons_from_config
        from bucket_strategy import bucket_strategy_from_config
        from shadow_paper import shadow_paper_from_config

        self.market_context_cfg = market_context_from_config(config.get("market_context"))
        self.trade_learning_cfg = trade_learning_from_config(config.get("trade_learning"))
        self.trade_lessons_cfg = trade_lessons_from_config(config.get("trade_lessons"))
        self.bucket_strategy_cfg = bucket_strategy_from_config(config.get("bucket_strategy"))
        self.shadow_paper_cfg = shadow_paper_from_config(config.get("shadow_paper"))
        from quadrant_strategy import quadrant_strategy_from_config
        from entry_timing import entry_timing_from_config

        self.quadrant_strategy_cfg = quadrant_strategy_from_config(config.get("quadrant_strategy"))
        self.entry_timing_cfg = entry_timing_from_config(config.get("entry_timing"))
        from persona_council import persona_council_from_config

        self.persona_council_cfg = persona_council_from_config(config.get("persona_council"))
        from capital_scaling import capital_scaling_from_config

        self.capital_scaling_cfg = capital_scaling_from_config(config.get("capital_scaling"))
        from profitability import profitability_from_config

        self.profitability_raw = config.get("profitability") or {}
        self.profitability_cfg = profitability_from_config(self.profitability_raw)
        from bucket_strategy import bucket_strategy_from_config
        from shadow_paper import shadow_paper_from_config

        self.bucket_strategy_cfg = bucket_strategy_from_config(config.get("bucket_strategy"))
        self.shadow_paper_cfg = shadow_paper_from_config(config.get("shadow_paper"))

    def run(self, poller: PollerHandoff) -> DecisionHandoff:
        from supervisor_hints import supervisor_hints_from_config
        from quadrant_strategy import apply_quadrant_signal

        portfolio = portfolio_from_dict(poller.portfolio)
        discovery_scan = poller.discovery_scan
        pending: list[dict[str, Any]] = []
        supervisor_hints = supervisor_hints_from_config(self.config)

        for raw_snapshot in poller.snapshots:
            snapshot = snapshot_from_dict(raw_snapshot)
            key = f"{normalize_symbol(snapshot.asset.symbol)}|{snapshot.asset.market}"
            watch_entry = poller.watchlist_by_symbol.get(key, {})
            signal = build_signal(snapshot, self.strategy)
            if watch_entry.get("price_change_pct") is not None:
                signal = replace(
                    signal,
                    indicators={
                        **signal.indicators,
                        "price_change_pct_24h": str(
                            normalize_price_change_pct_24h(watch_entry.get("price_change_pct"))
                        ),
                    },
                )
            if watch_entry:
                signal = replace(
                    signal,
                    indicators={
                        **signal.indicators,
                        "discovery_bucket": str(watch_entry.get("bucket") or ""),
                        "discovery_source": str(watch_entry.get("source") or ""),
                    },
                )
            signal = apply_quadrant_signal(
                signal,
                self.quadrant_strategy_cfg,
                bucket=str(watch_entry.get("bucket") or ""),
                source=str(watch_entry.get("source") or ""),
            )
            from entry_economics import suppress_ineligible_discovery_short

            signal = suppress_ineligible_discovery_short(
                signal,
                discovery_short_mode=str(self.strategy.discovery_short_mode),
                allow_discovery_shorts=bool(self.strategy.allow_discovery_shorts),
                profitability_raw=self.profitability_raw,
            )
            sym = normalize_symbol(snapshot.asset.symbol)
            pos = portfolio.positions.get(sym)
            if pos is not None and pos.quantity != 0:
                sync_position_peak(self.memory, sym, snapshot.price, pos.quantity)
            signal = apply_holding_priority_signal(
                snapshot,
                signal,
                self.strategy,
                portfolio,
                self.memory,
                self.risk,
                supervisor_hints,
                trade_learning=poller.trade_learning,
            )
            decision = apply_risk_controls(
                snapshot,
                signal,
                self.strategy,
                self.risk,
                portfolio,
                self.memory,
                auto_exec=self.auto_exec,
                benchmark_gate=poller.benchmark_gate,
                execution=self.execution,
                market_context=poller.market_context,
                trade_learning=poller.trade_learning,
                trade_learning_cfg=self.trade_learning_cfg,
                trade_lessons=poller.trade_lessons,
                trade_lessons_cfg=self.trade_lessons_cfg,
                market_context_cfg=self.market_context_cfg,
                bucket_strategy_cfg=self.bucket_strategy_cfg,
                quadrant_strategy_cfg=self.quadrant_strategy_cfg,
                entry_timing_cfg=self.entry_timing_cfg,
                profitability_raw=self.profitability_raw,
                persona_council_cfg=self.persona_council_cfg,
                capital_scaling_cfg=self.capital_scaling_cfg,
            )
            watch_source = str(watch_entry.get("source", ""))
            is_managed_position = watch_source.startswith("holding") or watch_source.startswith("pinned")
            analyze_only = (
                self.discovery_cfg.enabled
                and watch_entry
                and not watch_entry.get("executable", True)
                and not is_managed_position
            )
            if analyze_only:
                gate_reason = "regime/score gate"
                if not self.discovery_cfg.trade_discovered:
                    gate_reason = "trade_discovered=false"
                elif watch_entry.get("regime_pass") is False:
                    gate_reason = f"regime mismatch ({watch_entry.get('regime_kind', 'unknown')})"
                elif watch_entry.get("block_reason") == "min_notional_exceeds_max_trade_quote":
                    gate_reason = "max_trade_quote below exchange min notional"
                discovery_override = should_override_discovery_gate(
                    decision=decision,
                    signal=signal,
                    watch_entry=watch_entry,
                    trade_learning=poller.trade_learning,
                )
                if not discovery_override:
                    decision = RiskDecision(
                        False,
                        BLOCKED,
                        decision.reasons,
                        decision.blocked_reasons + [
                            f"Discovery non-executable for {snapshot.asset.symbol}; {gate_reason}."
                        ],
                        decision.order,
                        decision.effective_risk,
                    )
                else:
                    try:
                        signal.indicators["discovery_gate_override"] = gate_reason
                    except Exception:
                        pass
            if poller.account_error:
                decision = RiskDecision(
                    False,
                    BLOCKED,
                    decision.reasons,
                    decision.blocked_reasons + [f"Live account refresh failed: {poller.account_error}"],
                    None,
                    decision.effective_risk,
                )
            effective_risk = decision.effective_risk or self.risk
            rationale = build_trade_rationale(
                snapshot, signal, decision, effective_risk, portfolio, self.memory,
                market_context=poller.market_context,
                trade_learning=poller.trade_learning,
                trade_lessons=poller.trade_lessons,
            )
            if discovery_scan.get("enabled"):
                rationale["marketDiscovery"] = {
                    "source": watch_entry.get("source", "unknown"),
                    "executable": watch_entry.get("executable", True),
                    "regimeKind": watch_entry.get("regime_kind"),
                    "regimePass": watch_entry.get("regime_pass"),
                    "discoveryScore": watch_entry.get("discovery_score"),
                    "blockReason": watch_entry.get("block_reason"),
                    "bucket": watch_entry.get("bucket"),
                    "priceChangePct24h": watch_entry.get("price_change_pct"),
                    "rangePosition24h": watch_entry.get("range_position_24h"),
                    "quoteVolume24h": watch_entry.get("quote_volume"),
                    "universeSize": discovery_scan.get("filters", {}),
                }
            if poller.benchmark_gate.get("enabled"):
                rationale["benchmarkGate"] = poller.benchmark_gate
            pending.append(
                {
                    "snapshot": raw_snapshot,
                    "signal": signal_to_dict(signal),
                    "decision": decision_to_dict(decision),
                    "rationale": rationale,
                }
            )

        return DecisionHandoff(
            cycle_id=poller.cycle_id,
            decided_at=int(time.time()),
            portfolio=poller.portfolio,
            pending=pending,
        )

    def persist(self, pipeline_cfg: PipelineConfig, handoff: DecisionHandoff) -> Path:
        base = Path(pipeline_cfg.handoff_dir)
        path = base / f"{handoff.cycle_id}-decisions.json"
        write_json(path, handoff.to_dict())
        write_json(base / "latest-decisions.json", handoff.to_dict())
        update_pipeline_state(pipeline_cfg, handoff.cycle_id, "decision")
        return path


def record_blocked_counterfactual_handoff(
    shadow_cfg: Any,
    *,
    pending: list[dict[str, Any]],
    portfolio: PaperPortfolio,
    watchlist_by_symbol: dict[str, Any],
    is_live: bool,
) -> int:
    if not is_live or shadow_cfg is None:
        return 0
    from shadow_paper import (
        record_blocked_counterfactual,
        reset_blocked_counterfactual_cycle,
        should_shadow_blocked_counterfactual,
    )

    reset_blocked_counterfactual_cycle()
    recorded = 0
    for item in pending:
        signal = signal_from_dict(item["signal"])
        decision = decision_from_dict(item["decision"])
        if decision.approved or decision.order is None:
            continue
        snapshot = snapshot_from_dict(item["snapshot"])
        key = f"{normalize_symbol(snapshot.asset.symbol)}|{snapshot.asset.market}"
        watch_entry = watchlist_by_symbol.get(key, {})
        pos_qty = portfolio.position(normalize_symbol(snapshot.asset.symbol)).quantity
        if not should_shadow_blocked_counterfactual(
            shadow_cfg,
            reduce_only=decision.order.reduce_only,
            position_qty=pos_qty,
            signal_action=signal.action,
            confidence=signal.confidence,
            blocked_reasons=list(decision.blocked_reasons or []),
            has_order=True,
        ):
            continue
        record_blocked_counterfactual(
            shadow_cfg,
            snapshot=snapshot,
            signal=signal,
            decision=decision,
            watch_entry=watch_entry or None,
        )
        recorded += 1
    return recorded


class ExecutionWorker:
    def __init__(self, config: dict[str, Any], memory: TradingMemory) -> None:
        self.config = config
        self.memory = memory
        self.strategy = strategy_from_config(config.get("strategy", {}))
        self.risk = risk_from_config(config.get("risk", {}))
        self.execution = execution_from_config(self.config.get("execution", {}), fallback_mode=self.risk.mode)
        validate_risk_execution_mode_alignment(self.risk, self.execution)
        self.auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
        from trade_outcomes import trade_learning_from_config
        from trade_lessons import trade_lessons_from_config

        self.trade_learning_cfg = trade_learning_from_config(config.get("trade_learning"))
        self.trade_lessons_cfg = trade_lessons_from_config(config.get("trade_lessons"))
        from shadow_paper import shadow_paper_from_config

        self.shadow_paper_cfg = shadow_paper_from_config(config.get("shadow_paper"))
        from quadrant_strategy import quadrant_strategy_from_config
        from bucket_strategy import bucket_strategy_from_config
        from entry_timing import entry_timing_from_config
        from market_context import market_context_from_config
        from persona_council import persona_council_from_config
        from capital_scaling import capital_scaling_from_config

        self.quadrant_strategy_cfg = quadrant_strategy_from_config(config.get("quadrant_strategy"))
        self.bucket_strategy_cfg = bucket_strategy_from_config(config.get("bucket_strategy"))
        self.entry_timing_cfg = entry_timing_from_config(config.get("entry_timing"))
        self.market_context_cfg = market_context_from_config(config.get("market_context"))
        self.persona_council_cfg = persona_council_from_config(config.get("persona_council"))
        self.capital_scaling_cfg = capital_scaling_from_config(config.get("capital_scaling"))
        self.profitability_raw = config.get("profitability") or {}
        self.is_live = self.execution.mode == LIVE or self.risk.mode == LIVE

    def _refresh_live_portfolio(
        self,
        assets: list[AssetConfig],
        static_assets: list[AssetConfig],
    ) -> tuple[PaperPortfolio | None, str | None]:
        try:
            return fetch_live_portfolio(self.execution, assets or static_assets), None
        except Exception as exc:
            return None, str(exc)

    def _live_execution_preflight(
        self,
        *,
        snapshot: MarketSnapshot,
        signal: Signal,
        decision: RiskDecision,
        portfolio: PaperPortfolio,
        poller: PollerHandoff,
    ) -> RiskDecision:
        if not self.is_live or not decision.approved or decision.order is None or decision.order.reduce_only:
            return decision
        refreshed = apply_risk_controls(
            snapshot,
            signal,
            self.strategy,
            self.risk,
            portfolio,
            self.memory,
            auto_exec=self.auto_exec,
            benchmark_gate=poller.benchmark_gate,
            execution=self.execution,
            market_context=poller.market_context,
            trade_learning=poller.trade_learning,
            trade_learning_cfg=self.trade_learning_cfg,
            trade_lessons=poller.trade_lessons,
            trade_lessons_cfg=self.trade_lessons_cfg,
            market_context_cfg=self.market_context_cfg,
            bucket_strategy_cfg=self.bucket_strategy_cfg,
            quadrant_strategy_cfg=self.quadrant_strategy_cfg,
            entry_timing_cfg=self.entry_timing_cfg,
            profitability_raw=self.profitability_raw,
            persona_council_cfg=self.persona_council_cfg,
            capital_scaling_cfg=self.capital_scaling_cfg,
        )
        if refreshed.approved:
            return refreshed
        return RiskDecision(
            False,
            BLOCKED,
            refreshed.reasons,
            ["Execution preflight: refreshed live account state invalidated the order."]
            + refreshed.blocked_reasons,
            refreshed.order or decision.order,
            refreshed.effective_risk,
        )

    def run(self, poller: PollerHandoff, decisions: DecisionHandoff) -> list[ExecutionRecord]:
        portfolio = portfolio_from_dict(decisions.portfolio)
        static_assets = assets_from_config(self.config.get("assets", []))
        assets = [asset_from_dict(item["asset"]) for item in poller.snapshots]
        records: list[ExecutionRecord] = []

        for item in decisions.pending:
            snapshot = snapshot_from_dict(item["snapshot"])
            signal = signal_from_dict(item["signal"])
            decision = decision_from_dict(item["decision"])
            rationale = dict(item.get("rationale", {}))
            key = f"{normalize_symbol(snapshot.asset.symbol)}|{snapshot.asset.market}"
            watch_entry = poller.watchlist_by_symbol.get(key, {})
            if self.is_live and decision.approved and decision.order is not None and not decision.order.reduce_only:
                latest, refresh_error = self._refresh_live_portfolio(assets, static_assets)
                if latest is None:
                    decision = RiskDecision(
                        False,
                        BLOCKED,
                        decision.reasons,
                        decision.blocked_reasons
                        + [f"Execution preflight live account refresh failed: {refresh_error}"],
                        decision.order,
                        decision.effective_risk,
                    )
                else:
                    portfolio = latest
                    decision = self._live_execution_preflight(
                        snapshot=snapshot,
                        signal=signal,
                        decision=decision,
                        portfolio=portfolio,
                        poller=poller,
                    )
                    effective_risk = decision.effective_risk or self.risk
                    rationale = build_trade_rationale(
                        snapshot,
                        signal,
                        decision,
                        effective_risk,
                        portfolio,
                        self.memory,
                        market_context=poller.market_context,
                        trade_learning=poller.trade_learning,
                        trade_lessons=poller.trade_lessons,
                    )
                    if "marketDiscovery" in item.get("rationale", {}):
                        rationale["marketDiscovery"] = item["rationale"]["marketDiscovery"]
                    if poller.benchmark_gate.get("enabled"):
                        rationale["benchmarkGate"] = poller.benchmark_gate
            record = execute_decision(
                snapshot,
                decision,
                signal,
                self.strategy,
                self.risk,
                portfolio,
                self.memory,
                self.execution,
                rationale,
                auto_exec=self.auto_exec,
                trade_learning_cfg=self.trade_learning_cfg,
                trade_learning=poller.trade_learning,
                trade_lessons_cfg=self.trade_lessons_cfg,
                shadow_paper_cfg=self.shadow_paper_cfg,
                watch_entry=watch_entry or None,
                quadrant_strategy_cfg=self.quadrant_strategy_cfg,
            )
            append_ledger(self.config.get("ledger_path"), record)
            records.append(record)
            if self.is_live and record.status == "executed_live":
                latest, _ = self._refresh_live_portfolio(assets, static_assets)
                if latest is not None:
                    portfolio = latest

        if self.is_live:
            if not poller.account_error:
                latest, _ = self._refresh_live_portfolio(assets, static_assets)
                if latest is not None:
                    portfolio = latest
                process_live_position_tracking_at_cycle_end(
                    self.memory,
                    portfolio,
                    self.execution,
                    self.auto_exec,
                    self.strategy,
                    trade_learning=poller.trade_learning,
                )
            self.memory.save(self.execution.state_path)
        return records

    def persist(self, pipeline_cfg: PipelineConfig, cycle_id: str, records: list[ExecutionRecord]) -> Path:
        base = Path(pipeline_cfg.handoff_dir)
        payload = {
            "cycleId": cycle_id,
            "executedAt": int(time.time()),
            "count": len(records),
            "statuses": [record.status for record in records],
        }
        path = base / f"{cycle_id}-execution.json"
        write_json(path, payload)
        write_json(base / "latest-execution.json", payload)
        update_pipeline_state(pipeline_cfg, cycle_id, "execution")
        return path


class TradingPipeline:
    def __init__(self, config: dict[str, Any], memory: TradingMemory | None = None) -> None:
        self.config = config
        self.pipeline_cfg = pipeline_from_config(config.get("pipeline", {}))
        risk = risk_from_config(config.get("risk", {}))
        execution = execution_from_config(config.get("execution", {}), fallback_mode=risk.mode)
        validate_risk_execution_mode_alignment(risk, execution)
        is_live = execution.mode == LIVE or risk.mode == LIVE
        self.memory = memory or TradingMemory.load(execution.state_path if is_live else None)

    def run_cycle(self, cycle_id: str | None = None) -> list[ExecutionRecord]:
        poller = MarketPoller(self.config, self.pipeline_cfg, cycle_id=cycle_id, memory=self.memory)
        poller_handoff = poller.run()
        if self.pipeline_cfg.persist_handoffs:
            poller.persist(poller_handoff)

        decision_worker = DecisionWorker(self.config, self.memory)
        decision_handoff = decision_worker.run(poller_handoff)
        if self.pipeline_cfg.persist_handoffs:
            decision_worker.persist(self.pipeline_cfg, decision_handoff)

        execution_worker = ExecutionWorker(self.config, self.memory)
        portfolio = portfolio_from_dict(decision_handoff.portfolio)
        risk = risk_from_config(self.config.get("risk", {}))
        execution = execution_from_config(self.config.get("execution", {}), fallback_mode=risk.mode)
        is_live = execution.mode == LIVE or risk.mode == LIVE
        from shadow_paper import shadow_paper_from_config

        shadow_cfg = shadow_paper_from_config(self.config.get("shadow_paper"))
        record_blocked_counterfactual_handoff(
            shadow_cfg,
            pending=decision_handoff.pending,
            portfolio=portfolio,
            watchlist_by_symbol=poller_handoff.watchlist_by_symbol,
            is_live=is_live,
        )
        records = execution_worker.run(poller_handoff, decision_handoff)
        if self.pipeline_cfg.persist_handoffs:
            execution_worker.persist(self.pipeline_cfg, poller_handoff.cycle_id, records)

        from shadow_paper import run_shadow_paper_cycle

        if shadow_cfg.enabled and poller_handoff.snapshots:
            prices: dict[str, Decimal] = {}
            for item in poller_handoff.snapshots:
                snap = snapshot_from_dict(item)
                prices[normalize_symbol(snap.asset.symbol)] = snap.price
            auto_exec = auto_execution_from_config(self.config.get("auto_execution", {}))
            run_shadow_paper_cycle(
                shadow_cfg,
                prices=prices,
                observed_at=int(time.time()),
                take_profit_pct=shadow_cfg.take_profit_pct,
                stop_loss_pct=shadow_cfg.stop_loss_pct,
            )
            from trade_outcomes import compute_trade_learning_snapshot, trade_learning_from_config

            compute_trade_learning_snapshot(trade_learning_from_config(self.config.get("trade_learning")))
        return records

    def run_poller_only(self, cycle_id: str | None = None) -> PollerHandoff:
        poller = MarketPoller(self.config, self.pipeline_cfg, cycle_id=cycle_id)
        handoff = poller.run()
        if self.pipeline_cfg.persist_handoffs:
            poller.persist(handoff)
        return handoff

    def run_decision_only(self, cycle_id: str | None = None) -> DecisionHandoff:
        poller_handoff = self._load_poller_handoff(cycle_id)
        decision_worker = DecisionWorker(self.config, self.memory)
        handoff = decision_worker.run(poller_handoff)
        if self.pipeline_cfg.persist_handoffs:
            decision_worker.persist(self.pipeline_cfg, handoff)
        return handoff

    def run_execution_only(self, cycle_id: str | None = None) -> list[ExecutionRecord]:
        poller_handoff = self._load_poller_handoff(cycle_id)
        decision_handoff = self._load_decision_handoff(cycle_id)
        execution_worker = ExecutionWorker(self.config, self.memory)
        records = execution_worker.run(poller_handoff, decision_handoff)
        if self.pipeline_cfg.persist_handoffs:
            execution_worker.persist(self.pipeline_cfg, poller_handoff.cycle_id, records)
        return records

    def _load_poller_handoff(self, cycle_id: str | None) -> PollerHandoff:
        base = Path(self.pipeline_cfg.handoff_dir)
        path = base / f"{cycle_id}-poller.json" if cycle_id else base / "latest-poller.json"
        raw = read_json(path)
        return PollerHandoff(**{k: raw[k] for k in PollerHandoff.__dataclass_fields__ if k in raw})

    def _load_decision_handoff(self, cycle_id: str | None) -> DecisionHandoff:
        base = Path(self.pipeline_cfg.handoff_dir)
        path = base / f"{cycle_id}-decisions.json" if cycle_id else base / "latest-decisions.json"
        raw = read_json(path)
        return DecisionHandoff(**{k: raw[k] for k in DecisionHandoff.__dataclass_fields__ if k in raw})


def load_pipeline_status(handoff_dir: str = "logs/pipeline") -> dict[str, Any]:
    base = Path(handoff_dir)
    status: dict[str, Any] = {"enabled": (base / "pipeline-state.json").exists()}
    for name in ("pipeline-state.json", "latest-poller.json", "latest-decisions.json", "latest-execution.json"):
        path = base / name
        if path.exists():
            try:
                status[name.replace(".json", "").replace("-", "_")] = read_json(path)
            except json.JSONDecodeError:
                status[name.replace(".json", "").replace("-", "_")] = {"error": "invalid json"}
    return status
