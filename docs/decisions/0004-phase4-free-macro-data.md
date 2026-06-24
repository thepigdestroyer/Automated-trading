# ADR 0004: Phase 4 free macro data — FRED/ALFRED, Finnhub, CFTC COT

- Status: Accepted
- Date: 2026-06
- Context: Phase 4 of the amended roadmap (`docs/roadmap-amended-2026-05-adversarial.md`)

## Context

The strategy needs macro regime context (rates, curve, USD, positioning) but
the project is zero-cost-only. The amended roadmap named three free sources
(Finnhub, FRED/ALFRED, CFTC) and dropped Nitter (unreliable/blocked).

## Decision

A `macro_data.py` module with three defensive clients + a `MacroContextClient`
aggregator, mirroring the `vix.py` template (settings flag, injectable
fetcher, never raises into the cycle).

1. **FRED/ALFRED** (`FredClient`): latest observation per configured series,
   with `realtime_start`/`realtime_end` pinned to "now" for point-in-time
   (vintage) correctness, and `staleness_days` surfaced so the LLM cannot
   treat a month-old CPI print as today's news. Default series: DGS10,
   T10Y2Y, DTWEXBGS, DFF, DCOILBRENTEU.
2. **Finnhub** (`FinnhubClient`): general market news count + top headlines as
   a corroboration signal (the dedicated headline feed remains primary).
   A premium-gated endpoint (401/403) degrades to a note, not an error.
3. **CFTC COT** (`CftcClient`): net non-commercial (speculative) positioning
   via the public Socrata API (no key), with `publication_lag_days` surfaced
   (COT is released Friday for the prior Tuesday — lagged, contrarian context
   only, never a timing trigger).

Wiring: fetched once per cycle behind a TTL cache
(`macro_context_refresh_minutes`, default 180) because the data updates
daily/weekly — re-fetching every 15-min cycle would waste the free Finnhub
60 req/min budget for no new signal. Attached to the decision payload at
`guardrail_context.macro_context`. Prompt guidance frames all of it as
slow-moving context, explicitly NOT a timing trigger.

Live-verified against the real endpoints before shipping (DGS10 4.51%,
T10Y2Y +0.34, Finnhub 100 headlines, CFTC GOLD net +180,220 / 8-day lag) —
the parser handles the real response shapes, not just the mocked fixtures.

## Alternatives rejected

1. **Fetch every cycle** — wastes the Finnhub free budget; macro data has no
   intra-cycle news. TTL cache instead.
2. **Block trades when macro data is missing** — no: macro is supplementary
   context. Absence degrades to a note; the trend setup still governs.
3. **Finnhub economic calendar / COT-via-Finnhub** — premium-gated on the
   free tier; we take the calendar from the existing econ-calendar client and
   COT direct from CFTC (genuinely free). The 401/403 path is handled so a
   future tier change won't crash.
4. **Default FRED call (realtime = latest revision)** — would silently use
   revised data. We pin realtime to "now" so the value is what was actually
   knowable at decision time (matters when this feeds any replay/eval).
5. **Nitter** — dropped per the amended roadmap (unreliable/blocked).

## Consequences

- Secrets: `FRED_API_KEY` / `FINNHUB_API_KEY` live in `.env` only (never
  committed; the memory pre-commit hook would block them regardless). Free
  tier, revocable, low blast radius.
- New outbound dependencies (stlouisfed.org, finnhub.io, cftc.gov); all
  timeout-bounded (`macro_http_timeout_seconds`, default 8s) and failure-
  isolated per source.
- COT market names with commas ("CRUDE OIL, LIGHT SWEET") require a `;`/`|`
  delimiter when overriding `CFTC_MARKETS` via env (comma-split would break
  them); documented in the config validator.
