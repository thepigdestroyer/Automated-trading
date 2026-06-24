# ADR 0005: Sequence the quant layers by evidence-gate; stay on Kimi K2.6

- Status: Accepted
- Date: 2026-06-24
- Context: design discussion on adding the five "missing" quant layers + an LLM model bump

## Context

Two requests: (1) sketch implementations for the five mathematical layers the
bot lacks (alpha/return forecast, GARCH vol, covariance/Markowitz/Kelly
portfolio optimisation, HMM regime model, Bayesian calibration feedback);
(2) move the LLM to a newer model ("Opus 4.8 via Anthropic OAuth", then
"Kimi K2.7").

Both requests risk re-introducing the exact failure the adversarial review
diagnosed: complexity added to an **unproven** strategy (current state:
0/60 closed paper trades). The forward-walk verdict gate exists precisely so
the strategy must *earn* added machinery with evidence.

## Decision

### Quant layers — gate by evidence, not sophistication
Layers are split into Tier 1 (edge-independent: vol forecast, regime model,
calibration feedback — improve risk control + measurement, safe to build) and
Tier 2 (edge-dependent: covariance/Kelly optimisation, alpha forecast —
allocate capital to an edge, dangerous before proof). Build order:
backtester → A vol → B regime (shadow) → **[wait for first forward-walk
verdict]** → C calibration → D risk-parity → **[only if edge proven]** →
E factors → D Kelly. Full sketch in `docs/roadmap-quant-layers-2026-06.md`.
Nothing in Tier 2 is built before a positive verdict.

### LLM model — stay on Kimi K2.6
- Anthropic Opus 4.8 rejected: API pricing (~$1000s/month at the bot's ~4
  calls/cycle × ~40k-token, 24/7 volume) violates the zero-cost constraint;
  Max-subscription OAuth is rate-limited + ToS-grey for a headless trading
  daemon + needs a token-refreshing proxy sidecar.
- Kimi K2.7 rejected: **no general K2.7 exists.** NVIDIA NIM (free) tops out
  at `kimi-k2.6`; OpenRouter only has `kimi-k2.7-code` (coding-specialised,
  pay-per-token, probable downgrade for macro reasoning + JSON). Verified
  against both catalogues 2026-06-24.
- **Stay on `moonshotai/kimi-k2.6`** — the latest *general* Kimi, free on NIM,
  already running. A model swap is eval-safe only because we are at 0/60; once
  trades accumulate, the model must stay frozen within a forward-walk window
  (forward_eval has prompt/model drift detection).

## Alternatives rejected

1. **Build all five layers now** — repeats the 0/11 over-engineering mistake;
   adds parameters/overfitting surface with zero added evidence.
2. **Kelly/mean-variance before a proven edge** — mis-estimated μ → over-bet →
   ruin. Only risk-parity (edge-agnostic) is permitted pre-verdict.
3. **ML alpha model first** — factors (momentum/carry/value) before any fitted
   model; ML is a later, separately-gated decision.
4. **Opus via API key** — breaks zero-cost.
5. **kimi-k2.7-code** — wrong specialisation (code, not reasoning) + paid.

## Consequences

- The expensive layers wait on the evidence the forward-walk is accumulating;
  the cheap, edge-independent ones (vol forecast, EIA data) can proceed when
  the user chooses.
- Highest-value near-term, philosophy-aligned add identified: **EIA energy
  data** (fundamental, free, fits the `macro_data.py` pattern).
- Latent note: the local `.env` is stale (`kimi-k2.5` + OpenRouter base);
  harmless (OCI is authoritative) but would mislead a local run.
