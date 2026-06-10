# ADR 0002: Forward-walk paper eval — cost-aware simulator, deterministic decode, strict verdict

- Status: Accepted
- Date: 2026-06
- Context: Phase 3 of the amended roadmap (`docs/roadmap-amended-2026-05-adversarial.md`)

## Context

The adversarial review established that LLM-in-the-loop strategies CANNOT be
backtested (training-cutoff contamination): the only valid evaluation is a
forward walk on post-cutoff dates. That requires (a) trade outcomes to exist
in paper mode at all, (b) those outcomes to not flatter the strategy, and
(c) the decision process to be reproducible.

The v1 `paper_pnl.py` violated (b) on four counts: entries at mid (no
spread/slippage), stop/TP checked against the last CLOSE only (intrabar
stop-outs invisible — overstates winners), no holding expiry, no financing.
It also had no join key to decision records, making confidence calibration
(Brier) impossible.

## Decision

1. **Cost-aware simulator (`paper_pnl.py` v2, paper_schema=2)**
   - Adverse fills on BOTH legs: `mid ± (spread/2 + slippage)` bps against
     the trade.
   - Intrabar High/Low stop/TP detection over every bar since entry.
   - **Stop-first tie-break** when one bar touches both levels (conservative:
     when in doubt, we lost).
   - `max_holding_hours` expiry at the last bar close (mirrors the live
     executor's holding discipline).
   - Financing deducted: `entry × swap_bps/1e4 × hold_hours/8766`.
   - Outputs per close: `r_multiple`, `pnl_price_units`, `pnl_pct`, `win`,
     `hold_hours`, `financing_cost`, plus `decision_id`/`confidence`.
   - Injectable `bar_fetcher` (tests run without network); legacy v1 rows
     still evaluate (degrade to cost-free).

2. **Deterministic decode** (`config.py`): `llm_temperature=0.0` +
   `llm_seed=7` applied on the shared structured-chat path. Providers that
   ignore `seed` still get greedy decoding.

3. **Strict verdict gate (`forward_eval.py`)**: no expectancy verdict until
   ≥ 60 trades AND the seeded-bootstrap 95% CI clears zero:
   `insufficient_data` → `positive/negative_expectancy` / `indeterminate`.
   Report includes Wilson-CI win rate, Brier + calibration table, per-symbol
   breakdown, symbol-concentration (effective-N warning), and prompt-version
   drift detection (`prompt_sha` must be constant within an eval window).
   CLI: `python -m trading_bot.forward_eval`.

## Alternatives rejected

1. **IG demo fills (DRY_RUN=false on demo)** — realistic fills, but the user
   explicitly chose DRY_RUN=true, and demo fills add broker-side state that
   complicates a clean eval window. Revisit after a positive paper verdict.
2. **TP-first / random tie-break on both-touched bars** — flatters the
   strategy; the eval must be pessimistic where ambiguous.
3. **Eval verdict on n < 60 with wide CIs** — under-powered conclusions are
   exactly the 0/11 mistake the review flagged; refuse to conclude instead.
4. **LLM backtest** — invalid (training contamination); permanently rejected
   in the amended roadmap.

## Consequences

- Paper trades now accumulate joinable, cost-honest outcomes; the eval can
  answer "is there edge?" with explicit uncertainty.
- temperature=0 may slightly reduce decision diversity vs. provider-default
  sampling; acceptable — reproducibility is a hard requirement for eval
  validity.
- A prompt change mid-window invalidates the window (drift warning surfaces
  this); freeze the prompt until n ≥ 60.
- Simulator still approximates: hourly bars (gaps between bars invisible),
  stop fills assumed AT the stop level (no gap-through slippage beyond the
  static buffer). Both biases are conservative-ish but documented.
