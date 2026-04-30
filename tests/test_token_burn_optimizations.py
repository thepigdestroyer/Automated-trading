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
