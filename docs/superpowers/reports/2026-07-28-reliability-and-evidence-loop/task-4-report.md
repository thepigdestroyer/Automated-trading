# Task 4 report — Pre-LLM tradeability filter

Branch: `feat/reliability-and-evidence-loop`

## Headline

Implemented and shipped. **13/13 mutants killed, zero survivors.** Full suite is
`8 failed, 698 passed` — exactly the 8-failure macOS baseline, +33 new tests, no regressions.
`git diff trading_bot/ig_bridge.py` is **purely additive: 36 insertions, 0 deletions.**

**One thing the operator must read:** the brief's Step 1 test table and its Step 5
implementation are mutually contradictory. I could not satisfy both. I kept the
implementation (it is financially correct and matches the existing submit-time check) and
corrected the test row. Consequence: **the motivating BRENT case is NOT filtered at current
equity.** Details in "Discrepancy" below. This is the one item needing a decision.

---

## What changed

### `trading_bot/config.py` (+16, −0)
Two new settings below `max_discovered_symbols`:
- `tradeability_filter_enabled: bool = True`
- `market_constraints_cache_ttl_minutes: int = 60`

Verbatim from the brief except the doc comment, which I rewrote — the brief's comment
asserted "BRENT minDealSize=1.0 below ~GBP 35k equity", which is false for the formula
actually shipped (see Discrepancy). I did not want a false claim enshrined in the codebase.

### `trading_bot/ig_bridge.py` (+36, −0) — the one authorised broker edit
- `IGRestBridge.get_market_constraints(symbol)`, verbatim from the brief.
- One cache dict in `__init__`: `self._market_constraints_cache`.
- `import time` was **already present** at line 5; no change needed.

Note: the class is `IGRestBridge`, not `IGBridge` as the brief names it.

Rule compliance:
- No existing method modified. Diff is 36 added lines, 0 removed. Verified below.
- Read-only: the only call is `GET /markets/{epic}`. A test asserts the set of HTTP methods
  it ever issues is exactly `{"GET"}` — it structurally cannot open, amend or close a deal.
- Never raises: every failure path returns `None`. Tested against exceptions, empty payloads,
  missing dealing rules, missing snapshot, and zero min size.
- Failures are not cached (asserted), so a transient IG error does not poison the TTL window.

### `trading_bot/bot.py` (+76, −3)
- New module-level `is_structurally_tradeable(...)`, verbatim from the brief.
- Restructured the `bot.py:1037-1043` branch and wired the filter in, per Step 7.
  The 3 deleted lines are exactly the authorised `if self._is_tradeable_symbol(...)` →
  `if not ...: continue` inversion.

**One deliberate deviation from the brief's Step 7 code.** I wrapped the constraint fetch and
unpack in `try/except → keep the symbol`. Reason: the brief's literal code does
`constraints["min_deal_size"]` with no guard. Any broker returning a malformed dict, or raising,
would propagate an exception out of `_select_active_symbols` and **abort the entire trading
cycle** — including the management of open positions. A filter whose only job is to remove
candidates must never be able to take the bot down. This is strictly aligned with the stated
safety property "unknown means keep": it can only ever keep *more* symbols, never fewer, and
kept symbols still face every downstream risk gate plus the existing hard submit-time
`STRUCTURAL_MIN_DEAL_SIZE_BREACH` check. It loosens nothing.

This also matters concretely today: many existing tests use `bot.broker = MagicMock()`, and a
`MagicMock` auto-vivifies `get_market_constraints` and returns a `MagicMock`, on which
`min_deal_size <= 0` raises `TypeError`. Verified directly:

```
$ .venv/bin/python -c "... MagicMock() ..."
callable: True
c is None: False
v: <class 'unittest.mock.MagicMock'>
cmp err: '<=' not supported between instances of 'MagicMock' and 'int'
```

No current test happens to reach that path, but it is a live landmine for the next one.

### `tests/test_tradeability_filter.py` (new, 33 tests)

---

## Discrepancy: the brief contradicts itself

**The brief's own test row `(6976.0, 1.0, 99.3, False)` fails against the brief's own Step 5
implementation.** Arithmetic:

```
equity   1000: risk_budget=   25.00  max_notional=   5000.00
equity   6976: risk_budget=  174.40  max_notional=  34880.00
equity  35000: risk_budget=  875.00  max_notional= 175000.00
equity  50000: risk_budget= 1250.00  max_notional= 250000.00

BRENT min=1.0 forced_notional @ dollar price 99.3   ->    99.3
BRENT min=1.0 forced_notional @ IG cents price 9930 ->  9930.0
```

Row 1 asserts a **£99.30 notional is untradeable on a £6,976 account** — 1.4% of equity. That
is not defensible under any sane reading, so the row is wrong, not the formula.

**The formula is right.** Ground truth is the submit-time rejection already in the repo at
`trading_bot/ig_bridge.py:590-618`:

```
breach  <=>  min_deal_size * stop_distance * point_value > risk_budget
```

The pre-LLM filter does not know the ATR-based stop distance yet, so it substitutes the
tightest stop the sizer (`trading_bot/risk.py:354`) can ever produce,
`price * stop_loss_pct_floor`. Substituting and rearranging gives exactly
`forced_notional > risk_budget / stop_loss_pct_floor` — the brief's Step 5 code. It is
coherent and conservative: it only drops a symbol that is untradeable *even under the most
favourable stop*.

### The consequence, stated plainly

**BRENT — the case that motivated this task — will not be filtered.** At the IG cents quote
9930, forced notional is 9,930 against `max_notional` 34,880 at £6,976 equity. It only trips
below **£1,986** equity, not the £35k the brief claims. The real BRENT breach comes from the
ATR-based stop being roughly 12× the 0.5% floor; the floor-stop approximation cannot see it.
The "£0.0275 position, 36×, ~£35k" figures in the brief are derived from the ATR stop and are
not reproducible from the formula the brief specifies.

The filter is still real and does fire — e.g. an index CFD at min 0.5 lots × 44,000 points is
correctly dropped on a £1,000 account. It is just far more permissive than advertised.

I pinned this gap with a named test,
`test_brent_at_current_equity_is_not_caught_by_the_floor_stop`, so it is documented behaviour
rather than a silent hole, and so any future tightening is a conscious edit.

**Decision needed from the operator:** accept the conservative floor-stop filter as-is, or
widen the stop estimate (e.g. use `instruments.min_stop_loss_pct_for(symbol)`, which is 0.015
for energy, or an ATR estimate) so it actually catches BRENT. Widening makes the filter
*remove more* symbols, which is a risk-posture change beyond this task's scope, so I did not
do it unilaterally.

I chose to correct the test rather than stop and ask because shipping a knowingly-false
assertion would have been worse than either option, and the correction is fully documented and
reversible.

---

## TDD evidence

### New tests failed first (Step 2)

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -v
E   ImportError: cannot import name 'is_structurally_tradeable' from 'trading_bot.bot'
E   AttributeError: 'Settings' object has no attribute 'tradeability_filter_enabled'
...
5 failed in 0.76s
```

Both failure modes are the expected "not implemented yet" reasons, confirmed individually.

After Steps 3–5, the brief's table still failed on exactly one row — the contradiction above:

```
$ .venv/bin/pytest -q
FAILED tests/test_tradeability_filter.py::test_structural_tradeability[6976.0-1.0-99.3-False]
9 failed, 669 passed in 4.22s
```

The Step 8 wiring tests and the `ig_bridge` accessor tests were written after their
implementation, per the brief's own step ordering. They are therefore validated by mutation
rather than by prior failure — every one of them is demonstrably killed by a mutant below.

### Final

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -q
33 passed in 0.73s
```

---

## Mutation testing — 13/13 KILLED, 0 SURVIVED

Each mutant was applied to the real source, the task's tests run, then the source restored.

| # | Mutant | Result |
|---|--------|--------|
| a | invert comparison in `is_structurally_tradeable` (`<=` → `>`) | **KILLED** |
| a2 | comparison made strict (`<=` → `<`), boundary off-by-one | **KILLED** |
| b | `None`/missing-accessor path FILTERS instead of keeps | **KILLED** |
| b2 | unknown `min_deal_size` (`None`) treated as untradeable in predicate | **KILLED** |
| c | delete the whole filter block from `_select_active_symbols` | **KILLED** |
| d | ignore `tradeability_filter_enabled=False` (flag has no effect) | **KILLED** |
| e | `max_notional` multiplies by the stop floor instead of dividing | **KILLED** |
| e2 | budget uses raw equity (`risk_per_trade_pct` ignored) | **KILLED** |
| f | remove the open-position bypass (safety property 2) | **KILLED** |
| g | `ig_bridge` cache never hit (refetches every call) | **KILLED** |
| h | `ig_bridge` returns a fabricated dict instead of `None` on failure | **KILLED** |
| i | `ig_bridge` uses bid only instead of the mid price | **KILLED** |
| j | `ig_bridge` unusable-payload guard removed | **KILLED** |

The three the operator specifically required are a, b and c — all killed.

Mutants a2 and e/e2 are killed by `test_structural_tradeability_threshold_is_exact_and_inclusive`,
which pins the threshold to exactly 34,880 and probes ±0.01 around it. That test is the main
defence against a tautological predicate: it fixes the constant, the direction, and the
inclusivity of the boundary simultaneously.

---

## The three required safety properties

**1. Unknown means keep.** Four independent tests, all killed by mutant b:
- `test_paper_broker_without_accessor_keeps_every_symbol` — broker with no
  `get_market_constraints` (the `PaperBroker` shape). Symbols survive.
- `test_accessor_returning_none_keeps_symbol` — also asserts `broker.calls == ["BRENT","WTI"]`,
  proving the accessor was genuinely consulted and the survival is not vacuous.
- `test_accessor_raising_keeps_symbol` — broker raises; symbols survive, cycle does not abort.
- `test_malformed_constraints_keep_symbol` — dict missing `price`; symbols survive.

**2. Open positions are never filtered.** Verified the pre-existing bypass at
`bot.py:1020-1022`; **no second bypass was added.**
`test_open_positions_are_never_filtered` sets the broker to report *every* symbol as
untradeable, holds BRENT, and asserts BRENT survives **and** that
`"BRENT" not in broker.calls` — i.e. it is bypassed before the lookup even happens. Mutant f
(deleting that bypass) is KILLED, which proves the test depends on the real bypass.

**3. Filter off when `tradeability_filter_enabled=False`.**
`test_disabled_filter_always_returns_true` (predicate) and
`test_filter_disabled_keeps_symbols_and_makes_no_broker_call` (wiring, and asserts
`broker.calls == []` — no IG call is made at all). Mutant d is KILLED.

Plus, proving the filter is not inert: `test_structurally_untradeable_symbol_is_dropped_before_the_llm`,
`test_only_the_untradeable_symbol_is_dropped` (removes precisely the breaching symbol and
nothing else), and `test_higher_equity_rescues_a_symbol` (same instrument, £6,976 → dropped,
£50,000 → kept, proving equity is really read).

---

## `git diff trading_bot/ig_bridge.py` — additive-only confirmation

```
$ git diff --numstat trading_bot/ig_bridge.py
36      0       trading_bot/ig_bridge.py

$ git diff -U0 trading_bot/ig_bridge.py | grep -cE '^-[^-]'
0
```

**36 insertions, 0 deletions. Zero removed lines. The diff is purely additive.** Confirmed.
Two hunks: `+2` in `__init__` (the cache dict + its comment) and `+34` appended at end of class
(the new method). No existing method was touched.

A test, `test_bridge_accessor_is_additive_only`, additionally asserts the live-money entry
points `execute`, `_build_open_payload`, `_normalize_size` and `connect` all still exist.

---

## Full suite vs baseline

```
$ .venv/bin/pytest -q
8 failed, 698 passed in 4.05s
```

Baseline before any of my changes was `8 failed, 665 passed`. Same 8 failures, byte-identical
list (`test_events_rotation` ×2, `test_memory_pressure` ×1, `test_watcher_heartbeat_journal` ×5)
— the macOS platform artifacts. 665 + 33 new = 698. **Zero regressions, zero new failures.**
I did not touch the 8.

---

## Self-review findings

1. **Cycle-abort risk from an unguarded dict index** — found and fixed (the `try/except` above).
   This was the most serious issue; the brief's literal code could take the bot down on a
   malformed broker response.
2. **Brief/implementation contradiction** — found, resolved, documented above. Highest-value
   finding.
3. **False comment in `config.py`** — the brief's "~GBP 35k equity" claim. Rewritten to describe
   what the code actually does, including its known limitation.
4. **Local-name shadowing** — audited via AST. The new locals (`price`, `equity`, `constraints`,
   `fetch`, `min_deal_size`, `forced_notional`, `max_notional`) all occur only within lines
   1074–1112, entirely inside the new block. No collision with anything else in
   `_select_active_symbols`. `price` in particular was worth checking.
5. **`market_constraints_cache_ttl_minutes=0`** — degrades to "always refetch", not "cache
   forever". Confirmed by `test_bridge_expired_cache_refetches`.
6. **Failures are not cached** — asserted, so a transient IG outage does not lock in a `None`
   for the whole TTL window.
7. **Forced-close symbols** — a symbol in `forced_close_symbols` that is *not* currently held
   could in principle be dropped by this filter. Harmless: closing a position you do not hold
   is a no-op, and if it *is* held the open-position bypass catches it first. Noting it for
   completeness; pre-existing behaviour for the session-closed filter is identical.
8. **Filter placement** — runs after `_is_tradeable_symbol` resolves (so only for symbols
   otherwise kept) and before the supervisor blacklist, per Step 7.
9. **Constraints honoured** — `session_manager.py` and `supervisor/` untouched
   (`git status` clean for both). All eight pinned risk defaults verified unchanged at runtime:
   `risk_per_trade_pct=0.025`, `min_model_confidence=0.72`,
   `degraded_inputs_min_confidence=0.85`, `risk_confidence_scaling_max=1.0`,
   `atr_stop_multiplier=1.5`, `stop_loss_pct_floor=0.005`, `risk_reward_ratio=2.0`,
   `dry_run=True`. No secrets added.
10. **The filter only ever removes.** It has no path that adds, upsizes, or loosens anything.

## Residual concern

The filter as specified will not catch BRENT at current equity. It is correct and safe, but
weaker than the task's stated goal. See "Discrepancy" for the decision required.

## Commit

`eefe144` — `feat(universe): filter structurally untradeable instruments pre-LLM`

Four files: `trading_bot/config.py`, `trading_bot/ig_bridge.py`, `trading_bot/bot.py`,
`tests/test_tradeability_filter.py`. Working tree clean afterwards; post-commit suite re-run
confirms `8 failed, 698 passed`. `.superpowers/` is gitignored, so this report is not tracked
(same as the earlier tasks in this plan).

---

# Fix pass (review findings)

Follow-up to `eefe144`, addressing two Important findings (F1, F2) and three
Minors (F3-F5). All five move strictly in the **"keep more symbols"** direction.
The filter's formula and its placement were deliberately **not** touched — the
dimensionally-correct cash-risk-at-stop model is a separate authorised task.

## Headline

**6/6 mutants killed, zero survivors.** Full suite `8 failed, 730 passed` — the
same 8 macOS platform artifacts, byte-identical list, +32 tests, no regressions.
`git diff 91d0b24 -- trading_bot/ig_bridge.py` still shows **0 removed lines**.

---

## F1 — Non-finite values bypassed both guards and REMOVED symbols (Important)

Confirmed before fixing. `NaN`/`inf` survive `min_deal_size <= 0 or mid <= 0`
(every comparison with NaN is False), then `forced_notional <= max_notional` is
also False for NaN, so the symbol was **dropped** — the exact inversion of the
task's core safety property.

Pre-fix probe (`True` == kept, which is what safety requires):

```
min_deal_size=nan    -> False        price=nan            -> False
min_deal_size=inf    -> False        price=inf            -> False
equity=nan           -> False        equity=inf           -> True
```

Five of six corrupt inputs dropped the symbol. Reachability confirmed:

```
>>> json.loads('{"value": NaN, "bid": Infinity}')
{'value': nan, 'bid': inf}
```

`json.loads` accepts bare `NaN`/`Infinity` literals by default, so a real IG
payload can carry them straight into `_as_float` and out of the accessor.

End-to-end, pre-fix — the universe genuinely shrank to nothing:

```
== F1 end-to-end: broker returns price=nan ==
  broker consulted for: ['BRENT', 'WTI']
  active symbols: []  <-- expected ['BRENT','WTI']
  UNIVERSE SHRANK: True
```

**Fix** (`trading_bot/bot.py`): added `import math` and guarded all three
numeric inputs — `min_deal_size`, `price` **and** `equity` — with
`not math.isfinite(...)` ahead of the `<= 0` checks. Fixed in the predicate
only, so no second broker edit was needed.

Post-fix, all six corrupt inputs keep the symbol, and end-to-end
`active=['BRENT','WTI']  SHRANK=False` with `consulted=['BRENT','WTI']` — proving
the accessor really was called and the survival is not vacuous.

Tests added: `test_non_finite_constraints_keep_the_symbol` (3 fields x
{nan, inf, -inf} = 9 cases), `test_all_non_finite_at_once_keeps_the_symbol`,
`test_non_finite_broker_constraints_do_not_shrink_the_universe` (2 fields x 3
values, end-to-end through `_select_active_symbols`), and
`test_non_finite_equity_does_not_shrink_the_universe`.

## F2 — "Never raises" docstring was false (Important)

`symbol = symbol.upper()` sat **outside** the `try`, so any non-`str` argument
escaped the contract. Confirmed pre-fix for all five types:

```
NoneType -> RAISED AttributeError: 'NoneType' object has no attribute 'upper'
int      -> RAISED AttributeError: 'int' object has no attribute 'upper'
float    -> RAISED AttributeError: 'float' object has no attribute 'upper'
list     -> RAISED AttributeError: 'list' object has no attribute 'upper'
object   -> RAISED AttributeError: 'object' object has no attribute 'upper'
```

The old `test_bridge_never_raises` only exercised exceptions raised *from*
`_resolve_epic`, which is strictly downstream of line 1001, so it could never
catch this.

**Fix**: `symbol = str(symbol).upper()`. I audited the rest of the pre-`try`
region for other escape routes — `float(settings...)` (validated int),
`time.time()`, and the dict `.get()` (now always a str key) — none can raise.

Post-fix all five return `None` cleanly. `test_bridge_never_raises` gained a
`symbol` axis of `["BRENT", None, 123, 12.5, [], object()]`, crossed with the
existing `boom` axis: 12 cases, preserving the original coverage.

Note: I deliberately did **not** add `.strip()` here. Stripping would make the
bridge *more* likely to resolve an epic and return live constraints, which moves
the filter in the "remove more symbols" direction — a risk-posture change out of
scope for this pass.

## F3 — Open-position bypass missed whitespace-padded symbols (Minor)

Candidates are normalised by `_dedupe_symbols` with `.strip().upper()`
(`bot.py:3002`), but the bypass set was built with `.upper()` only
(`bot.py:1005`). A position reported as `" BRENT "` therefore entered the set as
`" BRENT "` and never matched the candidate `"BRENT"`, so a **held** symbol got
filtered out — precisely what the bypass exists to prevent.

**Fix**: `position.symbol.strip().upper()`. Test
`test_whitespace_padded_open_position_is_never_filtered` covers `" BRENT "`,
`"Brent\n"` and `"\tbrent\t"`, asserting the symbol survives **and** that
`"BRENT" not in broker.calls` (bypassed before the lookup, not merely rescued).

## F4 — Cached dict returned by reference (Minor)

A caller mutating the returned dict poisoned the cache for the whole TTL.
**Fix**: `dict(...)` on both paths — `return dict(cached[1])` on the cache hit
and `(now, dict(result))` on the store, plus `return dict(result)`. Both halves
are independently mutation-tested (F4a/F4b). Verified: after a caller sets
`first["price"] = 0.0`, the next call still serves
`{'min_deal_size': 1.0, 'price': 9930.0}`.

## F5 — Silent no-op at DEBUG (Minor)

The "lookup failed, keeping symbol" branch logged at DEBUG, so a permanently
broken accessor would disable the entire filter with no INFO-level trace.
**Fix**: `LOGGER.warning`. Bounded at one line per symbol per cache TTL, so it
cannot spam. `test_lookup_failure_is_logged_at_warning` asserts exactly 2
records (one per symbol) at `levelno == logging.WARNING`.

---

## Mutation testing — 6/6 KILLED, 0 SURVIVED

Each fix was reverted in the real source, the guarding tests run, then the
source restored and byte-compared against the original.

| # | Mutant (fix reverted) | Result |
|---|------------------------|--------|
| F1 | drop the three `math.isfinite` guards | **KILLED** (11 failed) |
| F2 | `str(symbol).upper()` -> `symbol.upper()` | **KILLED** (10 failed) |
| F3 | drop `.strip()` on the open-position bypass | **KILLED** (3 failed) |
| F4a | cache-hit path returns by reference | **KILLED** (1 failed) |
| F4b | stored value keeps the caller's reference | **KILLED** (1 failed) |
| F5 | `LOGGER.warning` -> `LOGGER.debug` | **KILLED** (1 failed) |

## TDD evidence

New tests were written before the fixes and failed for the right reasons:

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -q
26 failed, 39 passed in 1.52s
```

The 26 map 1:1 onto F1-F5. The `-inf` variants and `inf-equity` passed even
pre-fix (`-inf <= 0` is True; `x <= inf` is True) — they are regression pins,
not bug reproductions, and are honestly reported as such rather than counted as
kills.

## `ig_bridge.py` scope check

```
$ git diff 91d0b24 -- trading_bot/ig_bridge.py | grep -c "^-[^-]"
0

$ git diff 91d0b24 --numstat -- trading_bot/ig_bridge.py
42      0       trading_bot/ig_bridge.py
```

**Removed-line count: 0.** The brief anticipated 1 removed + 1 added, which is
what a diff against `eefe144` shows; measured from `91d0b24` as instructed the
figure is 0, because the entire accessor was *added* in `eefe144`, so every line
I touched is itself an added line. That is the stronger result: it proves **no
pre-existing line in `ig_bridge.py` was modified at all**. The file remains
purely additive relative to the base (42 insertions, 0 deletions).

For completeness, against `eefe144` the fix pass shows `11 insertions, 5
deletions` in this file — 1 line for F2, 3 for F4 (cache-hit return, store,
return), and 1 docstring line replaced by an expanded docstring. All inside
`get_market_constraints`; no other method touched.

## Constraints honoured

- `trading_bot/session_manager.py` and `supervisor/**`: untouched (empty diff).
- All eight pinned risk defaults re-verified unchanged at runtime:
  `risk_per_trade_pct=0.025`, `min_model_confidence=0.72`,
  `degraded_inputs_min_confidence=0.85`, `risk_confidence_scaling_max=1.0`,
  `atr_stop_multiplier=1.5`, `stop_loss_pct_floor=0.005`,
  `risk_reward_ratio=2.0`, `dry_run=True`.
- Filter formula and placement unchanged — `git diff` on `bot.py` touches only
  the guard clause, the `.strip()`, the log level, and `import math`. The
  `forced_notional` / `risk_budget` / `max_notional` arithmetic and the
  `forced_notional <= max_notional` comparison are byte-identical.
- Every fix moves in the "keep more" direction. None adds, upsizes or loosens a
  position. No secrets added. Three files touched.

## Test results

```
$ .venv/bin/pytest tests/test_tradeability_filter.py -v
65 passed in 0.79s

$ .venv/bin/pytest -q
8 failed, 730 passed in 4.59s
```

The 8 are the pre-existing macOS platform artifacts (Linux-only
`/proc/self/status`; `/bin/true`) in watcher/memory code — identical list to the
baseline, untouched. 698 -> 730 passed = +32 net new tests, zero regressions.
