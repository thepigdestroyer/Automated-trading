# Trading Bot Profitability Roadmap (2026-05)

**Status**: APPROVED by operator 2026-05-21. Implement ALL phases. Keep live (`dry_run=false`). Zero-cost constraint — free data sources only.

**Baseline at approval**: equity £10,000 → £6,981 (-30%) over 17 days. 11 closed trades, 0 wins (0% win rate). 100% energy fills (WTI/BRENT). HOLD 82% of decisions. 55 restarts/15d.

## Diagnosis (root causes by money-on-table)

1. Single news source (Google News RSS, US-en) → one-narrative anchoring → 100% energy concentration.
2. No quant/math signals — LLM eyeballs raw RSI/MACD/ATR floats in prose → no edge, 0% win rate.
3. No feedback from trade outcomes (TradeJournal exists but not fed to prompts).
4. No backtest harness — every change is a live £-risk experiment.
5. LLM doing arithmetic it shouldn't (stop/sizing) then code overrides it.
6. Account too small (£7k) → BRENT/GOLD/NATGAS blocked by minDealSize → forced into WTI only.
7. Broker CLOSE bug (`validation.null-not-allowed.request` × 43) — can't exit cleanly.
8. Hourly-bar vol annualised with ×√252 (daily-bar constant) — vol off by ~3.2×.
9. LLM provider availability fragile (now on NVIDIA NIM).

## Phases

### Phase 0 — Stop the bleeding (Week 1)
- 0.1 ~~Pause live~~ — OPERATOR OVERRIDE: keep live.
- 0.2 Fix `validation.null-not-allowed.request` on CLOSE (ig_bridge.py — needs explicit file naming).
- 0.3 Fix `pnl_cash=null` on every closed trade (portfolio/runtime_state layer). Unblocks feedback loop.
- 0.4 Reduce `subagent_failure_streak` alarm noise (re-baseline post-NVIDIA-NIM).
- 0.5 Fix hourly-vol annualisation bug (indicators.py).

### Phase 1 — Quant signal layer (Weeks 2-3)
- 1.1 New `signals.py`: per-symbol `SignalPack` of pre-classified labels (trend/momentum/mean-rev-z/vol-regime/bband-position). LLM consumes labels not floats.
- 1.2 Bollinger / Donchian / VWAP-deviation / mean-reversion z-score.
- 1.3 Cross-asset rolling correlation matrix (DXY/SPX/VIX/GOLD/oil/USDJPY) → surface `correlation_breaks`.
- 1.4 (moved from 0.5) vol math fix.
- 1.5 Move stop/TP/sizing math fully OUT of LLM → LLM emits `{direction, confidence, signal_horizon_h}` only.

### Phase 2 — Feedback loop (depends on 0.3)
- 2.1 Feed TradeJournal summary into PM decide() (win-rate by symbol/regime, last-10 trades).
- 2.2 Confidence calibration (emitted-confidence vs actual-outcome).
- 2.3 Auto-blacklist (symbol, direction) after N consecutive losers.

### Phase 3 — Backtest harness (Week 4, biggest unlock)
- 3.1 Replay harness: saved (market_data, news, runtime_state) → step bot → simulated fills → P&L.
- 3.2 A/B all Phase 1+2 changes in harness before live.

### Phase 4 — Data expansion (FREE sources only)
- 4.1 Second news source: Marketaux free tier OR Finnhub free tier OR Nitter/RSS (decorrelate from Google News).
- 4.2 FRED API (free) for macro releases.
- 4.3 CFTC Commitment-of-Traders (free, weekly) — oil/gold/FX positioning.
- 4.4 ~~X/Twitter paid firehose~~ → FREE alt: Nitter RSS scrape or stocktwits free API.

### Phase 5 — Architecture (optional)
- 5.1 Multi-agent voting (3 prompts, confidence-weighted).
- 5.2 Two-stage decision (cheap deterministic filter → expensive LLM).

## Rejected (for now)
- ML model on bars (dataset too small — 11 trades). Revisit after 6mo harness data.
- HFT/sub-second (architecture is hourly).
- Replace LLM entirely (its edge is narrative integration).
- Switch broker (account-size problem, not broker problem).

## Exit criteria
- Phase 0: bot can exit positions cleanly, reads own P&L, vol math correct.
- Phase 1+2: harness shows positive expectancy across ≥2 distinct replay weeks.
- Overall: 0% → 40%+ win rate over 100 trades, positive Sharpe over rolling month.
