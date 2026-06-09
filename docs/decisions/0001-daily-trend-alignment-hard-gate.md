# ADR 0001: Daily trend alignment is a hard executor gate, not an LLM hint

- Status: Accepted
- Date: 2026-05
- Context: Phase 2 strategy pivot (amended roadmap `docs/roadmap-amended-2026-05-adversarial.md`)

## Context

The 4-expert adversarial review concluded the bot's intraday news-chasing has no
durable edge, and that the only edge available to a retail CFD account is
timeframe + patience: daily trend-following / breakout. We added a `DailyTrend`
classifier (`trading_bot/daily_trend.py`) and needed to decide HOW trend
alignment is enforced.

## Decision

Trend alignment is enforced as a **hard, deterministic gate in the executor**
(`risk.py:_trend_alignment_block`), not as a soft instruction to the LLM.

- Reject any BUY when `daily_trend.direction == "down"`.
- Reject any SELL when `daily_trend.direction == "up"`.
- Reject any entry when `trend_strength < min_trend_strength` (chop).
- Reversals/closes are exempt (only NEW entries are gated).
- **Degrade-open**: a missing `daily_trend` (e.g. Yahoo daily-fetch failure)
  does NOT block — trading continues without the trend filter rather than
  halting. Rationale: a data outage should not silently flatten the strategy;
  the other gates (cost-vs-edge, ATR stop, guardrails) still apply.
- The LLM is *also* told the rule (prompt guidance in `openai_decision.py`) so
  it stops wasting recommendations that will be discarded — but the prompt is
  advisory; the executor is authoritative.

Config: `require_trend_alignment=True`, `min_trend_strength=20.0`.

## Alternatives rejected

1. **LLM-only enforcement (soft hint).** Rejected: the same review showed the
   LLM cannot be trusted to apply numeric thresholds consistently, and an
   un-gated LLM previously levered the worst signals. A deterministic gate is
   auditable and cannot be argued out of.
2. **Block on missing daily data (degrade-closed).** Rejected: a transient
   Yahoo outage would halt all trading. Degrade-open keeps the other risk
   controls in force while losing only the trend filter.
3. **Gate on intraday trend.** Rejected: the whole thesis of the pivot is that
   intraday has no edge. The dominant timeframe must be daily.

## Consequences

- Counter-trend entries are impossible while the gate is on, by construction.
- Mean-reversion is only reachable when daily direction is "sideways".
- Adds one daily frame classification per symbol per cycle, reusing the daily
  frame already downloaded for higher-TF fields (no extra network call).
- New failure surface is contained: classifier is pure + defensive (short/NaN
  frames → neutral verdict), and the gate degrades open.
