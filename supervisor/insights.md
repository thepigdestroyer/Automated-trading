# Supervisor Insights

Long-term memory for the trading bot supervisor. Updated by the bot-supervisor agent after each review.  
Do not edit manually without noting the change here.

---

## Symbol notes

### GOLD
- IG EPIC `MT.D.GC.FWM2.IP` (JUN-26 continuous): **minimum deal size is 0.5 contracts**. The bot's ATR-scaled risk model produces a raw quantity of ~0.06–0.08 for a £10,000 account at standard settings. IG rounds this up to 0.5, creating a ~7–10× size overrun relative to intended per-trade risk. At 0.5 contracts, each 1-point move in GOLD price equals ~£24.75 in GBP account value. A 23.5-point stop (typical 0.5% stop at ~4700) implies ~£291 risk per trade — roughly 2.9% of a £10k account in a single position. **Do not trade GOLD until the size-check sanity gate is implemented or per-trade risk budget is recalibrated to 0.5-contract minimum.** [Source: reviews/2026-04-28-adhoc-1.md]
- GOLD stop-out in the 2026-04-28 run occurred during a RemoteDisconnected outage window (02:13–02:22 UTC); the broker closed the position at stop while the bot had no connectivity. No `trade_close` event was emitted; the loss was only discovered on next successful reconnect. [Source: reviews/2026-04-28-adhoc-1.md]

### WTI
- IG EPIC `CC.D.CL.UMP.IP`: SIZE_INCREMENT issue (bot sizing at ~3.07x non-compliant with minDealSize) was fixed in commit `47b4979`. All WTI SIZE_INCREMENT rejections in the 2026-04-28 run were pre-fix. [Source: reviews/2026-04-28-adhoc-1.md]
- WTI shows strong response to Iran/Hormuz geopolitical supply-shock narrative. In the 2026-04-28 run, 7 consecutive BUY signals at 0.72–0.82 confidence were generated across a 4+ hour window with consistent Iran standoff thesis. The Iran/Hormuz energy-supply-shock regime then persisted across **25 consecutive cycles** in the 2026-04-29 session — the LLM tagged every cycle `energy-supply-shock` and produced 13 WTI BUY plans at 0.72–0.85 confidence over 24h.
- **WTI stop-distance unit mismatch (CRITICAL):** Portfolio Manager produces `stop_distance` in instrument-price units (dollars: e.g. 1.38 = $1.38 = ~138 IG pts at 1pt=1¢). IG's `min_stop_distance = 12.0 pts` (= 12¢ = $0.12). The pre-submit gate compares the two values directly, and on 2026-04-29 it rejected 8 of 13 submissions with `stop_distance=1.38 < required=12.0`. The 3 fills that succeeded did so because IG silently clamped both `stopLevel` AND `limitLevel` to the 12pt minimum, producing a 1:1 RR product with negative edge after spread. **Do not interpret a successful WTI fill as a tradable opportunity until #1 in the 2026-04-29-adhoc-1 review lands.** [Source: reviews/2026-04-29-adhoc-1.md]

### NATGAS
- IG EPIC `CC.D.NG.UMP.IP`: 2× `ATTACHED_ORDER_LEVEL_ERROR` rejections at 2026-04-29T09:36 and 09:42 UTC for SELL plans with stop_distance=0.0134 and TP_distance=0.0268/0.0322. Likely same unit-mismatch class as WTI (instrument-price units vs IG pts). [Source: reviews/2026-04-29-adhoc-1.md]

---

## Data quality

- The Yahoo Finance market-data feed via `t212_cfd_bot.market_data` consistently drops 2 outlier bars/cycle for `NG=F`, 1 for `CL=F`, and 1 for `BZ=F` at the 15% gap threshold. The pattern is too consistent (every cycle, ~38 occurrences in 9h) to be noise — the upstream feed has chronic late-bar quality issues for these futures. TA indicators (RSI, MACD, SMA-20/50) are computed against the post-drop series. Until the feed or threshold is reviewed, treat NATGAS/WTI/BRENT indicator values as carrying a "last bar may be 1–2 sessions stale" caveat. [Source: reviews/2026-04-28-adhoc-2.md]
- The news synthesiser fails JSON parsing in roughly 1-in-5 cycles (3/15 = 20% in the 2026-04-28 run), exclusively during high-volume Iran/energy headline windows. Failures appear at varying char positions (e.g. 23782, 5929) indicating large-payload comma-drop, not a deterministic prompt bug. The bot recovers gracefully with degraded research, but energy-trade decisions during high-news windows are made on weaker context exactly when conviction matters most. [Source: reviews/2026-04-28-adhoc-2.md]

---

## Regime observations

- The LLM generates a unique free-text `market_regime` label almost every cycle, even when the macro backdrop is stable. In the 2026-04-28 run, all 15 completed cycles produced 15 unique regime strings for what was effectively the same Iran/Hormuz supply-shock regime. The `regime_history` field in `runtime_state.json` is therefore not useful for pattern matching. **Regime standardisation to a short enumerated token is required before supervisor rules can use regime history.** [Source: reviews/2026-04-28-adhoc-1.md]
- When the bot labels the same session as both "risk-on" and "risk-off" in adjacent cycles (e.g. 04:38 UTC "risk-on with energy supply shock overlay" vs 05:16 UTC "Risk-off with geopolitical supply shock"), this reflects LLM label drift, not a genuine regime change. WTI entry signals and technical indicators were consistent throughout. [Source: reviews/2026-04-28-adhoc-1.md]

---

## Infrastructure observations

- **IG keep-alive disconnect:** IG drops idle keep-alive sockets approximately every 15–20 minutes. Before the 47b4979 fix, this caused ~48% of cycle attempts to fail (14 of 29) in the overnight run. The fix adds a transport-level retry with session rebuild. **Confirmed working post-fix on 2026-04-29:** 13 IG transport errors observed in 24h, all transparently retried, zero cycle aborts attributable to IG transport. [Source: reviews/2026-04-28-adhoc-1.md, commit 47b4979; reviews/2026-04-29-adhoc-1.md]
- **`runtime_state.json` is unreliable as a supervisor signal source on the current build:** `account_balance`, `starting_balance`, `daily_pnl`, `closed_trades` all null/empty after a full trading day. The bot's internal accounting is correct (Guardrails log line reports `daily_drawdown=0.36%` and `account=£9,638.13`) but the on-disk state file is empty. Use the bot.log `Guardrails:` line as the authoritative source until the runtime-state writer is fixed. [Source: reviews/2026-04-29-adhoc-1.md]
- **`trade_close` reconciliation fix (commit `362241f`) does NOT cover normal broker-driven stop-outs.** 3 WTI fills opened on 2026-04-29T09:56–10:04 and were closed (likely at the IG-clamped 12pt stop) without producing any `trade_close` event. `runtime_state.closed_trades=[]` at end of session. The previous fix appears scoped only to outage-induced position vanishings. [Source: reviews/2026-04-29-adhoc-1.md]
- **`log_silence` watcher rule is dominated by false positives:** 22 triggers in 24h on 2026-04-29, 19 suppressed by `previous_pid_still_alive`, 3 actual (and unnecessary) restarts. The rule is currently safe only because the suppression layer exists; the underlying file-mtime detector should be replaced with a positive `cycle_heartbeat` event. [Source: reviews/2026-04-29-adhoc-1.md]
- **IG silently clamps BOTH `stopLevel` and `limitLevel` to the symbol minimum.** Confirmed on the 09:56, 09:57, 10:04 WTI fills on 2026-04-29: planned TPs of 215–267pts came back as exactly 12pt targets in the broker confirm. Pre-submit risk must validate `take_profit_distance` against `min_limit_distance` and reject if too tight, mirroring the existing stop-distance check. [Source: reviews/2026-04-29-adhoc-1.md]
- **Watcher double-restart at startup:** The supervisor `process_dead` rule fired twice in 37 seconds at 23:51 and 23:52 UTC during initial startup (PIDs 163387 and 31229). The watcher polls faster than the bot PID file stabilises during the spawn sequence. Consider a 5–10s hold-off before re-checking after a restart. [Source: reviews/2026-04-28-adhoc-1.md]
- **Silent position closure during outage:** When the bot is offline (e.g. RemoteDisconnected window) and IG closes a position via stop order, no `trade_close` event is emitted to `events.jsonl`. The close is discovered on the next reconcile. This means the watcher's `consecutive_losses` and `same_symbol_stop_streak` rules are blind to stop-outs that occur during connectivity gaps. [Source: reviews/2026-04-28-adhoc-1.md]
- **Spread check during outage proceeds silently:** `ig_bridge` logs `WARNING: Spread check failed — proceeding` when a `RemoteDisconnected` hits the spread check. Post-47b4979 this should resolve, but the fallback to proceed without spread check is a risk during connectivity events. [Source: reviews/2026-04-28-adhoc-1.md]

---

## Risk management observations

- **minDealSize overrun is a critical risk:** When IG's `minDealSize` is larger than the bot's ATR-scaled quantity, the actual risk is multiplied by the ratio. GOLD produced a 7.8× overrun (0.0637 requested → 0.5 opened), consuming 2.91% of account equity in a single stop-out. A pre-submission size-vs-budget check is required. [Source: reviews/2026-04-28-adhoc-1.md]
- **Event gate timing (post-event window):** The current post-event blocking window is 15 minutes (asymmetric: 30 min before, 15 min after). For major central bank events (BOJ, Fed, ECB), 15 minutes may be too short to clear post-announcement volatility. [Source: reviews/2026-04-28-adhoc-1.md]
- **Confidence threshold at 0.72:** The threshold is a strict `< 0.72` filter. NATGAS at 0.71 was rejected in the 09:18 UTC cycle. The threshold is working as intended. [Source: reviews/2026-04-28-adhoc-1.md]
- **Reported pnl is approximate within ~5%:** The £291.31 GOLD loss exceeds the model-implied £277 loss by ~£14 (~5%). Without broker transaction-history reconciliation, the discrepancy can't be attributed to slippage, financing, or contract-multiplier mis-config. Until a transaction-history reconcile lands, treat all reported pnl as approximate. [Source: reviews/2026-04-28-adhoc-2.md]
- **`max_positions=2` behaves as a soft constraint, not a diversification mandate.** In the 02:51 UTC cycle of the 2026-04-28 run the LLM had GOLD long, recognised the available second slot, identified SILVER as a thematically aligned add candidate, and still chose HOLD. Acceptable conservative behaviour but the config name is misleading vs realised semantics. [Source: reviews/2026-04-28-adhoc-2.md]

---

## Watcher rule coverage gaps

The current watcher rules (`process_dead`, `consecutive_losses`, `same_symbol_stop_streak`, `daily_drawdown_breach`) do not cover three observable failure modes from the 2026-04-28 run:

- **Repeated execution-error events on the same symbol** — 6 WTI `SIZE_INCREMENT` rejections across a 4-hour window had no rule to surface the pattern. [Source: reviews/2026-04-28-adhoc-2.md] **REGRESSION CONFIRMED 2026-04-29:** the `repeated_execution_errors` rule (commit `5388070`) did NOT fire despite 8 same-symbol-same-error WTI `STOP_TOO_TIGHT_VS_SPREAD` events and 2 same-symbol-same-error NATGAS `ATTACHED_ORDER_LEVEL_ERROR` events in 24h. The rule appears to filter on `execution.error` strings only, missing `risk_block.reason` events. [Source: reviews/2026-04-29-adhoc-1.md]
- **Silent risk-gate bypass during transport outages** — when `ig_bridge` falls back to "Spread check failed — proceeding", no event is journaled, so a watcher rule cannot detect that risk gates are degraded. [Source: reviews/2026-04-28-adhoc-2.md]
- **Data-feed quality degradation** — `market_data` drops outlier bars every cycle but emits only WARNING log lines, not events. A genuinely broken feed would be invisible to the watcher. [Source: reviews/2026-04-28-adhoc-2.md]

---

## Pending suggestions

The following proposals from recent reviews are pending user approval via `/bot-apply`:

- [1] Size sanity-check: reject if IG minimum would exceed per-trade risk budget → [reviews/2026-04-28-adhoc-1.md#change-1]
- [2] Persist `trade_close` event during startup reconciliation → [reviews/2026-04-28-adhoc-1.md#change-2]
- [3] Standardise regime label to enumerated short token → [reviews/2026-04-28-adhoc-1.md#change-3]
- [4] Post-SIZE_INCREMENT retry with corrected size → [reviews/2026-04-28-adhoc-1.md#change-4]
- [5] Emit supervisor flag when minDealSize forces overrun > 2× → [reviews/2026-04-28-adhoc-1.md#change-5]
- [6] Raise post-event block window from 15 → 30 minutes → [reviews/2026-04-28-adhoc-1.md#change-6]
- [7] Pre-submission dealing-rules size validation on first entry → [reviews/2026-04-28-adhoc-1.md#change-7]
- [8] Promote market-data outlier-drop warnings to structured `data_quality` events + watcher rule → [reviews/2026-04-28-adhoc-2.md#8]
- [9] News-synthesiser: constrained schema, retry-on-parse-fail, fallback to last cycle's headlines → [reviews/2026-04-28-adhoc-2.md#9]
- [10] Watcher `repeated_execution_errors` rule (≥3 same-error events on same symbol within 4h → alert_only) → [reviews/2026-04-28-adhoc-2.md#10]
- [11] Persist `risk_gate_degraded` event when `ig_bridge` spread-check falls back to "proceeding" + watcher streak rule → [reviews/2026-04-28-adhoc-2.md#11]
- [12] Reconcile pnl from IG `transactions/history` on startup; populate confirmed close levels into synthesised `trade_close` events → [reviews/2026-04-28-adhoc-2.md#12]
- [13] Clarify `max_positions=2` semantics: prompt-level diversification reward **or** rename to `max_position_slots` with documented "premium adds only" intent → [reviews/2026-04-28-adhoc-2.md#13]

---

## Improvements applied

| Date | Change | Review source | Applied by | Review-on |
|---|---|---|---|---|
| 2026-04-28 | #1 Size sanity-check: reject trades when IG minDealSize overrun > 3× tolerance (commit `e8ddc3b`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #2 Emit synthesised `trade_close` event on startup reconciliation for vanished positions (commit `362241f`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #3 Constrain `market_regime` to enumerated short tokens in Portfolio Manager prompt (commit `09f7a94`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #4 Retry once after IG SIZE_INCREMENT rejection with re-normalised size (commit `fccca1f`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #5 Emit `SIZE_OVERRUN OVERRUN_RATIO=...` warning when minDealSize forces order >2× (commit `c9fdaac`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #6 Symmetric post-event block window via `high_impact_post_event_block_minutes` (default 30m) (commit `4bbec01`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #7 Pre-submission `PRE_SUBMIT_VERIFY` log of raw-vs-normalised size on first entry per symbol (commit `903594b`) | reviews/2026-04-28-adhoc-1.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #8 Structured `data_quality` events for outlier-bar drops + `data_feed_degraded` watcher rule (commit `75a4d2d`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #9 News-synthesiser headline truncation + one retry + fallback to last cycle on parse-fail; emits `news_synthesiser_failed` (commit `dc05fd4`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #10 Watcher rule `repeated_execution_errors` (≥3 same symbol+error in 4h → alert_only) (commit `5388070`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #11 Persist `risk_gate_degraded` event when spread-check falls back; watcher `risk_gate_degraded_streak` rule (commit `e3aaf75`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #12 Reconcile pnl from IG `transactions/history` on startup; populate confirmed close levels in `trade_close` events (commit `86fba3f`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-28 | #13 Portfolio Manager prompt: clarify max_positions semantics; reward thematic diversification at second-slot confidence ≥0.75 (commit `0cd7232`) | reviews/2026-04-28-adhoc-2.md | bot-supervisor agent | 2026-05-05 |
| 2026-04-29 | #1 Per-instrument `ig_price_scale` on InstrumentDefinition; risk.py scales stop_distance/take_profit_distance for WTI/BRENT/NATGAS by ×100 before plans leave the risk module (commit `91b79db`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #2 New `min_take_profit_distance` config + risk-gate floor; mirrors min_stop_distance check, prevents IG silently clamping limit to its 12pt minimum (commit `91b79db`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #3 `repeated_execution_errors` watcher rule extended to count `risk_block` events keyed by `risk_block:<REASON>` alongside execution.error strings (commit `7e72b0b`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #4 New `auto_blacklist_repeated_errors` watcher rule + wiring; auto-blacklists symbols after 5 same-(symbol, error) events in 4h with a 4h TTL (commit `dcc354c`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #5 `_refresh_runtime_state` now emits `trade_close` events for positions that vanish mid-session (broker-driven stop-outs), with optional broker tx-history enrichment (commit `06a2df6`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #6 Populate `runtime_state.json` with broker-confirmed `account.{balance,free_funds,currency,mode,updated_at}`, `account_balance`, `starting_balance`, `daily_pnl`, and `closed_trades[]` (commit `a6a83c7`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #7 New `cycle_heartbeat` event (start+end of each cycle) + `heartbeat_silence` watcher rule with default 50min threshold; preserves legacy log_silence for back-compat (commit `1e0abf9`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #8 Decision-log retention: `supervisor.remediations.rotate_decision_logs` gzips after 3d, deletes after 30d into `logs/archive/YYYY-MM/`; throttled to once per 24h via watcher state (commit `a51abcd`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #9 Structured `subagent_failure` events (provider, subagent, error_class, error_message, attempt) emitted for market_research / symbol_research / news_synthesiser failures + `subagent_failure_streak` watcher rule (≥3 in 30min → alert_only) (commit `1c8d89d`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |
| 2026-04-29 | #10 Last 3 cycles' execution outcomes threaded into Portfolio Manager prompt + new "Plan-loop avoidance" prompt section directing the LLM not to re-propose plans rejected with the same reason (commit `1e8e4c9`) | reviews/2026-04-29-adhoc-1.md | bot-supervisor agent | 2026-05-06 |

---

## Rejected ideas

(none yet)
