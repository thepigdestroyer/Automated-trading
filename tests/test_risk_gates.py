from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_bot.config import Settings
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    IndicatorSnapshot,
    OpenPosition,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager


def _ctx(symbol: str, price: float, atr: float | None = None) -> SymbolContext:
    p = price
    a = atr if atr is not None else price * 0.02
    return SymbolContext(
        symbol=symbol,
        display_name=symbol,
        asset_class="energy",
        as_of="2026-04-23T12:00:00+00:00",
        indicators=IndicatorSnapshot(
            price=p,
            sma_20=p,
            sma_50=p,
            ema_12=p,
            ema_26=p,
            rsi_14=50.0,
            macd=0.0,
            macd_signal=0.0,
            atr_14=a,
            volatility_20d=0.1,
            return_1d=0.0,
            return_5d=0.0,
        ),
        chart_note="test",
    )


def _decision(symbol: str, action: str, confidence: float = 0.85) -> DecisionEnvelope:
    trade_kwargs: dict = dict(
        symbol=symbol,
        action=action,
        confidence=confidence,
        thesis="t",
        news_bias="neutral",
        invalidate_if="x",
    )
    if action in {"BUY", "SELL"}:
        trade_kwargs.update(stop_loss_pct=0.012, take_profit_pct=0.024)
    return DecisionEnvelope(
        market_regime="mixed",
        summary="t",
        trades=[TradeDecision(**trade_kwargs)],
    )


def _acct(equity: float = 10_000.0, positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo",
        currency="GBP",
        total_funds=equity,
        free_funds=equity,
        open_positions=positions or [],
    )


def _base_guardrail(symbol: str, session_open: bool = True) -> dict:
    return {
        "block_new_entries": False,
        "block_reasons": [],
        "close_requests": [],
        "econ_events": [],
        "earnings_events": {},
        "session_open": {symbol: session_open},
    }


def test_event_window_blocks_new_buy():
    s = Settings()
    rm = RiskManager(s)
    now = datetime.now(timezone.utc)
    guardrail = _base_guardrail("WTI")
    guardrail["econ_events"] = [
        {
            "title": "FOMC",
            "when": (now + timedelta(minutes=10)).isoformat(),
            "impact": "high",
            "currency": "USD",
        }
    ]
    plans = rm.build_execution_plans(
        decision=_decision("WTI", "BUY"),
        symbol_contexts={"WTI": _ctx("WTI", 95.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert plans == []


def test_closed_session_blocks_new_entry():
    s = Settings()
    rm = RiskManager(s)
    guardrail = _base_guardrail("AAPL", session_open=False)
    plans = rm.build_execution_plans(
        decision=_decision("AAPL", "BUY"),
        symbol_contexts={"AAPL": _ctx("AAPL", 180.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert plans == []


def test_open_session_allows_entry():
    s = Settings()
    rm = RiskManager(s)
    guardrail = _base_guardrail("WTI", session_open=True)
    plans = rm.build_execution_plans(
        decision=_decision("WTI", "BUY"),
        symbol_contexts={"WTI": _ctx("WTI", 95.0, atr=1.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert len(plans) == 1
    assert plans[0].action == "BUY"


def test_consecutive_loss_pause_blocks_entry():
    s = Settings()
    rm = RiskManager(s)
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    guardrail = _base_guardrail("WTI")
    guardrail["consecutive_loss_state"] = {"pause_until": future}
    plans = rm.build_execution_plans(
        decision=_decision("WTI", "BUY"),
        symbol_contexts={"WTI": _ctx("WTI", 95.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert plans == []


def test_expired_consecutive_loss_pause_allows_entry():
    s = Settings()
    rm = RiskManager(s)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    guardrail = _base_guardrail("WTI")
    guardrail["consecutive_loss_state"] = {"pause_until": past}
    plans = rm.build_execution_plans(
        decision=_decision("WTI", "BUY"),
        symbol_contexts={"WTI": _ctx("WTI", 95.0, atr=1.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert len(plans) == 1


def test_low_confidence_rejected():
    s = Settings()
    rm = RiskManager(s)
    guardrail = _base_guardrail("WTI")
    plans = rm.build_execution_plans(
        decision=_decision("WTI", "BUY", confidence=0.5),
        symbol_contexts={"WTI": _ctx("WTI", 95.0)},
        account_snapshot=_acct(),
        last_trade_times={},
        guardrail_context=guardrail,
    )
    assert plans == []
