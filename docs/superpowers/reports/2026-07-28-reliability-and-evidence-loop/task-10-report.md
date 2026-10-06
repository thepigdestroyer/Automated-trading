# Task 10 report: Multi-provider news + real-time macro wire

**Status:** DONE_WITH_CONCERNS (concerns are operational findings about the
upstream feeds, not defects in the code — see §8)
**Commit:** `329dc37` — `feat(news): multi-provider aggregation + FinancialJuice realtime wire + Fed press`
**Branch:** `feat/reliability-and-evidence-loop`
**Full suite:** `8 failed, 850 passed` vs baseline `8 failed, 814 passed` — same 8 pre-existing macOS artifacts, 0 regressions, +36 new tests.

---

## 1. Changes made

| File | Change |
|---|---|
| `trading_bot/news_sources.py` | **new.** `LATENCY_RANK`, `latency_rank()`, `NewsProvider` protocol, `RssWireProvider`, `GoogleNewsProvider`, `NewsAggregator`, `build_market_news_providers()`, `_http_fetcher()` |
| `trading_bot/models.py` | `Headline.latency_class: Literal["realtime","official","aggregator"] \| None = None` |
| `trading_bot/config.py` | `financialjuice_enabled/_feed_url/_max_items`, `fed_press_enabled/_feed_url/_max_items`, `news_fetch_timeout_seconds` (exactly the brief's values) |
| `trading_bot/bot.py` | new `_fetch_market_headlines()`; `run_cycle` line 669 now calls it instead of `self.news_client.fetch_market_headlines()` |
| `trading_bot/news.py` | added `normalize_title_for_dedup()` — canonical home for the dedup rule |
| `trading_bot/openai_decision.py` | `_normalize_title_for_dedup` is now an alias importing the shared rule (behaviour identical; removes a second copy that could drift from the merge layer) |
| `tests/test_news_sources.py` | **new.** 36 tests |
| `tests/conftest.py` | sets `FINANCIALJUICE_ENABLED=false` / `FED_PRESS_ENABLED=false` for all unit tests |

Not touched: `ig_bridge.py`, `session_manager.py`, `supervisor/**`. No risk
default and no `dry_run` value changed. `GoogleNewsClient` is unmodified — it
is now wrapped by `GoogleNewsProvider` and tagged `aggregator`.

### Why conftest disables the feeds

Both flags ship **ON**. But `run_cycle()` is executed by 8 existing tests, and
with the flags on those tests would have made real HTTP calls to
financialjuice.com and federalreserve.gov. Turning them off in `conftest.py`
keeps the whole suite offline (full run is 5.2s). The shipped defaults are not
left unasserted: `test_both_verified_feeds_are_enabled_by_default_in_production`
reads `Settings.model_fields[...].default` directly, and the builder/provider
behaviour is covered with injected fetchers.

### Design decisions beyond the brief

1. **Scrubbing is enforced twice** — in `RssWireProvider` and again in
   `NewsAggregator._normalise`. A future provider that forgets to scrub still
   cannot inject text into a prompt. Both sites are mutation-tested.
2. **`source_priority` is left `None` for wire providers.** `bot.py:1339`
   (`_count_breaking_high_priority`) counts headlines with `source_priority <= 2`
   to invalidate the market-research / news-synthesis LLM caches. Stamping 40
   FinancialJuice squawk lines as top-tier would blow the invalidation
   threshold every single cycle and multiply token spend. `latency_class`
   carries the freshness signal instead. No cache behaviour changed.
3. **Latency classes are interleaved, not strictly sorted** — see §5. This was
   a defect I found by measuring, not by reasoning.

---

## 2. TDD: the tests failed first

```
$ .venv/bin/pytest tests/test_news_sources.py -q
27 failed, 1 passed in 0.22s

$ .venv/bin/pytest tests/test_news_sources.py -q 2>&1 | grep -c ModuleNotFoundError
27
E       ModuleNotFoundError: No module named 'trading_bot.news_sources'
```

The 1 passing test was `test_prompt_budget_settings_are_unchanged`, which
asserts pre-existing config — correctly passing before any implementation.

After Steps 3–6:

```
$ .venv/bin/pytest tests/test_news_sources.py -q
28 passed in 0.17s
```

One of my own tests was wrong and I fixed the **test**, not the code:
`test_duplicate_title_across_providers_prefers_realtime_copy` originally used
`"Fed cuts rates by 25bps!"` vs `"fed cuts rates by 25 bps"` and asserted they
collapse. They do not — the repo's normalisation is case/punctuation/whitespace
insensitive, not token-splitting, so `25bps` != `25 bps`. Making the normaliser
"smarter" would have increased false-positive dedup (silently dropping distinct
stories), so I corrected the test to the real semantics and documented the limit
in its docstring.

The later interleave work was also TDD'd:

```
$ .venv/bin/pytest tests/test_news_sources.py -q -k "monopolise or interleave"
E       AssertionError: assert ['flash 0', '...story 0', ...] == [...]
E         At index 1 diff: 'flash 1' != 'story 0'
1 failed, 1 passed
# ... implement _interleave_by_latency ...
$ .venv/bin/pytest tests/test_news_sources.py -q
36 passed in 1.60s
```

---

## 3. Mutation table (test-integrity requirement)

Each mutation applied to the committed implementation, `pytest
tests/test_news_sources.py -q` run, then reverted (verified byte-identical with
`diff` against a pre-mutation copy; `grep MUTANT` over the repo returns
nothing).

| # | Mutation | Result | Killed by |
|---|---|---|---|
| A | Removed `try/except` around the provider call in `NewsAggregator` | **KILLED** (7) | `test_one_failing_provider_does_not_kill_the_others`, `..._rss_provider_whose_fetcher_raises_is_isolated_end_to_end`, `..._every_provider_failing_returns_empty_and_never_raises`, `..._provider_failure_emits_event_with_provider_name`, `..._broken_event_logger_does_not_break_the_cycle`, `..._default_http_fetcher_raises_on_http_error...`, `..._bot_survives_a_total_google_news_outage` |
| B | Inverted latency preference in dedup (`<` → `>`) | **KILLED** (3) | `test_duplicate_url_prefers_lower_latency_source`, `..._duplicate_title_across_providers_prefers_realtime_copy`, `..._official_beats_aggregator_but_loses_to_realtime` |
| C1 | Dropped `scrub_headline` in `RssWireProvider` | **KILLED** (1) | `test_rss_wire_provider_scrubs_prompt_injection` |
| C2 | Dropped `scrub_headline` at the merge boundary | **KILLED** (1) | `test_aggregator_scrubs_titles_from_providers_that_did_not` |
| D | Ignored `max_items` | **KILLED** (1) | `test_rss_wire_provider_respects_max_items` |
| E | Dropped the title-prefix strip | **KILLED** (2) | `test_rss_wire_provider_parses_and_strips_prefix`, `..._financialjuice_provider_strips_its_title_prefix_when_built_from_settings` |
| **F** | **Reverted `run_cycle` to `self.news_client.fetch_market_headlines()`** | **SURVIVED → fixed → KILLED** | see below |
| G | Dropped latency ordering (`return kept`) | **KILLED** (1) | `test_realtime_items_come_first_so_the_prompt_budget_favours_them` |
| H | Stopped stamping the provider's `latency_class` onto unstamped headlines | **KILLED** (2) | `test_aggregator_stamps_provider_latency_class_when_headline_lacks_one`, `..._bot_fetches_market_headlines_through_the_aggregator` |
| I | Ignored `financialjuice_enabled` (`if True:`) | **KILLED** (2) | `test_build_market_news_providers_honours_disable_flags`, `..._bot_survives_a_total_google_news_outage` (suite time also jumped 0.7s → 26.9s, i.e. the offline guarantee depends on this path) |
| J | Replaced the FinancialJuice URL with an unverified guess (ECB) | **KILLED** (2) | `test_build_market_news_providers_uses_only_the_two_verified_urls`, `..._both_verified_feeds_are_enabled_by_default_in_production` |
| K | Replaced the interleave with a strict latency sort | **KILLED** (1) | `test_no_single_source_can_monopolise_the_prompt_budget` |

### The survivor (F) and its fix

`bot.py`'s call site was reverted to the old single-source line and **all 33
tests still passed.** My bot-level tests called `bot._fetch_market_headlines()`
directly, so nothing pinned that `run_cycle` actually uses it — the provider
abstraction could have been dead code in production while the suite stayed
green. This is precisely the failure mode flagged in the task.

Fix: added `test_run_cycle_gets_its_headlines_from_the_multi_provider_path`,
which reuses the existing `_make_cycle_bot` harness, runs the real `run_cycle`,
and asserts (a) `_fetch_market_headlines` was called once, (b)
`news_client.fetch_market_headlines` was **not** called, and (c) the merged
headline actually arrives in `decide(market_headlines=...)` with its
`latency_class` intact — so the assertion is not vacuous. Verified it fails
against mutant F and passes on the restored code.

---

## 4. Live smoke test (real network)

### Run 1 — per-provider, `now = 2026-07-30T12:36:27Z`

```
provider=financialjuice  latency=realtime  max_items=40
url=https://www.financialjuice.com/feed.ashx?xy=rss
items returned: 40
  [2026-07-30T12:35:17+00:00] (age 0.02h) 'BoE Gov. Bailey: Market conditions important part of the QT judgment.'
      source=FinancialJuice domain=financialjuice.com latency=realtime
  [2026-07-30T12:33:05+00:00] (age 0.06h) 'BoE Gov. Bailey: It is not wise to wait for all second-round effects to emerge.'
  [2026-07-30T12:32:12+00:00] (age 0.07h) 'US Sentiment: Mixed Earnings Reaction, Warsh Credibility Comes Into Question - FJElite'

provider=fed_press  latency=official  max_items=10
url=https://www.federalreserve.gov/feeds/press_all.xml
items returned: 10
  [2026-07-29T18:00:00+00:00] (age 18.61h) 'Federal Reserve issues FOMC statement'
      source=Federal Reserve domain=federalreserve.gov latency=official
  [2026-07-16T18:00:00+00:00] (age 330.61h) 'Agencies issue joint statement on handling of highly sensitive information during bank examinations'
  [2026-07-16T15:00:00+00:00] (age 333.61h) 'Federal Reserve Board issues enforcement action with former chief lending officer of Heritage State Bank'
```

**Item counts: FinancialJuice 40 (capped from 100 by `max_items=40`), Fed press 10.
Neither is zero; both are shipping.** Newest FinancialJuice item was 70 seconds
old — the sub-minute granularity the task is after. Prefix stripping and
timestamp parsing both confirmed on live data.

### Run 2 — through `NewsAggregator`, one fetch per provider (the production path)

```
now (UTC): 2026-07-30T12:37:45.238295+00:00
EVENT news_provider_empty: {'provider': 'google_news', 'latency_class': 'aggregator'}
merged count: 50  by class: {'realtime': 40, 'official': 10}
first 6 in prompt order:
  realtime   [2026-07-30T12:36:49+00:00] 'Microsoft ponders an open-weight AI, as China models gain ground - Nikkei. $MSFT'
  realtime   [2026-07-30T12:35:17+00:00] 'BoE Gov. Bailey: Market conditions important part of the QT judgment.'
  realtime   [2026-07-30T12:33:05+00:00] 'BoE Gov. Bailey: It is not wise to wait for all second-round effects to emerge.'
  realtime   [2026-07-30T12:32:12+00:00] 'US Sentiment: Mixed Earnings Reaction, Warsh Credibility Comes Into Question - F'
  realtime   [2026-07-30T12:30:48+00:00] 'US GDP Deflator SA Advance Actual 6.3% (Forecast -, Previous 3.6%)'
  realtime   [2026-07-30T12:30:48+00:00] 'US PCE Prices Advance Actual 5.1% (Forecast -, Previous 4.6%)'
prefix leaks: 0
```

(Google was deliberately stubbed empty in the smoke script, which is why
`news_provider_empty` fires — the zero-item path working on real telemetry.)

### Failure isolation, observed live twice — not simulated

Both of these happened for real during smoke testing, and in both cases the
cycle continued with the surviving providers:

```
news provider financialjuice failed: 429 Client Error: Too Many Requests for url: https://www.financialjuice.com/feed.ashx?xy=rss
news provider fed_press failed: ('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))
```

The 429 was self-inflicted (I fetched the feed twice within one second in run 1;
production fetches once per cycle). The Fed `ConnectionResetError` was
spontaneous and transient — the same URL returned 10 items minutes earlier and
later. This is the strongest evidence in the report that requirement 1 holds:
two different real upstream failures, neither propagated.

---

## 5. Prompt budget: measured, not assumed

Live fetch of both feeds plus a synthetic 6-item Google set, run through the
**unchanged** `compress_headlines_for_prompt` with production settings:

**Before the interleave fix (strict latency ordering):**
```
merged: 46  {'realtime': 40, 'aggregator': 6}
after compress (max_items=20, max_age=12h, byte_cap=8192): 20
composition: {'realtime': 20}
sources: {'FinancialJuice': 20}
```

All 20 market slots went to FinancialJuice; Google News was pushed out
entirely. That trades one single-source dependency for another — the opposite
of this task's purpose, and it means one misbehaving feed could own 100% of the
prompt. Fixed by round-robining the latency classes
(`NewsAggregator._interleave_by_latency`), which keeps the freshest item first
but guarantees every class a share of the fixed budget.

**After:**
```
merged: 56  {'realtime': 40, 'official': 10, 'aggregator': 6}
after compress (max_items=20, max_age=12h, byte_cap=8192): 20
composition: {'realtime': 14, 'aggregator': 6}
sources: {'FinancialJuice': 14, 'Reuters': 6}
title+snippet bytes: 2261 / 8192
```

`headline_prompt_max_market_items=20`, `headline_prompt_max_per_symbol=8` and
`headline_prompt_max_total_bytes=8192` are **unchanged** and asserted by
`test_prompt_budget_settings_are_unchanged`. The byte cap is at 28% — dedup and
the 12h age gate absorb the extra volume, exactly as the brief predicted.

One honest caveat: the new `latency_class` field adds ~25 bytes per headline to
the serialised payload (`full serialised bytes: 10895` for 20 headlines). The
8192 cap governs title+snippet only, as it always has, so no configured budget
was raised — but the shipped JSON is marginally larger than before.

---

## 6. Test commands and output

```
$ .venv/bin/pytest tests/test_news_sources.py -q
36 passed in 1.60s

$ .venv/bin/pytest tests/test_news.py tests/test_news_memory.py tests/test_pm_prompt_dedup.py -q
(all pass — GoogleNewsClient untouched, dedup refactor behaviour-identical)

$ .venv/bin/pytest -q
8 failed, 850 passed in 5.22s
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick
```

Baseline before this task was `8 failed, 814 passed` with **the identical 8
names** (Linux-only `/proc/self/status` and `/bin/true` platform artifacts in
watcher/memory code). Not touched, not fixed, per instruction. Suite runtime is
unchanged at ~5s, confirming no test acquired a network dependency.

---

## 7. Self-review notes

- **Requirement 1 (isolation):** every provider is called in its own
  `try/except Exception`; raises, timeouts, HTTP errors, malformed bodies and
  zero-item returns are all logged and skipped, with
  `news_provider_failure` / `news_provider_empty` events. The event logger
  itself is wrapped so telemetry cannot become a crash path. Proven by mutant A
  (7 kills) and by two real upstream failures during smoke testing.
- **Requirement 2 (no network in unit tests):** `RssWireProvider` takes
  `fetcher`; the default HTTP path is exercised only via a monkeypatched
  `requests.get`. Full suite runtime 5.2s. Mutant I incidentally proved the
  offline guarantee is load-bearing (26.9s when the disable flag is ignored).
- **Requirement 3 (scrubbing):** enforced at the provider *and* at the merge
  boundary; mutants C1 and C2 each killed independently.
- **Requirement 4 (budget):** no cap changed; measured 2261/8192 bytes with all
  three providers live.
- **Requirement 5 (latency dedup):** URL-hash and normalised-title dedup, both
  keeping the lowest `latency_rank`. Mutant B killed by 3 tests.
- **Dedup rule de-duplicated:** the brief allowed an "equivalent" normaliser. I
  moved the rule into `news.py` and had `openai_decision` import it instead,
  because two copies of a near-duplicate rule in the merge layer and the prompt
  layer would eventually disagree, and the disagreement would be invisible.
- **Weakest remaining point:** title dedup is textual, not semantic. A
  rewritten headline about the same event survives as a separate item, so
  Google/FinancialJuice overlap is only partly collapsed. Deliberate — a fuzzier
  matcher risks silently dropping distinct stories, which is the worse error.
- Verified no mutation residue: `grep MUTANT` over the repo is empty and both
  mutated files `diff` clean against pre-mutation copies.

---

## 8. Concerns and recommended follow-ups

1. **FinancialJuice rate-limits (HTTP 429).** Two fetches within one second
   were rejected. Production fetches once per cycle so this should not trigger,
   but a short cycle interval, a restart loop, or any future second caller
   would silently lose the realtime feed (gracefully — logged and skipped). A
   small TTL cache in front of `RssWireProvider` would remove the risk.
2. **federalreserve.gov is intermittently flaky** from this machine
   (`ConnectionResetError(54)` once, fine on two other attempts). Isolated
   correctly; worth watching in the journal via `news_provider_failure`.
3. **The Fed feed contributes almost nothing most of the time.** All 10 items
   were >12h old in the live sample, so the pre-existing
   `headline_prompt_max_age_hours=12` gate drops them — the FOMC statement
   itself was 18.6h old and did not make the prompt. It only contributes within
   12h of a release, which is when it matters, but expect it to look like a
   dead provider on most cycles. Do not "fix" that by raising the age gate
   without a separate decision.
4. **Additional sources worth verifying before adding** (deliberately NOT
   shipped, since an unverified URL is a permanently silent provider): the EIA
   open API with a free key (weekly petroleum/natural-gas status — directly
   relevant to WTI/BRENT/NATGAS), ECB via `https://www.ecb.europa.eu/rss/press.xml`
   (the `.html` variant failed), Bank of England via its news-by-type RSS
   endpoints, CME/ICE holiday-and-notice feeds, and Reuters/Bloomberg terminal
   feeds (paid). Each needs a live probe first.
5. **`source_priority` is `None` for wire items**, so FinancialJuice and Fed
   headlines do not participate in the `_count_breaking_high_priority` cache
   invalidation counter. That is the intentionally conservative choice (see
   §1), but it means a genuine FOMC surprise arriving only on the wire will not
   by itself invalidate the market-research cache. Worth a follow-up task that
   weights `latency_class` into that counter with a tighter threshold.
6. **The prompt does not explain `latency_class` to the model.** The field is
   serialised into every headline (via `model_dump`), and the existing prompt
   text only tells the model to weight `source_priority`. Adding one line to
   the market/PM prompts telling it to prefer `realtime` over `aggregator`
   would realise the rest of this task's value; it was out of scope here
   (`openai_decision.py` prompt text was not in the brief's file list).

---

## Fix pass (review findings)

**Status:** all 12 findings fixed (C1, I2-I6, M7-M12). One item explicitly
scoped out and named in M16 below.
**Full suite:** `8 failed, 871 passed` vs the pre-fix `8 failed, 850 passed` —
same 8 pre-existing macOS artifacts, 0 regressions, +21 tests.
**Files:** `trading_bot/news_sources.py`, `trading_bot/config.py`,
`supervisor/watcher.py` (one default only, per authorisation),
`tests/test_news_sources.py`, `tests/test_news.py`,
`tests/test_supervisor_rules.py`. `bot.py` needed no change — C1 was a missing
test, not a missing behaviour. `ig_bridge.py`, `session_manager.py`,
`GoogleNewsClient`, the two feed URLs, the prompt budget, every risk default and
`dry_run` are untouched.

### C1 — the whole fan-out could be reverted to Google-only (`bot.py:625`)

Reproduced the survivor exactly: `providers = [GoogleNewsProvider(self.news_client)]`
(with the import added so it is a faithful revert and not a `NameError`) →
**9 failed, 869 passed**, i.e. only my new test caught it. Before the fix that
same mutant was `8 failed` = green.

Fix: `test_run_cycle_really_fetches_both_wire_feeds_and_merges_them`. It enables
both flags locally (`_make_cycle_bot(..., financialjuice_enabled=True,
fed_press_enabled=True)` — kwargs beat conftest's env), runs the **real**
`run_cycle`, and asserts:

- `requested == [financialjuice url, fed url]` — both feeds actually requested,
  in fetch order;
- all three sources arrive in the `decide(market_headlines=...)` payload with the
  right `source` and `latency_class` (`FinancialJuice`/`realtime`,
  `Federal Reserve`/`official`, `Reuters`/`aggregator`);
- the FinancialJuice title is prefix-stripped (`US CPI 3.1% vs 3.3% expected`),
  proving the real `RssWireProvider` parse ran, not a stub;
- the wire leads prompt slot 0.

Offline: it patches `requests.get`, the module's only HTTP entry point, and
`bodies[url]` raises `KeyError` for any URL other than the two verified ones, so
a future guessed feed URL fails loudly rather than silently going to the network.
Confirmed with a socket trip-wire (`socket.socket.connect` raising):
`tests/test_news_sources.py tests/test_news.py` → **66 passed in 1.26s**, no
socket attempted.

Deliberately *not* done: removing the conftest flags. They keep the other ~8
`run_cycle` tests offline; the gap was one missing positive test, not the
defaults.

### I2 — `feedparser.parse()` on a `str` = SSRF / local-file-read

Confirmed the primitive first (ad-hoc probe, `feedparser 6.0.13`):

```
str body   "file:///tmp/ssrf_probe.xml"  -> entries: 1 ['LOCAL FILE READ']
bytes body b"file:///tmp/ssrf_probe.xml" -> entries: 0
str  "http://127.0.0.1:9/x.xml" -> bozo_exception URLError  (real connection attempted)
```

and located the single fetch entry point feedparser uses for both schemes,
`feedparser.http.get`:

```
str  http://127.0.0.1:9/x.xml -> RAISED: outbound fetch attempted for 'http://127.0.0.1:9/x.xml'
str  file:///tmp/ssrf_probe.xml -> RAISED: outbound fetch attempted for 'file:///tmp/ssrf_probe.xml'
bytes (both)                    -> fetch calls: 0, entries: 0
```

Fix (`news_sources.py:172`):
```python
feed = feedparser.parse(body.encode("utf-8") if isinstance(body, str) else (body or b""))
```
Bytes are always feed content, never a locator. Four tests: a `file://` body
(pointing at a planted, parseable RSS file in `tmp_path`) and an
`http://169.254.169.254/...` body both yield zero entries **and** trip no
`feedparser.http.get` spy; plus a bytes body still parses, and a `None` body
yields zero items. Mutant `I2-revert-str-parse` → both SSRF tests fail.

### I3 — unpinned `raise_for_status()` inverted the 429 diagnosis

Two tests. `test_http_fetcher_propagates_status_errors_instead_of_returning_the_body`
pins the call directly (`pytest.raises(RuntimeError, match="429")`), and the
pre-existing isolation test now also asserts the *diagnosis*:
`news_provider_failure` in the events and `news_provider_empty` **not** in them.
That second assertion is the one that matters — without `raise_for_status()` the
429 body parses to zero entries, so the aggregator still "survives" and the feed
is reported as merely quiet, exactly inverting the reading of the real 429
FinancialJuice returned during this work (§8.1). `M14-drop-raise_for_status` now
fails both.

### I4 — `source_priority=None` on wire items

`test_wire_headlines_never_count_as_breaking_high_priority` builds 40 real
FinancialJuice items through `build_market_news_providers` (so the `None` comes
from the production path) and asserts
`bot._count_breaking_high_priority(wire_items) == 0`, with a positive control
(`+ one source_priority=1 headline` → `== 1`) so it cannot pass vacuously.
`M27-source_priority=1` now fails. Left as `None` deliberately — see §8.5; the
alternative was 40 "breaking high-priority" items per cycle invalidating the
market-research and news-synthesis TTL caches every cycle.

### I5 — the URL-hash dedup key was unpinned

`test_duplicate_url_prefers_lower_latency_source` used the same URL *and* the
same title, so the title key alone collapsed the pair. The two stubs now carry
an identical URL and **different** titles (`"Fed cuts rates, updated with
analyst reaction"` vs `"Fed cuts"` — Google's re-worded "updated" headline
against the wire's original, the common real case), and the test asserts which
copy survived (`title == "Fed cuts"`). `M25-drop-URL-dedup-key` now fails.

### I6 — the volume increase would trip `news_memory_growth_anomaly` forever

Re-derived the numbers with the real `NewsMemory` at the shipped
`poll_interval_seconds=900`, `news_memory_hours=48`, `news_memory_max_items=2000`:

```
new URLs/cycle= 6 -> crosses 500 at 21.0 h ; crosses 2500 at never ; peak=1158
new URLs/cycle=20 -> crosses 500 at  6.5 h ; crosses 2500 at never ; peak=2000
new URLs/cycle=46 -> crosses 500 at  2.8 h ; crosses 2500 at never ; peak=2000
new URLs/cycle=50 -> crosses 500 at  2.8 h ; crosses 2500 at never ; peak=2000
```

Confirms the finding (6.5h / 2.8h vs the review's 6.2h / 2.5h) and adds one
point: even the *old* single-source rate crosses 500 in 21h, so 500 only ever
held because real feeds repeat URLs across cycles. Everything plateaus at the
bot's own 2000-entry retention cap, so a threshold below 2000 fires `alert_only`
forever — a permanent false alarm whose only effect is alert fatigue masking real
alerts.

`supervisor/watcher.py`: `news_memory_max_size` 500 → **2500**, above the
retention cap, with the comment recording that 500 predates multi-source news.
Nothing else under `supervisor/` touched (`git diff` on that file is this field
plus its comment). Pinned from the test side by
`test_news_memory_size_threshold_sits_above_the_bots_own_retention_cap`, which
asserts the *relationship* (`default > Settings().news_memory_max_items`) rather
than the literal, so a future revert to 500 fails.
`I6-revert-threshold-to-500` → that test fails. The existing
`test_news_memory_growth_anomaly_*` tests pass `max_size=500` explicitly and are
unaffected.

### M7 — interleaving was per-latency-class, not per-provider

`_interleave` now buckets on `(latency_rank, provider_index)`.
`test_two_providers_in_the_same_latency_class_both_get_prompt_slots`: two
`realtime` wires × 40 items → head of the merged list is
`["a 0", "b 0", "a 1", "b 1"]` and the second wire gets exactly 10 of the 20
prompt slots (it got 0 before). `M7-bucket-on-latency-only` → fails.

### M8 — dedup ordering inversion

The promoted copy now carries its **own** `(provider_index, item_index)` origin
and buckets are sorted by it, so a promotion cannot inherit the loser's list
position. `test_promoted_duplicate_does_not_jump_ahead_of_fresher_wire_items`:
Google (fetched first) rebroadcasts the wire's oldest item; the merged order is
`wire item 0..3`, where before the fix `wire item 3` — the oldest — took slot 0
ahead of three fresher wire items. `M8-promoted-copy-keeps-losers-origin` → fails.
Within-class provider ranking is still preserved (existing test unchanged and
still passing).

### M9 — `max_items <= 0` meant unbounded

`financialjuice_max_items: int = Field(default=40, ge=1)` and
`fed_press_max_items: int = Field(default=10, ge=1)`. `FINANCIALJUICE_MAX_ITEMS=0`
now fails at config load instead of quietly returning all ~100 items; the
`*_enabled` flag is the way to disable a feed. Test covers `0` and `-1` for both
fields plus `1` still valid. `M9-revert-ge1-on-max-items` → fails.

### M10 — three things sat outside the isolation boundary

Both `getattr` reads moved inside the `try` (with `name` pre-seeded to the class
name so the failure event can still identify a provider whose own `name`
property is what raised), and the `_dedupe` call is wrapped, emitting
`news_dedupe_failure` and falling back to the raw merge filtered to genuine
`Headline` instances — so a malformed row costs that row, not the cycle, and
nothing downstream-hostile is passed on. Three tests: an item with
`latency_class`/`title`/`snippet` but no `.url` (was an `AttributeError` straight
out of `fetch_market_headlines`), and a raising `name` / raising `latency_class`
property (parametrised).

`except Exception` kept deliberately — `test_operator_interrupts_are_not_swallowed_by_the_isolation`
asserts `KeyboardInterrupt` still propagates, so Ctrl-C during a news fetch keeps
working. Mutants: `M10a-metadata-read-outside-try` → both parametrised cases
fail; `M10b-drop-dedupe-isolation` → the no-`url` test fails.

### M11 — `_normalise`'s broad `except Exception` was unpinned

`test_non_headline_objects_from_a_provider_are_dropped_not_fatal` feeds
`["x", {"t": 1}, None, valid_headline]` and asserts only the valid headline
survives. `M17-remove-_normalise-guard` (narrowed to `ZeroDivisionError`) → fails.

### M12 — the Fed provider was invisible-idle

New `NewsAggregator._warn_if_stale`: when a provider's **freshest** item is older
than `headline_prompt_max_age_hours`, emit `news_provider_stale`
(`provider`, `latency_class`, `items`, `freshest_age_hours`, `max_age_hours`).
Reporting only — the headlines are still returned and the 12h prompt gate is
unchanged (raising it is a separate decision, §8.3). Wrapped in its own
`try/except` so telemetry cannot become a crash path, and silent when items carry
no usable timestamp (absence of a date is not evidence of staleness — Google
items can arrive undated). Three tests: fires once for a Fed-shaped feed aged
68/330/595h with the right freshest age; silent when one item is fresh; silent
for undated items. `M12-drop-stale-check` → fails.

Expect this roughly once per cycle for `fed_press` between releases. That is the
point — it is a journal event, not an alert, and no watcher rule was added
(`supervisor/` is out of scope beyond I6).

### M16 — the 80-char dedup cap (`news.py:52`)

Pinned, without changing behaviour: `test_normalize_title_for_dedup_truncates_to_80_chars`
uses two titles sharing an 80-char prefix and diverging after
(`"... minutes show"` vs `"... - Reuters"`), asserting they normalise equal and
to exactly 80 chars, plus a companion test that titles differing *inside* the
first 80 chars stay distinct (so the cap is not doing the collapsing on its own).
`M16-drop-80-char-dedup-cap` → fails. **Not** in scope and not done: making title
dedup semantic (§7 "weakest remaining point") — a fuzzier matcher silently drops
distinct stories, which is the worse error.

### 7-mutant re-run

Each mutation applied to the fixed tree, full `pytest -q` run, then reverted and
verified byte-identical against a pre-mutation copy (`grep MUTANT` over the repo
is empty). Baseline is `8 failed` (the pre-existing macOS artifacts), so any
count above 8 is a kill.

| # | Mutation | Before | Now | Killed by |
|---|---|---|---|---|
| M7 | `bot._fetch_market_headlines` builds providers with a dead fetcher (`fetcher=lambda url: ""`) | SURVIVED | **KILLED** (9 failed) | `test_run_cycle_really_fetches_both_wire_feeds_and_merges_them` |
| M14 | delete `response.raise_for_status()` | SURVIVED | **KILLED** (10 failed) | `test_http_fetcher_propagates_status_errors...`, `test_default_http_fetcher_raises_on_http_error_so_aggregator_isolates` |
| M16 | `return cleaned[:80]` → `return cleaned` | SURVIVED | **KILLED** (9 failed) | `test_normalize_title_for_dedup_truncates_to_80_chars` |
| M17 | `_normalise` guard → `except ZeroDivisionError` | SURVIVED | **KILLED** (9 failed) | `test_non_headline_objects_from_a_provider_are_dropped_not_fatal` |
| M19 | `providers = [GoogleNewsProvider(self.news_client)]` (Google-only revert) | SURVIVED | **KILLED** (9 failed) | `test_run_cycle_really_fetches_both_wire_feeds_and_merges_them` |
| M25 | drop the URL-hash key from `_dedupe_keys` | SURVIVED | **KILLED** (10 failed) | `test_duplicate_url_prefers_lower_latency_source`, `test_an_item_without_a_url_does_not_escape...` |
| M27 | `source_priority=None` → `1` on wire items | SURVIVED | **KILLED** (9 failed) | `test_wire_headlines_never_count_as_breaking_high_priority` |

Second round, one mutant per fix that the 7 above do not cover — all **KILLED**:
`I2-revert-str-parse` (2 tests), `M7-bucket-on-latency-only`,
`M8-promoted-copy-keeps-losers-origin`, `M9-revert-ge1-on-max-items`,
`M10a-metadata-read-outside-try` (2 tests), `M10b-drop-dedupe-isolation`,
`M12-drop-stale-check`, `I6-revert-threshold-to-500`.

Note on M19: applied naively it is a `NameError` (`GoogleNewsProvider` is not
imported in `bot.py`), which fails 8 unrelated tests for the wrong reason. The
run above adds the import so the mutant is a faithful Google-only revert; it then
fails exactly one test, the new one.

### Test output

```
$ .venv/bin/pytest tests/test_news_sources.py tests/test_news.py -v
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
collected 66 items
...
============================== 66 passed in 0.85s ==============================

$ .venv/bin/pytest -q
8 failed, 871 passed in 5.03s
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick

$ PYTHONPATH=/tmp .venv/bin/pytest tests/test_news_sources.py tests/test_news.py -q -p socketguard
66 passed in 1.26s          # socket.connect raises; the news suite is fully offline
```

Identical 8 names as the pre-fix baseline, runtime unchanged at ~5s (no test
acquired a network dependency). **No live call was made to FinancialJuice or
federalreserve.gov during this fix pass** — every probe was offline (local
`file://`, a closed local port, and stubbed responses).
