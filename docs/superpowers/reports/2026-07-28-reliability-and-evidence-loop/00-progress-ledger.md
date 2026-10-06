# SDD Progress — 2026-07-28-reliability-and-evidence-loop

Plan: docs/superpowers/plans/2026-07-28-reliability-and-evidence-loop.md
Branch: feat/reliability-and-evidence-loop
Base commit: 3b7133d

## Environment
- macOS; uv-managed .venv on CPython 3.12.13 (system python is 3.9, project needs >=3.11)
- Test runner: .venv/bin/pytest
- BASELINE (before any task): 596 passed, 8 failed
- The 8 failures are macOS platform artifacts, NOT regressions:
  - tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
    -> bot.py:389 reads Linux-only /proc/self/status
  - tests/test_watcher_heartbeat_journal.py (5 tests) + tests/test_events_rotation.py (2 tests)
    -> fixture uses /bin/true which does not exist on macOS (it is /usr/bin/true)
  - None of the 9 planned tasks touch watcher/ or memory-pressure code.
  - Any task must leave these at exactly 8 failures; any NEW failure is a regression.

## Tasks
(none complete yet)

Task 1: complete (commits f09f68e..a602edc, review clean after 2 fix rounds)
  - Mark-don't-drop outlier bars; DataQuality report; ADR 0006.
  - Suite: 626 passed, 8 baseline failed.
  - INTERFACE CHANGE for later tasks: DataQuality gained `bars_dropped_empty`,
    `bars_volume_filled`, and an `imputed_ratio` property (returns 1.0 when
    bars_total==0, so the fallback context reads as fully degraded instead of
    raising ZeroDivisionError). Consumers should use `is_degraded` /
    `imputed_ratio`, never raw `bars_imputed / bars_total`.
  - The data_quality event emits `bars_dropped` = OUTLIER COUNT ONLY (legacy key
    that supervisor/rules.py data_feed_degraded depends on). Do not repurpose it.

Task 2: complete (commits 4e1b717..e0c4f15, review clean after 1 fix round)
  - degraded-inputs confidence gate (0.85) for NEW entries; CLOSE/HOLD structurally exempt.
  - Suite: 651 passed, 8 baseline failed.
  - PLAN DEFECT FOUND: the plan's specified enforcement point would have STRANDED
    EXITS (continue fired before the reversal CLOSE leg was appended) under BOTH
    allow_shorts settings. Enforcement now sits beside the other entry-only blocks
    at risk.py:261-271, reusing the "Converted -> CLOSE" idiom. Evaluation stays
    early (context in scope, pre-sizing).
  - Cross-field validator added: DEGRADED_INPUTS_MIN_CONFIDENCE must be >=
    MIN_MODEL_CONFIDENCE, else the gate is silently inert.
  - OPEN FOLLOW-UP (not in this plan): openai_decision.py does not publish the
    degraded floor to the LLM, so the model keeps emitting ~0.75 entries the
    executor discards. Fail-closed, so wasteful not unsafe.

Task 3: complete (commits e0c4f15..91d0b24, review clean after 1 fix round)
  - HolidayCalendar bounded to rolling +/-90d window, rebuilt once per UTC day.
  - MEASURED: unbounded peak=93.7MB retained=77.3MB -> bounded peak=2.6MB retained=2.2MB.
  - PLAN FACT CORRECTED: the cost is NOT the 1678-2100 schedule range (real default is
    2006-2027, schedule only ~1.5MB). It is the per-minute index built lazily on the
    first is_trading_minute() call (~76MB of the 77.3MB).
  - PLAN DEFECT: the plan's verbatim __init__ early-return left _cache unset ->
    AttributeError on direct _get. Fixed.
  - is_session_open is now airtight fail-open (datetime.max/min, None, date all -> True).
  - Unbounded fallback logs WARNING only when it actually succeeds; `FX` (5 instruments)
    is not an exchange_calendars name so BOTH calls fail there and nothing is restored.
  - Suite: 665 passed, 8 baseline failed.

Task 4: complete (commits 91d0b24..bfe460b, review clean after 1 fix round)
  - is_structurally_tradeable() + IGRestBridge.get_market_constraints (authorised broker
    edit; 42 insertions / 0 deletions, purely additive, no existing line touched).
  - Non-finite (NaN/inf) constraints now KEEP the symbol; garbage must never shrink universe.
  - Suite: 730 passed, 8 baseline failed.
  - KNOWN LIMITATION (accepted, superseded by Task 4b): the notional formula
    (min_deal_size*price vs equity*risk_pct/stop_floor) is ~350x too loose to filter
    BRENT, the motivating case. It also ignores point_value/ig_price_scale, so for
    cents-quoted instruments it can OVER-remove. Task 4b replaces the model.

Task 4b: complete (commits 666d2c5..6d754ed, review clean after 1 fix round)
  - structural_cash_risk_breach() mirrors ig_bridge:592-600 (cash-risk-at-stop).
  - BRENT now filtered above atr_14 1.46 (prod ATR ~3.97 -> GBP595 vs GBP218 ceiling);
    kept at GBP50k equity. Motivating case finally caught.
  - Track-A deliberately receives the UNFILTERED universe (paper book; broker minimums
    irrelevant; narrowing would bias the mechanical-vs-LLM comparison).
  - structural_cash_risk_drop json_log event added for the evidence loop.
  - Suite: 814 passed, 8 baseline failed.

Task 10: complete (commits 74cef79..7ff5281, review clean after 1 fix round)
  - news_sources.py: NewsProvider protocol, RssWireProvider, NewsAggregator.
  - FinancialJuice (realtime wire, VERIFIED live 40 items newest ~7min) + Fed press
    (official, verified 10 items but usually >12h so age-gated; emits news_provider_stale).
  - Headline.latency_class added; latency-aware dedup; per-(class,provider) interleave so
    no single feed can monopolise the 20 prompt slots (was: FJ took all 20).
  - SECURITY: feedparser.parse() was handed a str, so a feed body of file:///... was READ
    FROM DISK and http://... caused a real outbound connect. Now encoded to bytes; both
    yield 0 items. Verified.
  - AUTHORISED supervisor edit: news_memory_max_size 500 -> 2500 (500 was calibrated at
    216 entries pre-multi-source; ~8x volume would have made the anomaly rule a permanent
    false alarm).
  - Suite: 871 passed, 8 baseline failed.
  - DECISION: OpenBB rejected (heavy dep tree onto a 400MB-capped VM we just reclaimed
    91MB on; its news providers are mostly paid and the free ones duplicate Finnhub).

Task 5: implemented (commits 7ff5281..d84ca50) -- f4936dc guided-json (flag default OFF + probe
  script), d84ca50 deterministic exit-only fallback when decide() raises or returns non-envelope.
  - Subagent was ABORTED mid-mutation-testing, leaving a mutant applied in bot.py (`if False:`).
    Controller restored it and finished the task directly.
  - Test file was a TIME BOMB: positions aged vs fixed NOW=2026-05-21 but run_cycle reads the
    wall clock. freezegun fixed it but SEGFAULTED on lazy imports while frozen; replaced with a
    narrow monkeypatch of trading_bot.bot.datetime.
  - Test read record["payload"]; JsonEventLogger writes record["data"]. Fixed.
  - Mutation: 4/4 killed (fallback removed; trade-level CLOSE filter removed; both filters
    removed; non-envelope branch disabled). Added an entry-injection test because the original
    "never opens" test was vacuous against filter removal.
  - Suite: 897 passed, 8 baseline failed.

!! UNRELATED USER WORK IN THE TREE (Sep 25-28, NOT from this plan) -- DO NOT TOUCH, DO NOT COMMIT:
   staged deletions of .claude/**, .opencode/prompts/bot-daily-review.md,
   supervisor/run_daily_review.sh; modified .opencode/**, CLAUDE.md, two old plan docs;
   untracked scripts/bot-dir.sh, scripts/push-bot-data, scripts/sync-bot-data.
   It is a supervisor-tooling portability migration (BOT_DIR resolution, off the WSL path).
   ALL COMMITS MUST USE EXPLICIT PATHS: `git commit -m ... -- <paths>`. A plain `git commit`
   would sweep these staged deletions into a task commit.
Task 5: complete (commits 7ff5281..707d177, review Approved; 1 Minor fix pass)
  - Fallback order loop now isolates each CLOSE (one raising execute no longer skips the rest).
  - Tests chdir to tmp so pytest on the server cannot overwrite logs/llm_parse_failures/.
  - Probe now scores both arms identically (shared select_parse_text) at 8000 tokens.
  - FACT CORRECTED: the 491 subagent_failure events were NOT decide() failures (decide() never
    emitted that event), and decide() failures did not cause the 55 restarts.
  - FOLLOW-UP (outside plan): failures EARLIER in the cycle (vix, econ calendar, load_snapshot,
    _compute_entry_block_lite) or the no-symbols early return still skip exit management.
  - Suite: 905 passed, 8 baseline failed.
Task 6: complete (commits 707d177..66b8b6c, review Approved; 1 Minor fix pass)
  - compute_trend_frame(): vectorised O(n), row t == old scalar rule on daily[:t+1] (no lookahead).
    Parity verified every bar of 31 adversarial real-data shapes vs AST-identical frozen oracle.
    30 symbols x 5y in 64ms.
  - Intentional divergence: +/-inf High/Low now treated as missing (documented in ADR 0007).
  - !! FOR TASK 7: live yf.download(interval="1d") INCLUDES TODAY'S STILL-FORMING BAR, so the live
    trend verdict / Donchian breakout rest on a PARTIAL close. Backtest uses settled closes. The
    backtest must act on row t at bar t+1's open, and the live/backtest data gap must be reported.
  - Suite: 919 passed, 8 baseline failed.
Task 7: complete (commits 01a9b70..09d3eaf, adversarial review: code CORRECT -- independent
  plain-loop rewrite reproduced all 4 headline numbers exactly, 8 symbols trade-by-trade,
  hand-checked trades to 6dp). 1 reporting-honesty fix pass (cost scenarios, week-clustered CI,
  stop-first disclosure).
  RESULT (5y x 28 symbols, longs, week-clustered 95% CI):
    daily_atr        max -0.087 [-0.162,-0.012] | typical -0.009 [-0.085,+0.065] | gross +0.020 [-0.055,+0.094]
    live_hourly_equiv max -0.361 [-0.461,-0.265] | typical -0.128 [-0.227,-0.033] | gross -0.053 [-0.153,+0.045]
  CONCLUSION: NO DETECTABLE EDGE. Gross ~0; no year/asset class with gross CI>0; survivorship and
  understated funding both FLATTER it. Live hourly-ATR stops materially worse. 80% of daily_atr exits
  are the 72h time stop; only 2.3% reach the 2R target.
  Suite: 965 passed, 8 baseline failed.
  >> ESCALATED TO USER before Tasks 8-9 (their value depends on this verdict).
Tasks 8-9: SKIPPED (operator decision) -- Task 7 backtest already delivered the Track-A verdict.
FINAL REVIEW: Ready to push YES; no Critical. Fixed I1 (LLM-vs-TrackA comparison restricted to
  common universe), I2 (held moves are real: only reverting spikes imputed; degradation only counts
  within last 50 bars), M1-M7. Commits 204d624..3c29e4d. Suite 992 passed, 8 baseline failed.
PRE-REGISTERED TEST (Turtle System 1 exits on Track-A entry): FAIL.
  Pre-reg 3120e9f pushed 14:23:45 BEFORE implementation (6cef185) and result (32fc7c8).
  typical: +0.057R [-0.118,+0.234] n=518; last 2y -0.147R. Positive point estimate rests on a few
  outliers (PLATINUM +13.5R, NVDA +13.2R of +29.6R total). Decaying: 2021-23 +, 2024 ~0, 2025-26 -.
  Per commitments: no further parameter search on this data.
