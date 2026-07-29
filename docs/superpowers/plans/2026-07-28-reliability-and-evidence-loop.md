# Bot Reliability + Evidence-Loop Acceleration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five confirmed correctness/reliability bugs that corrupt or destabilise every decision, then accelerate the forward-walk evidence loop from months to days so the strategy's edge can be proven or disproven quickly.

**Architecture:** Three independent workstreams. (A) *Data integrity* — stop silently mutilating the price index, and make degraded inputs a first-class, decision-blocking signal rather than an invisible failure. (B) *Reliability* — bound calendar memory, filter structurally untradeable instruments before the LLM sees them, and make the LLM JSON path deterministic-with-fallback. (C) *Evidence throughput* — refactor the daily-trend rule into a vectorised frame that serves both live and backtest (so the two can never drift), then run Track-A over the full universe and add a backtester that produces a provisional verdict in hours instead of quarters.

**Tech Stack:** Python 3.11+, pandas, numpy, pydantic v2 (`StrictModel`), pytest, yfinance, OpenAI-compatible client against NVIDIA NIM.

## Global Constraints

- **Paper mode is not to be disabled.** `DRY_RUN=true` stays true for the entire plan. No task may enable live order placement.
- **No edits to `trading_bot/ig_bridge.py` or `trading_bot/session_manager.py`** (broker code) except **Task 4**, which the operator has explicitly authorised to add one **read-only, cached** accessor to `ig_bridge.py`. That accessor must not alter any existing method, must not submit or modify orders, and must degrade to `None` on any error so a failed lookup can never block trading.
- **Never commit `.env`.** Mask all API keys in logs, commit messages, and test fixtures.
- **Existing live-risk defaults must not change:** `risk_per_trade_pct=0.025`, `risk_confidence_scaling_max=1.0`, `min_model_confidence=0.72`, `atr_stop_multiplier`, `stop_loss_pct_floor`, `risk_reward_ratio=2.0`. Any new gate may only *reject* trades, never upsize or loosen.
- **`allow_shorts` (live) stays `False`.** Task 10 adds a separate paper-only short permission.
- **TDD:** every task writes a failing test first, verifies the failure, implements minimally, verifies pass, commits.
- **Test isolation:** all tests must construct `Settings` with `_env_file=None` or rely on the `_isolate_env` autouse fixture in `tests/conftest.py`. Deployment `.env` values must never leak into assertions about defaults.
- **Run the full suite before each commit:** `.venv/bin/pytest -q`.
- **Test baseline (macOS dev machine): `596 passed, 8 failed`.** Those 8 failures pre-date this plan and are platform artifacts, not regressions: `tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status` (reads Linux-only `/proc/self/status` via `bot.py:389`), plus 5 in `tests/test_watcher_heartbeat_journal.py` and 2 in `tests/test_events_rotation.py` (fixture uses `/bin/true`, which on macOS is `/usr/bin/true`). No task in this plan touches watcher or memory-pressure code. **Any task that produces a 9th failure has caused a regression — fix it, do not rebase the baseline.** Do not "fix" these 8 as part of this plan; they pass on the Linux deployment target.
- **The dev machine has no `trading-bot` SSH alias and no IG credentials.** Every step is local-only: implement, test, commit. Deployment and any live-API probe is operator-run.
- **Commit message convention:** `type(scope): summary` — matching existing history (`feat(eval):`, `fix(macro):`, `test(risk):`, `docs:`).

---

## File Structure

**Created:**
- `trading_bot/backtest.py` — vectorised historical simulator for the mechanical (Track-A) rule set. Owns: bar-by-bar trade generation, cost application, stat aggregation. Depends on `daily_trend`, `forward_eval`, `config`, `instruments`.
- `tests/test_backtest.py`, `tests/test_data_quality.py`, `tests/test_degraded_inputs.py`, `tests/test_tradeability_filter.py`, `tests/test_daily_trend_frame.py`, `tests/test_track_a_universe.py`
- `docs/decisions/0006-mark-dont-drop-outlier-bars.md`
- `docs/decisions/0007-shared-trend-frame-live-and-backtest.md`

**Modified:**
- `trading_bot/models.py` — add `DataQuality`; add `data_quality` field to `SymbolContext`.
- `trading_bot/market_data.py:46-98` — `_apply_sanity_checks` marks/imputes instead of dropping; returns a report.
- `trading_bot/daily_trend.py:60-133` — extract vectorised `compute_trend_frame`; `build_daily_trend` becomes its last row.
- `trading_bot/calendars.py:156-187` — bound `exchange_calendars` construction to a rolling date window.
- `trading_bot/config.py` — new settings (all defaulting to current behaviour or safe-off).
- `trading_bot/risk.py` — new `DEGRADED_INPUTS_BLOCK` gate.
- `trading_bot/bot.py` — thread data quality into guardrail context; tradeability pre-filter; Track-A universe wiring.
- `trading_bot/track_a.py` — paper-only short permission.
- `trading_bot/openai_decision.py:1048-1084` — optional NIM guided JSON + deterministic fallback.

---

## Workstream A — Data Integrity

### Task 1: Mark-don't-drop outlier bars

Dropping outlier rows (`market_data.py:88`) removes timestamps from the index. Every downstream `rolling()`/`ewm()` then computes over a silently discontinuous series — ~2.4 bars/cycle on energy, 693 in 15 days. Fix: keep the row, neutralise the corrupt values by time-interpolation, and report it.

**Files:**
- Modify: `trading_bot/models.py` (add `DataQuality`, extend `SymbolContext`)
- Modify: `trading_bot/market_data.py:28-98`
- Test: `tests/test_data_quality.py`

**Interfaces:**
- Produces: `trading_bot.models.DataQuality(bars_total: int, bars_imputed: int, max_gap_pct: float | None, is_degraded: bool, note: str | None)`; `SymbolContext.data_quality: DataQuality | None`; `YahooMarketDataClient.fetch_history` sets `self._last_quality: DataQuality | None`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_quality.py`:

```python
"""Outlier bars must be neutralised in place, never dropped.

Dropping rows breaks index continuity, which silently corrupts every
rolling/EWM indicator computed downstream.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.config import Settings
from trading_bot.market_data import YahooMarketDataClient


def _frame_with_spike() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(40)]
    close[20] = 900.0  # corrupted tick
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.001 for c in close],
            "Low": [c * 0.999 for c in close],
            "Close": close,
            "Volume": [1000.0] * 40,
        },
        index=idx,
    )


def _client() -> YahooMarketDataClient:
    return YahooMarketDataClient(Settings(_env_file=None))


def test_outlier_bar_is_not_dropped_index_stays_contiguous():
    client = _client()
    frame = _frame_with_spike()
    original_index = frame.index.copy()

    client._apply_sanity_checks(frame, "TEST")

    assert len(frame) == 40
    assert frame.index.equals(original_index)


def test_outlier_close_is_replaced_with_interpolated_value():
    client = _client()
    frame = _frame_with_spike()

    client._apply_sanity_checks(frame, "TEST")

    spiked = float(frame["Close"].iloc[20])
    assert spiked < 200.0
    assert 101.0 < spiked < 105.0


def test_quality_report_counts_imputed_bars():
    client = _client()
    frame = _frame_with_spike()

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_total == 40
    assert report.bars_imputed == 1
    assert report.is_degraded is True
    assert report.max_gap_pct is not None and report.max_gap_pct > 100.0


def test_clean_frame_reports_no_degradation():
    client = _client()
    idx = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(40)]
    frame = pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.001 for c in close],
            "Low": [c * 0.999 for c in close],
            "Close": close,
            "Volume": [1000.0] * 40,
        },
        index=idx,
    )

    report = client._apply_sanity_checks(frame, "TEST")

    assert report.bars_imputed == 0
    assert report.is_degraded is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_data_quality.py -v`
Expected: FAIL — `AttributeError`/`TypeError` because `_apply_sanity_checks` returns `None`, and `test_outlier_bar_is_not_dropped_index_stays_contiguous` fails with `len(frame) == 39`.

- [ ] **Step 3: Add the `DataQuality` model**

In `trading_bot/models.py`, add above `class SymbolContext`:

```python
class DataQuality(StrictModel):
    """Per-symbol price-feed integrity report for one fetch.

    Outlier bars are neutralised in place (time-interpolated) rather than
    dropped, so the bar index stays contiguous and rolling/EWM indicators
    remain valid. `is_degraded` is the signal the executor and the LLM see.
    """

    bars_total: int
    bars_imputed: int = 0
    max_gap_pct: float | None = None
    is_degraded: bool = False
    note: str | None = None
```

Then add this field to `SymbolContext`:

```python
    data_quality: DataQuality | None = None
```

- [ ] **Step 4: Rewrite `_apply_sanity_checks` to impute and report**

In `trading_bot/market_data.py`, add `import numpy as np` beside the pandas import, add `DataQuality` to the `trading_bot.models` import, and replace lines 46-98 with:

```python
    def _apply_sanity_checks(self, history: pd.DataFrame, data_symbol: str) -> DataQuality:
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(history.columns)
        if missing:
            raise ValueError(f"{data_symbol}: missing columns {sorted(missing)}")

        bars_total = int(history.shape[0])
        bars_imputed = 0
        max_gap_pct: float | None = None

        close = history["Close"].astype(float)
        returns = close.pct_change().abs().dropna()
        if not returns.empty:
            median_abs = float(returns.median())
            # 15% floor handles futures rollover gaps; 10x median catches truly
            # corrupted ticks. Energy needs a wider floor (large normal gaps).
            floor_pct = 0.30 if data_symbol in {"CL=F", "BZ=F", "NG=F"} else 0.15
            extreme_threshold = max(median_abs * 10.0, floor_pct)
            bad_rows = returns[returns > extreme_threshold].index
            if len(bad_rows) > 0:
                import logging

                bars_imputed = int(len(bad_rows))
                max_gap_pct = round(float(returns.loc[bad_rows].max()) * 100.0, 2)
                logging.getLogger(__name__).warning(
                    "%s: imputing %d outlier bar(s) with gap > %.1f%% "
                    "(index preserved)",
                    data_symbol, bars_imputed, extreme_threshold * 100,
                )
                if self._event_logger is not None:
                    try:
                        last_good_ts = (
                            history.index[-1].isoformat()
                            if len(history.index) > 0 else None
                        )
                        self._event_logger.log(
                            "data_quality",
                            {
                                "symbol": data_symbol,
                                "bars_imputed": bars_imputed,
                                "threshold_pct": round(extreme_threshold * 100.0, 2),
                                "gap_pct_max": max_gap_pct,
                                "last_good_ts": last_good_ts,
                            },
                        )
                    except Exception:
                        pass
                # Mark-don't-drop: blank the corrupt OHLC then interpolate on
                # the time axis. This preserves index continuity so rolling /
                # EWM windows stay aligned to real elapsed time.
                ohlc = ["Open", "High", "Low", "Close"]
                history.loc[bad_rows, ohlc] = np.nan
                history[ohlc] = (
                    history[ohlc]
                    .interpolate(method="time", limit_direction="both")
                    .ffill()
                    .bfill()
                )

        # Forex tickers (e.g. JPY=X, EURUSD=X) have no centralised volume.
        is_forex = data_symbol.endswith("=X")
        volume = history["Volume"].astype(float)
        if not is_forex and (volume > 0).sum() == 0:
            raise ValueError(f"{data_symbol}: all rows have zero volume")

        non_nan = history.dropna(how="any").shape[0]
        if non_nan < int(history.shape[0] * 0.8):
            raise ValueError(f"{data_symbol}: >20% NaN rows after fetch")

        degraded_ratio = (bars_imputed / bars_total) if bars_total else 0.0
        return DataQuality(
            bars_total=bars_total,
            bars_imputed=bars_imputed,
            max_gap_pct=max_gap_pct,
            is_degraded=bars_imputed > 0,
            note=(
                f"{bars_imputed}/{bars_total} bars imputed "
                f"({degraded_ratio * 100:.1f}%)"
                if bars_imputed else None
            ),
        )
```

- [ ] **Step 5: Store and attach the report**

In `trading_bot/market_data.py`, in `__init__` add `self._last_quality: DataQuality | None = None`. In `fetch_history`, change line 43 to:

```python
        self._last_quality = self._apply_sanity_checks(history, data_symbol)
```

In `build_symbol_context`, capture it right after `history = self.fetch_history(...)`:

```python
        data_quality = self._last_quality
```

and pass `data_quality=data_quality` into the `SymbolContext(...)` constructor.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_data_quality.py tests/test_market_data.py -v`
Expected: PASS (all four new tests plus the existing market-data suite).

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 8: Write the ADR**

Create `docs/decisions/0006-mark-dont-drop-outlier-bars.md`:

```markdown
# 0006 — Mark, don't drop, outlier bars

Date: 2026-07-28
Status: accepted

## Context

`YahooMarketDataClient._apply_sanity_checks` dropped bars whose absolute
return exceeded max(10x median, 15%/30% floor). Roughly 2.4 bars per cycle
on energy symbols (693 in a 15-day window) were removed. Dropping a row
removes its timestamp from the index, so every subsequent `rolling()` and
`ewm()` computed over a discontinuous series while still being labelled as
an N-period window. Indicators were therefore subtly wrong on exactly the
symbols the bot traded most.

## Decision

Keep every bar. Blank the corrupt OHLC values and time-interpolate them,
preserving index continuity. Emit a `DataQuality` report per fetch
(`bars_total`, `bars_imputed`, `max_gap_pct`, `is_degraded`) and attach it
to `SymbolContext`.

Imputation is not "correct" — a genuine rollover gap is real price movement
and interpolating it understates the move. But the filter only fires on
ticks already classified as corrupt, and a contiguous index with one
interpolated bar is strictly less wrong than a silently gapped index that
misaligns every window.

## Consequences

- Rolling/EWM windows are time-aligned again.
- Degradation becomes visible to the executor via the Task 2 degraded-inputs gate.
- `bars_dropped` is renamed `bars_imputed` in `data_quality` events; the
  supervisor's `data_feed_stale` rule keys on `last_good_ts` and is unaffected.
```

- [ ] **Step 9: Commit**

```bash
git add trading_bot/models.py trading_bot/market_data.py tests/test_data_quality.py docs/decisions/0006-mark-dont-drop-outlier-bars.md
git commit -m "fix(data): mark-don't-drop outlier bars to preserve index continuity"
```

---

### Task 2: `degraded_inputs` gate

Right now a failed feed produces a silent hole and the bot trades at full confidence. This makes degradation explicit and blocks *new* entries unless conviction is unusually high. It never blocks exits.

**Files:**
- Modify: `trading_bot/config.py`
- Modify: `trading_bot/risk.py`
- Modify: `trading_bot/bot.py`
- Test: `tests/test_degraded_inputs.py`

**Interfaces:**
- Consumes: `SymbolContext.data_quality` from Task 1. **As built, `DataQuality` is `(bars_total, bars_imputed, bars_dropped_empty, bars_volume_filled, max_gap_pct, is_degraded, note)` plus an `imputed_ratio` property.** Gate on `is_degraded` (it already folds in outlier imputation, pre-existing NaN fills, and dropped wholly-empty bars). If you need a magnitude, use `imputed_ratio` — never raw `bars_imputed / bars_total`, which is a `ZeroDivisionError` on the fallback context where `bars_total == 0`.
- Produces: `Settings.degraded_inputs_min_confidence: float`; rejection reason string `DEGRADED_INPUTS_BLOCK`; `guardrail_context["degraded_inputs"]: list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_degraded_inputs.py`:

```python
"""Degraded price inputs must raise the bar for NEW entries, never block exits."""

from __future__ import annotations

from trading_bot.config import Settings
from trading_bot.models import DataQuality


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_default_degraded_confidence_floor_is_above_normal_floor():
    s = _settings()
    assert s.degraded_inputs_min_confidence > s.min_model_confidence


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_degraded_inputs.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'degraded_inputs_min_confidence'` and `ImportError: cannot import name 'degraded_input_reasons'`.

- [ ] **Step 3: Add the setting**

In `trading_bot/config.py`, directly below `min_model_confidence: float = 0.72` add:

```python
    # When any input feed for a symbol is degraded (imputed bars, failed
    # macro/news fetch), a NEW entry needs this much conviction instead of
    # `min_model_confidence`. Exits and closes are never gated on this.
    degraded_inputs_min_confidence: float = 0.85
```

- [ ] **Step 4: Add the helper to `risk.py`**

In `trading_bot/risk.py`, add near the other module-level helpers (below `_round_trip_cost_price_units`):

```python
def degraded_input_reasons(
    quality_by_symbol: Mapping[str, "DataQuality | None"],
) -> list[str]:
    """Human-readable degradation reasons, one per affected symbol.

    A missing report means "not measured", which is NOT the same as degraded —
    we do not want an unrelated plumbing gap to freeze all trading.
    """
    reasons: list[str] = []
    for symbol, quality in sorted(quality_by_symbol.items()):
        if quality is None or not quality.is_degraded:
            continue
        reasons.append(
            f"{symbol}:{quality.bars_imputed}/{quality.bars_total} bars imputed"
        )
    return reasons
```

Add `from typing import Mapping` (or extend the existing typing import) and import `DataQuality` from `trading_bot.models` under `TYPE_CHECKING` if the module avoids runtime model imports; otherwise import it directly alongside the other model imports.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_degraded_inputs.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Enforce the gate in `build_execution_plans`**

In `trading_bot/risk.py`, inside `build_execution_plans`. The loop variables are `trade` (with `trade.confidence`), `symbol`, `action`, and `context` (assigned at line 158). Rejections in this method are `LOGGER.info(...)` followed by `continue` — there is **no** `rejected` list and no `_rejection` helper; do not invent one.

Insert immediately after the invalid-price check (which currently ends at line 162 with `continue`), so `context` is in scope and the gate runs before any sizing work:

```python
            # Degraded inputs (Task 1 DataQuality): require higher conviction
            # for NEW entries. CLOSE is never gated here — we must always be
            # able to exit an open position.
            if action in {"BUY", "SELL"}:
                quality = context.data_quality
                if quality is not None and quality.is_degraded:
                    degraded_floor = float(
                        self.settings.degraded_inputs_min_confidence
                    )
                    if trade.confidence < degraded_floor:
                        LOGGER.info(
                            "Rejected %s %s: DEGRADED_INPUTS_BLOCK confidence "
                            "%.2f below degraded floor %.2f (%s)",
                            action, symbol, trade.confidence, degraded_floor,
                            quality.note,
                        )
                        continue
```

- [ ] **Step 7: Add a gate test**

Append to `tests/test_degraded_inputs.py` a test that builds a `SymbolContext` with `data_quality=DataQuality(bars_total=100, bars_imputed=5, is_degraded=True)` and a BUY plan at `confidence=0.75`, calls `RiskManager.build_execution_plans`, and asserts the plan is rejected with `DEGRADED_INPUTS_BLOCK` in the reason. Model the fixture setup on the existing `tests/test_risk_gates.py` helpers — reuse them rather than writing new scaffolding.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add trading_bot/config.py trading_bot/risk.py tests/test_degraded_inputs.py
git commit -m "feat(risk): degraded-inputs confidence gate for new entries"
```

---

## Workstream B — Reliability

### Task 3: Bound `exchange_calendars` memory

`HolidayCalendar._get` calls `xcals.get_calendar(code)` which materialises a full schedule (1678→2100) per exchange, ~119 MB total, driving the 400 MB RSS soft-restarts every ~10 cycles.

**Files:**
- Modify: `trading_bot/calendars.py:156-187`
- Test: `tests/test_calendars.py` (extend)

**Interfaces:**
- Produces: `HolidayCalendar(window_days: int = 90)`; internal daily window refresh.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calendars.py`:

```python
def test_holiday_calendar_requests_bounded_window(monkeypatch):
    """A full 1678-2100 schedule is ~119MB per exchange. We must request a
    narrow window instead."""
    from trading_bot import calendars as cal_mod

    captured: dict[str, object] = {}

    class _FakeCal:
        def is_trading_minute(self, ts):
            return True

    def _fake_get_calendar(code, **kwargs):
        captured["code"] = code
        captured["kwargs"] = kwargs
        return _FakeCal()

    monkeypatch.setattr(cal_mod.xcals, "get_calendar", _fake_get_calendar)

    hc = cal_mod.HolidayCalendar(window_days=90)
    hc._get("XLON")

    assert "start" in captured["kwargs"]
    assert "end" in captured["kwargs"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_calendars.py::test_holiday_calendar_requests_bounded_window -v`
Expected: FAIL — `KeyError: 'start'` (current code passes no kwargs), or `TypeError` on `window_days`.

- [ ] **Step 3: Implement the bounded window**

Replace `HolidayCalendar.__init__` and `_get` in `trading_bot/calendars.py`:

```python
class HolidayCalendar:
    """Session calendar with a bounded date window.

    `xcals.get_calendar(code)` builds the full 1678-2100 schedule (~119 MB
    across our exchanges) which dominated process RSS and drove memory
    soft-restarts. We only ever ask "is the session open now?", so a rolling
    +/- `window_days` window is sufficient. The window is rebuilt lazily once
    per UTC day.
    """

    def __init__(self, window_days: int = 90):
        if xcals is None:
            self._available = False
            return
        self._available = True
        self._window_days = int(window_days)
        self._cache: dict[str, object] = {}
        self._window_day: str | None = None

    def _window_bounds(self, now: datetime) -> tuple[str, str]:
        start = (now - timedelta(days=self._window_days)).date().isoformat()
        end = (now + timedelta(days=self._window_days)).date().isoformat()
        return start, end

    def _maybe_roll_window(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._window_day != today:
            self._cache.clear()
            self._window_day = today

    def is_session_open(self, exchange_code: str, ts: datetime) -> bool:
        if not self._available:
            return True
        self._maybe_roll_window(ts)
        cal = self._get(exchange_code, now=ts)
        if cal is None:
            return True
        try:
            return bool(cal.is_trading_minute(pd.Timestamp(ts)))
        except Exception:
            return True

    def is_half_day(self, exchange_code: str, ts: datetime) -> bool:
        # conservative default: never report half-day unless a real-calendar
        # signal clearly shows it
        return False

    def _get(self, exchange_code: str, now: datetime | None = None):
        if exchange_code in self._cache:
            return self._cache[exchange_code]
        now = now or datetime.now(timezone.utc)
        start, end = self._window_bounds(now)
        try:
            cal = xcals.get_calendar(exchange_code, start=start, end=end)
        except Exception:
            try:
                cal = xcals.get_calendar(exchange_code)
            except Exception:
                cal = None
        self._cache[exchange_code] = cal
        return cal
```

Ensure `timedelta` and `timezone` are imported at the top of `calendars.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_calendars.py -v`
Expected: PASS.

- [ ] **Step 5: Verify real memory improvement**

Run:

```bash
.venv/bin/python -c "
import tracemalloc, datetime as dt
from trading_bot.calendars import HolidayCalendar
tracemalloc.start()
hc = HolidayCalendar(window_days=90)
for code in ['XLON','XNYS','XNAS']:
    hc.is_session_open(code, dt.datetime.now(dt.timezone.utc))
cur, peak = tracemalloc.get_traced_memory()
print(f'peak={peak/1e6:.1f}MB')
"
```

Expected: peak well under 40 MB (previously ~119 MB for the full schedules). Record the number in the commit message.

- [ ] **Step 6: Commit**

```bash
git add trading_bot/calendars.py tests/test_calendars.py
git commit -m "perf(calendars): bound exchange_calendars to a rolling window"
```

---

### Task 4: Pre-LLM tradeability filter

BRENT's `minDealSize=1.0` inflates a £0.0275 position 36×, so it can never fill below ~£35k equity. Today the bot discovers this only at submit time, after spending a candidate slot and LLM tokens, then re-proposes it. Filter it out during symbol selection.

**Files:**
- Modify: `trading_bot/config.py`
- Modify: `trading_bot/ig_bridge.py` (add ONE read-only cached accessor — operator-authorised; see Global Constraints)
- Modify: `trading_bot/bot.py:1012-1043` (the per-symbol filter loop inside `_select_active_symbols`)
- Test: `tests/test_tradeability_filter.py`

**Interfaces:**
- Produces: `Settings.tradeability_filter_enabled: bool = True`; `Settings.market_constraints_cache_ttl_minutes: int = 60`; `IGBridge.get_market_constraints(symbol: str) -> dict[str, float] | None` returning `{"min_deal_size": float, "price": float}` or `None`; module-level `is_structurally_tradeable(*, settings, equity, min_deal_size, price) -> bool` in `bot.py`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tradeability_filter.py`:

```python
"""Instruments whose broker minimum deal size structurally exceeds the risk
budget must be filtered out BEFORE the LLM sees them."""

from __future__ import annotations

import pytest

from trading_bot.config import Settings


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_tradeability_filter_enabled_by_default():
    assert _settings().tradeability_filter_enabled is True


@pytest.mark.parametrize(
    "equity,min_deal_size,price,expected",
    [
        (6976.0, 1.0, 99.3, False),   # BRENT at current equity: untradeable
        (50000.0, 1.0, 99.3, True),   # same instrument, larger account
        (6976.0, 0.01, 99.3, True),   # WTI-like min size: fine
        (6976.0, None, 99.3, True),   # unknown min size: do not filter
    ],
)
def test_structural_tradeability(equity, min_deal_size, price, expected):
    from trading_bot.bot import is_structurally_tradeable

    s = _settings()
    assert (
        is_structurally_tradeable(
            settings=s,
            equity=equity,
            min_deal_size=min_deal_size,
            price=price,
        )
        is expected
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tradeability_filter.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_structurally_tradeable'`.

- [ ] **Step 3: Add the setting**

In `trading_bot/config.py`, below `max_discovered_symbols`:

```python
    # Drop candidates whose broker minimum deal size would force a position
    # larger than the per-trade risk budget can support. Prevents burning
    # candidate slots + LLM tokens on structurally unfillable instruments
    # (e.g. BRENT minDealSize=1.0 below ~GBP 35k equity).
    tradeability_filter_enabled: bool = True

    # TTL for the cached /markets/{epic} constraint lookup used by the
    # tradeability filter. Dealing rules are near-static; the price is only
    # needed for an order-of-magnitude notional check, so a long TTL is fine
    # and keeps one extra IG call per symbol per hour, not per cycle.
    market_constraints_cache_ttl_minutes: int = 60
```

- [ ] **Step 4: Add the read-only cached accessor to `ig_bridge.py`**

This is the one operator-authorised broker edit. Add a new method to `IGBridge` — do **not** modify any existing method. It reuses the same `/markets/{epic}` call that `_build_open_payload` already makes at line 559, which returns both `dealingRules.minDealSize` and the `snapshot` bid/offer.

```python
    def get_market_constraints(self, symbol: str) -> dict[str, float] | None:
        """Read-only: cached minDealSize + mid price for pre-trade filtering.

        Used by the tradeability filter to drop candidates the broker's
        minimum deal size makes structurally unfillable at current equity.
        Never raises and never mutates order state: any failure returns None,
        which callers must treat as "unknown, do not filter".
        """
        symbol = symbol.upper()
        ttl = float(self.settings.market_constraints_cache_ttl_minutes) * 60.0
        now = time.time()
        cached = self._market_constraints_cache.get(symbol)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        try:
            epic = self._resolve_epic(symbol)
            market = self._request("GET", f"/markets/{epic}", version="3")
            dealing_rules = market.get("dealingRules", {}) or {}
            snapshot = market.get("snapshot", {}) or {}
            min_deal_size = _as_float(
                dealing_rules.get("minDealSize", {}).get("value")
            )
            bid = _as_float(snapshot.get("bid"))
            offer = _as_float(snapshot.get("offer"))
            mid = (bid + offer) / 2.0 if bid > 0 and offer > 0 else max(bid, offer)
            if min_deal_size <= 0 or mid <= 0:
                return None
            result = {"min_deal_size": float(min_deal_size), "price": float(mid)}
        except Exception as exc:
            LOGGER.debug("get_market_constraints failed for %s: %s", symbol, exc)
            return None
        self._market_constraints_cache[symbol] = (now, result)
        return result
```

In `IGBridge.__init__`, add `self._market_constraints_cache: dict[str, tuple[float, dict[str, float]]] = {}`. Confirm `time` is imported at the top of `ig_bridge.py`; add `import time` if absent.

- [ ] **Step 5: Implement the predicate**

Add to `trading_bot/bot.py` at module level (above `class TradingBot`):

```python
def is_structurally_tradeable(
    *,
    settings: Settings,
    equity: float,
    min_deal_size: float | None,
    price: float,
) -> bool:
    """False when the broker minimum deal size cannot fit the risk budget.

    Unknown min size (None) returns True: absence of data must not silently
    shrink the tradeable universe.
    """
    if not settings.tradeability_filter_enabled:
        return True
    if min_deal_size is None or min_deal_size <= 0 or price <= 0 or equity <= 0:
        return True
    # Notional the broker will force us into at its minimum size.
    forced_notional = float(min_deal_size) * float(price)
    # Budget: the cash we are willing to put at risk on one trade, scaled by
    # the stop distance floor -> the largest notional whose stop-out loss
    # stays inside the budget.
    risk_budget = float(equity) * float(settings.risk_per_trade_pct)
    max_notional = risk_budget / max(float(settings.stop_loss_pct_floor), 1e-6)
    return forced_notional <= max_notional
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tradeability_filter.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Wire it into symbol selection**

In `TradingBot._select_active_symbols`, inside the existing per-symbol loop that begins `for symbol in candidate_symbols:` (`bot.py:1012`). Insert the check immediately **after** the `self._is_tradeable_symbol(symbol)` branch resolves — i.e. only run it for symbols that are otherwise going to be kept.

Critically, the open-position bypass already exists at `bot.py:1020-1022` (`if symbol in open_position_symbol_set: tradeable_symbols.append(symbol); continue`), so open positions are structurally exempt already — do not add a second bypass.

The filter must:

1. Do nothing when `self.settings.tradeability_filter_enabled` is False.
2. Call `getattr(self.broker, "get_market_constraints", None)`; if the attribute is missing or returns `None`, **keep** the symbol (unknown != untradeable). The paper broker has no such method — that path must stay working.
3. Use `account_snapshot.total_funds` as equity.
4. Log each exclusion at INFO with `symbol`, `min_deal_size`, `forced_notional`, `max_notional`.

Restructure lines 1037-1043 to:

```python
            if not self._is_tradeable_symbol(symbol):
                LOGGER.info(
                    "Skipping %s because the broker could not resolve it as tradeable",
                    symbol,
                )
                continue

            # Structural min-deal-size filter: drop candidates the broker's
            # minimum size makes unfillable at current equity, before they
            # consume a candidate slot and LLM tokens.
            if self.settings.tradeability_filter_enabled:
                fetch = getattr(self.broker, "get_market_constraints", None)
                constraints = fetch(symbol) if callable(fetch) else None
                if constraints is not None:
                    equity = float(account_snapshot.total_funds or 0.0)
                    if not is_structurally_tradeable(
                        settings=self.settings,
                        equity=equity,
                        min_deal_size=constraints["min_deal_size"],
                        price=constraints["price"],
                    ):
                        forced_notional = (
                            constraints["min_deal_size"] * constraints["price"]
                        )
                        max_notional = (
                            equity * self.settings.risk_per_trade_pct
                            / max(self.settings.stop_loss_pct_floor, 1e-6)
                        )
                        LOGGER.info(
                            "Skipping %s — STRUCTURAL_MIN_DEAL_SIZE: min_deal_size=%s "
                            "forced_notional=%.2f > max_notional=%.2f at equity %.2f",
                            symbol, constraints["min_deal_size"],
                            forced_notional, max_notional, equity,
                        )
                        continue

            tradeable_symbols.append(symbol)
```

- [ ] **Step 8: Add a wiring test**

Add a test to `tests/test_tradeability_filter.py` proving the paper-broker path is unaffected: build a `TradingBot` (or a minimal stub exposing `broker` without `get_market_constraints`), run `_select_active_symbols`, and assert candidate symbols survive. A broker lacking the accessor must never lose symbols.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: 8 failed (the macOS baseline), 0 new failures.

- [ ] **Step 10: Commit**

```bash
git add trading_bot/config.py trading_bot/ig_bridge.py trading_bot/bot.py tests/test_tradeability_filter.py
git commit -m "feat(universe): filter structurally untradeable instruments pre-LLM"
```

---

### Task 5: NIM guided JSON (flagged off) + deterministic exit fallback

Two separate problems. (a) JSON is scraped out of the thinking channel with `json_repair` salvage — 491 `subagent_failure` events. NIM supports `nvext.guided_json`, but the session proved `response_format=json_object` silently kills thinking mode, so guided decoding **must be proven empirically before being trusted**. Ship it behind a flag defaulting to **off**. (b) Regardless of (a), a failed `decide()` currently means no exit management that cycle — that is the dangerous part, and it gets a deterministic fallback.

**Files:**
- Modify: `trading_bot/config.py`
- Modify: `trading_bot/openai_decision.py:1048-1084`
- Test: `tests/test_llm_guided_json.py`

**Interfaces:**
- Produces: `Settings.llm_guided_json_enabled: bool = False`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_guided_json.py`:

```python
"""Guided JSON must be opt-in and must inject NIM's nvext.guided_json schema."""

from __future__ import annotations

from trading_bot.config import Settings


def test_guided_json_defaults_off():
    """NIM's json_object mode silently suppressed thinking; guided_json is
    unproven on this stack, so it must not be on by default."""
    assert Settings(_env_file=None).llm_guided_json_enabled is False


def test_guided_json_payload_includes_schema_when_enabled():
    from trading_bot.openai_decision import build_nvext_extra_body

    schema = {"type": "object", "properties": {"direction": {"type": "string"}}}
    body = build_nvext_extra_body(guided_json_enabled=True, schema=schema)

    assert body["chat_template_kwargs"] == {"thinking": True}
    assert body["nvext"]["guided_json"] == schema


def test_guided_json_absent_when_disabled():
    from trading_bot.openai_decision import build_nvext_extra_body

    body = build_nvext_extra_body(guided_json_enabled=False, schema={"a": 1})

    assert body == {"chat_template_kwargs": {"thinking": True}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_llm_guided_json.py -v`
Expected: FAIL — missing setting and `ImportError: cannot import name 'build_nvext_extra_body'`.

- [ ] **Step 3: Add the setting**

In `trading_bot/config.py`:

```python
    # NVIDIA NIM guided decoding (`nvext.guided_json`). OFF by default: the
    # 2026-05-20 migration proved `response_format=json_object` silently
    # suppresses Kimi K2.6's thinking channel, and guided_json may do the
    # same. Enable only after scripts/probe_guided_json.py shows thinking
    # tokens are still emitted.
    llm_guided_json_enabled: bool = False
```

- [ ] **Step 4: Extract the extra-body builder**

In `trading_bot/openai_decision.py`, add at module level:

```python
def build_nvext_extra_body(
    *, guided_json_enabled: bool, schema: dict | None
) -> dict:
    """Assemble NIM `extra_body`.

    `chat_template_kwargs.thinking` enables Kimi K2.6 max reasoning.
    `nvext.guided_json` constrains decoding to the schema — this removes the
    need for reasoning-channel scraping, but is gated because it may suppress
    thinking the way `response_format=json_object` did.
    """
    body: dict = {"chat_template_kwargs": {"thinking": True}}
    if guided_json_enabled and schema:
        body["nvext"] = {"guided_json": schema}
    return body
```

Then in `_run_openrouter_structured_chat`, replace the inline `extra_body={...}` (lines 1070-1072) with:

```python
            extra_body=build_nvext_extra_body(
                guided_json_enabled=self.settings.llm_guided_json_enabled,
                schema=schema_model.model_json_schema(),
            ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_llm_guided_json.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Write the probe script**

Create `scripts/probe_guided_json.py` — a standalone script the operator runs **on the OCI box** with real credentials. It must issue two identical `decide`-shaped calls, one with `llm_guided_json_enabled=False` and one with `True`, and print for each: `finish_reason`, `content_len`, `reasoning_content_len`, `reasoning_tokens`, and whether the payload parsed first-try without `json_repair`. Guided JSON is adopted only if reasoning length stays non-trivial **and** first-try parse rate improves.

- [ ] **Step 7: Commit**

```bash
git add trading_bot/config.py trading_bot/openai_decision.py tests/test_llm_guided_json.py scripts/probe_guided_json.py
git commit -m "feat(llm): optional NIM guided-json decoding behind a flag + probe script"
```

- [ ] **Step 8: Deterministic exit-management fallback**

Separate commit. When `decide()` raises or returns unparseable output, the cycle currently skips exit management entirely. Add a deterministic fallback that, for every **open** position, still applies the mechanical rules: honour stop/TP levels, enforce `default_max_holding_hours`, and emit CLOSE plans where breached — with no LLM involvement. Write the failing test first (`tests/test_decide_fallback.py`): assert that when the LLM client raises, `run_cycle` still produces a CLOSE plan for a position past its max holding time. Then implement, run `.venv/bin/pytest -q`, and commit:

```bash
git commit -m "fix(llm): deterministic exit management when decide() fails"
```

---

## Workstream C — Evidence Throughput

### Task 6: Vectorised shared trend frame

The backtester must exercise the *same* rule as live, or it proves nothing. Extract the per-bar computation into `compute_trend_frame`, and make `build_daily_trend` return its last row. Behaviour-preserving refactor — the existing `tests/test_daily_trend.py` is the regression gate.

**Files:**
- Modify: `trading_bot/daily_trend.py`
- Test: `tests/test_daily_trend_frame.py`

**Interfaces:**
- Produces: `compute_trend_frame(daily: pd.DataFrame) -> pd.DataFrame` with columns `direction`, `donchian_breakout`, `sma_50_200_cross`, `trend_strength`, `close_vs_sma50_bps`, indexed identically to `daily`.
- Consumes: nothing.
- Consumed by: Task 7 (`backtest.py`) and Task 8 (Track-A universe contexts).

- [ ] **Step 1: Write the failing test**

Create `tests/test_daily_trend_frame.py`:

```python
"""The vectorised trend frame must agree with build_daily_trend bar-for-bar.

The backtester consumes the frame; the live path consumes build_daily_trend.
If they diverge, backtest results describe a strategy we do not run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.daily_trend import build_daily_trend, compute_trend_frame


def _daily(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def test_frame_has_a_row_per_bar():
    daily = _daily()
    frame = compute_trend_frame(daily)
    assert len(frame) == len(daily)
    assert frame.index.equals(daily.index)


def test_last_row_matches_build_daily_trend():
    daily = _daily()
    frame = compute_trend_frame(daily)
    trend = build_daily_trend(daily)

    last = frame.iloc[-1]
    assert last["direction"] == trend.direction
    assert last["donchian_breakout"] == trend.donchian_breakout
    assert last["sma_50_200_cross"] == trend.sma_50_200_cross
    assert round(float(last["trend_strength"]), 1) == trend.trend_strength


def test_matches_build_daily_trend_at_earlier_cutoffs():
    """Bar t in the frame must equal build_daily_trend(daily[:t+1]) — this is
    the no-lookahead guarantee the backtest depends on."""
    daily = _daily()
    frame = compute_trend_frame(daily)

    for cutoff in (150, 220, 299):
        expected = build_daily_trend(daily.iloc[: cutoff + 1])
        row = frame.iloc[cutoff]
        assert row["direction"] == expected.direction, f"cutoff={cutoff}"
        assert row["donchian_breakout"] == expected.donchian_breakout, f"cutoff={cutoff}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_daily_trend_frame.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_trend_frame'`.

- [ ] **Step 3: Implement `compute_trend_frame`**

In `trading_bot/daily_trend.py`, add a vectorised implementation computing, for every bar: rolling `sma50`/`sma200`; `sma_50_200_cross`; Donchian prior-20-day high/low **shifted by one bar** (`.rolling(20).max().shift(1)`) so the current bar is excluded exactly as line 95 does; `trend_strength` from a vectorised `_adx_proxy` (return the full smoothed `adx` series clipped to [0,100], filled with 0.0 where NaN); `sma50_rising` via `sma50 >= sma50.shift(5)` (matching `iloc[-6]`); and `direction` from the same three-way condition at lines 113-116. Bars with fewer than 30 valid closes must produce `direction="sideways"`, `donchian_breakout="none"`, `sma_50_200_cross="none"`, `trend_strength=0.0` — matching the `neutral` verdict.

Then rewrite `build_daily_trend` to call `compute_trend_frame` and build the `DailyTrend` from the last row, preserving the existing `summary` string format exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_daily_trend_frame.py tests/test_daily_trend.py -v`
Expected: PASS — including every pre-existing `test_daily_trend.py` case, which is the proof the refactor is behaviour-preserving.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Write the ADR**

Create `docs/decisions/0007-shared-trend-frame-live-and-backtest.md` recording: the live path and the backtester must share one rule implementation; the frame is the single source of truth; bar `t` is guaranteed to equal `build_daily_trend(daily[:t+1])` (no lookahead), enforced by `test_matches_build_daily_trend_at_earlier_cutoffs`.

- [ ] **Step 7: Commit**

```bash
git add trading_bot/daily_trend.py tests/test_daily_trend_frame.py docs/decisions/0007-shared-trend-frame-live-and-backtest.md
git commit -m "refactor(strategy): vectorised trend frame shared by live and backtest"
```

---

### Task 7: Vectorised backtester

Track-A is backtest-valid (pure price functions, no LLM training-cutoff contamination — ADR 0003). A backtest over years of daily bars gives a provisional verdict in hours instead of waiting ~60 forward-walk trades.

**Files:**
- Create: `trading_bot/backtest.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `compute_trend_frame` (Task 6); `Settings`; `wilson_ci` and `bootstrap_expectancy_ci` from `trading_bot.forward_eval`.
- Produces: `BacktestTrade` dataclass; `simulate_symbol(daily, symbol, settings, *, spread_bps, slippage_bps) -> list[BacktestTrade]`; `run_backtest(frames, settings) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backtest.py`:

```python
"""Backtest must reproduce the Track-A rule with costs and no lookahead."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.config import Settings


def _trending_up(n: int = 400) -> pd.DataFrame:
    close = np.linspace(100.0, 200.0, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_uptrend_generates_long_trades():
    from trading_bot.backtest import simulate_symbol

    trades = simulate_symbol(
        _trending_up(), "WTI", _settings(), spread_bps=5.0, slippage_bps=5.0
    )
    assert trades, "a clean uptrend must produce at least one long entry"
    assert all(t.side == "buy" for t in trades)


def test_no_lookahead_entry_uses_next_bar_open():
    """A signal on bar t must fill on bar t+1, never on bar t's close."""
    from trading_bot.backtest import simulate_symbol

    daily = _trending_up()
    trades = simulate_symbol(
        daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0
    )
    first = trades[0]
    entry_idx = daily.index.get_loc(first.entry_at)
    assert entry_idx >= 1
    assert abs(first.entry - float(daily["Open"].iloc[entry_idx])) < 1e-6


def test_costs_reduce_expectancy():
    from trading_bot.backtest import simulate_symbol

    daily = _trending_up()
    free = simulate_symbol(daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0)
    costly = simulate_symbol(daily, "WTI", _settings(), spread_bps=50.0, slippage_bps=50.0)
    assert sum(t.pnl_pct for t in costly) < sum(t.pnl_pct for t in free)


def test_run_backtest_reports_wilson_ci_and_trade_count():
    from trading_bot.backtest import run_backtest

    report = run_backtest({"WTI": _trending_up()}, _settings())
    assert report["trades"] >= 1
    assert "win_rate_ci" in report
    lo, hi = report["win_rate_ci"]
    assert 0.0 <= lo <= hi <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backtest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trading_bot.backtest'`.

- [ ] **Step 3: Implement the backtester**

Create `trading_bot/backtest.py`. Start with the trade record — these exact field names are what the tests and `run_backtest` read:

```python
@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str              # "buy" | "sell"
    entry_at: pd.Timestamp # bar whose Open filled the entry (signal bar + 1)
    entry: float           # adverse-filled entry price
    stop: float
    tp: float
    exit_at: pd.Timestamp
    exit: float            # adverse-filled exit price
    reason: str            # "stop" | "tp" | "time" | "end_of_data"
    r_multiple: float      # realised P&L / initial risk
    pnl_pct: float         # net of spread + slippage, signed
```

Then the simulation. Requirements, all load-bearing:

1. **Rule parity** — derive signals from `compute_trend_frame(daily)`. Entry condition must be exactly Track-A's (`daily_trend.direction == "up"` and `donchian_breakout == "up"` for long; mirrored for short; both gated on `trend_strength >= settings.min_trend_strength`). Shorts only when `settings.paper_allow_shorts` is true — read it as `getattr(settings, "paper_allow_shorts", False)` so this task can land before Task 9 adds the setting. The tests in this task cover longs only; short coverage arrives with Task 9.
2. **No lookahead** — a signal observed on bar `t` fills at bar `t+1`'s Open. Never fill on the signalling bar.
3. **Stop/TP parity** — `stop_distance = max(ATR14 * settings.atr_stop_multiplier, price * settings.stop_loss_pct_floor, 0.01)`, `tp_distance = stop_distance * settings.risk_reward_ratio` — identical to `track_a.decide_track_a` lines 75-85. ATR-14 computed from the daily frame.
4. **Intrabar exit priority** — if a bar's Low breaches the stop **and** its High breaches the TP, record the **stop** (pessimistic; we cannot know the intrabar path).
5. **Time stop** — force-close after `settings.default_max_holding_hours`.
6. **Costs** — apply spread and slippage adversely on both legs, mirroring `paper_pnl._adverse_fill`.
7. **One open position per symbol**, and honour `settings.track_a_cooldown_hours` between entries.
8. `run_backtest` aggregates across symbols and returns `{"trades", "wins", "win_rate", "win_rate_ci", "expectancy_pct", "expectancy_ci", "by_symbol"}`, reusing `wilson_ci` and `bootstrap_expectancy_ci` from `forward_eval` so the numbers are directly comparable to the forward-walk report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backtest.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Add a CLI entry point**

Add a `main()` to `backtest.py` (mirroring `forward_eval.main`) that downloads N years of daily bars for the tradeable universe via `yfinance`, runs the backtest, and prints the report as JSON. Guard with `if __name__ == "__main__":`.

- [ ] **Step 6: Run it for real and record the result**

Run: `.venv/bin/python -m trading_bot.backtest --years 5 > /tmp/backtest_5y.json && cat /tmp/backtest_5y.json`

Record `trades`, `win_rate`, `win_rate_ci`, and `expectancy_pct` in the commit message. **This is the first real evidence about the strategy's edge** — if the expectancy CI sits entirely below zero across a 5-year sample, that is a finding to escalate immediately, not to bury.

- [ ] **Step 7: Commit**

```bash
git add trading_bot/backtest.py tests/test_backtest.py
git commit -m "feat(eval): vectorised Track-A backtester with cost model"
```

---

### Task 8: Track-A over the full universe

Track-A currently only sees the ≤6 symbols the LLM selected (`bot.py:858` passes `symbol_contexts`), so the mechanical twin — the cheapest, fastest source of evidence — is throttled by the expensive path. Give it its own universe on daily bars.

**Files:**
- Modify: `trading_bot/config.py`
- Modify: `trading_bot/bot.py:854-860`
- Test: `tests/test_track_a_universe.py`

**Interfaces:**
- Consumes: `compute_trend_frame` (Task 6).
- Produces: `Settings.track_a_universe_enabled: bool = True`; `Settings.track_a_daily_cache_ttl_minutes: int = 720`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_track_a_universe.py` asserting: (a) `track_a_universe_enabled` defaults True; (b) the Track-A context builder returns a context for **every** tradeable instrument, not just the LLM-selected subset; (c) daily frames are served from cache within the TTL (assert the fetch function is called once across two consecutive builds).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_track_a_universe.py -v`
Expected: FAIL — missing settings and builder.

- [ ] **Step 3: Implement**

Add the two settings to `config.py`. Add a `_build_track_a_contexts()` method to `TradingBot` that, for every instrument passing the Task 4 tradeability filter, fetches **daily** bars only (TTL-cached — daily bars change once per day, so a 12-hour TTL keeps Yahoo load flat), computes `DailyTrend` via `compute_trend_frame(...).iloc[-1]` and ATR-14 from the same daily frame, and returns lightweight context objects exposing the three attributes `decide_track_a` reads: `symbol`, `indicators.price`, `indicators.atr_14`, plus `daily_trend`.

Then change `bot.py:858` to pass these contexts instead of `symbol_contexts`:

```python
                contexts = (
                    self._build_track_a_contexts()
                    if self.settings.track_a_universe_enabled
                    else symbol_contexts
                )
                run_track_a_cycle(contexts, self.track_a_pnl, self.settings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_track_a_universe.py tests/test_track_a.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

```bash
.venv/bin/pytest -q
git add trading_bot/config.py trading_bot/bot.py tests/test_track_a_universe.py
git commit -m "feat(eval): run Track-A across the full tradeable universe"
```

---

### Task 9: Paper-track shorts

`allow_shorts=False` means Track-A cannot take a single short. In a downtrend it sits flat, and the forward-walk sample is censored to long-only — biasing the verdict and halving throughput. Live stays long-only; paper does not.

**Files:**
- Modify: `trading_bot/config.py`
- Modify: `trading_bot/track_a.py:66-73`
- Test: `tests/test_track_a.py` (extend)

**Interfaces:**
- Produces: `Settings.paper_allow_shorts: bool = True`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_track_a.py`:

```python
def test_paper_track_takes_shorts_even_when_live_shorts_disabled():
    """Live trading stays long-only, but censoring shorts from the paper
    sample biases the forward-walk verdict and halves throughput."""
    from trading_bot.config import Settings
    from trading_bot.models import IndicatorSnapshot, SymbolContext
    from trading_bot.track_a import decide_track_a

    settings = Settings(_env_file=None, allow_shorts=False, paper_allow_shorts=True)
    context = SymbolContext(
        symbol="WTI",
        display_name="WTI",
        asset_class="commodity",
        as_of="2026-07-28T00:00:00+00:00",
        indicators=IndicatorSnapshot(
            price=100.0, sma_20=100.0, sma_50=100.0, ema_12=100.0, ema_26=100.0,
            rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=2.0,
            volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
        ),
        daily_trend=_trend(direction="down", breakout="down", strength=35.0),
        headlines=[],
        chart_note="t",
    )

    signal = decide_track_a(context, settings)

    assert signal is not None
    assert signal.side == "sell"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_track_a.py::test_paper_track_takes_shorts_even_when_live_shorts_disabled -v`
Expected: FAIL — `decide_track_a` returns `None` because it reads `settings.allow_shorts`.

- [ ] **Step 3: Add the setting**

In `trading_bot/config.py`, directly below `allow_shorts: bool = False`:

```python
    # Paper/eval books may take shorts even while live trading is long-only.
    # Censoring shorts from the forward-walk sample biases the verdict (we
    # would only ever measure the strategy in rising markets) and halves the
    # rate at which independent trades accumulate.
    paper_allow_shorts: bool = True
```

- [ ] **Step 4: Use it in Track-A**

In `trading_bot/track_a.py`, replace lines 68-71:

```python
    elif trend.direction == "down" and trend.donchian_breakout == "down":
        # Track-A is a paper book: it uses paper_allow_shorts, not the live
        # allow_shorts permission.
        if not getattr(settings, "paper_allow_shorts", False):
            return None
        side = "sell"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_track_a.py -v`
Expected: PASS.

- [ ] **Step 6: Confirm live is untouched**

Run: `.venv/bin/pytest tests/test_risk_gates.py -q` and confirm no live path consults `paper_allow_shorts`:

```bash
grep -rn "paper_allow_shorts" trading_bot/
```

Expected: matches only in `config.py`, `track_a.py`, and `backtest.py`.

- [ ] **Step 7: Commit**

```bash
git add trading_bot/config.py trading_bot/track_a.py tests/test_track_a.py
git commit -m "feat(eval): allow shorts on paper tracks while live stays long-only"
```

---

## Final Verification

- [ ] **Full suite**

Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Paper mode still enforced**

```bash
grep -n "dry_run" trading_bot/config.py
```
Expected: `dry_run: bool = True`.

- [ ] **No secrets staged**

```bash
git diff --cached --name-only | xargs grep -lE "nvapi-|sk-or-v1-|AIza|[0-9a-f]{32}" || echo "clean"
```
Expected: `clean`.

- [ ] **Backtest verdict recorded**

Confirm `/tmp/backtest_5y.json` results are captured in the Task 7 commit message and summarised for the operator.

- [ ] **Deploy** (operator-run, requires OCI access)

```bash
git push origin master
# on OCI:
ssh trading-bot 'cd /home/hostgbr/Automated-trading && git stash && git pull --ff-only && git stash pop && sudo systemctl restart trading-bot.service trading-bot-watcher.service'
```

Then verify both units are `active`, and confirm paper mode at process level:

```bash
ssh trading-bot 'tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value trading-bot.service)/environ | grep DRY_RUN'
```
Expected: `DRY_RUN=true`.

---

## Out of Scope (evidence-gated — do not start)

Per ADR 0005, these stay gated behind the forward-walk verdict and the Task 7 backtest result:

- GARCH/EWMA volatility forecasting
- HMM regime model
- Bayesian calibration → fractional-Kelly sizing
- Ledoit-Wolf covariance / risk-parity optimisation
- Alpha factor model (momentum/carry/value)
- Additional data sources (GDELT, central-bank RSS, MOVE, Baker Hughes rig count)

Reason: adding alpha layers before knowing whether the base rule has an edge makes attribution impossible. Task 7 delivers that answer first.

## Prerequisite (operator, before deploy)

Rotate every credential exposed in the public share link: NVIDIA NIM, Finnhub, FRED, EIA, OpenRouter, IG demo. The share page is world-readable and contains them in plaintext.
