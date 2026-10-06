# Task 3 report — Bound `exchange_calendars` memory

Branch: `feat/reliability-and-evidence-loop`
Commit: `78d9e7a` — `perf(calendars): bound exchange_calendars to a rolling window`
(single commit; `.superpowers/` is gitignored so this report is untracked, as
with the task-1 and task-2 reports)

## 1. What changed

**`trading_bot/calendars.py`** — replaced `HolidayCalendar.__init__` / `_get` (was lines 156-187):

- `HolidayCalendar(window_days: int = 90)`.
- `_window_bounds(now)` → ISO `start`/`end` at `now -/+ window_days`.
- `_get(code, now=None)` calls `xcals.get_calendar(code, start=..., end=...)`.
- **Fallback (brief requirement 1):** if the bounded call raises, retry once
  *without* bounds; only if that also raises do we cache `None`.
- **Daily roll (brief requirement 2):** `_maybe_roll_window(now)` clears the
  cache when the UTC date changes. `is_session_open` calls it *before* `_get`,
  so the cache is keyed to the day of the timestamp being queried.
- Corrected the class docstring to the measured numbers (see §6).
- `timedelta` / `timezone` were already imported (line 5); no import change.

**`tests/test_calendars.py`** — appended 6 tests (brief's one + 5 more that the
mutation requirements demand):

| Test | Guards |
|---|---|
| `test_holiday_calendar_requests_bounded_window` | brief's test verbatim; `start`/`end` present |
| `test_holiday_calendar_window_brackets_the_queried_day` | bounds are *correct*, not merely present |
| `test_holiday_calendar_retries_unbounded_when_window_rejected` | fallback: bounded attempt, then unbounded retry |
| `test_holiday_calendar_rebuilds_window_once_per_utc_day` | rebuild on new UTC day, reuse within a day |
| `test_holiday_calendar_fails_open_when_lookup_raises` | fail-open for both `get_calendar` and `is_trading_minute` |
| `test_holiday_calendar_fails_open_when_library_missing` | `xcals is None` → permissive |

### Two intentional deviations from the brief's verbatim implementation

1. The brief's `__init__` early-returns when `xcals is None`, leaving
   `_cache`, `_window_days` and `_window_day` **unset**. Any direct call to
   `_get` / `_maybe_roll_window` on a host without `exchange_calendars` would
   then raise `AttributeError` — the opposite of fail-open. Mine always
   initialises the attributes and sets `_available = xcals is not None`.
2. `_get` gained a leading `if not self._available: return None` guard
   (callers treat `None` as "permissive").

Public behaviour is identical to the brief; these only remove a crash path.
Covered by `test_holiday_calendar_fails_open_when_library_missing`.

## 2. TDD evidence — the tests failed first

```
$ .venv/bin/pytest tests/test_calendars.py -v
...
E       TypeError: HolidayCalendar.__init__() got an unexpected keyword argument 'window_days'
tests/test_calendars.py:184: TypeError
=========================== short test summary info ============================
FAILED tests/test_calendars.py::test_holiday_calendar_requests_bounded_window
FAILED tests/test_calendars.py::test_holiday_calendar_window_brackets_the_queried_day
FAILED tests/test_calendars.py::test_holiday_calendar_retries_unbounded_when_window_rejected
FAILED tests/test_calendars.py::test_holiday_calendar_rebuilds_window_once_per_utc_day
FAILED tests/test_calendars.py::test_holiday_calendar_fails_open_when_lookup_raises
FAILED tests/test_calendars.py::test_holiday_calendar_fails_open_when_library_missing
========================= 6 failed, 3 passed in 1.05s ==========================
```

Failure reason is exactly the brief's Step 2 prediction (`TypeError` on
`window_days`). After implementing:

```
$ .venv/bin/pytest tests/test_calendars.py -v
tests/test_calendars.py::test_econ_calendar_filters_by_impact_and_window PASSED
tests/test_calendars.py::test_earnings_calendar_reads_yf_calendar PASSED
tests/test_calendars.py::test_holiday_calendar_is_session_open_weekday_us PASSED
tests/test_calendars.py::test_holiday_calendar_requests_bounded_window PASSED
tests/test_calendars.py::test_holiday_calendar_window_brackets_the_queried_day PASSED
tests/test_calendars.py::test_holiday_calendar_retries_unbounded_when_window_rejected PASSED
tests/test_calendars.py::test_holiday_calendar_rebuilds_window_once_per_utc_day PASSED
tests/test_calendars.py::test_holiday_calendar_fails_open_when_lookup_raises PASSED
tests/test_calendars.py::test_holiday_calendar_fails_open_when_library_missing PASSED
============================== 9 passed in 0.64s ===============================
```

## 3. Mutation testing — the tests can actually fail

Driver (`/tmp/mutate.py`) applies one mutation, runs the file's tests, then
restores from backup; it asserts each anchor matches exactly once and verifies
byte-identical restoration at the end (`restored to original: True`).

```
### MUTATION a: drop start=/end= kwargs  -> exit 1 (tests CAUGHT it)
    FAILED ...::test_holiday_calendar_requests_bounded_window
    FAILED ...::test_holiday_calendar_window_brackets_the_queried_day
    FAILED ...::test_holiday_calendar_retries_unbounded_when_window_rejected
    FAILED ...::test_holiday_calendar_rebuilds_window_once_per_utc_day
    4 failed, 5 passed in 0.85s

### MUTATION b: never clear cache on day roll  -> exit 1 (tests CAUGHT it)
    FAILED ...::test_holiday_calendar_rebuilds_window_once_per_utc_day
    1 failed, 8 passed in 0.49s

### MUTATION c1: lookup failure raises instead of fail-open  -> exit 1 (tests CAUGHT it)
    FAILED ...::test_holiday_calendar_fails_open_when_lookup_raises
    1 failed, 8 passed in 0.50s

### MUTATION c2: is_trading_minute failure raises instead of fail-open  -> exit 1 (tests CAUGHT it)
    FAILED ...::test_holiday_calendar_fails_open_when_lookup_raises
    1 failed, 8 passed in 0.49s

### MUTATION d: no unbounded retry fallback  -> exit 1 (tests CAUGHT it)
    FAILED ...::test_holiday_calendar_retries_unbounded_when_window_rejected
    1 failed, 8 passed in 0.49s

restored to original: True
```

All 5 mutations killed, including the three the task explicitly required
(a, b, c). No surviving mutants.

## 4. Memory measurement (the justification for the task)

Brief's Step 5 script, unmodified:

```
$ .venv/bin/python -c "import tracemalloc, datetime as dt; from trading_bot.calendars import HolidayCalendar; ..."
peak=2.6MB
```

Same probe against the **pre-change** implementation: `peak=93.7MB`.

Controlled A/B in one process (same 3 codes, same `is_trading_minute` call):

```
UNBOUNDED (with is_trading_minute):
  peak=  93.7MB retained=  77.3MB
    XLON: range 2006-07-31 -> 2027-07-29  sessions=5306  minutes=2,695,980
    XNYS: range 2006-07-31 -> 2027-07-29  sessions=5281  minutes=2,051,490
    XNAS: range 2006-07-31 -> 2027-07-29  sessions=5281  minutes=2,051,490
BOUNDED +/-90d (with is_trading_minute):
  peak=   2.5MB retained=   2.1MB
    XLON: range 2026-04-30 -> 2026-10-27  sessions=126  minutes=64,260
    XNYS: range 2026-04-30 -> 2026-10-27  sessions=125  minutes=48,750
    XNAS: range 2026-04-30 -> 2026-10-27  sessions=125  minutes=48,750
SAVED peak 91.2MB (97.3%)  retained 75.2MB
```

| | before | after | saved |
|---|---|---|---|
| tracemalloc peak | **93.7 MB** | **2.5-2.6 MB** | **91.2 MB (-97.3%)** |
| tracemalloc retained (steady state) | 77.3 MB | 2.1 MB | 75.2 MB (-97.3%) |
| process RSS delta | 145.2 MB | 4.8 MB | ~140 MB |

Comfortably beats the brief's "well under 40 MB" bar. This is a real, large
result, not a null one — against a 400 MB cap it removes ~77 MB of permanent
resident footprint.

### But the brief's stated cause was wrong, and it matters

The brief (and the plan doc) says the cost is "the full 1678-2100 schedule".
On `exchange_calendars` 4.13.2 that is **not** what happens:

- The unbounded default range is **2006-07-31 → 2027-07-29** (20y back, 1y
  forward), not 1678-2100.
- The *session schedule* is cheap: 15,868 rows, ~1.5 MB peak. Bounding it
  alone would have saved under 1 MB.
- The real cost is the **per-minute index** built lazily on the first
  `is_trading_minute()` call: ~6.8M minute timestamps → 93.7 MB.

So the fix works, but for a different reason than documented. Anyone who had
"optimised" this by bounding the schedule without ever calling
`is_trading_minute` would have measured ~0.8 MB saved and concluded the task
was pointless. I corrected the class docstring to the measured explanation so
the next reader isn't misled.

Also note **`XNAS` is an alias of `XNYS`** in this library — the three codes in
the probe are only two distinct calendars.

## 5. No memory leak from the daily roll (checked, because it could have been one)

A per-day rebuild is only a win if the previous day's calendar is actually
freed. `exchange_calendars` has two internal caches; I verified both:

- `global_calendar_dispatcher._calendars`: **empty** for bounded calls — it is
  only populated for fully-default `get_calendar(name)`. No accumulation.
- `_factory_output_cache` (`calendar_utils.py:212`): keyed by factory name
  only, and `_fabricate` **overwrites** the entry each call. Holds at most one
  calendar per exchange.

Empirically: `old calendar evicted after refabricate? True`, and 45 simulated
day-rolls × 3 exchanges (135 builds) stays flat —

```
  day  0: current= 2.17MB peak= 2.56MB xcals_cache_entries=2
  day 10: current= 2.32MB peak= 2.83MB xcals_cache_entries=2
  day 44: current= 2.32MB peak= 2.89MB xcals_cache_entries=2
```

## 6. Behaviour / regression checks

Fail-open properties confirmed live (not just via fakes):

```
in-window  : False          # correct: probe ran outside NYSE hours
out-of-window (+400d): False  # window re-centres on the queried ts; no raise
unknown code: True          # permissive
default ctor still works: True   # bot.py:158 uses HolidayCalendar()
```

`is_half_day` unchanged (still constant `False`). `bot.py:158` needs no change
— `window_days` defaults to 90.

## 7. Full suite vs baseline

```
$ .venv/bin/pytest -q --no-header
FAILED tests/test_events_rotation.py::test_watcher_rotates_events_jsonl
FAILED tests/test_events_rotation.py::test_watcher_throttles_rotation
FAILED tests/test_memory_pressure.py::test_get_rss_bytes_reads_proc_self_status
FAILED tests/test_watcher_heartbeat_journal.py::test_first_tick_emits_heartbeat
FAILED tests/test_watcher_heartbeat_journal.py::test_state_records_last_heartbeat_at
FAILED tests/test_watcher_heartbeat_journal.py::test_subsequent_tick_within_interval_does_not_emit
FAILED tests/test_watcher_heartbeat_journal.py::test_heartbeat_emits_again_after_interval
FAILED tests/test_watcher_heartbeat_journal.py::test_short_interval_emits_on_every_tick
8 failed, 657 passed in 4.11s
```

Baseline was `651 passed, 8 failed`. Now `657 passed, 8 failed` — +6 passing
(exactly the new tests), **same 8 pre-existing macOS platform artifacts**, no
regressions. Not touched, as instructed.

## 8. Self-review findings

1. **Docstring was factually wrong as briefed** — fixed (§4). Shipping the
   "1678-2100 / 119 MB" claim would have embedded a false rationale in code.
2. **`__init__` crash path in the briefed version** — fixed (§1, deviation 1).
3. **New CPU cost, once per UTC day: ~1.14 s** to rebuild 3 calendars (measured
   over 45 rolls). Previously paid once at startup (~2.0 s unbounded). It is a
   synchronous stall in the first cycle after UTC midnight. Trivial against a
   multi-minute cycle, but it is a new recurring cost and worth knowing.
4. **Cache thrash risk if timestamps interleave across days.** The window is
   keyed on the *queried* timestamp, so alternating live and historical
   timestamps would clear and rebuild (~1.1 s) on every alternation. Results
   stay correct. The live path (`bot.py`) always passes `now`, so this does not
   arise today; it would bite a future backtest that reuses `HolidayCalendar`.
5. **`window_days=90` is generous.** ±90d is ~180 days of minutes for a
   question that only concerns "now". ±7d would cut the residual 2.1 MB further,
   but 90 is what the interface specifies and the remaining footprint is
   already negligible.
6. **Fail-open is now genuinely tested**, not assumed — previously only the
   happy path (`test_holiday_calendar_is_session_open_weekday_us`) was covered.

## 9. Constraints observed

- No changes to `ig_bridge.py`, `session_manager.py`, or `supervisor/`.
- No risk defaults in `config.py` touched.
- Only `trading_bot/calendars.py` and `tests/test_calendars.py` modified.
- No secrets; no network calls added.

---

# Fix pass (review findings)

Commit: `91d0b24` — `fix(calendars): airtight fail-open + warn on unbounded fallback (review findings)`
Parent: `78d9e7a`. Files: `trading_bot/calendars.py`, `tests/test_calendars.py` only.

## C1 — `is_session_open` now airtight fail-open

`_maybe_roll_window(ts)` and `_window_bounds()` sat *outside* the `try`, so they
propagated. Reproduced on `78d9e7a`:

```
datetime.max         -> OverflowError: date value out of range
datetime.min         -> OverflowError: date value out of range
None                 -> AttributeError: 'NoneType' object has no attribute 'date'
date(2026,4,23)      -> AttributeError: 'datetime.date' object has no attribute 'date'
```

Collapsed to a single guard wrapping roll + `_get` + `is_trading_minute`, with a
`LOGGER.debug(..., exc_info=True)` so the swallow is traceable. The property is
specified on the method, not on its callers — production call sites happening to
wrap it in `except Exception` does not discharge it.

Test: `test_holiday_calendar_fails_open_on_hostile_timestamps`, parametrized over
all four inputs, asserting `True` and no exception. All four now pass; reverting
the try-block placement fails all four (see mutant C1).

## C2 — unbounded fallback is now loud, but only when it is real

Confirmed the regression the review measured. Forcing the bounded call to fail
across XLON/XNYS/XNAS: `peak=93.8MB retained=77.4MB` — matching the pre-fix
baseline (93.7/77.3), re-paid on every UTC day roll, previously with zero signal
(`LOGGER` was defined but unused in this class).

**Deviation from the reviewed snippet, deliberate.** The snippet warns on *any*
bounded failure. But the bounded and unbounded calls fail *together* whenever the
code is not an `exchange_calendars` name at all — and that is a live production
case, not a hypothetical:

```
XNYS   bounded OK                         unbounded OK
XLON   bounded OK                         unbounded OK
XFRA   bounded OK                         unbounded OK
FX     bounded FAIL (InvalidCalendarName) unbounded FAIL (InvalidCalendarName)
CMES   bounded OK                         unbounded OK
```

`FX` is the `exchange_code` of 5 instruments in `instruments.py` and is passed
straight to `is_session_open` at `bot.py:1028`, `bot.py:2253`, `bot.py:2540`. On
that path nothing is built, so nothing is restored — the snippet would emit
`retrying UNBOUNDED (restores ~77 MB retained)` with a full traceback, every UTC
day, as a standing falsehood. That is precisely how a warning gets tuned out,
which would defeat C2's purpose.

So the warning fires in the `else` of the retry — i.e. only when the unbounded
calendar was genuinely built and the memory genuinely came back. It carries the
rejected bounds, the code, the cause, and `exc_info` of the *bounded* failure.
The both-failed path gets `LOGGER.debug` instead: still a trail, no false alarm.
C2's stated requirement (fallback used AND a warning logged when the bounded call
raises) is met unchanged by `test_holiday_calendar_warns_when_falling_back_to_unbounded`.

Live, at WARNING level:

```
-- real production codes (expect NO warnings) --
   XNYS  is_session_open -> False     XLON  -> True    XFRA -> True
   FX    is_session_open -> True      CMES  -> True
-- genuine fallback (bounded rejected, unbounded OK) --
WARNING: Bounded window 2026-04-30..2026-10-27 rejected for XNYS (narrow window
         rejected); retried UNBOUNDED (restores ~77 MB retained)
         Traceback (most recent call last): ... ValueError: narrow window rejected
```

Tests: `..._warns_when_falling_back_to_unbounded` (fallback used + exactly one
warning + bounds + `exc_info`), `..._does_not_warn_when_code_is_not_a_calendar`
(FX: no warning, but a DEBUG trail exists), `..._does_not_warn_on_the_happy_path`.

## I1 — `_available` mechanism pinned; four surviving mutants killed

Confirmed the review's diagnosis: with `xcals = None`, `xcals.get_calendar(...)`
raises `AttributeError`, which `_get`'s pre-existing `except Exception` swallows
into `cal = None` → `True`. The result-only assertion therefore could not
distinguish "guard works" from "guard absent". Added state assertions
(`_available is False`, `_get(...) is None`, `_cache == {}`, `_window_day is None`).

Driver applies one mutation, runs the test, restores from the original text, and
verifies byte-identical restoration. **BEFORE** = the pre-fix result-only test
body; **AFTER** = the same test with state assertions.

| Mutant | BEFORE (result-only) | AFTER (state assertions) |
|---|---|---|
| m1 drop `if not self._available: return None` in `_get` | `1 passed` — **SURVIVED** | `1 failed` — **KILLED** |
| m2 hardcode `self._available = True` | `1 passed` — **SURVIVED** | `1 failed` — **KILLED** |
| m3 drop `is_session_open`'s early-return guard | `1 passed` — **SURVIVED** | `1 failed` — **KILLED** |
| m4 drop BOTH `_available` guards | `1 passed` — **SURVIVED** | `1 failed` — **KILLED** |

Why each dies: m1/m4 let `_get` reach the cache write, so `_cache == {}` breaks;
m2 breaks `_available is False` directly; m3 lets `_maybe_roll_window` run, so
`_window_day is None` breaks.

Four further mutants on this pass, all **KILLED**: C1 (roll/bounds back outside
the try → 4 failed), C2 (warning removed → 1 failed), C2b (warn unconditionally,
i.e. the reviewed snippet's false alarm → 1 failed), M2 (`now.date()` → 1 failed).
`restored byte-identical: True`, `surviving mutants: none`.

## M1 — docstring timestamp count corrected

Re-measured rather than copied:

```
XLON: minutes=2,695,980   XNYS: minutes=2,051,490   XNAS: minutes=2,051,490
XNAS is XNYS: True
distinct sum XLON+XNYS = 4,747,470     naive triple-count = 6,798,960
```

The old "~6.8M" was the triple-count (6,798,960) — it counted `XNAS`, which is
the *same object* as `XNYS` sharing one index. Now reads "~4.7M timestamps across
XLON+XNYS (XNAS aliases XNYS -- same object, shared index)".

## M2 — roll keyed on the UTC day, per the contract

`now.date()` is the date in `ts`'s own tzinfo; the docstring promises "once per
UTC day". Now `now.astimezone(timezone.utc).date()`. `timezone` was already
imported (line 5); no import change needed. Test
`..._rolls_on_utc_day_not_local_day` uses 2026-04-23 23:00Z vs the same instant
at +10:00 (local date 2026-04-24) and asserts the window does not roll.

**Known residual, deliberately not changed:** `_window_bounds` still uses
`now.date()`, so for a non-UTC-tz caller the roll key and the window centre can
differ by one day. Immaterial against a ±90-day window, every current caller
passes UTC, and M2 scoped the change to `_maybe_roll_window`. Flagging rather
than silently widening scope.

## Test results

```
$ .venv/bin/pytest tests/test_calendars.py -v
... 17 passed in 0.69s
```

New this pass (8): 4× `fails_open_on_hostile_timestamps`,
`warns_when_falling_back_to_unbounded`, `does_not_warn_when_code_is_not_a_calendar`,
`does_not_warn_on_the_happy_path`, `rolls_on_utc_day_not_local_day`.

```
$ .venv/bin/pytest -q --no-header
8 failed, 665 passed in 4.15s
```

Baseline `657 passed, 8 failed` → `665 passed, 8 failed`: +8 passing (exactly the
new tests), **the same 8 pre-existing macOS platform artifacts** (Linux-only
`/proc/self/status`, `/bin/true`) in watcher/memory code. Not touched, as
instructed. No regressions.

## Preserved behaviour / no perf regression

```
unknown code 'NOPE'      -> True        library missing -> True (cache {}, window_day None)
default ctor works       -> True        XNYS Thu 15:00Z -> True    XNYS Sun 15:00Z -> False
bounded memory after fix -> peak=2.6MB retained=2.3MB   (was 2.6/2.2 at 78d9e7a)
```

±90-day default, per-day roll design and the fallback's existence all unchanged.
No changes to `ig_bridge.py`, `session_manager.py`, `supervisor/`, or risk
defaults. No secrets, no new network calls.
