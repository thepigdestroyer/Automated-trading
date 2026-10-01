"""A failed ``decide()`` must not mean "no exit management this cycle".

THE FAILURE MODE THIS EXISTS TO CLOSE
-------------------------------------
The bot reads the Portfolio Manager's JSON out of Kimi K2.6's *reasoning*
channel and repairs it with ``json_repair``. Over 15 days that produced 491
``subagent_failure`` events. When the salvage fails, ``decide()`` raises, the
exception propagates out of ``run_cycle``, and the cycle produces **no plans at
all** -- including no CLOSE plans for positions that have already breached their
stop, their target, or ``default_max_holding_hours``.

That is the dangerous half. A broken LLM response leaves real money in real
positions unmanaged, and the next cycle inherits the same problem. Exit
management is mechanical and does not need a language model.

THE INVARIANT, AND ITS HARD LIMIT
---------------------------------
On LLM failure the bot applies the mechanical exit rules itself and emits CLOSE
plans where breached. It may do **nothing else**. An LLM failure must never
become an entry signal, a reversal, or a resize -- so the fallback is CLOSE-only
by construction *and* by an explicit downstream filter, and that is asserted
here rather than assumed.

Both failure shapes must reach the fallback:
  1. the client raising (network, 429, provider error), and
  2. the client returning output that fails to parse or validate.
Shape 2 is exercised through the real engine and the real parse path, not by
faking an exception, because "unparseable output" is the shape that actually
dominates production.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Settings
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    IndicatorSnapshot,
    OpenPosition,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager

NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Pin the clock ``trading_bot.bot`` reads to NOW.

    Positions are aged relative to NOW, but ``run_cycle`` stamps
    ``cycle_started_at`` from ``datetime.now()``. Without this the "healthy
    position" and boundary tests are time bombs: they pass on the day they are
    written and fail once real time drifts past ``default_max_holding_hours``.

    Deliberately NOT freezegun: it patches the clock process-wide, and a module
    first lazy-imported while frozen segfaults the interpreter in
    ``_compile_bytecode``. Patching only the name bot.py resolves is narrower
    and import-safe.
    """
    monkeypatch.setattr("trading_bot.bot.datetime", _FrozenDatetime)


def _settings(tmp_path: Path, **over) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key="test",
        broker="paper",
        dry_run=True,
        symbols=["BRENT"],
        auto_discover_symbols=False,
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        supervisor_blacklist_path=tmp_path / "blacklist.json",
        **over,
    )


def _indicators(price: float = 99.3, atr_14: float = 1.2) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
        rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=atr_14,
        volatility_20d=0.0, return_1d=0.0, return_5d=0.0,
    )


def _context(symbol: str = "BRENT", *, price: float = 99.3) -> SymbolContext:
    return SymbolContext(
        symbol=symbol,
        display_name=symbol,
        asset_class="commodity",
        as_of=NOW.isoformat(),
        indicators=_indicators(price),
        headlines=[],
        chart_note="test",
    )


def _position(
    position_id: str = "P1",
    symbol: str = "BRENT",
    *,
    direction: str = "buy",
    price: float = 99.3,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id, symbol=symbol, direction=direction,
        quantity=1.0, current_price=price, pnl=0.0,
    )


def _account(open_positions=None, total: float = 6976.0) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP", total_funds=total, free_funds=total,
        open_positions=open_positions or [],
    )


def _make_bot(tmp_path: Path, *, position_age_hours: float | None, **over):
    """A bot wired far enough to run a full cycle, with a REAL RiskManager.

    The risk manager must be real: the whole claim is "a CLOSE *plan* is still
    produced", and a MagicMock risk manager would make that claim unfalsifiable.
    """
    from trading_bot.bot import TradingBot

    settings = _settings(tmp_path, **over)
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.risk_manager = RiskManager(settings)

    positions: dict[str, Any] = {}
    if position_age_hours is not None:
        first_seen = NOW - timedelta(hours=position_age_hours)
        positions["P1"] = {
            "symbol": "BRENT",
            "first_seen_at": first_seen.isoformat(),
            "entry_price": 99.3,
            "highest_price": 99.3,
            "lowest_price": 99.3,
            "direction": "buy",
            "max_holding_hours": settings.default_max_holding_hours,
        }
    bot.runtime_state = {
        "daily": {}, "positions": positions, "open_position_invalidations": {},
    }

    bot.broker = MagicMock()
    bot.broker.get_account_snapshot = MagicMock(
        return_value=_account([_position()])
    )
    bot.broker.execute = MagicMock(
        side_effect=lambda plan, dry_run, cycle_id: __import__(
            "trading_bot.models", fromlist=["ExecutionResult"]
        ).ExecutionResult(
            plan=plan, status="dry_run", submitted_at=NOW.isoformat(),
        )
    )
    bot.news_client = MagicMock()
    bot.news_client.fetch_market_headlines = MagicMock(return_value=[])
    bot.entity_news_client = MagicMock()
    bot.entity_news_client.fetch_all = MagicMock(return_value={})
    bot.vix_client = MagicMock()
    bot.vix_client.fetch = MagicMock(return_value=None)
    bot.correlation_client = MagicMock()
    bot.correlation_client.fetch = MagicMock(return_value=None)
    bot.econ_calendar = MagicMock()
    bot.econ_calendar.events_next_hours = MagicMock(return_value=[])
    bot.prorealtime_client = MagicMock()
    bot.prorealtime_client.load_snapshot = MagicMock(return_value=None)
    bot.market_data_client = MagicMock()
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.alerts = MagicMock()
    bot.paper_pnl = None
    bot.track_a_pnl = None
    bot.decision_engine = MagicMock()
    bot.decision_engine.discover_symbols = MagicMock(return_value=[])
    bot.stop_event = Event()
    bot.wake_event = Event()
    bot.operator_lock = Lock()
    bot.model_lock = Lock()
    bot._current_entity_news = {}
    bot._current_market_vix = None
    bot._current_econ_events = []
    bot._current_correlation_context = None
    bot._current_macro_context = None
    bot._current_news_synthesis = None
    bot._current_cycle_id = "test_cycle"
    bot._cycle_counter = 0
    bot._memory_profile_started = False
    bot.last_trade_times = {}

    # Trim the cycle to the decision section under test. Note that
    # _build_guardrail_context and _collect_guardrail_close_requests are NOT
    # mocked: the mechanical exit rules are the thing being tested.
    bot._news_memory_metrics = MagicMock(
        return_value={"size": 0, "oldest_published_at": None}
    )
    bot._refresh_runtime_state = MagicMock()
    bot._get_operator_context = MagicMock(return_value={})
    bot._fetch_macro_context_cached = MagicMock(return_value=None)
    bot._compute_entry_block_lite = MagicMock(
        return_value={
            "block_new_entries": False,
            "daily_loss_breached": False,
            "block_reasons": [],
            "drawdown_close_only": False,
            "drawdown_pct": 0.0,
        }
    )
    bot._build_market_research = MagicMock(return_value=None)
    bot._build_symbol_research = MagicMock(return_value=None)
    bot._select_active_symbols = MagicMock(return_value=["BRENT"])
    bot._collect_symbol_contexts = MagicMock(return_value={"BRENT": _context()})
    bot._drop_unfillable_contexts = MagicMock(
        side_effect=lambda contexts, account_snapshot: contexts
    )
    bot._run_track_a = MagicMock()
    bot._log_decision_records = MagicMock()
    return bot


def _plans(bot) -> list:
    """Run a cycle and return the plans the risk manager actually produced."""
    captured: list = []
    real_build = bot.risk_manager.build_execution_plans

    def _spy(**kwargs):
        plans = real_build(**kwargs)
        captured.extend(plans)
        return plans

    bot.risk_manager.build_execution_plans = _spy
    bot.run_cycle()
    return captured


def _events(bot) -> list[dict]:
    path = bot.settings.json_log_path
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


class _Boom(RuntimeError):
    """The client blew up (network / 429 / provider error)."""


# --- Shape 1: the client raises ---------------------------------------------


def test_llm_exception_still_closes_a_position_past_max_holding(tmp_path):
    """THE test from the brief.

    A position 100h old against a 72h ``default_max_holding_hours`` must still
    get a CLOSE plan when the LLM client raises. Before the fallback this cycle
    produced nothing at all and the position stayed open indefinitely.
    """
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))

    plans = _plans(bot)

    assert bot.decision_engine.decide.called, "the LLM path was never reached"
    closes = [p for p in plans if p.action == "CLOSE"]
    assert len(closes) == 1, f"expected exactly one CLOSE plan, got {plans}"
    assert closes[0].symbol == "BRENT"
    assert closes[0].position_id == "P1"
    assert "72" in closes[0].reason or "holding" in closes[0].reason.lower(), (
        f"the CLOSE must be attributed to the max-holding rule: {closes[0].reason}"
    )


def test_run_cycle_does_not_propagate_the_llm_exception(tmp_path):
    """The cycle must survive. If the exception escapes run_cycle the watcher
    restarts the process -- 55 times in 15 days -- and nothing is managed."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))

    results = bot.run_cycle()  # must not raise

    assert [r.plan.action for r in results] == ["CLOSE"]


# --- Shape 2: the client returns output that will not parse ------------------


def _engine_returning(content: str):
    """A REAL decision engine whose client returns ``content`` verbatim.

    This drives the genuine failure path -- ``_extract_json_text`` ->
    ``parse_json_lenient`` -> ``json_repair`` -> validate -- rather than
    simulating it with a raised exception.
    """
    from trading_bot.openai_decision import OpenAIDecisionEngine

    settings = Settings(
        _env_file=None, llm_provider="openrouter", openrouter_api_key="test",
        broker="paper", dry_run=True,
    )
    engine = OpenAIDecisionEngine(settings)

    class _Message:
        def __init__(self):
            self.content = content
            self.reasoning = None
            self.reasoning_content = None

    class _Choice:
        def __init__(self):
            self.message = _Message()
            self.finish_reason = "stop"

    class _Response:
        def __init__(self):
            self.choices = [_Choice()]
            self.provider = "nvidia-nim"
            self.usage = None

    class _Completions:
        def create(self, **kwargs):
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    engine.client = _Client()
    return engine


def test_real_engine_really_does_raise_on_unparseable_output(tmp_path):
    """Positive control for the test below.

    If ``decide()`` silently returned something usable for this input, the
    parse-failure test would be vacuous. Pin that unparseable output genuinely
    raises out of the real engine.
    """
    engine = _engine_returning("I considered the setup but produced no JSON.")
    with pytest.raises(Exception):
        engine.decide(
            account_snapshot=_account([_position()]),
            symbol_contexts={"BRENT": _context()},
            market_headlines=[],
        )


@pytest.mark.parametrize(
    "content",
    [
        "I considered the setup but produced no JSON at all.",
        '{"market_regime": "risk-off", "summary": "x", "trades": [',
        "",
    ],
    ids=["no_json", "truncated_json", "empty"],
)
def test_unparseable_llm_output_still_closes_a_breached_position(tmp_path, content):
    """Shape 2 must reach the same fallback as shape 1.

    A fallback wired only to ``except`` on transport errors would leave the
    dominant production failure -- salvage-defeating model output -- completely
    unhandled.
    """
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine = _engine_returning(content)

    plans = _plans(bot)

    closes = [p for p in plans if p.action == "CLOSE"]
    assert len(closes) == 1, f"expected a CLOSE for the stale position, got {plans}"
    assert closes[0].position_id == "P1"


def test_schema_valid_json_that_fails_validation_still_closes(tmp_path):
    """Parseable JSON that is not a DecisionEnvelope is also 'unparseable
    output' as far as the cycle is concerned."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine = _engine_returning('{"unexpected": [1, 2, 3]}')

    plans = _plans(bot)

    closes = [p for p in plans if p.action == "CLOSE"]
    assert len(closes) == 1, f"expected a CLOSE, got {plans}"


def test_decide_returning_a_non_envelope_still_closes(tmp_path):
    """Defensive third shape: if any future code path swallows the parse error
    and hands back ``None``, that must also route to the fallback rather than
    crashing on ``decision.market_regime``."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(return_value=None)

    plans = _plans(bot)

    closes = [p for p in plans if p.action == "CLOSE"]
    assert len(closes) == 1, f"expected a CLOSE, got {plans}"


# --- The hard limit: CLOSE only, never an entry -----------------------------


def test_fallback_never_opens_reverses_or_resizes(tmp_path):
    """An LLM failure must not become a trade signal.

    Every plan the fallback cycle emits must be a CLOSE of a position we already
    hold. No BUY, no SELL, and nothing for a symbol we are not already in.
    """
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))

    plans = _plans(bot)

    assert plans, "not vacuous: the cycle really did produce plans"
    assert {p.action for p in plans} == {"CLOSE"}, [
        (p.action, p.symbol) for p in plans
    ]
    assert all(p.position_id == "P1" for p in plans)


def test_fallback_drops_an_entry_even_if_an_override_injects_one(tmp_path, monkeypatch):
    """Defence in depth for the test above.

    Today the synthetic decision starts empty and both overrides only ever add
    CLOSEs, so no BUY can reach the fallback -- but that is a property of
    *other* code. This pins the fallback's own CLOSE-only filter, so a future
    override that emits an entry still cannot turn an LLM outage into a trade.
    """
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))
    real_overrides = bot._apply_guardrail_overrides

    def _inject_entry(**kwargs):
        decision = real_overrides(**kwargs)
        entry = TradeDecision(
            symbol="BRENT", action="BUY", confidence=0.99,
            thesis="injected entry", news_bias="positive", invalidate_if="x",
        )
        return decision.model_copy(update={"trades": [*decision.trades, entry]})

    monkeypatch.setattr(bot, "_apply_guardrail_overrides", _inject_entry)

    # Assert on what the fallback HANDS the risk manager, not on the plans it
    # returns: BRENT is already held long, so the risk manager would reject a
    # same-direction BUY on its own and a plan-level assertion would pass even
    # with the fallback's filter deleted.
    seen_trades: list = []
    real_build = bot.risk_manager.build_execution_plans

    def _spy(**kwargs):
        seen_trades.extend(kwargs["decision"].trades)
        return real_build(**kwargs)

    bot.risk_manager.build_execution_plans = _spy
    bot.run_cycle()

    assert seen_trades, "not vacuous: the breached position must still be closed"
    assert {t.action for t in seen_trades} == {"CLOSE"}, [
        (t.action, t.symbol) for t in seen_trades
    ]


def test_fallback_emits_no_plans_when_nothing_is_breached(tmp_path):
    """The negative control that stops the test above being trivially true.

    A position well inside its holding window must be left alone. If the
    fallback closed everything on every LLM hiccup it would be strictly worse
    than doing nothing -- it would liquidate a healthy book on a parse error.
    """
    bot = _make_bot(tmp_path, position_age_hours=1.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))

    plans = _plans(bot)

    assert plans == [], f"a healthy position must not be closed: {plans}"


def test_max_holding_boundary_is_the_configured_value(tmp_path):
    """Pin the threshold to the setting, not to a hard-coded 72.

    Kills a mutant that closes everything, and one that reads the wrong knob.
    """
    for hours, expect_close in ((71.0, False), (73.0, True)):
        bot = _make_bot(tmp_path, position_age_hours=hours)
        bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))
        plans = _plans(bot)
        assert bool(plans) is expect_close, f"age={hours}h -> {plans}"

    # And it really is driven by the config value, not the number 72.
    bot = _make_bot(tmp_path, position_age_hours=30.0,
                    default_max_holding_hours=24)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))
    assert len(_plans(bot)) == 1, "a lowered max_holding_hours must be honoured"


def test_per_position_max_holding_override_is_honoured(tmp_path):
    """The runtime-state value set at entry wins over the config default,
    exactly as the LLM-path guardrail does."""
    bot = _make_bot(tmp_path, position_age_hours=30.0)
    bot.runtime_state["positions"]["P1"]["max_holding_hours"] = 24
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))

    assert len(_plans(bot)) == 1


def test_trailing_exit_breach_also_closes(tmp_path):
    """Max holding is not the only mechanical rule. The trailing-exit rule must
    fire in the fallback too, or a position that has given back its gains sits
    unmanaged until the holding clock runs out."""
    bot = _make_bot(
        tmp_path,
        position_age_hours=1.0,  # nowhere near the holding limit
        trailing_exit_activation_pct=0.01,
        trailing_exit_pct=0.005,
    )
    # Long from 99.3, peaked at 105.0 (+5.7%, past the 1% activation), now back
    # to 100.0 -- a 4.8% retrace from the peak, past the 0.5% trail.
    bot.runtime_state["positions"]["P1"].update(
        {"entry_price": 99.3, "highest_price": 105.0}
    )
    bot.broker.get_account_snapshot = MagicMock(
        return_value=_account([_position(price=100.0)])
    )
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))

    plans = _plans(bot)

    closes = [p for p in plans if p.action == "CLOSE"]
    assert len(closes) == 1, f"trailing exit must close in the fallback: {plans}"
    assert "trailing" in closes[0].reason.lower(), closes[0].reason


def test_multiple_positions_only_the_breached_one_closes(tmp_path):
    """Per-position evaluation, not all-or-nothing."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.runtime_state["positions"]["P2"] = {
        "symbol": "WTI",
        "first_seen_at": (NOW - timedelta(hours=2)).isoformat(),
        "entry_price": 65.0, "highest_price": 65.0, "lowest_price": 65.0,
        "direction": "buy", "max_holding_hours": 72,
    }
    bot.broker.get_account_snapshot = MagicMock(
        return_value=_account([
            _position("P1", "BRENT"),
            _position("P2", "WTI", price=65.0),
        ])
    )
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))

    plans = _plans(bot)

    assert [(p.action, p.position_id) for p in plans] == [("CLOSE", "P1")]


# --- The fallback must not fire when the LLM worked -------------------------


def test_successful_decide_is_untouched(tmp_path):
    """Positive control: a working LLM keeps its own plans.

    Without this, "the fallback produced a CLOSE" could equally be explained by
    the fallback having replaced the normal path entirely.
    """
    bot = _make_bot(tmp_path, position_age_hours=1.0)
    bot.decision_engine.decide = MagicMock(
        return_value=DecisionEnvelope(
            market_regime="risk-on",
            summary="model summary",
            trades=[
                TradeDecision(
                    symbol="BRENT", action="CLOSE", confidence=0.9,
                    thesis="model wants out", news_bias="neutral",
                    invalidate_if="x", close_position_id="P1",
                )
            ],
        )
    )

    plans = _plans(bot)

    assert len(plans) == 1
    assert plans[0].action == "CLOSE"
    assert plans[0].reason == "model wants out", (
        "the model's own plan must survive, not be replaced by the fallback"
    )


def test_successful_decide_can_still_open_a_position(tmp_path):
    """The CLOSE-only clamp must apply ONLY to the fallback path. A healthy
    cycle must still be able to enter."""
    bot = _make_bot(tmp_path, position_age_hours=None)
    bot.broker.get_account_snapshot = MagicMock(return_value=_account([]))
    bot.decision_engine.decide = MagicMock(
        return_value=DecisionEnvelope(
            market_regime="risk-on",
            summary="model summary",
            trades=[
                TradeDecision(
                    symbol="BRENT", action="BUY", confidence=0.95,
                    thesis="clear long setup", news_bias="positive",
                    invalidate_if="x", signal_horizon="swing",
                )
            ],
        )
    )

    plans = _plans(bot)

    assert [p.action for p in plans] == ["BUY"], (
        f"the clamp must not leak into the healthy path: {plans}"
    )


# --- Observability: the failure must be loud --------------------------------


def test_llm_failure_emits_a_subagent_failure_event(tmp_path):
    """The watcher's subagent_failure_streak rule must still see the failure.
    Recovering silently would hide a broken LLM behind a working bot."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("provider 500"))

    _plans(bot)

    failures = [e for e in _events(bot) if e.get("event") == "subagent_failure"]
    assert len(failures) == 1, _events(bot)
    data = failures[0]["data"]
    assert data["error_class"] == "_Boom"
    assert "provider 500" in data["error_message"]


def test_fallback_does_not_pollute_regime_history(tmp_path):
    """The synthetic decision carries no market view. Appending its placeholder
    regime would feed a fabricated regime into the NEXT cycle's prompt."""
    bot = _make_bot(tmp_path, position_age_hours=100.0)
    bot.decision_engine.decide = MagicMock(side_effect=_Boom("x"))

    _plans(bot)

    assert bot.runtime_state.get("regime_history", []) == []
