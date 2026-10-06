"""Degraded price inputs must raise the bar for NEW entries, never block exits."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trading_bot.config import Settings
from trading_bot.models import DataQuality, DecisionEnvelope, OpenPosition, SymbolContext, TradeDecision
from trading_bot.risk import RiskManager

# Reuse the established fixtures rather than re-scaffolding them.
from tests.test_overnight_pnl_credit import _make_bot
from tests.test_risk_gates import _acct, _base_guardrail, _ctx, _decision

LOGGER_NAME = "trading_bot.risk"


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_default_degraded_confidence_floor_is_above_normal_floor():
    s = _settings()
    assert s.degraded_inputs_min_confidence > s.min_model_confidence


def test_degraded_floor_below_normal_floor_is_rejected():
    """A degraded floor beneath `min_model_confidence` is unreachable: the
    normal floor already rejects everything below itself, so the gate would be
    a silent no-op. That must be a hard config error, not a quiet downgrade."""
    with pytest.raises(ValidationError) as exc:
        _settings(degraded_inputs_min_confidence=0.10)
    message = str(exc.value)
    assert "DEGRADED_INPUTS_MIN_CONFIDENCE" in message
    assert "MIN_MODEL_CONFIDENCE" in message


def test_degraded_floor_equal_to_normal_floor_is_accepted():
    """Boundary: equal is the weakest LEGAL setting — the gate is inert but
    not misleading, since nothing below the normal floor was tradeable anyway."""
    s = _settings(degraded_inputs_min_confidence=0.72, min_model_confidence=0.72)
    assert s.degraded_inputs_min_confidence == 0.72


def test_degraded_floor_above_normal_floor_is_accepted():
    s = _settings(degraded_inputs_min_confidence=0.95)
    assert s.degraded_inputs_min_confidence == 0.95


def test_raising_normal_floor_past_the_degraded_floor_is_rejected():
    """The validator is cross-field, not a constant check: pushing
    MIN_MODEL_CONFIDENCE above the default 0.85 degraded floor inverts the two
    and must fail just as loudly."""
    with pytest.raises(ValidationError):
        _settings(min_model_confidence=0.90)


def test_degraded_quality_flags_symbol():
    from trading_bot.risk import degraded_input_reasons

    quality = DataQuality(bars_total=100, bars_imputed=3, is_degraded=True)
    reasons = degraded_input_reasons({"WTI": quality})
    assert reasons == ["WTI:3/100 bars imputed"]


def test_clean_quality_flags_nothing():
    from trading_bot.risk import degraded_input_reasons

    quality = DataQuality(bars_total=100, bars_imputed=0, is_degraded=False)
    assert degraded_input_reasons({"WTI": quality}) == []


def test_missing_quality_is_not_treated_as_degraded():
    from trading_bot.risk import degraded_input_reasons

    assert degraded_input_reasons({"WTI": None}) == []


def test_reasons_are_sorted_and_skip_healthy_symbols():
    """One entry per DEGRADED symbol, deterministically ordered."""
    from trading_bot.risk import degraded_input_reasons

    reasons = degraded_input_reasons(
        {
            "WTI": DataQuality(bars_total=100, bars_imputed=5, is_degraded=True),
            "GOLD": DataQuality(bars_total=100, bars_imputed=0, is_degraded=False),
            "BRENT": DataQuality(bars_total=80, bars_imputed=2, is_degraded=True),
            "AAPL": None,
        }
    )
    assert reasons == ["BRENT:2/80 bars imputed", "WTI:5/100 bars imputed"]


def test_reasons_handles_empty_fallback_context_without_zero_division():
    """The market-data fallback context has bars_total=0 and is_degraded=True;
    formatting a reason for it must not raise."""
    from trading_bot.risk import degraded_input_reasons

    reasons = degraded_input_reasons(
        {"WTI": DataQuality(bars_total=0, is_degraded=True, note="no history")}
    )
    assert reasons == ["WTI:0/0 bars imputed"]


# --- build_execution_plans gate -------------------------------------------
#
# All gate tests below hold the OTHER gates constant (WTI @ 95.0, atr=1.0 is
# the setup tests/test_risk_gates.py::test_open_session_allows_entry proves is
# tradeable) so a rejection can only come from the degraded-inputs gate. Each
# blocking test also asserts the DEGRADED_INPUTS_BLOCK reason was logged, and
# each is paired with a control that passes.


def _quality(**over) -> DataQuality:
    kwargs: dict = dict(bars_total=100, bars_imputed=5, is_degraded=True)
    kwargs.update(over)
    return DataQuality(**kwargs)


def _ctx_with_quality(
    symbol: str = "WTI",
    price: float = 95.0,
    atr: float = 1.0,
    quality: DataQuality | None = None,
) -> SymbolContext:
    """The shared _ctx fixture, plus a DataQuality report."""
    return _ctx(symbol, price, atr=atr).model_copy(update={"data_quality": quality})


def _plans(settings: Settings, decision: DecisionEnvelope, context: SymbolContext, positions=None):
    return RiskManager(settings).build_execution_plans(
        decision=decision,
        symbol_contexts={context.symbol: context},
        account_snapshot=_acct(positions=positions),
        last_trade_times={},
        guardrail_context=_base_guardrail(context.symbol),
    )


def _blocked(caplog) -> bool:
    return any("DEGRADED_INPUTS_BLOCK" in r.getMessage() for r in caplog.records)


def test_degraded_context_rejects_buy_below_degraded_floor(caplog):
    """confidence 0.75 clears min_model_confidence (0.72) but not the degraded
    floor (0.85), so the entry is refused."""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(),
            _decision("WTI", "BUY", confidence=0.75),
            _ctx_with_quality(quality=_quality()),
        )
    assert plans == []
    assert _blocked(caplog)


def test_healthy_context_allows_the_same_buy():
    """Control for the test above: identical trade, clean feed → it trades.
    Proves the rejection came from the degraded gate, not another guard."""
    plans = _plans(
        _settings(),
        _decision("WTI", "BUY", confidence=0.75),
        _ctx_with_quality(quality=_quality(bars_imputed=0, is_degraded=False)),
    )
    assert len(plans) == 1
    assert plans[0].action == "BUY"


def test_unmeasured_quality_allows_the_same_buy():
    """data_quality=None means "not measured", NOT degraded. An unrelated
    plumbing gap must not freeze all trading."""
    plans = _plans(
        _settings(),
        _decision("WTI", "BUY", confidence=0.75),
        _ctx_with_quality(quality=None),
    )
    assert len(plans) == 1
    assert plans[0].action == "BUY"


def test_degraded_context_allows_buy_above_degraded_floor(caplog):
    """The gate is a raised threshold, not a blanket ban: high conviction still
    gets through on a degraded feed."""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(),
            _decision("WTI", "BUY", confidence=0.90),
            _ctx_with_quality(quality=_quality()),
        )
    assert len(plans) == 1
    assert plans[0].action == "BUY"
    assert not _blocked(caplog)


def test_degraded_context_allows_buy_exactly_at_degraded_floor():
    """Boundary: the floor is inclusive (reject strictly BELOW it)."""
    settings = _settings()
    plans = _plans(
        settings,
        _decision("WTI", "BUY", confidence=settings.degraded_inputs_min_confidence),
        _ctx_with_quality(quality=_quality()),
    )
    assert len(plans) == 1


def test_degraded_context_rejects_sell_below_degraded_floor(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(allow_shorts=True),
            _decision("WTI", "SELL", confidence=0.75),
            _ctx_with_quality(quality=_quality()),
        )
    assert plans == []
    assert _blocked(caplog)


def test_degraded_context_never_blocks_close(caplog):
    """We must always be able to exit a position, especially on a degraded
    feed. CLOSE carries a low confidence here and is still honoured."""
    position = OpenPosition(
        symbol="WTI", position_id="DI001", quantity=1.0,
        current_price=95.0, direction="buy",
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(),
            _decision("WTI", "CLOSE", confidence=0.10),
            _ctx_with_quality(quality=_quality(bars_imputed=99)),
            positions=[position],
        )
    assert len(plans) == 1
    assert plans[0].action == "CLOSE"
    assert plans[0].position_id == "DI001"
    assert not _blocked(caplog)


def test_degraded_context_never_blocks_close_on_empty_fallback_context(caplog):
    """The worst case: bars_total=0 (no history at all). CLOSE still passes."""
    position = OpenPosition(
        symbol="WTI", position_id="DI002", quantity=1.0,
        current_price=95.0, direction="buy",
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(),
            _decision("WTI", "CLOSE", confidence=0.10),
            _ctx_with_quality(quality=DataQuality(bars_total=0, is_degraded=True)),
            positions=[position],
        )
    assert len(plans) == 1
    assert plans[0].action == "CLOSE"
    assert not _blocked(caplog)


def test_degraded_context_still_exits_when_a_reversal_carries_the_exit(caplog):
    """A BUY/SELL that flips an existing opposite-direction position carries an
    EXIT leg. With the default allow_shorts=False, SELL-against-a-long is
    converted to CLOSE — that is a live exit path, and a degraded feed must not
    strand the position. Regression guard: gating this before the reversal
    branch silently swallowed the CLOSE leg."""
    position = OpenPosition(
        symbol="WTI", position_id="DI001", quantity=1.0,
        current_price=95.0, direction="buy",
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(),
            _decision("WTI", "SELL", confidence=0.75),
            _ctx_with_quality(quality=_quality()),
            positions=[position],
        )
    assert [p.action for p in plans] == ["CLOSE"]
    assert plans[0].position_id == "DI001"


def test_degraded_reversal_exits_but_does_not_re_enter(caplog):
    """Pins the degraded gate's REVERSAL log path specifically.

    With shorts AND reversals enabled, a degraded low-conviction reversal emits
    ONLY the exit leg. Be precise about what proves what:

    * The CLOSE-only OUTCOME is co-enforced — it is not evidence of this gate.
      On a reversal the position being closed is still in
      `open_positions_by_symbol`, so `CorrelationCap.would_breach('WTI',
      ['WTI'])` self-correlates at 1.0 > cap 0.7 and converts to CLOSE anyway
      (`risk.py:401-404`). Measured: degraded/clean x 0.75/0.90 all produce
      `['CLOSE']`, so `[p.action for p in plans] == ['CLOSE']` alone
      discriminates nothing.
    * What IS specific to this gate is WHICH branch fires first and what it
      logs. The degraded gate sits at `risk.py:261` — well before the
      correlation cap — so it must claim the conversion, via the
      `if reversing_position:` branch, not the plain "Rejected" path.

    Hence the exact-line assertion below: it fails if that branch is removed,
    reordered behind the correlation cap, or downgraded to a plain rejection.
    """
    position = OpenPosition(
        symbol="WTI", position_id="DI001", quantity=1.0,
        current_price=95.0, direction="buy",
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(allow_shorts=True, allow_position_reversals=True),
            _decision("WTI", "SELL", confidence=0.75),
            _ctx_with_quality(quality=_quality()),
            positions=[position],
        )
    assert [p.action for p in plans] == ["CLOSE"]
    assert plans[0].position_id == "DI001"
    assert any(
        "Converted SELL WTI into CLOSE because DEGRADED_INPUTS_BLOCK" in r.getMessage()
        for r in caplog.records
    )


def test_degraded_context_does_not_gate_hold(caplog):
    """HOLD produces no plan anyway, but it must not be attributed to this
    gate — otherwise the logs mislead during an incident."""
    decision = DecisionEnvelope(
        market_regime="mixed",
        summary="t",
        trades=[TradeDecision(
            symbol="WTI", action="HOLD", confidence=0.10,
            thesis="t", news_bias="neutral", invalidate_if="x",
        )],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(_settings(), decision, _ctx_with_quality(quality=_quality()))
    assert plans == []
    assert not _blocked(caplog)


def test_degraded_gate_cannot_upsize_or_loosen_anything():
    """The gate may only REJECT. A degraded high-conviction entry must be sized
    and stopped identically to the same entry on a clean feed."""
    decision = _decision("WTI", "BUY", confidence=0.90)
    degraded = _plans(_settings(), decision, _ctx_with_quality(quality=_quality()))
    healthy = _plans(
        _settings(),
        decision,
        _ctx_with_quality(quality=_quality(bars_imputed=0, is_degraded=False)),
    )
    assert len(degraded) == len(healthy) == 1
    assert degraded[0].quantity == healthy[0].quantity
    assert degraded[0].stop_distance == healthy[0].stop_distance
    assert degraded[0].take_profit_distance == healthy[0].take_profit_distance


# --- guardrail_context visibility ------------------------------------------


def _guardrail(bot, symbol_contexts: dict[str, SymbolContext]) -> dict:
    return bot._build_guardrail_context(
        account_snapshot=_acct(),
        symbol_contexts=symbol_contexts,
        prorealtime_snapshot=None,
        now=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_guardrail_context_publishes_degraded_inputs(tmp_path):
    """The degradation must be visible to the LLM prompt and the decision log,
    not just to the executor's rejection path."""
    bot = _make_bot(tmp_path)
    ctx = _guardrail(
        bot,
        {
            "WTI": _ctx_with_quality("WTI", quality=_quality()),
            "GOLD": _ctx_with_quality(
                "GOLD", price=4700.0,
                quality=_quality(bars_imputed=0, is_degraded=False),
            ),
        },
    )
    assert ctx["degraded_inputs"] == ["WTI:5/100 bars imputed"]


def test_guardrail_context_degraded_inputs_empty_on_healthy_feeds(tmp_path):
    bot = _make_bot(tmp_path)
    ctx = _guardrail(
        bot,
        {"WTI": _ctx_with_quality("WTI", quality=_quality(bars_imputed=0, is_degraded=False))},
    )
    assert ctx["degraded_inputs"] == []


def test_guardrail_context_degraded_inputs_does_not_block_new_entries(tmp_path):
    """Degradation raises the confidence bar; it must NOT set the global
    block_new_entries kill-switch, which would stop all trading."""
    bot = _make_bot(tmp_path)
    ctx = _guardrail(bot, {"WTI": _ctx_with_quality("WTI", quality=_quality())})
    assert ctx["degraded_inputs"] == ["WTI:5/100 bars imputed"]
    assert ctx["block_new_entries"] is False
    assert ctx["block_reasons"] == []


# --- broker-snapshot fallback context ---------------------------------------


def test_position_fallback_context_is_degraded_not_unmeasured(tmp_path, caplog):
    """`_build_position_fallback_context` fabricates every indicator from one
    price (ATR=0). With data_quality=None it read as "not measured", so a
    reversal's new leg on that placeholder skipped the degraded floor."""
    bot = _make_bot(tmp_path)
    position = OpenPosition(
        symbol="WTI", position_id="DI003", quantity=1.0,
        current_price=95.0, direction="buy",
    )

    context = bot._build_position_fallback_context(
        symbol="WTI", open_position=position, headlines=[],
    )

    assert context is not None
    assert context.data_quality is not None
    assert context.data_quality.bars_total == 0
    assert context.data_quality.is_degraded is True
    assert context.data_quality.note == context.chart_note

    # The degraded gate -- not merely the correlation cap -- claims the
    # reversal, and the exit leg still goes through.
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        plans = _plans(
            _settings(allow_shorts=True, allow_position_reversals=True),
            _decision("WTI", "SELL", confidence=0.75),
            context,
            positions=[position],
        )
    assert [p.action for p in plans] == ["CLOSE"]
    assert plans[0].position_id == "DI003"
    assert any(
        "Converted SELL WTI into CLOSE because DEGRADED_INPUTS_BLOCK" in r.getMessage()
        for r in caplog.records
    )
