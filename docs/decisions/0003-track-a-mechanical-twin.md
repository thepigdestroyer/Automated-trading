# ADR 0003: Track-A — deterministic signals-only twin strategy

- Status: Accepted
- Date: 2026-06
- Context: Phase 3 of the amended roadmap; complements ADR 0002 (forward-walk eval)

## Context

The strategy pivot demoted the LLM to a regime-veto on top of mechanical
daily-trend rules. That demotion raises a falsifiable question: **does the
LLM add anything at all?** If a purely mechanical strategy performs as well
or better, the LLM layer is cost (tokens, latency, failure modes) without
benefit. We need a controlled comparison, not an opinion.

## Decision

Run a second, fully deterministic strategy ("Track-A") in parallel with the
LLM track, in paper mode only:

- **Entry rule** (`track_a.py:decide_track_a`): long when daily trend is up
  AND Donchian-20 breakout up; short when down AND breakout down; both gated
  on `trend_strength >= min_trend_strength` and shorts permission. Nothing
  else — no news, no LLM, no discretion.
- **Stops/TP mirror the live executor** (`risk.py:290`): stop =
  max(ATR14 x atr_stop_multiplier, price x stop_loss_pct_floor, 0.01),
  TP = stop x risk_reward_ratio — in unscaled data-feed price units (the
  paper simulator operates on Yahoo prices, not IG-scaled levels).
- **Same cost-aware simulator** (PaperPnL v2, ADR 0002), separate state file
  (`track_a_pnl.json`) so the books never mix.
- **Guards**: one open Track-A position per symbol; 24 h per-symbol re-entry
  cooldown (a persisting breakout must not machine-gun entries).
- **Confidence = None** on Track-A rows: there is no probabilistic claim to
  calibrate; Brier applies to the LLM track only.
- **CLI comparison** (`python -m trading_bot.forward_eval`): both reports
  side-by-side + `llm_value_add_r` (LLM expectancy minus Track-A expectancy)
  + `comparison_valid` (both tracks past the 60-trade floor).
  Amendment (2026-10): the cash-risk tradeability filter narrows the LLM
  track only (in dry-run neither paper book meets broker minimums; the filter
  keeps the LLM book live-realistic), while Track-A keeps the full universe.
  Track-A entries are therefore tagged `in_llm_universe`, and
  `llm_value_add_r` is computed against `track_a_same_universe` — Track-A
  trades on symbols the LLM could not trade that cycle are excluded. The
  unrestricted figure is reported as `llm_value_add_r_unrestricted`.

## Alternatives rejected

1. **A/B by alternating cycles between tracks** — halves the sample rate of
   both tracks and entangles them with shared cooldowns; parallel books on
   identical inputs is the cleaner controlled comparison.
2. **Backtesting Track-A only and comparing to forward LLM results** —
   Track-A IS backtest-valid (no contamination), but comparing a backtest
   window to a forward window confounds regime with strategy. Forward-walk
   both on the same window. (A Track-A backtest remains available later as a
   sanity check on the rules themselves.)
3. **Track-A submitting real demo orders** — no: it is an instrument of
   measurement, not an execution pathway. Keeping it simulator-only means
   zero broker-side risk and zero interference with the LLM track's account
   state.

## Consequences

- Every cycle in paper mode now runs the mechanical rules over the same
  symbol contexts the LLM sees — same data, same costs, different brain.
- Decision rule for the future: if after both tracks reach n >= 60 the
  `llm_value_add_r` CI does not clear zero, the LLM veto should be removed
  (or inverted if negative) — the comparison is designed to make that call
  mechanical, not emotional.
- Track-A entries are intentionally rarer than LLM entries (textbook
  breakouts only); reaching n = 60 will take longer for Track-A.
