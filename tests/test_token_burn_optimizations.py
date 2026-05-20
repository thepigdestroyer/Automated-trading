"""Token-burn reduction optimizations (review 2026-04-30-token-burn).

Covers strategies A/C/F/G/H from the post-deployment cost review:

* **A** Skip the four-LLM-call cycle when no new entries are possible
  (daily-loss guardrail breached, outside entry window, or PRT
  flatten_all). On the WTI -£565 day the bot ran 14 full cycles with
  zero possibility of trading — pure waste.
* **C** TTL cache for ``market_research`` and ``news_synthesis``. Macro
  themes are sticky; re-running them every 15-30 min is wasteful.
  Cache invalidates on TTL expiry OR on N+ fresh high-priority
  headlines.
* **F** Per-call ``max_output_tokens`` caps to bound long completions.
* **G** Headline compression for prompt payloads: drop > 12h old,
  dedupe near-duplicate titles, truncate title/snippet, cap item count.
* **H** Lower ``openai_subagent_reasoning_effort`` (default ``low``)
  for the three subagents while keeping the decision call at the
  configured (medium) effort.

These tests pin the behaviour rather than relying on observation in
production: the optimizations cannot regress without a failing test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    Headline,
    IndicatorSnapshot,
    MarketResearchEnvelope,
    NewsSynthesisEnvelope,
    OpenPosition,
    SymbolContext,
)
from trading_bot.openai_decision import (
    OpenAIDecisionEngine,
    compress_headlines_for_prompt,
)


# --- Helpers -----------------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        openai_api_key="test",
        max_daily_loss_pct=0.03,
    )
    base.update(overrides)
    return Settings(**base)


def _make_bot(tmp_path: Path, **settings_overrides) -> TradingBot:
    settings = _settings(tmp_path, **settings_overrides)
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    bot._current_market_vix = None
    bot._current_econ_events = []
    bot._current_entity_news = {}
    bot._current_cycle_id = "test_cycle"
    bot._market_research_cache = None
    bot._news_synthesis_cache = None
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.earnings_calendar = MagicMock()
    bot.earnings_calendar.next_earnings_date = MagicMock(return_value=None)
    bot.forced_close_symbols = set()
    bot.last_trade_times = {}
    # Per review 2026-05-02-adhoc-1 #7: bot.run_cycle now checks
    # self.stop_event for cooperative shutdown.
    from threading import Event
    bot.stop_event = Event()
    bot.wake_event = Event()
    bot._cycle_counter = 0
    bot._memory_profile_started = False
    return bot


def _account(*, total: float, free: float | None = None,
             open_positions: list[OpenPosition] | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=total,
        free_funds=free if free is not None else total,
        open_positions=open_positions or [],
    )


def _headline(
    title: str,
    *,
    age_hours: float | None = 1.0,
    novelty: str = "new",
    source_priority: int = 1,
    published_at: str | None = None,
    snippet: str | None = None,
) -> Headline:
    return Headline(
        title=title,
        source="Test",
        url=f"https://example.com/{title.replace(' ', '-').lower()}",
        published_at=published_at,
        source_domain="bloomberg.com",
        source_priority=source_priority,
        snippet=snippet,
        age_hours=age_hours,
        novelty=novelty,
    )


# --- Strategy A: short-circuit on entry block --------------------------------


def test_lite_check_drawdown_breached_blocks(tmp_path):
    """When daily drawdown exceeds the configured limit, the lite
    pre-check returns block_new_entries=True without calling any LLM."""
    bot = _make_bot(tmp_path, max_daily_loss_pct=0.03)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=9_300.0)  # -7% from starting

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert result["block_new_entries"] is True
    assert result["daily_loss_breached"] is True
    assert result["drawdown_pct"] >= 0.06
    assert any("daily drawdown" in r for r in result["block_reasons"])


def test_lite_check_within_budget_does_not_block(tmp_path):
    bot = _make_bot(tmp_path, max_daily_loss_pct=0.03)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=9_900.0)  # -1% from starting

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert result["block_new_entries"] is False
    assert result["daily_loss_breached"] is False


def test_lite_check_outside_entry_window_blocks(tmp_path):
    bot = _make_bot(
        tmp_path,
        entry_start_hour_utc=9,
        entry_end_hour_utc=17,
    )
    # 03:00 UTC — outside 09-17 window
    now = datetime(2026, 4, 30, 3, 0, tzinfo=timezone.utc)
    bot.runtime_state["daily"] = {
        "date": now.date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=10_000.0)

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=now,
    )

    assert result["block_new_entries"] is True
    assert any("outside the configured UTC window" in r for r in result["block_reasons"])


def test_lite_check_overnight_credit_subtracted_correctly(tmp_path):
    """Overnight credit must be subtracted from gross drawdown so a
    legitimate -3% intraday after a 4% overnight credit doesn't block."""
    bot = _make_bot(tmp_path, max_daily_loss_pct=0.03)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
        "overnight_pnl_credit": 400.0,  # yesterday's overnight loss credited
    }
    account = _account(total=9_400.0)  # gross -6%, but -£400 was overnight

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    # Gross dd = 600, minus 400 overnight credit = 200 / 10000 = 2% — NOT breached.
    assert result["block_new_entries"] is False
    assert result["daily_loss_breached"] is False


def test_short_circuit_skips_llm_calls_when_blocked(tmp_path):
    """Full run_cycle integration: when blocked, no LLM call is made."""
    bot = _make_bot(tmp_path)
    bot.broker.get_account_snapshot = MagicMock(
        return_value=_account(total=9_300.0)  # -7% breached
    )
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
    }

    bot.market_data_client = MagicMock()
    bot.prorealtime_client = MagicMock()
    bot.prorealtime_client.load_snapshot = MagicMock(return_value=None)
    bot.prorealtime_client.export_watchlist = MagicMock()
    bot.news_client = MagicMock()
    bot.entity_news_client = MagicMock()
    bot.vix_client = MagicMock()
    bot.vix_client.fetch = MagicMock(return_value=None)
    bot.econ_calendar = MagicMock()
    bot.econ_calendar.events_next_hours = MagicMock(return_value=[])
    bot.alerts = MagicMock()
    bot.paper_pnl = None

    # Mock decision_engine with strict assertion: ANY LLM call must fail the test.
    bot.decision_engine = MagicMock()
    bot.decision_engine.research_market = MagicMock(
        side_effect=AssertionError("research_market called on blocked cycle")
    )
    bot.decision_engine.synthesise_news = MagicMock(
        side_effect=AssertionError("synthesise_news called on blocked cycle")
    )
    bot.decision_engine.research_symbols = MagicMock(
        side_effect=AssertionError("research_symbols called on blocked cycle")
    )
    bot.decision_engine.decide = MagicMock(
        side_effect=AssertionError("decide called on blocked cycle")
    )
    bot.decision_engine.discover_symbols = MagicMock(
        side_effect=AssertionError("discover_symbols called on blocked cycle")
    )

    bot.risk_manager = MagicMock()
    bot.risk_manager.build_execution_plans = MagicMock(return_value=[])

    # Locks
    from threading import Lock
    bot.operator_lock = Lock()
    bot.model_lock = Lock()
    bot.operator_notes = []
    bot.focused_symbols = set()
    bot.excluded_symbols = set()
    bot.paused = False

    results = bot.run_cycle()

    assert results == []
    bot.decision_engine.research_market.assert_not_called()
    bot.decision_engine.synthesise_news.assert_not_called()
    bot.decision_engine.research_symbols.assert_not_called()
    bot.decision_engine.decide.assert_not_called()


def test_short_circuit_emits_llm_phase_skipped_event(tmp_path):
    """The skipped cycle must emit an audit event so cost analysis can
    see the saving."""
    bot = _make_bot(tmp_path)
    now = datetime.now(timezone.utc)
    bot.runtime_state["daily"] = {
        "date": now.date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=9_300.0)
    entry_block = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=now,
    )

    bot.broker.execute = MagicMock()
    bot.risk_manager = MagicMock()
    bot.risk_manager.build_execution_plans = MagicMock(return_value=[])

    bot._run_short_circuit_cycle(
        cycle_started_at=now,
        account_snapshot=account,
        prorealtime_snapshot=None,
        entry_block=entry_block,
        operator_context={},
    )

    # Event-log file should contain llm_phase_skipped event
    events = bot.settings.json_log_path.read_text(encoding="utf-8")
    assert "llm_phase_skipped" in events


# --- Strategy C: TTL cache ---------------------------------------------------


def test_market_research_cache_hit_within_ttl(tmp_path):
    """Within TTL, the cached envelope is returned instead of calling LLM."""
    bot = _make_bot(tmp_path, market_research_cache_minutes=90)
    sentinel = MarketResearchEnvelope(
        market_regime="neutral-wait", summary="cached", candidates=[]
    )
    bot._market_research_cache = {
        "at": datetime.now(timezone.utc) - timedelta(minutes=30),
        "value": sentinel,
        "breaking_count": 0,
    }
    bot.decision_engine = MagicMock()
    bot.decision_engine.research_market = MagicMock(
        side_effect=AssertionError("LLM called despite valid cache")
    )

    from threading import Lock
    bot.model_lock = Lock()

    result = bot._build_market_research(
        account_snapshot=_account(total=10_000.0),
        market_headlines=[_headline("h1")],
    )

    assert result is sentinel
    bot.decision_engine.research_market.assert_not_called()


def test_market_research_cache_miss_after_ttl(tmp_path):
    bot = _make_bot(tmp_path, market_research_cache_minutes=10)
    bot._market_research_cache = {
        "at": datetime.now(timezone.utc) - timedelta(minutes=15),
        "value": MarketResearchEnvelope(
            market_regime="neutral-wait", summary="stale", candidates=[]
        ),
        "breaking_count": 0,
    }
    fresh = MarketResearchEnvelope(
        market_regime="risk-on-momentum", summary="fresh", candidates=[]
    )
    bot.decision_engine = MagicMock()
    bot.decision_engine.research_market = MagicMock(return_value=fresh)
    from threading import Lock
    bot.model_lock = Lock()

    result = bot._build_market_research(
        account_snapshot=_account(total=10_000.0),
        market_headlines=[_headline("h1")],
    )

    assert result is fresh
    bot.decision_engine.research_market.assert_called_once()


def test_market_research_cache_invalidated_by_breaking_news(tmp_path):
    """N+ new high-priority headlines invalidate the cache."""
    bot = _make_bot(
        tmp_path,
        market_research_cache_minutes=90,
        breaking_news_invalidation_threshold=2,
    )
    bot._market_research_cache = {
        "at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "value": MarketResearchEnvelope(
            market_regime="neutral-wait", summary="cached", candidates=[]
        ),
        "breaking_count": 0,
    }
    fresh = MarketResearchEnvelope(
        market_regime="risk-off-geopolitical", summary="fresh", candidates=[]
    )
    bot.decision_engine = MagicMock()
    bot.decision_engine.research_market = MagicMock(return_value=fresh)
    from threading import Lock
    bot.model_lock = Lock()

    # Now there are 3 new high-priority "new" headlines: cache should refresh
    headlines = [
        _headline("Iran strike", source_priority=1, novelty="new"),
        _headline("Fed pause", source_priority=2, novelty="new"),
        _headline("BoJ intervention", source_priority=1, novelty="new"),
    ]

    result = bot._build_market_research(
        account_snapshot=_account(total=10_000.0),
        market_headlines=headlines,
    )

    assert result is fresh
    bot.decision_engine.research_market.assert_called_once()


def test_news_synthesis_cache_invalidated_when_active_symbols_change(tmp_path):
    """If active_symbols set differs from the cached run, refresh."""
    bot = _make_bot(tmp_path, news_synthesis_cache_minutes=60)
    bot._news_synthesis_cache = {
        "at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "value": NewsSynthesisEnvelope(),
        # Cache stores active_symbols as sorted tuple — must match the
        # store path (`tuple(sorted(...))`).
        "active_symbols": tuple(sorted(("WTI", "GOLD"))),
        "breaking_count": 0,
    }

    cached = bot._news_synthesis_cache_lookup(
        market_headlines=[_headline("h1")],
        active_symbols=["WTI", "GOLD"],
        now=datetime.now(timezone.utc),
    )
    assert cached is not None  # match — should hit

    cached_diff = bot._news_synthesis_cache_lookup(
        market_headlines=[_headline("h1")],
        active_symbols=["WTI", "GOLD", "NATGAS"],  # added a new symbol
        now=datetime.now(timezone.utc),
    )
    assert cached_diff is None  # mismatch — should miss


# --- Strategy F: max_output_tokens caps --------------------------------------


def test_decide_passes_max_output_tokens(tmp_path):
    settings = _settings(tmp_path, decision_max_output_tokens=4321)
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings

    captured = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.update(kwargs)
        return DecisionEnvelope(market_regime="neutral-wait", summary="t", trades=[])

    engine._run_structured_generation = fake_run
    engine.decide(
        account_snapshot=_account(total=10_000.0),
        symbol_contexts={},
        market_headlines=[],
    )
    assert captured.get("max_output_tokens") == 4321


def test_research_market_passes_subagent_caps(tmp_path):
    settings = _settings(
        tmp_path,
        subagent_max_output_tokens_market=1234,
        openai_subagent_reasoning_effort="low",
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.update(kwargs)
        return MarketResearchEnvelope(
            market_regime="neutral-wait", summary="t", candidates=[]
        )

    engine._run_structured_generation = fake_run
    engine.research_market([_headline("h")], max_symbols=4)
    assert captured.get("max_output_tokens") == 1234
    assert captured.get("reasoning_effort") == "low"


def test_subagent_max_token_zero_disables_cap(tmp_path):
    """Setting the cap to 0 (or empty) disables it; max_output_tokens=None."""
    settings = _settings(tmp_path, subagent_max_output_tokens_market=0)
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.update(kwargs)
        return MarketResearchEnvelope(
            market_regime="neutral-wait", summary="t", candidates=[]
        )

    engine._run_structured_generation = fake_run
    engine.research_market([_headline("h")], max_symbols=4)
    assert captured.get("max_output_tokens") is None


# --- Strategy G: headline compression ----------------------------------------


def test_compress_drops_old_headlines():
    now = datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc)
    items = [
        _headline("fresh now", age_hours=1.0),
        _headline("recent", age_hours=6.0),
        _headline("stale day-old", age_hours=20.0),
    ]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=12,
        max_items=10,
        dedupe_enabled=False,
        max_title_chars=200,
        max_snippet_chars=300,
        now=now,
    )
    titles = [h.title for h in out]
    assert "stale day-old" not in titles
    assert "fresh now" in titles
    assert "recent" in titles


def test_compress_dedupes_near_duplicate_titles():
    items = [
        _headline("Iran missile strike rocks Hormuz"),
        _headline("Iran Missile Strike Rocks Hormuz!"),  # near-dup capitalisation/punctuation
        _headline("Iran missile strike rocks Hormuz"),  # exact dup
        _headline("Fed cuts rates 25bps"),
    ]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=24,
        max_items=10,
        dedupe_enabled=True,
        max_title_chars=200,
        max_snippet_chars=300,
    )
    titles = [h.title for h in out]
    assert len(titles) == 2
    assert "Iran missile strike rocks Hormuz" in titles
    assert "Fed cuts rates 25bps" in titles


def test_compress_truncates_title_and_snippet():
    long_title = "A" * 500
    long_snippet = "B" * 1000
    items = [_headline(long_title, snippet=long_snippet)]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=24,
        max_items=10,
        dedupe_enabled=False,
        max_title_chars=100,
        max_snippet_chars=200,
    )
    assert len(out[0].title) == 100
    assert len(out[0].snippet) == 200


def test_compress_caps_total_items():
    items = [_headline(f"news {i}", age_hours=1.0) for i in range(50)]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=24,
        max_items=15,
        dedupe_enabled=False,
        max_title_chars=200,
        max_snippet_chars=300,
    )
    assert len(out) == 15


def test_compress_keeps_unparseable_age_headlines():
    """Headlines without parseable age are kept (better to over-include
    than silently lose news the bot can't classify)."""
    h = Headline(
        title="undated",
        source="Test",
        url="https://example.com/undated",
        published_at=None,
        age_hours=None,
        source_domain="example.com",
    )
    out = compress_headlines_for_prompt(
        [h],
        max_age_hours=12,
        max_items=10,
        dedupe_enabled=False,
        max_title_chars=200,
        max_snippet_chars=300,
    )
    assert len(out) == 1


# --- Strategy H: subagent reasoning effort overrides --------------------------


def test_subagent_calls_use_low_reasoning_effort(tmp_path):
    """All three subagents must pass reasoning_effort=low to
    _run_structured_generation, while decide() inherits the configured
    (medium) effort."""
    settings = _settings(
        tmp_path,
        openai_subagent_reasoning_effort="low",
        openai_reasoning_effort="medium",
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured: list[dict] = []

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.append({"schema": schema_name, **kwargs})
        # Return a valid envelope of the expected type
        if schema_name == "market_research_envelope":
            return MarketResearchEnvelope(
                market_regime="neutral-wait", summary="t", candidates=[]
            )
        if schema_name == "news_synthesis_envelope":
            return NewsSynthesisEnvelope()
        if schema_name == "symbol_research_envelope":
            from trading_bot.models import SymbolResearchEnvelope
            return SymbolResearchEnvelope(symbols=[])
        if schema_name == "decision_envelope":
            return DecisionEnvelope(market_regime="neutral-wait", summary="t", trades=[])
        return None

    engine._run_structured_generation = fake_run

    engine.research_market([_headline("h")], max_symbols=4)
    engine.synthesise_news(
        market_headlines=[_headline("h")],
        per_symbol_headlines={},
        entity_news={},
        open_positions=[],
        regime_history=[],
        active_symbols=[],
    )
    indicators = IndicatorSnapshot(
        price=100, sma_20=100, sma_50=100, ema_12=100, ema_26=100,
        rsi_14=50, macd=0, macd_signal=0, atr_14=1, volatility_20d=0.2,
        return_1d=0, return_5d=0,
    )
    ctx = SymbolContext(
        symbol="WTI", display_name="WTI", asset_class="energy",
        as_of=datetime.now(timezone.utc).isoformat(),
        indicators=indicators, headlines=[], chart_note="t",
    )
    engine.research_symbols(
        account_snapshot=_account(total=10_000.0),
        symbol_contexts={"WTI": ctx},
        market_headlines=[],
    )
    engine.decide(
        account_snapshot=_account(total=10_000.0),
        symbol_contexts={"WTI": ctx},
        market_headlines=[],
    )

    by_schema = {row["schema"]: row for row in captured}

    # Subagents → low
    assert by_schema["market_research_envelope"]["reasoning_effort"] == "low"
    assert by_schema["news_synthesis_envelope"]["reasoning_effort"] == "low"
    assert by_schema["symbol_research_envelope"]["reasoning_effort"] == "low"

    # Decision → reasoning_effort not overridden (None means use default)
    decision_kwargs = by_schema["decision_envelope"]
    assert decision_kwargs.get("reasoning_effort") in (None, "medium")


# --- Strategy: rate-limit backoff (review 2026-05-02-rate-limit) -----------


def test_rate_limit_backoff_retries_on_429(tmp_path, monkeypatch):
    """A RateLimitError (429) must trigger sleep+retry; the next call
    should succeed without the engine surfacing the error to the bot."""
    import openai

    settings = _settings(
        tmp_path,
        llm_rate_limit_max_retries=3,
        llm_rate_limit_base_delay_seconds=0.0,  # zero delay for fast tests
        llm_rate_limit_jitter_seconds=0.0,
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings

    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.openai_decision.time.sleep", sleeps.append)

    call_count = {"n": 0}

    def fake_call():
        call_count["n"] += 1
        if call_count["n"] < 3:
            # Build a minimal RateLimitError instance
            response = MagicMock()
            response.status_code = 429
            response.request = MagicMock()
            raise openai.RateLimitError(
                message="Rate limit exceeded",
                response=response,
                body={"error": {"code": 429}},
            )
        return "ok"

    result = engine._call_with_rate_limit_backoff(fake_call, label="test")
    assert result == "ok"
    assert call_count["n"] == 3
    # 2 retries → 2 sleep calls
    assert len(sleeps) == 2


def test_rate_limit_backoff_gives_up_after_max_retries(tmp_path, monkeypatch):
    """When 429s persist past max_retries, the error must bubble up so
    the higher-level fallback (e.g. news_synth fallback to previous
    cycle) can engage."""
    import openai

    settings = _settings(
        tmp_path,
        llm_rate_limit_max_retries=2,
        llm_rate_limit_base_delay_seconds=0.0,
        llm_rate_limit_jitter_seconds=0.0,
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    monkeypatch.setattr("trading_bot.openai_decision.time.sleep", lambda _: None)

    response = MagicMock()
    response.status_code = 429
    response.request = MagicMock()

    def always_429():
        raise openai.RateLimitError(
            message="Rate limit exceeded",
            response=response,
            body={"error": {"code": 429}},
        )

    with pytest.raises(openai.RateLimitError):
        engine._call_with_rate_limit_backoff(always_429, label="test")


def test_rate_limit_backoff_disabled_when_retries_zero(tmp_path, monkeypatch):
    """Setting max_retries=0 disables the backoff entirely — useful
    when an upstream caller wants to handle 429s itself."""
    import openai

    settings = _settings(
        tmp_path,
        llm_rate_limit_max_retries=0,
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.openai_decision.time.sleep", sleeps.append)

    response = MagicMock()
    response.status_code = 429
    response.request = MagicMock()

    def always_429():
        raise openai.RateLimitError(
            message="Rate limit exceeded",
            response=response,
            body={"error": {"code": 429}},
        )

    with pytest.raises(openai.RateLimitError):
        engine._call_with_rate_limit_backoff(always_429, label="test")
    assert sleeps == []  # No sleep on first failure when retries=0


def test_rate_limit_backoff_uses_exponential_growth(tmp_path, monkeypatch):
    """Sleep delays should grow exponentially (5, 10, 20s) with the
    base settings — not constant. Otherwise three rapid retries inside
    the same throttled window are no better than one."""
    import openai

    settings = _settings(
        tmp_path,
        llm_rate_limit_max_retries=4,
        llm_rate_limit_base_delay_seconds=2.0,
        llm_rate_limit_jitter_seconds=0.0,  # deterministic for assertion
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.openai_decision.time.sleep", sleeps.append)

    response = MagicMock()
    response.status_code = 429
    response.request = MagicMock()

    call_count = {"n": 0}

    def fail_then_ok():
        call_count["n"] += 1
        if call_count["n"] < 5:
            raise openai.RateLimitError(
                message="x",
                response=response,
                body={"error": {"code": 429}},
            )
        return "done"

    engine._call_with_rate_limit_backoff(fail_then_ok, label="test")
    # 4 retries → 4 sleeps with exponential delays: 2, 4, 8, 16
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_lite_check_all_sessions_closed_blocks_with_no_positions(tmp_path):
    """Per review 2026-05-02-adhoc-1 #1: when every candidate symbol's
    exchange session is closed AND there are no open positions, the
    lite pre-check must short-circuit the cycle."""
    bot = _make_bot(tmp_path)
    # Force all session lookups to return False (closed)
    bot.holiday_calendar.is_session_open = MagicMock(return_value=False)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=10_000.0)  # No positions, no drawdown

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    assert result["block_new_entries"] is True
    assert result["all_sessions_closed"] is True
    assert any(
        "all candidate-symbol sessions are closed" in r
        for r in result["block_reasons"]
    )


def test_lite_check_all_sessions_closed_does_not_block_with_open_position(tmp_path):
    """When sessions are closed BUT there's an open position to manage,
    we MUST NOT short-circuit — the bot still needs to run the full
    cycle to consider closes."""
    bot = _make_bot(tmp_path)
    bot.holiday_calendar.is_session_open = MagicMock(return_value=False)
    bot.runtime_state["daily"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "starting_equity": 10_000.0,
    }
    open_pos = OpenPosition(
        position_id="P1", symbol="WTI", direction="buy", quantity=1.0,
        current_price=99.0,
    )
    account = _account(total=10_000.0, open_positions=[open_pos])

    result = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=datetime.now(timezone.utc),
    )

    # all_sessions_closed should still be True (the calendar says so)
    # but block_new_entries must NOT include the sessions-closed reason
    # because the open position bypasses it.
    sessions_in_reasons = any(
        "all candidate-symbol sessions are closed" in r
        for r in result["block_reasons"]
    )
    assert sessions_in_reasons is False


def test_select_active_symbols_filters_session_closed(tmp_path):
    """Per review 2026-05-02-adhoc-1 #1 (defense-in-depth): when the
    calendar says ALL exchanges are closed, no symbols (other than
    those with open positions) should pass through
    _select_active_symbols."""
    settings = _settings(tmp_path, symbols=["WTI", "GOLD"])
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=False)  # all closed
    bot._is_tradeable_symbol = lambda sym: True
    bot._dedupe_symbols = lambda items: list(dict.fromkeys(items))
    # Decision engine isn't called when symbols are configured + auto_discover off
    bot.decision_engine = MagicMock()
    bot.decision_engine.discover_symbols = MagicMock(return_value=[])

    active = bot._select_active_symbols(
        account_snapshot=_account(total=10_000.0),
        market_headlines=[],
    )
    # All symbols filtered because all sessions closed
    assert "WTI" not in active
    assert "GOLD" not in active
    # Calendar must have been consulted
    assert bot.holiday_calendar.is_session_open.called


def test_select_active_symbols_keeps_open_position_even_if_session_closed(tmp_path):
    """Open positions must bypass the session-closed filter so the
    bot can still consider closes when the market is closed."""
    settings = _settings(tmp_path, symbols=["WTI"])
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=False)  # all closed
    bot._is_tradeable_symbol = lambda sym: True
    bot._dedupe_symbols = lambda items: list(dict.fromkeys(items))
    bot.decision_engine = MagicMock()
    bot.decision_engine.discover_symbols = MagicMock(return_value=[])

    open_pos = OpenPosition(
        position_id="P1", symbol="WTI", direction="buy", quantity=1.0,
        current_price=99.0,
    )
    active = bot._select_active_symbols(
        account_snapshot=_account(total=10_000.0, open_positions=[open_pos]),
        market_headlines=[],
    )
    # WTI must be in the active list because we have an open position
    assert "WTI" in active


def test_short_circuit_purges_news_memory(tmp_path):
    """Per review 2026-05-02-adhoc-1 #10: short-circuit cycles must
    still purge stale news_memory items so weekend cycles don't
    let 60h-old headlines linger."""
    bot = _make_bot(tmp_path, news_memory_hours=24)
    now = datetime.now(timezone.utc)
    # Seed news_memory with one fresh item and one stale item (>24h)
    stale_ts = (now - timedelta(hours=48)).isoformat()
    fresh_ts = (now - timedelta(hours=1)).isoformat()
    bot.runtime_state["news_memory"] = {
        "items": [
            {"url_hash": "stale", "title": "old", "first_seen_cycle_ts": stale_ts,
             "published_at": stale_ts},
            {"url_hash": "fresh", "title": "new", "first_seen_cycle_ts": fresh_ts,
             "published_at": fresh_ts},
        ],
        "last_purged_at": None,
    }
    bot.runtime_state["daily"] = {
        "date": now.date().isoformat(),
        "starting_equity": 10_000.0,
    }
    account = _account(total=10_000.0)
    entry_block = bot._compute_entry_block_lite(
        account_snapshot=account,
        prorealtime_snapshot=None,
        now=now,
    )
    # Force a block reason so the short-circuit fires
    entry_block["block_new_entries"] = True
    entry_block["block_reasons"] = ["test forced block"]
    entry_block.setdefault("close_requests", [])

    bot.broker.execute = MagicMock()
    bot.risk_manager = MagicMock()
    bot.risk_manager.build_execution_plans = MagicMock(return_value=[])

    bot._run_short_circuit_cycle(
        cycle_started_at=now,
        account_snapshot=account,
        prorealtime_snapshot=None,
        entry_block=entry_block,
        operator_context={},
    )
    items_after = bot.runtime_state["news_memory"]["items"]
    titles = [i.get("title") for i in items_after]
    assert "old" not in titles, f"stale 48h item should have been purged; got {titles}"
    assert "new" in titles, f"fresh 1h item should have survived; got {titles}"


def test_dedupe_execution_outcomes_collapses_repeats():
    """Standalone unit test for the dedupe helper."""
    from trading_bot.openai_decision import _dedupe_execution_outcomes

    outcomes = [
        {"cycle_id": "c1", "outcomes": [
            {"symbol": "NATGAS", "action": "BUY", "status": "failed",
             "error": "exchange session closed", "submitted_at": "t1"},
        ]},
        {"cycle_id": "c2", "outcomes": [
            {"symbol": "NATGAS", "action": "BUY", "status": "failed",
             "error": "exchange session closed", "submitted_at": "t2"},
        ]},
        {"cycle_id": "c3", "outcomes": [
            {"symbol": "NATGAS", "action": "BUY", "status": "failed",
             "error": "exchange session closed", "submitted_at": "t3"},
            {"symbol": "WTI", "action": "BUY", "status": "submitted",
             "error": "", "submitted_at": "t4"},
        ]},
    ]
    deduped = _dedupe_execution_outcomes(outcomes)
    assert len(deduped) == 2
    natgas = next(d for d in deduped if d["symbol"] == "NATGAS")
    wti = next(d for d in deduped if d["symbol"] == "WTI")
    assert natgas["count"] == 3
    assert natgas["first_seen_at"] == "t1"
    assert natgas["last_seen_at"] == "t3"
    assert wti["count"] == 1


def test_dedupe_execution_outcomes_handles_empty():
    from trading_bot.openai_decision import _dedupe_execution_outcomes
    assert _dedupe_execution_outcomes([]) == []
    assert _dedupe_execution_outcomes(None) == []


def test_compress_headlines_byte_cap_enforced():
    """Per review 2026-05-02-adhoc-1 #2: stop including headlines once
    cumulative title+snippet bytes exceed max_total_bytes."""
    items = [
        _headline(title="A" * 100, snippet="B" * 100, age_hours=1.0),
        _headline(title="C" * 100, snippet="D" * 100, age_hours=1.0),
        _headline(title="E" * 100, snippet="F" * 100, age_hours=1.0),
    ]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=24,
        max_items=10,
        dedupe_enabled=False,
        max_title_chars=200,
        max_snippet_chars=200,
        max_total_bytes=300,  # ~ 1.5 items worth
    )
    # First item (200 bytes) fits, second (200 bytes) takes us to 400 > 300 cap → break
    assert len(out) == 1


def test_compress_headlines_byte_cap_disabled_when_zero():
    items = [_headline(title="A" * 200, snippet="B" * 200) for _ in range(5)]
    out = compress_headlines_for_prompt(
        items,
        max_age_hours=24,
        max_items=10,
        dedupe_enabled=False,
        max_title_chars=200,
        max_snippet_chars=200,
        max_total_bytes=0,  # disabled
    )
    assert len(out) == 5


def test_synthesise_news_tighten_adds_retry_directive(tmp_path):
    """Per review 2026-05-02-adhoc-1 #2: when called with tighten=True,
    the news synthesiser must append the explicit truncation directive
    AND lower the output cap. Both are required to reverse the
    previous "retry produces more output" regression."""
    settings = _settings(tmp_path, subagent_max_output_tokens_news=4000)
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["max_output_tokens"] = kwargs.get("max_output_tokens")
        return NewsSynthesisEnvelope()

    engine._run_structured_generation = fake_run

    # First — tighten=False (normal path)
    engine.synthesise_news(
        market_headlines=[], per_symbol_headlines={}, entity_news={},
        open_positions=[], regime_history=[], active_symbols=[],
        tighten=False,
    )
    normal_prompt = captured["system_prompt"]
    normal_max = captured["max_output_tokens"]

    # Second — tighten=True (retry path)
    engine.synthesise_news(
        market_headlines=[], per_symbol_headlines={}, entity_news={},
        open_positions=[], regime_history=[], active_symbols=[],
        tighten=True,
    )
    tight_prompt = captured["system_prompt"]
    tight_max = captured["max_output_tokens"]

    assert "RETRY DIRECTIVE" in tight_prompt
    assert "OUTPUT MUST BE LESS THAN" in tight_prompt
    assert "RETRY DIRECTIVE" not in normal_prompt
    # Tighten cap should be <= normal cap
    assert tight_max < normal_max
    assert tight_max <= 2000


def test_rate_limit_backoff_does_not_retry_other_exceptions(tmp_path, monkeypatch):
    """Non-rate-limit errors (RuntimeError, ValueError, etc.) must
    surface immediately — no point retrying a code bug."""
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.openai_decision.time.sleep", sleeps.append)

    def boom():
        raise RuntimeError("structural error in payload")

    with pytest.raises(RuntimeError, match="structural error"):
        engine._call_with_rate_limit_backoff(boom, label="test")
    assert sleeps == []


# --- News-synth JSON resilience (review 2026-05-02-news-synth-json) ----------


def test_parse_json_lenient_succeeds_on_valid_json():
    from trading_bot.openai_decision import parse_json_lenient

    out = parse_json_lenient('{"a": 1, "b": "hello"}')
    assert out == {"a": 1, "b": "hello"}


def test_parse_json_lenient_repairs_missing_comma():
    """The exact pattern observed in production:
    line N column 2 'Expecting , delimiter' between two sibling fields."""
    from trading_bot.openai_decision import parse_json_lenient

    bad = '{\n  "themes": [],\n  "per_symbol_summaries": {}\n  "regime_implications": "ok"\n}'
    out = parse_json_lenient(bad)
    assert out["themes"] == []
    assert out["regime_implications"] == "ok"


def test_parse_json_lenient_repairs_trailing_comma():
    from trading_bot.openai_decision import parse_json_lenient

    bad = '{"a": 1, "b": [1, 2, 3,], "c": null,}'
    out = parse_json_lenient(bad)
    assert out["a"] == 1
    assert out["b"] == [1, 2, 3]


def test_parse_json_lenient_unwraps_markdown_fence():
    """LLMs sometimes ignore 'no markdown fences' and wrap output anyway."""
    from trading_bot.openai_decision import parse_json_lenient

    fenced = '```json\n{"x": 42}\n```'
    out = parse_json_lenient(fenced)
    assert out["x"] == 42


def test_parse_json_lenient_raises_on_pure_garbage():
    from trading_bot.openai_decision import parse_json_lenient

    with pytest.raises((json.JSONDecodeError, ValueError)):
        parse_json_lenient("totally not json at all $$$ #@!")


def test_parse_json_lenient_raises_on_empty_string():
    from trading_bot.openai_decision import parse_json_lenient

    with pytest.raises(json.JSONDecodeError):
        parse_json_lenient("")


def test_synthesise_news_propagates_model_override(tmp_path):
    """When ``news_synthesis_model`` is set, that model name reaches
    ``_run_structured_generation`` instead of openai_model."""
    settings = _settings(
        tmp_path,
        openai_model="default-model",
        news_synthesis_model="claude-3.5-haiku",
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.update(kwargs)
        return NewsSynthesisEnvelope()

    engine._run_structured_generation = fake_run
    engine.synthesise_news(
        market_headlines=[],
        per_symbol_headlines={},
        entity_news={},
        open_positions=[],
        regime_history=[],
        active_symbols=[],
    )
    assert captured.get("model_override") == "claude-3.5-haiku"


def test_synthesise_news_no_override_when_unset(tmp_path):
    settings = _settings(
        tmp_path,
        openai_model="default-model",
    )
    assert settings.news_synthesis_model is None
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured.update(kwargs)
        return NewsSynthesisEnvelope()

    engine._run_structured_generation = fake_run
    engine.synthesise_news(
        market_headlines=[],
        per_symbol_headlines={},
        entity_news={},
        open_positions=[],
        regime_history=[],
        active_symbols=[],
    )
    assert captured.get("model_override") is None


def test_news_synth_prompt_is_concise(tmp_path):
    """Pin the prompt brevity directives so a future edit can't quietly
    revert to '2-4 paragraph narrative' (the verbose phrasing that
    produced ~3500-token outputs and consistent JSON malformation)."""
    settings = _settings(tmp_path)
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    captured: dict = {}

    def fake_run(*, schema_model, schema_name, system_prompt, payload, **kwargs):
        captured["system_prompt"] = system_prompt
        return NewsSynthesisEnvelope()

    engine._run_structured_generation = fake_run
    engine.synthesise_news(
        market_headlines=[],
        per_symbol_headlines={},
        entity_news={},
        open_positions=[],
        regime_history=[],
        active_symbols=[],
    )
    prompt = captured["system_prompt"].lower()
    # Brevity directive
    assert "concise" in prompt or "brief" in prompt
    # Sentence-budget hints (replacing the old "2-4 paragraph narrative")
    assert "2-3 sentence" in prompt or "1-3 sentence" in prompt or "1-2 sentence" in prompt
    # JSON discipline reminder added in the same review
    assert "comma" in prompt
    # The old verbose phrasing must not have crept back
    assert "2-4 paragraph" not in prompt
    assert "3-5 sentence" not in prompt


def test_openrouter_chat_kwargs_shape_nvidia_nim(tmp_path, monkeypatch):
    """Pin the post-2026-05-20 NVIDIA NIM kwargs shape so a future
    revert to the OpenRouter Fireworks provider-pin or to
    response_format=json_object can't quietly slip through.

    The migration:
    - Dropped `response_format={"type":"json_object"}` (NVIDIA NIM's
      Kimi K2.6 chat template suppresses thinking when json_object is
      active; structured output is still produced via in-prompt schema
      + lenient parser).
    - Replaced `extra_body.provider` (OpenRouter sub-provider pin) with
      `extra_body.chat_template_kwargs={"thinking": True}` to enable
      Kimi K2.6 max-reasoning on NVIDIA NIM.
    """
    from trading_bot.models import DecisionEnvelope

    settings = _settings(tmp_path, openai_api_key="test")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        llm_provider="openrouter",
        openrouter_api_key="test",
        openai_model="moonshotai/kimi-k2.6",
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    engine.client = MagicMock()

    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        fake = MagicMock()
        fake.choices = [MagicMock(message=MagicMock(content='{"market_regime":"neutral-wait","summary":"x","trades":[]}'))]
        return fake

    engine.client.chat.completions.create = fake_create

    result = engine._run_structured_generation(
        schema_model=DecisionEnvelope,
        schema_name="decision_envelope",
        system_prompt="test",
        payload={},
    )
    assert isinstance(result, DecisionEnvelope)

    # Shape assertions: NVIDIA NIM thinking-mode payload
    assert "response_format" not in captured, (
        "response_format must NOT be set on the structured chat path "
        "after the 2026-05-20 NVIDIA NIM migration — it suppresses "
        "Kimi K2.6 thinking mode."
    )
    extra_body = captured.get("extra_body") or {}
    assert extra_body.get("chat_template_kwargs") == {"thinking": True}, (
        f"extra_body.chat_template_kwargs must enable thinking, got: {extra_body!r}"
    )
    assert "provider" not in extra_body, (
        "extra_body.provider (OpenRouter sub-provider pin) must not be "
        "present after the NVIDIA NIM migration. Got: "
        f"{extra_body!r}"
    )


def test_llm_default_headers_gated_on_openrouter_domain(tmp_path):
    """Pin the post-2026-05-20 header-gating: OpenRouter-branded
    headers (HTTP-Referer, X-OpenRouter-Title) are only attached
    when base_url actually points at openrouter.ai, not when the
    bot has been pointed at NVIDIA NIM / another OpenAI-compatible
    endpoint via the OPENROUTER_BASE_URL env override."""
    nvidia_settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        llm_provider="openrouter",
        openrouter_api_key="test",
        openrouter_base_url="https://integrate.api.nvidia.com/v1",
        openrouter_site_url="https://example.com",
        openrouter_app_title="X",
        openai_model="moonshotai/kimi-k2.6",
    )
    assert nvidia_settings.llm_default_headers == {}, (
        "No OpenRouter-branded headers should be sent when base_url "
        "is NVIDIA NIM"
    )

    or_settings = nvidia_settings.model_copy(
        update={"openrouter_base_url": "https://openrouter.ai/api/v1"}
    )
    headers = or_settings.llm_default_headers
    assert headers.get("HTTP-Referer") == "https://example.com"
    assert headers.get("X-OpenRouter-Title") == "X"


def test_openrouter_path_uses_lenient_parser(tmp_path, monkeypatch):
    """Integration: a malformed-but-repairable JSON response from the
    OpenRouter chat-completions path must be parsed successfully."""
    from trading_bot.models import DecisionEnvelope

    settings = _settings(tmp_path, openai_api_key="test")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    settings = Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        llm_provider="openrouter",
        openrouter_api_key="test",
        openai_model="gpt-test",
    )
    engine = OpenAIDecisionEngine.__new__(OpenAIDecisionEngine)
    engine.settings = settings
    engine.client = MagicMock()

    # Malformed JSON: missing comma before "trades" — repairable
    malformed = (
        '{\n'
        '  "market_regime": "neutral-wait",\n'
        '  "summary": "test"\n'
        '  "trades": []\n'
        '}'
    )
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=malformed))]
    engine.client.chat.completions.create = MagicMock(return_value=fake_response)

    result = engine._run_structured_generation(
        schema_model=DecisionEnvelope,
        schema_name="decision_envelope",
        system_prompt="test",
        payload={},
    )
    # If the repair pass works, we get a valid DecisionEnvelope; if not,
    # this raises a JSONDecodeError or ValidationError.
    assert isinstance(result, DecisionEnvelope)
    assert result.market_regime == "neutral-wait"
    assert result.summary == "test"
