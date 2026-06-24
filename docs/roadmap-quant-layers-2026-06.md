# Roadmap: the five missing quantitative layers (2026-06)

Status: **design sketch — not yet scheduled.** Gated on the forward-walk verdict.

## Governing principle

The 4-expert adversarial review concluded the strategy has **no proven edge**
and that the failure mode to avoid is *over-engineering an unproven system*
(the original 0/11). Therefore these layers are sequenced by an **evidence
gate**, not by sophistication. Tier 1 improves risk control + measurement and
is safe to build regardless of edge. Tier 2 allocates capital to an edge and
is dangerous to build before that edge is demonstrated.

**Current state: 0/60 closed paper trades. Build nothing in Tier 2 until the
first positive forward-walk verdict.**

## Prerequisite — vectorised backtester (`backtest/engine.py`)

Three layers below (vol forecast, regime, alpha factors) are *backtest-valid*
(no LLM training-cutoff contamination), so they validate on history far faster
than the LLM forward-walk. Build this harness first: bar-replay, the existing
cost model (`risk._round_trip_cost_price_units` + `paper_pnl` fills), and
metrics (Sharpe, max-DD, turnover, t-stat of factor premium, QLIKE for vol).

---

## Tier 1 — edge-INDEPENDENT (safe to build now)

### Layer A — Volatility forecast (`vol_forecast.py`)
- **Math**: EWMA (RiskMetrics) `σ²ₜ = λσ²ₜ₋₁ + (1−λ)r²ₜ₋₁`, λ≈0.94 → upgrade to
  GARCH(1,1) `σ²ₜ = ω + αr²ₜ₋₁ + βσ²ₜ₋₁` (MLE via `arch`, MIT). Forecast σ over
  the expected holding horizon.
- **Plug-in**: replaces realized `volatility_20d` in `risk.py` sizing; feeds
  `paper_pnl` expected adverse excursion. Vol-targeting is strictly improved by
  a better σ.
- **Validate**: out-of-sample QLIKE / log-lik vs realized-σ.
- **Gate**: none. Cleanest immediate win.

### Layer B — Statistical regime model (`regime_model.py`)
- **Math**: 3-state Gaussian HMM (`hmmlearn`, BSD), obs `[return, realized vol,
  VIX term-structure]`; Baum-Welch fit on rolling history, forward-filter the
  posterior state probability. States ≈ {quiet-trend, volatile-chop, crisis}.
- **Plug-in**: replaces the LLM string-label + VIX flag; gates the trend filter
  (trend-trade only in trend-states), cuts size in crisis, enters LLM context.
- **Validate**: recover synthetic switch-points; min-dwell constraint vs
  relabel flicker.
- **Gate**: shadow-mode first (log, don't act), compare to outcomes, then act.

### Layer C — Bayesian calibration feedback (`calibration.py`)
- **Math**: per signal-bucket (trend-strength decile × confidence band),
  Beta-Binomial posterior `Beta(α₀+wins, β₀+losses)`, posterior mean shrunk to
  base rate. Map calibrated p̂ → fractional-Kelly (capped) sizing. Brier
  reliability decomposition discounts confidence when miscalibrated.
- **Plug-in**: the *explicitly deferred* "feed calibration into sizing" —
  replaces flat `confidence_multiplier=1.0`. `forward_eval` already computes
  the Brier/calibration inputs.
- **Gate**: per-bucket min sample (~20); sequence **after the first verdict**.

---

## Tier 2 — edge-DEPENDENT (gate HARD on a positive verdict)

### Layer D — Covariance + portfolio optimisation (`portfolio_opt.py`)
- **Math**: Σ via Ledoit-Wolf shrinkage (extend the correlation pipeline
  Pearson→covariance). Two regimes:
  - *no edge (now)*: min-variance / risk-parity `w ∝ Σ⁻¹𝟙`, leverage-capped —
    edge-agnostic concentration reduction. Safe-ish.
  - *proven edge (later)*: fractional-Kelly / mean-variance `w ∝ Σ⁻¹μ` with μ
    from Layer E. **Full Kelly without reliable μ = ruin** — cap at ¼–½ Kelly.
- **Gate**: risk-parity variant after Tier 1; μ-based MVO/Kelly gated on Layer E
  + positive verdict.

### Layer E — Alpha / return forecast (`alpha_model.py`) — the over-fitting trap
- **Math, simplest-first**: documented retail-robust factors before any ML —
  time-series momentum (trailing 3–12m), cross-sectional momentum (rank vs
  universe), carry (futures basis/roll-yield), value (deviation from long-run
  anchor). Combine by z-score + inverse-vol weight — **not a fitted model** —
  until years of data exist. ML (gradient-boosting + nested walk-forward CV) is
  a much later, separate decision.
- **Plug-in**: provides μ for Layer D; a quantitative prior under the LLM veto.
- **Gate (hardest)**: factors are backtest-valid, so each must independently
  pass backtest (Sharpe/DD/turnover/t-stat) → paper forward-walk → then live
  sizing. **No factor sizes anything until it clears the same 60-trade verdict
  the LLM track faces.** Do not start until the base trend+veto strategy has a
  positive verdict, or run as a fully parallel research track that proves
  itself before any capital.

---

## Recommended build order

```
backtester → A (vol) → B (regime, shadow)
  → [WAIT for first forward-walk verdict]
  → C (calibration) → D-risk-parity
  → [ONLY if edge proven] → E (factors) → D-Kelly
```

---

## Information sources to add (curated for this macro CFD book)

Bottleneck is *proving edge*, not data volume — prefer **fundamental
primary-source** signals over more sentiment feeds (which add overfitting
surface). All free-tier; verify current limits at signup.

| Priority | Source | Free? | Signal | Plug-in |
|---|---|---|---|---|
| 1 | EIA API | key | Weekly crude/NatGas inventories — #1 fundamental for the energy book | `macro_data.py` |
| 2 | GDELT | no key | Quantified geopolitical event tone (Iran/energy) | new `events.py` |
| 3 | Central-bank RSS (Fed/ECB/BoE/BoJ) | yes | Primary rate-policy signal for USD/rates/gold | `news.py` |
| 4 | MOVE index + CBOE put/call + breadth | yes (Yahoo/CBOE) | Bond-vol + positioning extremes | `vix.py` sibling |
| 5 | Marketaux / Tiingo news | tier | Entity sentiment to corroborate Finnhub | `macro_data.py` |
| 6 | ECB SDW / BoE / US Treasury FiscalData | yes | Non-US rates + auctions for FX/curve | `macro_data.py` |
| 7 | Baker Hughes rig count | yes | Oil-supply lead indicator | `events.py` |

**Single highest-value add: EIA** — largest information gap for an
energy-heavy book, a hard fundamental (not sentiment), drops straight into the
`macro_data.py` defensive-client pattern.

## LLM model note (2026-06-24)

Investigated Kimi K2.7: **no general K2.7 exists.** NVIDIA NIM (free endpoint)
tops out at `moonshotai/kimi-k2.6`; OpenRouter only has `kimi-k2.7-code` (a
coding-specialised variant, pay-per-token, likely a downgrade for macro
reasoning). Decision: **stay on K2.6** (latest general Kimi, free, already
running). Re-check the NIM catalog when a general K2.7 ships.
