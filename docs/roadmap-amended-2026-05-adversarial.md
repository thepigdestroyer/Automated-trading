# Amended Roadmap — Post-Adversarial-Review (2026-05)

**Status**: DRAFT for operator decision. Supersedes/reframes `roadmap-profitability-2026-05.md` after a 4-expert adversarial review (ML systems engineer, quant mathematician, professional systematic trader, backtest specialist).

## The uncomfortable headline

All four experts independently reached the same verdict: **the original roadmap is competent engineering aimed at the wrong problem.** The bot's 0/11 win rate is not a data-quality or arithmetic bug that signals + correlations will fix — it is an **edge-existence** problem. Polishing the inputs to a strategy with no edge does not create edge.

Key convergent findings:

| # | Finding | Experts | Severity |
|---|---|---|---|
| A | **No informational edge.** Trading public Google-News RSS 15-30min late on liquid futures (WTI/Brent). The move has already happened. Bot systematically buys post-headline tops. | Trader (CRITICAL), ML eng | CRITICAL |
| B | **0/11 is worse than random** (p≈0.0005). A coin flip yields ~5 wins. Entries are anti-predictive (chasing exhausted moves) + ATR×1.5 hourly stops sit inside the noise band → whipsawed out. | Trader, Mathematician | CRITICAL |
| C | **LLM backtest is invalid by construction.** Kimi K2.6 was trained on the 2026-04/05 dates we'd replay — it knows how each week resolved. Phase 3 "backtest unlock" produces confident false green-lights, biased toward false profitability. Only forward-walk paper trading after the training cutoff is valid. | Backtest (CRITICAL) | CRITICAL |
| D | **Confidence levers the worst signal.** `risk_confidence_scaling_max=2.5` sizes UP on uncalibrated LLM confidence that clusters 0.72-0.85 just above the 0.72 gate. With 0% win rate, this amplifies losers. | ML eng, Mathematician | CRITICAL |
| E | **Statistically under-powered.** Need 60-780 *independent* trades to distinguish edge from noise; we have 11, and 100% energy concentration makes effective N ≈ 1-2. Phase 2 feedback loops would fit noise. | Mathematician, Backtest | HIGH |
| F | **Phantom stop-loss.** The PM prompt spends ~700 tokens/cycle on `stop_loss_pct` + per-symbol floors that `risk.py:247` throws away (it computes the real stop from ATR). The LLM steers a lever that doesn't exist. | ML eng | HIGH/CRITICAL |
| G | **Two real bugs in code shipped today** (see below). | Mathematician | HIGH |

## Bugs in code shipped today (fix regardless of strategy decision)

1. **Vol annualisation still wrong for session-traded instruments** (`indicators.py:_bars_per_year`). Today's fix uses calendar-time median bar spacing → 8760 bars/yr for hourly. But crude trades ~23h/day, equities ~6.5h/day. Median spacing is ~1h (most bars are intraday), so it computes 8760 when the instrument actually has ~1,690 (equity) to ~5,970 (crude) bars/yr → **overstates vol 1.2-2.3×**. Correct: `bars_per_year = n_bars / (calendar_span_days / 365.25)` — counts actual bar density over the actual span, self-calibrating for session hours AND weekends. (The original `√252` hardcode was wrong the *other* way, ~4.9× under, for 24h instruments.)

2. **MACD momentum label inverted** (`signals.py:81`). For `hist <= 0` after the sign-cross check, code returns `"reversing" if growing else "steady"`. A growing-magnitude negative histogram is *accelerating bearish momentum*, not reversing. Correct: within a consistent sign (cross already handled at line 74-76), `accelerating if growing else fading` for both signs.

## Amended strategy — what the experts agree IS salvageable

The codebase (risk gates, reconciliation, observability, the new deterministic signals/correlations) is genuinely good. The path to salvage is a **strategy pivot**, not a rewrite:

1. **Demote the LLM from entry-timing to regime-veto.** Its only legitimate edge is narrative/regime classification (risk-on/off, event-block), NOT picking entry direction on a 15-min news cycle.
2. **Switch from intraday news-chasing to daily-timeframe trend-following / breakout** (e.g. Donchian-20/55 with ATR-trailing stops). Retail's only durable edge is timeframe + patience, not speed/information. Decisions once daily at the close → eliminates the news-latency loss engine entirely.
3. **Cost-vs-edge gate** that the *original design spec already specified but was never built*: refuse any trade where `expected_edge < 2×(spread + slippage + financing)`.
4. **Flatten confidence sizing** (`risk_confidence_scaling_max=1.0`) until a reliability diagram proves calibration.
5. **Forward-walk paper evaluation**, never LLM backtest. Freeze prompt+model, `temperature=0`, log every (input snapshot, raw LLM I/O, decision, outcome), decorrelate instruments, run to ≥60 independent trades, report expectancy + 95% CI + Brier calibration — not raw win-rate.

## Amended phases

### Phase 0+ — Immediate, unambiguous (do now, any strategy)
- 0.6 Fix vol annualisation (session-time bar count). [bug shipped today]
- 0.7 Fix MACD momentum inversion. [bug shipped today]
- 0.8 Flatten confidence sizing `risk_confidence_scaling_max → 1.0`. [stops levering losers]
- 0.9 Wrap `decide()` in try/except with deterministic exit-management fallback (currently a decision failure silently drops the whole cycle incl. exits). [ML eng H2]
- 0.10 Decision-logging schema: flat table `(cycle_id, ts, symbol, signal_pack, corr_context, llm_confidence, direction, entry, stop, tp, fill, exit, pnl, regime, prompt_sha)` + raw LLM prompt/completion archive. Precondition for ALL evaluation. [Backtest F9]

### Phase 1.5 (re-prioritised to HIGH) — Execute the LLM/code contract fix
- LLM emits `{direction, confidence, signal_horizon}` only. Code owns ALL stop/TP/size math. Delete the phantom stop-loss prompt block (~700 tokens/cycle). [ML eng C1]
- Switch to NIM guided JSON decoding (`nvext.guided_json`) — eliminates the reasoning-channel scrape path that can capture a *discarded* hypothesis as the decision. [ML eng C3]

### THE DECISION POINT — live vs paper
4/4 experts say: move to paper/forward-walk NOW; gate live trading on demonstrated positive expectancy. This **conflicts with the operator's stated preference** ("keep live, loss data is informative"). Operator must decide with full information. Options:
- (P) Paper/forward-walk now. Stops the bleed. Build valid eval. Re-deploy live only after ≥60 independent paper trades show +expectancy.
- (L) Keep live but at flat + reduced size (combine 0.8 with `risk_per_trade_pct` cut) while building the pivot. Still bleeds, slower.
- (H) Hybrid: keep live ONLY on the new daily-trend strategy once built; paper everything until then.

### Phase 2 (re-scoped) — Strategy pivot, not feedback loop
- Build the daily-timeframe trend/breakout strategy with LLM-as-regime-veto.
- Cost-vs-edge gate.
- Feedback loop (original Phase 2) deferred until there are enough independent trades for it to not fit noise.

### Phase 3 (re-scoped) — Two-track evaluation
- Track A: deterministic harness for signals/correlations/paper-PnL ONLY (with point-in-time data + realistic fill/slippage/financing model). Never backtests the LLM.
- Track B: forward-walk paper protocol (frozen prompt, temp=0, full logging, decorrelated instruments, ≥60 independent trades, pre-registered success threshold).

### Phase 4 (amended data sources)
- Drop Nitter (dead). Finnhub (60 req/min free) as primary news. FRED-ALFRED *vintages* (only true point-in-time source). CFTC COT with explicit release-lag modeling.

### Rejected / deferred
- Multi-agent voting (Phase 5.1): 3× same base model = correlated errors, not edge. Deferred/dropped.
- LLM backtest (original Phase 3): invalid by construction. Deleted.

## Honest verdict (trader, verbatim sense)
Salvageable as an engineering platform; the current strategy is not. The path: take it off live money (or flatten+shrink), build the forward-walk eval, state ONE falsifiable edge hypothesis (e.g. "daily 20-day oil breakouts with 3×ATR trail are +EV net of cost"), prove it in valid forward-walk, then re-deploy. Accept that £7k is below the size where IG minimums allow real diversification — even a good strategy will be a concentrated high-variance bet until the account is ~3× larger.
