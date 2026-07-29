"""Instruments whose broker minimum deal size structurally exceeds the risk
budget must be filtered out BEFORE the LLM sees them.

Ground truth for the predicate is the submit-time rejection already in
``ig_bridge._build_open_payload`` (``STRUCTURAL_MIN_DEAL_SIZE_BREACH``):

    breach  <=>  min_deal_size * stop_distance * point_value > risk_budget

The pre-LLM filter does not yet know the ATR-based stop distance, so it uses
the *tightest stop the sizer can ever produce* -- ``price * stop_loss_pct_floor``
-- which makes the cash risk as small as possible and therefore the filter as
permissive as possible. Rearranged, that is exactly:

    forced_notional (= min_deal_size * price)  >  risk_budget / stop_loss_pct_floor

So the filter only drops a symbol when it is untradeable *even under the most
favourable stop*. See ``.superpowers/sdd/task-4-report.md`` for why this means
the motivating BRENT case is NOT caught at current equity.

TASK 4b supersedes that formula (everything below the
``Cash risk at stop`` banner). The notional-vs-notional comparison above is
dimensionally wrong -- it divides a cash budget by a fraction to get a notional
ceiling, which measured ~350x too loose -- so it is kept only as cheap
defence-in-depth. The correct model is the one the broker actually enforces at
submit time (``ig_bridge.py`` lines 596-600):

    cash_risk_at_stop = size * stop_distance * point_value
    budget_ceiling    = target_cash_risk * (1 + risk_size_vs_budget_tolerance)
    breach  <=>  cash_risk_at_stop > budget_ceiling

That needs the ATR-based stop, which exists on the *built symbol context* --
still before the LLM ``decide()`` call, so the token saving is preserved. The
filter is therefore per-cycle and dynamic: a symbol is dropped only when that
cycle's ATR makes it unfillable, never permanently (BRENT took 4 of the 11
historical fills, so a blanket ban would have removed real trades).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Settings
from trading_bot.instruments import get_instrument
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import (
    AccountSnapshot,
    IndicatorSnapshot,
    OpenPosition,
    SymbolContext,
)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


# --- Settings ----------------------------------------------------------------


def test_tradeability_filter_enabled_by_default():
    assert _settings().tradeability_filter_enabled is True


def test_market_constraints_cache_ttl_default():
    assert _settings().market_constraints_cache_ttl_minutes == 60


def test_filter_does_not_disturb_risk_defaults():
    """The filter must not move any risk knob it reads."""
    s = _settings()
    assert s.risk_per_trade_pct == 0.025
    assert s.stop_loss_pct_floor == 0.005


# --- Predicate ---------------------------------------------------------------


@pytest.mark.parametrize(
    "equity,min_deal_size,price,expected",
    [
        # Index-CFD shape: broker minimum 0.5 lots of a 44,000-point index.
        # forced_notional = 22,000 > max_notional = 5,000 -> untradeable.
        (1000.0, 0.5, 44000.0, False),
        # Same instrument, larger account: max_notional = 250,000 -> fine.
        (50000.0, 0.5, 44000.0, True),
        (6976.0, 0.01, 99.3, True),   # WTI-like min size: fine
        (6976.0, None, 99.3, True),   # unknown min size: do not filter
        (6976.0, 0.0, 99.3, True),    # nonsense min size: do not filter
        (0.0, 0.5, 44000.0, True),    # unknown equity: do not filter
        (6976.0, 0.5, 0.0, True),     # unknown price: do not filter
    ],
)
def test_structural_tradeability(equity, min_deal_size, price, expected):
    from trading_bot.bot import is_structurally_tradeable

    s = _settings()
    assert (
        is_structurally_tradeable(
            settings=s,
            equity=equity,
            min_deal_size=min_deal_size,
            price=price,
        )
        is expected
    )


_NAN = float("nan")
_INF = float("inf")
_NEG_INF = float("-inf")


@pytest.mark.parametrize(
    "field",
    ["min_deal_size", "price", "equity"],
)
@pytest.mark.parametrize(
    "value",
    [_NAN, _INF, _NEG_INF],
    ids=["nan", "inf", "-inf"],
)
def test_non_finite_constraints_keep_the_symbol(field, value):
    """Review finding F1: garbage must mean "keep", exactly like None does.

    NaN and inf slip past the ``<= 0`` guards -- every comparison with NaN is
    False -- and then ``forced_notional <= max_notional`` is also False for
    NaN, so the symbol was being DROPPED. That inverts this task's core safety
    property: absence or corruption of data must never shrink the tradeable
    universe. Reachable in production because ``json.loads`` accepts bare
    ``NaN``/``Infinity`` literals by default.
    """
    from trading_bot.bot import is_structurally_tradeable

    kwargs = {"equity": 6976.0, "min_deal_size": 1.0, "price": 99.3}
    kwargs[field] = value
    assert (
        is_structurally_tradeable(settings=_settings(), **kwargs) is True
    ), f"{field}={value!r} must KEEP the symbol, never drop it"


def test_all_non_finite_at_once_keeps_the_symbol():
    """Belt and braces: total corruption is still "keep"."""
    from trading_bot.bot import is_structurally_tradeable

    assert (
        is_structurally_tradeable(
            settings=_settings(), equity=_NAN, min_deal_size=_NAN, price=_NAN
        )
        is True
    )


def test_structural_tradeability_threshold_is_exact_and_inclusive():
    """Pin the exact budget arithmetic.

    max_notional = equity * risk_per_trade_pct / stop_loss_pct_floor
                 = 6976 * 0.025 / 0.005 = 34,880.

    This kills mutants that flip the comparison, swap the constants, or
    multiply where they should divide.
    """
    from trading_bot.bot import is_structurally_tradeable

    s = _settings()
    equity = 6976.0
    max_notional = equity * s.risk_per_trade_pct / s.stop_loss_pct_floor
    assert max_notional == pytest.approx(34_880.0)

    def check(forced_notional: float) -> bool:
        return is_structurally_tradeable(
            settings=s, equity=equity, min_deal_size=1.0, price=forced_notional
        )

    assert check(max_notional - 0.01) is True   # just inside
    assert check(max_notional) is True          # exactly on the line: allowed
    assert check(max_notional + 0.01) is False  # just outside
    assert check(max_notional * 10) is False    # far outside


def test_disabled_filter_always_returns_true():
    """Safety property 3: the predicate is inert when the flag is off."""
    from trading_bot.bot import is_structurally_tradeable

    off = _settings(tradeability_filter_enabled=False)
    on = _settings()
    # A case the enabled filter definitively rejects.
    kwargs = dict(equity=1000.0, min_deal_size=0.5, price=44000.0)
    assert is_structurally_tradeable(settings=on, **kwargs) is False
    assert is_structurally_tradeable(settings=off, **kwargs) is True


def test_brent_at_current_equity_is_not_caught_by_the_floor_stop():
    """Regression-pin for a known gap, documented in the task report.

    BRENT minDealSize=1.0 at the IG (cents) quote 9930 gives a forced notional
    of 9,930, which is well inside max_notional=34,880 at GBP 6,976 equity.
    The real breach comes from the ATR-based stop being ~12x the 0.5% floor,
    which this predicate deliberately does not model. If someone later tightens
    the stop estimate, this test should be updated consciously, not silently.
    """
    from trading_bot.bot import is_structurally_tradeable

    assert (
        is_structurally_tradeable(
            settings=_settings(), equity=6976.0, min_deal_size=1.0, price=9930.0
        )
        is True
    )


# --- Wiring into _select_active_symbols --------------------------------------


def _account(total: float = 6976.0, open_positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo",
        currency="GBP",
        total_funds=total,
        free_funds=total,
        open_positions=open_positions or [],
    )


def _make_bot(tmp_path: Path, broker, **overrides):
    from trading_bot.bot import TradingBot

    settings = _settings(
        broker="paper",
        symbols=["BRENT", "WTI"],
        auto_discover_symbols=False,
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        supervisor_blacklist_path=tmp_path / "blacklist.json",
        **overrides,
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = broker
    bot.holiday_calendar = MagicMock()
    bot.holiday_calendar.is_session_open = MagicMock(return_value=True)
    bot.decision_engine = MagicMock()
    bot.decision_engine.discover_symbols = MagicMock(return_value=[])
    return bot


class _BrokerNoAccessor:
    """Shape of PaperBroker: tradeable, but no get_market_constraints."""

    def is_tradeable_symbol(self, symbol: str) -> bool:
        return True


class _BrokerWithConstraints(_BrokerNoAccessor):
    def __init__(self, result):
        self._result = result
        self.calls: list[str] = []

    def get_market_constraints(self, symbol: str):
        self.calls.append(symbol)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# At GBP 6,976 equity max_notional = 34,880.
# Forced notional 1.0 * 50,000 = 50,000 > 34,880 -> structurally untradeable.
_UNTRADEABLE = {"min_deal_size": 1.0, "price": 50_000.0}
# Forced notional 0.01 * 99.3 = 0.99 -> comfortably tradeable.
_TRADEABLE = {"min_deal_size": 0.01, "price": 99.3}


def test_paper_broker_without_accessor_keeps_every_symbol(tmp_path):
    """Safety property 1a: PaperBroker has no get_market_constraints.

    A broker that cannot answer must never lose us a symbol.
    """
    bot = _make_bot(tmp_path, _BrokerNoAccessor())
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["BRENT", "WTI"]


def test_accessor_returning_none_keeps_symbol(tmp_path):
    """Safety property 1b: None means unknown, not untradeable."""
    broker = _BrokerWithConstraints(None)
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["BRENT", "WTI"]
    assert broker.calls == ["BRENT", "WTI"]  # it really was consulted


def test_accessor_raising_keeps_symbol(tmp_path):
    """A broken broker must not shrink the universe or abort the cycle."""
    broker = _BrokerWithConstraints(RuntimeError("IG down"))
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["BRENT", "WTI"]


def test_malformed_constraints_keep_symbol(tmp_path):
    """Missing keys are 'unknown', not 'untradeable'."""
    broker = _BrokerWithConstraints({"min_deal_size": 1.0})  # no price
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["BRENT", "WTI"]


@pytest.mark.parametrize(
    "value", [_NAN, _INF, _NEG_INF], ids=["nan", "inf", "-inf"]
)
@pytest.mark.parametrize("field", ["price", "min_deal_size"])
def test_non_finite_broker_constraints_do_not_shrink_the_universe(
    tmp_path, field, value
):
    """Review finding F1, end-to-end.

    ``json.loads`` accepts bare ``NaN``/``Infinity`` by default and
    ``_as_float("nan")`` succeeds, so a broker really can hand back a
    non-finite price. Before the fix, ``price=nan`` shrank ["BRENT","WTI"]
    to []. A corrupt answer must be treated exactly like None: keep.
    """
    constraints = {"min_deal_size": 1.0, "price": 99.3}
    constraints[field] = value
    broker = _BrokerWithConstraints(constraints)
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(
        account_snapshot=_account(), market_headlines=[]
    )
    assert active == ["BRENT", "WTI"]
    # Not vacuous: the accessor really was consulted and still kept both.
    assert broker.calls == ["BRENT", "WTI"]


def test_non_finite_equity_does_not_shrink_the_universe(tmp_path):
    """Review finding F1: a corrupt account snapshot must also mean "keep"."""
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(
        account_snapshot=_account(total=_NAN), market_headlines=[]
    )
    assert active == ["BRENT", "WTI"]


def test_structurally_untradeable_symbol_is_dropped_before_the_llm(tmp_path):
    """The filter must actually fire on a positive untradeable answer."""
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker)
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == []
    assert broker.calls == ["BRENT", "WTI"]


def test_only_the_untradeable_symbol_is_dropped(tmp_path):
    """The filter removes precisely the breaching symbol, nothing else."""

    class _PerSymbol(_BrokerNoAccessor):
        def get_market_constraints(self, symbol: str):
            return _UNTRADEABLE if symbol == "BRENT" else _TRADEABLE

    bot = _make_bot(tmp_path, _PerSymbol())
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["WTI"]


def test_open_positions_are_never_filtered(tmp_path):
    """Safety property 2: we must keep managing what we already hold.

    Handled by the pre-existing bypass at bot.py:1020-1022 -- this test pins
    that behaviour rather than adding a second bypass. The broker here reports
    every symbol as structurally untradeable; the held symbol must survive
    anyway, and must not even be looked up.
    """
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker)
    held = OpenPosition(
        position_id="P1", symbol="BRENT", direction="buy", quantity=1.0,
        current_price=99.3,
    )
    active = bot._select_active_symbols(
        account_snapshot=_account(open_positions=[held]), market_headlines=[]
    )
    assert active == ["BRENT"]          # held symbol survives
    assert "BRENT" not in broker.calls  # bypassed before the lookup


@pytest.mark.parametrize("held_symbol", [" BRENT ", "Brent\n", "\tbrent\t"])
def test_whitespace_padded_open_position_is_never_filtered(tmp_path, held_symbol):
    """Review finding F3: bypass used ``.upper()`` without ``.strip()``.

    Candidate symbols are normalised by ``_dedupe_symbols`` with
    ``.strip().upper()``, so a held symbol reported as " BRENT " landed in the
    bypass set as " BRENT " and never matched the candidate "BRENT". The held
    symbol was then filtered out -- exactly what the bypass exists to prevent.
    """
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker)
    held = OpenPosition(
        position_id="P1", symbol=held_symbol, direction="buy", quantity=1.0,
        current_price=99.3,
    )
    active = bot._select_active_symbols(
        account_snapshot=_account(open_positions=[held]), market_headlines=[]
    )
    assert active == ["BRENT"]          # survives despite the padding
    assert "BRENT" not in broker.calls  # bypassed before the lookup


def test_lookup_failure_is_logged_at_warning(tmp_path, caplog):
    """Review finding F5: this path was DEBUG.

    A permanently broken accessor would silently no-op the entire filter with
    no INFO-level trace. It is at most one line per symbol per TTL, so WARNING
    does not spam.
    """
    broker = _BrokerWithConstraints(RuntimeError("IG down"))
    bot = _make_bot(tmp_path, broker)
    with caplog.at_level(logging.WARNING, logger="trading_bot.bot"):
        active = bot._select_active_symbols(
            account_snapshot=_account(), market_headlines=[]
        )
    assert active == ["BRENT", "WTI"]
    hits = [
        r for r in caplog.records if "Tradeability lookup failed" in r.getMessage()
    ]
    assert len(hits) == 2, caplog.text  # one per symbol, visible at WARNING
    assert all(r.levelno == logging.WARNING for r in hits)


def test_filter_disabled_keeps_symbols_and_makes_no_broker_call(tmp_path):
    """Safety property 3: the flag switches the whole thing off."""
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker, tradeability_filter_enabled=False)
    active = bot._select_active_symbols(account_snapshot=_account(), market_headlines=[])
    assert active == ["BRENT", "WTI"]
    assert broker.calls == []  # not even consulted


def test_higher_equity_rescues_a_symbol(tmp_path):
    """Same instrument, bigger account -> kept. Proves equity is really read."""
    broker = _BrokerWithConstraints(_UNTRADEABLE)
    bot = _make_bot(tmp_path, broker)
    # max_notional at 6,976 = 34,880 (< 50,000 forced) -> dropped
    assert bot._select_active_symbols(
        account_snapshot=_account(total=6976.0), market_headlines=[]
    ) == []
    # max_notional at 50,000 = 250,000 (> 50,000 forced) -> kept
    assert bot._select_active_symbols(
        account_snapshot=_account(total=50_000.0), market_headlines=[]
    ) == ["BRENT", "WTI"]


# --- IGRestBridge.get_market_constraints -------------------------------------


def _bridge(tmp_path, **overrides):
    from trading_bot.ig_bridge import IGRestBridge

    settings = _settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        json_log_path=tmp_path / "events.jsonl",
        **overrides,
    )
    return IGRestBridge(settings)


_MARKET_PAYLOAD = {
    "dealingRules": {"minDealSize": {"value": 1.0, "unit": "AMOUNT"}},
    "snapshot": {"bid": 9928.0, "offer": 9932.0, "marketStatus": "TRADEABLE"},
}


def test_bridge_init_creates_the_constraints_cache(tmp_path):
    assert _bridge(tmp_path)._market_constraints_cache == {}


def test_bridge_parses_min_deal_size_and_mid_price(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._resolve_epic = lambda symbol: "EPIC.BRENT"
    bridge._request = MagicMock(return_value=_MARKET_PAYLOAD)

    assert bridge.get_market_constraints("brent") == {
        "min_deal_size": 1.0,
        "price": 9930.0,  # mid of 9928 / 9932
    }
    bridge._request.assert_called_once_with("GET", "/markets/EPIC.BRENT", version="3")


def test_bridge_caches_within_ttl_and_is_read_only(tmp_path):
    """One IG call per symbol per TTL, and never anything but a GET."""
    bridge = _bridge(tmp_path)
    bridge._resolve_epic = lambda symbol: "EPIC.BRENT"
    bridge._request = MagicMock(return_value=_MARKET_PAYLOAD)

    first = bridge.get_market_constraints("BRENT")
    second = bridge.get_market_constraints("BRENT")

    assert first == second
    assert bridge._request.call_count == 1
    methods = {call.args[0] for call in bridge._request.call_args_list}
    assert methods == {"GET"}  # never POST/PUT/DELETE: cannot open/close a deal


def test_bridge_expired_cache_refetches(tmp_path):
    bridge = _bridge(tmp_path, market_constraints_cache_ttl_minutes=0)
    bridge._resolve_epic = lambda symbol: "EPIC.BRENT"
    bridge._request = MagicMock(return_value=_MARKET_PAYLOAD)

    bridge.get_market_constraints("BRENT")
    bridge.get_market_constraints("BRENT")
    assert bridge._request.call_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},                                                        # nothing
        {"dealingRules": {}, "snapshot": {"bid": 1.0, "offer": 2.0}},  # no min size
        {"dealingRules": {"minDealSize": {"value": 1.0}}, "snapshot": {}},  # no price
        {"dealingRules": {"minDealSize": {"value": 0}},
         "snapshot": {"bid": 1.0, "offer": 2.0}},                  # zero min size
    ],
)
def test_bridge_returns_none_on_unusable_payload(tmp_path, payload):
    bridge = _bridge(tmp_path)
    bridge._resolve_epic = lambda symbol: "EPIC.X"
    bridge._request = MagicMock(return_value=payload)
    assert bridge.get_market_constraints("BRENT") is None


@pytest.mark.parametrize("boom", [RuntimeError("no epic"), ValueError("bad json")])
@pytest.mark.parametrize(
    "symbol",
    ["BRENT", None, 123, 12.5, [], object()],
    ids=["str", "none", "int", "float", "list", "object"],
)
def test_bridge_never_raises(tmp_path, boom, symbol):
    """Any failure is None, which callers treat as 'unknown, do not filter'.

    Review finding F2: the ``symbol`` axis is new. ``symbol = symbol.upper()``
    sat OUTSIDE the try, so any non-str argument raised AttributeError straight
    through the "Never raises" docstring. The old test only exercised
    exceptions raised from ``_resolve_epic``, so it missed this entirely.
    A non-str symbol cannot resolve to an epic in production either, so the
    raising ``_resolve_epic`` here is the realistic downstream behaviour.
    """
    bridge = _bridge(tmp_path)

    def _explode(*a, **k):
        raise boom

    bridge._resolve_epic = _explode
    bridge._request = MagicMock(side_effect=AssertionError("must not be reached"))
    assert bridge.get_market_constraints(symbol) is None
    assert bridge._market_constraints_cache == {}  # failures are not cached


def test_bridge_returns_a_copy_so_callers_cannot_poison_the_cache(tmp_path):
    """Review finding F4: the cached dict was handed out by reference.

    A caller mutating the returned dict corrupted the cache for the whole TTL,
    and every later caller in that window saw the poisoned values.
    """
    bridge = _bridge(tmp_path)
    bridge._resolve_epic = lambda symbol: "EPIC.BRENT"
    bridge._request = MagicMock(return_value=_MARKET_PAYLOAD)
    clean = {"min_deal_size": 1.0, "price": 9930.0}

    first = bridge.get_market_constraints("BRENT")
    assert first == clean
    first["price"] = 0.0            # hostile / careless caller
    first["min_deal_size"] = 999.0

    second = bridge.get_market_constraints("BRENT")
    assert bridge._request.call_count == 1  # genuinely served from cache
    assert second == clean                  # cache survived the mutation
    assert second is not first              # and is a distinct object

    second["price"] = -1.0                  # mutate the cache-hit result too
    assert bridge.get_market_constraints("BRENT") == clean
    assert bridge._market_constraints_cache["BRENT"][1] == clean


def test_bridge_accessor_is_additive_only():
    """The authorised edit adds a method; it must not shadow an existing one."""
    from trading_bot.ig_bridge import IGRestBridge

    assert hasattr(IGRestBridge, "get_market_constraints")
    # The live-money entry points must still exist and be untouched in shape.
    for name in ("execute", "_build_open_payload", "_normalize_size", "connect"):
        assert hasattr(IGRestBridge, name), name


# =============================================================================
# TASK 4b -- Cash risk at stop
#
# The dimensionally-correct model, mirroring ig_bridge.py:596-600. Runs on
# built symbol contexts (which have the ATR) but still before decide().
# =============================================================================


def test_cash_risk_breach_mirrors_ig_bridge_formula():
    """A forced min-deal-size position whose stop-out loss exceeds the budget
    ceiling is a structural breach -- the same test ig_bridge applies at
    submit time, but evaluated before the LLM sees the symbol."""
    from trading_bot.bot import structural_cash_risk_breach
    from trading_bot.instruments import get_instrument

    settings = Settings(_env_file=None)
    inst = get_instrument("BRENT")
    # ATR 1.2 price-units -> stop = max(1.2*1.5, 99.3*0.005, 0.01) = 1.80
    # -> 180 IG pts at ig_price_scale=100; forced size 1.0 -> risk GBP 180.
    # Budget at GBP 6976: 6976*0.025 = 174.40; ceiling *1.25 = 218.00.
    breach, diag = structural_cash_risk_breach(
        settings=settings, instrument=inst, price=99.3, atr_14=1.2,
        equity=6976.0, min_deal_size=1.0,
    )
    assert breach is False          # 180 <= 218
    assert diag["forced_cash_risk"] == pytest.approx(180.0, rel=1e-3)

    # Same instrument, wider ATR -> stop 4.5 price-units -> 450 pts -> GBP 450.
    breach2, diag2 = structural_cash_risk_breach(
        settings=settings, instrument=inst, price=99.3, atr_14=3.0,
        equity=6976.0, min_deal_size=1.0,
    )
    assert breach2 is True          # 450 > 218
    assert diag2["budget_ceiling"] == pytest.approx(218.0, rel=1e-3)


def test_no_breach_when_min_deal_size_does_not_inflate():
    """If the budget-correct size already exceeds the broker minimum there is
    no forced inflation, so there can be no structural breach."""
    from trading_bot.bot import structural_cash_risk_breach
    from trading_bot.instruments import get_instrument

    breach, _ = structural_cash_risk_breach(
        settings=Settings(_env_file=None), instrument=get_instrument("BRENT"),
        price=99.3, atr_14=1.2, equity=6976.0, min_deal_size=0.001,
    )
    assert breach is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_non_finite_or_nonpositive_inputs_never_breach(bad):
    """Garbage must never shrink the universe."""
    from trading_bot.bot import structural_cash_risk_breach
    from trading_bot.instruments import get_instrument

    for field in ("price", "atr_14", "equity", "min_deal_size"):
        kwargs = dict(
            settings=Settings(_env_file=None), instrument=get_instrument("BRENT"),
            price=99.3, atr_14=1.2, equity=6976.0, min_deal_size=1.0,
        )
        kwargs[field] = bad
        breach, _ = structural_cash_risk_breach(**kwargs)
        assert breach is False, f"{field}={bad} must not breach"


@pytest.mark.parametrize(
    "bad", [_NAN, _INF, _NEG_INF, 0.0, -1.0, None],
    ids=["nan", "inf", "-inf", "zero", "negative", "none"],
)
@pytest.mark.parametrize("field", ["price", "atr_14", "equity", "min_deal_size"])
def test_unusable_inputs_are_reported_as_insufficient_data(field, bad):
    """The guard must *recognise* garbage, not merely survive it.

    ``test_non_finite_or_nonpositive_inputs_never_breach`` is tautological for
    ``nan``: with the guard deleted, every nan path happens to fall out as
    "no breach" anyway (nan comparisons are all False), so it cannot detect a
    missing guard. Asserting the diagnostics reason pins the guard itself --
    delete it and there is no ``reason`` key to read.
    """
    from trading_bot.bot import structural_cash_risk_breach

    kwargs = dict(
        settings=_settings(), instrument=get_instrument("BRENT"),
        price=99.3, atr_14=1.2, equity=6976.0, min_deal_size=1.0,
    )
    kwargs[field] = bad
    breach, diag = structural_cash_risk_breach(**kwargs)
    assert breach is False
    assert diag.get("reason") == "insufficient_data", f"{field}={bad!r}: {diag}"


def test_diagnostics_report_the_budget_correct_size_when_it_dominates():
    """``max(raw_size, min_deal_size)`` -- not ``min_deal_size`` -- is what the
    sizer will actually submit.

    The breach *verdict* cannot distinguish the two (when raw_size wins, the
    forced cash risk equals the target exactly, which is inside the ceiling by
    construction; when min_deal_size wins, the expressions are identical). Only
    the diagnostics can, and they are what gets logged for the operator, so pin
    them here.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings()
    breach, diag = structural_cash_risk_breach(
        settings=s, instrument=get_instrument("BRENT"),
        price=99.3, atr_14=1.2, equity=6976.0, min_deal_size=0.001,
    )
    assert breach is False
    # raw_size = 174.40 / 180.0 = 0.96889, which is >> min_deal_size 0.001.
    assert diag["raw_size"] == pytest.approx(0.968889, rel=1e-4)
    assert diag["forced_size"] == pytest.approx(diag["raw_size"], rel=1e-9)
    # No inflation => cash risk lands exactly on the target, not on the floor.
    assert diag["forced_cash_risk"] == pytest.approx(174.40, rel=1e-4)
    assert diag["target_cash_risk"] == pytest.approx(174.40, rel=1e-4)


def test_breach_boundary_is_exact_and_inclusive():
    """Pin the exact comparison, including which side of the line is allowed.

    At GBP 7,200 equity: target = 180.00, ceiling = 180 * 1.25 = 225.00.
    ATR 1.5 -> stop 2.25 price-units -> 225.0 IG pts, so min_deal_size 1.0
    forces a cash risk of exactly 225.00 == the ceiling. Exactly on the line is
    ALLOWED (the guard is a strict ``>``, matching ig_bridge). All the values
    here are exactly representable in binary floating point, so this really is
    an equality test and it kills ``>=`` as well as ``<``/``<=``.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings()
    inst = get_instrument("BRENT")

    def call(min_deal_size: float):
        return structural_cash_risk_breach(
            settings=s, instrument=inst, price=99.3, atr_14=1.5,
            equity=7200.0, min_deal_size=min_deal_size,
        )

    on_line, diag = call(1.0)
    assert diag["budget_ceiling"] == 225.0
    assert diag["forced_cash_risk"] == 225.0
    assert diag["raw_size"] == 0.8            # broker minimum really does inflate
    assert on_line is False                   # equal is inside the budget

    just_over, _ = call(1.0001)
    assert just_over is True

    just_under, diag_under = call(0.9999)
    assert just_under is False
    assert diag_under["forced_cash_risk"] < 225.0


def test_ig_price_scale_is_applied_to_the_stop():
    """The bot-units -> IG-points conversion is load-bearing.

    Drop the multiplication and the filter silently becomes 100x weaker on
    BRENT/WTI (and 10,000x on FX): the same inputs flip from breach to no
    breach. Pin it by holding every input constant and moving only the scale.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings()
    scaled = get_instrument("BRENT")
    assert scaled.ig_price_scale == 100.0     # guards against a data change
    unscaled = replace(scaled, ig_price_scale=1.0)

    common = dict(settings=s, price=99.3, atr_14=3.0, equity=6976.0, min_deal_size=1.0)
    breach_scaled, diag_scaled = structural_cash_risk_breach(instrument=scaled, **common)
    breach_plain, diag_plain = structural_cash_risk_breach(instrument=unscaled, **common)

    assert diag_scaled["stop_ig_points"] == pytest.approx(450.0)
    assert diag_plain["stop_ig_points"] == pytest.approx(4.5)
    assert breach_scaled is True
    assert breach_plain is False


def test_fx_scale_of_10000_is_applied():
    """The other non-1.0 scale in production, in the opposite direction."""
    from trading_bot.bot import structural_cash_risk_breach

    eurusd = get_instrument("EURUSD")
    assert eurusd.ig_price_scale == 10000.0
    # stop = max(0.008*1.5, 1.083*0.005, 0.01) = 0.012 -> 120 IG pts (pips).
    # raw_size = 174.40/120 = 1.4533; a 2.0-contract minimum inflates it to
    # 2.0 -> GBP 240 at stop > ceiling 218.
    breach, diag = structural_cash_risk_breach(
        settings=_settings(), instrument=eurusd, price=1.0830, atr_14=0.008,
        equity=6976.0, min_deal_size=2.0,
    )
    assert diag["stop_ig_points"] == pytest.approx(120.0)
    assert diag["forced_cash_risk"] == pytest.approx(240.0)
    assert breach is True


def test_percentage_stop_floor_is_applied():
    """The stop must be the sizer's stop (risk.py:354), floors included.

    Every ATR in the other tests dominates ``price * stop_loss_pct_floor``, so
    deleting that term from the ``max()`` survived them all. GOLD at USD 4,700
    with a quiet ATR of 14.0 is the realistic case where the percentage floor
    wins: 14.0*1.5 = 21.0 but 4700*0.005 = 23.5, and at ig_price_scale=10 that
    is 235 IG pts rather than 210 -- which is the difference between breaching
    the 218 ceiling and not.
    """
    from trading_bot.bot import structural_cash_risk_breach

    gold = get_instrument("GOLD")
    assert gold.ig_price_scale == 10.0
    breach, diag = structural_cash_risk_breach(
        settings=_settings(), instrument=gold, price=4700.0, atr_14=14.0,
        equity=6976.0, min_deal_size=1.0,
    )
    assert diag["stop_ig_points"] == pytest.approx(235.0)   # not 210.0
    assert diag["raw_size"] == pytest.approx(0.742128, rel=1e-4)  # min really inflates
    assert diag["forced_cash_risk"] == pytest.approx(235.0)
    assert breach is True                                   # 235 > 218


def test_absolute_stop_floor_is_applied():
    """The 0.01 absolute floor in the sizer's ``max()`` must be there too.

    On a very quiet FX tape both other terms fall below it: EURUSD ATR 0.005
    gives 0.0075 and the percentage floor gives 0.005415, so the stop is the
    0.01 absolute floor = 100 pips, not 75.
    """
    from trading_bot.bot import structural_cash_risk_breach

    breach, diag = structural_cash_risk_breach(
        settings=_settings(), instrument=get_instrument("EURUSD"), price=1.0830,
        atr_14=0.005, equity=6976.0, min_deal_size=2.5,
    )
    assert diag["stop_ig_points"] == pytest.approx(100.0)   # not 75.0, not 54.15
    assert diag["forced_cash_risk"] == pytest.approx(250.0)
    assert breach is True                                   # 250 > 218


def test_point_value_per_unit_is_applied():
    """``point_value_per_unit`` is the third factor in ig_bridge's product and
    must not be silently dropped.

    It defaults to 1.0, so every other test in this file would pass with the
    multiplication deleted -- that mutant survived until this test existed. Pin
    it by moving only the point value.
    """
    from trading_bot.bot import structural_cash_risk_breach

    assert _settings().point_value_per_unit == 1.0   # default must not move
    common = dict(
        instrument=get_instrument("BRENT"), price=99.3, atr_14=1.2,
        equity=6976.0, min_deal_size=1.0,
    )
    # 180 IG pts. At GBP 1/pt the forced risk is GBP 180 <= ceiling 218.
    unit_breach, unit_diag = structural_cash_risk_breach(
        settings=_settings(point_value_per_unit=1.0), **common
    )
    assert unit_diag["forced_cash_risk"] == pytest.approx(180.0)
    assert unit_breach is False
    # At GBP 2/pt the identical stop costs GBP 360 > 218.
    doubled_breach, doubled_diag = structural_cash_risk_breach(
        settings=_settings(point_value_per_unit=2.0), **common
    )
    assert doubled_diag["stop_ig_points"] == pytest.approx(180.0)  # stop unchanged
    assert doubled_diag["forced_cash_risk"] == pytest.approx(360.0)
    assert doubled_diag["raw_size"] == pytest.approx(0.484444, rel=1e-4)
    assert doubled_breach is True


def test_brent_breach_threshold_at_current_equity():
    """The number this task exists to move (contrast with
    ``test_brent_at_current_equity_is_not_caught_by_the_floor_stop`` above).

    At GBP 6,976 with min_deal_size 1.0, BRENT breaches once
    atr_14 > 218 / (1.5 * 100) = 1.4533 price-units (~USD 1.45 of Brent).
    Realistic Brent daily ATR(14) is USD 1.5-2.5, so the motivating case is now
    caught on a typical cycle -- while a quiet USD 1.20-ATR cycle still trades.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings()
    inst = get_instrument("BRENT")
    threshold = (
        6976.0 * s.risk_per_trade_pct * (1.0 + s.risk_size_vs_budget_tolerance)
    ) / (s.atr_stop_multiplier * inst.ig_price_scale)
    assert threshold == pytest.approx(1.453333, rel=1e-5)

    def breach(atr_14: float) -> bool:
        result, _ = structural_cash_risk_breach(
            settings=s, instrument=inst, price=99.3, atr_14=atr_14,
            equity=6976.0, min_deal_size=1.0,
        )
        return result

    assert breach(threshold * 0.99) is False   # quiet tape: still tradeable
    assert breach(threshold * 1.01) is True    # normal tape: dropped
    assert breach(2.0) is True
    assert breach(2.5) is True


def test_higher_equity_rescues_the_same_atr():
    """Proves equity is really read, and that the drop is not sticky: the very
    same instrument/ATR is fillable on a bigger account."""
    from trading_bot.bot import structural_cash_risk_breach

    common = dict(
        settings=_settings(), instrument=get_instrument("BRENT"),
        price=99.3, atr_14=3.0, min_deal_size=1.0,
    )
    assert structural_cash_risk_breach(equity=6_976.0, **common)[0] is True
    assert structural_cash_risk_breach(equity=20_000.0, **common)[0] is False


def test_predicate_agrees_with_the_ig_bridge_submit_time_guard():
    """Cross-check against ig_bridge's arithmetic, written out independently.

    ig_bridge computes ``target_cash_risk`` from the *plan's* raw quantity and
    rejects when ``size * stop_distance * point_value`` exceeds
    ``target * (1 + tolerance)``. Recreating that here from the sizer's own
    formula (risk.py:354-363) proves the pre-LLM predicate is not just
    self-consistent but predicts the broker-layer rejection.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings()
    for symbol, price, atr_14, min_deal_size in [
        ("BRENT", 99.3, 3.0, 1.0),
        ("BRENT", 99.3, 1.2, 1.0),
        ("WTI", 65.42, 2.0, 1.0),
        ("WTI", 65.42, 0.9, 0.01),
        ("EURUSD", 1.0830, 0.008, 2.0),
        ("GOLD", 4700.0, 30.0, 0.1),
        ("GOLD", 4700.0, 14.0, 1.0),        # percentage stop floor dominates
        ("EURUSD", 1.0830, 0.005, 2.5),     # absolute 0.01 stop floor dominates
    ]:
        inst = get_instrument(symbol)
        # --- sizer (risk.py) ---
        unscaled_stop = max(
            atr_14 * s.atr_stop_multiplier, price * s.stop_loss_pct_floor, 0.01
        )
        stop_distance = unscaled_stop * inst.ig_price_scale
        point_value = s.point_value_per_unit
        target_cash_risk = 6976.0 * s.risk_per_trade_pct
        raw_size = target_cash_risk / (stop_distance * point_value)
        # --- ig_bridge (_build_open_payload) ---
        size = max(raw_size, min_deal_size)     # what _normalize_size does
        cash_risk_at_stop = size * stop_distance * point_value
        ceiling = (raw_size * stop_distance * point_value) * (
            1.0 + s.risk_size_vs_budget_tolerance
        )
        expected = cash_risk_at_stop > ceiling
        # --- predicate under test ---
        actual, diag = structural_cash_risk_breach(
            settings=s, instrument=inst, price=price, atr_14=atr_14,
            equity=6976.0, min_deal_size=min_deal_size,
        )
        assert actual is expected, f"{symbol} atr={atr_14}: {diag}"


# --- Wiring into the built-context pipeline -----------------------------------


def _indicators(price: float, atr_14: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
        rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=atr_14,
        volatility_20d=0.0, return_1d=0.0, return_5d=0.0,
    )


def _context(symbol: str, *, price: float, atr_14: float) -> SymbolContext:
    inst = get_instrument(symbol)
    return SymbolContext(
        symbol=inst.symbol,
        display_name=inst.display_name,
        asset_class=inst.asset_class,
        as_of="2026-07-29T00:00:00+00:00",
        indicators=_indicators(price, atr_14),
        headlines=[],
        chart_note="test",
    )


# BRENT at atr 3.0 -> 450 IG pts -> GBP 450 forced risk vs GBP 218 ceiling.
def _brent_breaching() -> SymbolContext:
    return _context("BRENT", price=99.3, atr_14=3.0)


# WTI at atr 0.9 -> 135 IG pts -> raw_size 1.29 > min 0.01, no inflation.
def _wti_fillable() -> SymbolContext:
    return _context("WTI", price=65.42, atr_14=0.9)


class _MinSizeBroker(_BrokerNoAccessor):
    """Broker that answers with a per-symbol minimum deal size."""

    def __init__(self, per_symbol: dict[str, float] | None = None, default=1.0):
        self._per_symbol = per_symbol or {}
        self._default = default
        self.calls: list[str] = []

    def get_market_constraints(self, symbol: str):
        self.calls.append(symbol)
        value = self._per_symbol.get(symbol.upper(), self._default)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return {"min_deal_size": value, "price": 9930.0}


def test_unfillable_context_is_dropped(tmp_path):
    """The whole point: a symbol whose ATR makes it unfillable this cycle never
    reaches the LLM payload."""
    broker = _MinSizeBroker({"BRENT": 1.0, "WTI": 0.01})
    bot = _make_bot(tmp_path, broker)
    contexts = {"BRENT": _brent_breaching(), "WTI": _wti_fillable()}

    kept = bot._drop_unfillable_contexts(contexts, account_snapshot=_account())

    assert list(kept) == ["WTI"]
    assert broker.calls == ["BRENT", "WTI"]   # both really were evaluated


def test_fillable_contexts_are_untouched(tmp_path):
    """The filter may only ever REMOVE: same objects, same order, no additions."""
    broker = _MinSizeBroker(default=0.01)
    bot = _make_bot(tmp_path, broker)
    contexts = {"WTI": _wti_fillable(), "BRENT": _context("BRENT", price=99.3, atr_14=1.2)}

    kept = bot._drop_unfillable_contexts(contexts, account_snapshot=_account())

    assert list(kept) == ["WTI", "BRENT"]     # order preserved
    assert kept["WTI"] is contexts["WTI"]     # identical objects, not rebuilt
    assert kept["BRENT"] is contexts["BRENT"]


def test_open_position_context_is_never_dropped(tmp_path):
    """Safety property 2: we must keep managing what we already hold."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    held = OpenPosition(
        position_id="P1", symbol="BRENT", direction="buy", quantity=1.0,
        current_price=99.3,
    )
    contexts = {"BRENT": _brent_breaching()}

    kept = bot._drop_unfillable_contexts(
        contexts, account_snapshot=_account(open_positions=[held])
    )

    assert list(kept) == ["BRENT"]
    assert broker.calls == []                 # bypassed before the lookup


@pytest.mark.parametrize("held_symbol", [" BRENT ", "Brent\n", "\tbrent\t"])
def test_whitespace_padded_open_position_context_is_never_dropped(tmp_path, held_symbol):
    """The bypass set must be built with ``.strip().upper()``, matching the
    existing convention in ``_select_active_symbols``. Without ``.strip()`` a
    held symbol reported as " BRENT " never matches its own context key and the
    position we hold gets hidden from the LLM."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    held = OpenPosition(
        position_id="P1", symbol=held_symbol, direction="buy", quantity=1.0,
        current_price=99.3,
    )

    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()},
        account_snapshot=_account(open_positions=[held]),
    )

    assert list(kept) == ["BRENT"]
    assert broker.calls == []


def test_broker_without_accessor_drops_nothing(tmp_path):
    """Safety property 3a: PaperBroker has no get_market_constraints."""
    bot = _make_bot(tmp_path, _BrokerNoAccessor())
    contexts = {"BRENT": _brent_breaching()}
    kept = bot._drop_unfillable_contexts(contexts, account_snapshot=_account())
    assert list(kept) == ["BRENT"]


def test_accessor_returning_none_drops_nothing(tmp_path):
    """Safety property 3b: None means unknown, not unfillable."""
    broker = _MinSizeBroker(default=None)
    bot = _make_bot(tmp_path, broker)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )
    assert list(kept) == ["BRENT"]
    assert broker.calls == ["BRENT"]          # consulted, and still kept


def test_accessor_raising_drops_nothing(tmp_path):
    """A broken broker must not shrink the universe or abort the cycle."""
    broker = _MinSizeBroker(default=RuntimeError("IG down"))
    bot = _make_bot(tmp_path, broker)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )
    assert list(kept) == ["BRENT"]


def test_malformed_constraints_drop_nothing(tmp_path):
    """A payload without min_deal_size is 'unknown', not 'unfillable'."""

    class _NoMinSize(_BrokerNoAccessor):
        def get_market_constraints(self, symbol: str):
            return {"price": 9930.0}

    bot = _make_bot(tmp_path, _NoMinSize())
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )
    assert list(kept) == ["BRENT"]


@pytest.mark.parametrize("value", [_NAN, _INF, _NEG_INF, 0.0, -1.0])
def test_non_finite_min_deal_size_drops_nothing(tmp_path, value):
    """Safety property 1, end-to-end: corrupt constraints mean keep."""
    broker = _MinSizeBroker(default=value)
    bot = _make_bot(tmp_path, broker)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )
    assert list(kept) == ["BRENT"]
    assert broker.calls == ["BRENT"]


@pytest.mark.parametrize("bad_equity", [_NAN, _INF, 0.0, -1.0])
def test_corrupt_equity_drops_nothing(tmp_path, bad_equity):
    """A corrupt account snapshot must not shrink the payload either."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account(total=bad_equity)
    )
    assert list(kept) == ["BRENT"]


@pytest.mark.parametrize("bad_atr", [_NAN, _INF, 0.0, -1.0])
def test_corrupt_atr_drops_nothing(tmp_path, bad_atr):
    """A context with an unusable ATR (e.g. the price-only fallback context,
    which sets atr_14=0.0) must be kept, not dropped."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _context("BRENT", price=99.3, atr_14=bad_atr)},
        account_snapshot=_account(),
    )
    assert list(kept) == ["BRENT"]


def test_malformed_context_is_kept_and_does_not_abort_the_cycle(tmp_path):
    """Safety property 5: one bad context must not take the cycle down."""

    class _Exploding:
        @property
        def indicators(self):
            raise RuntimeError("corrupt context")

    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    contexts = {"BRENT": _Exploding(), "WTI": _wti_fillable()}

    kept = bot._drop_unfillable_contexts(contexts, account_snapshot=_account())

    assert list(kept) == ["BRENT", "WTI"]     # kept, and WTI still evaluated


def test_filter_disabled_drops_nothing_and_makes_no_broker_call(tmp_path):
    """Safety property 4: the flag switches the whole thing off."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker, tradeability_filter_enabled=False)
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )
    assert list(kept) == ["BRENT"]
    assert broker.calls == []


def test_drop_is_logged_at_info_with_diagnostics(tmp_path, caplog):
    """The operator needs to see why a symbol vanished from the payload."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    with caplog.at_level(logging.INFO, logger="trading_bot.bot"):
        bot._drop_unfillable_contexts(
            {"BRENT": _brent_breaching()}, account_snapshot=_account()
        )
    hits = [
        r for r in caplog.records
        if "STRUCTURAL_CASH_RISK" in r.getMessage() and "BRENT" in r.getMessage()
    ]
    assert len(hits) == 1, caplog.text
    assert hits[0].levelno == logging.INFO
    message = hits[0].getMessage()
    assert "450" in message and "218" in message   # the diagnostics are present


def test_higher_equity_rescues_a_context(tmp_path):
    """Not sticky, not cached: the same context survives on a bigger account."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    assert list(
        bot._drop_unfillable_contexts(
            {"BRENT": _brent_breaching()}, account_snapshot=_account(total=6976.0)
        )
    ) == []
    assert list(
        bot._drop_unfillable_contexts(
            {"BRENT": _brent_breaching()}, account_snapshot=_account(total=20_000.0)
        )
    ) == ["BRENT"]


def test_filter_budgets_from_total_funds_not_free_funds(tmp_path):
    """A conscious permissive choice, pinned so it cannot drift silently.

    The sizer budgets from ``free_funds * risk_per_trade_pct_base``
    (risk.py:330), which is <= ``total_funds * risk_per_trade_pct``. Using total
    equity therefore makes the ceiling as generous as it can legitimately be, so
    this filter only ever removes symbols the broker would certainly reject. A
    false drop silently loses a real trade; a missed drop is still caught at
    submit time, so permissive is the correct direction to err.

    Here free=6,976 would breach (450 > 218) but total=20,000 does not
    (raw_size 1.11 > min 1.0, no inflation), and the symbol is KEPT.
    """
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    account = AccountSnapshot(
        mode="demo", currency="GBP", total_funds=20_000.0, free_funds=6_976.0,
    )
    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=account
    )
    assert list(kept) == ["BRENT"]


def test_low_atr_cycle_keeps_the_same_symbol_a_high_atr_cycle_drops(tmp_path):
    """Per-cycle and dynamic by design. BRENT took 4 of the 11 historical
    fills, so a blanket ban would have removed real trades: only the cycle
    whose ATR is genuinely unfillable loses the symbol."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    quiet = {"BRENT": _context("BRENT", price=99.3, atr_14=1.2)}
    wild = {"BRENT": _brent_breaching()}

    assert list(bot._drop_unfillable_contexts(quiet, account_snapshot=_account())) == ["BRENT"]
    assert list(bot._drop_unfillable_contexts(wild, account_snapshot=_account())) == []
    # and back again -- no state carried over
    assert list(bot._drop_unfillable_contexts(quiet, account_snapshot=_account())) == ["BRENT"]


# --- run_cycle wiring: the filter must run BEFORE decide() -------------------


class _Reached(Exception):
    """Sentinel: the LLM decision call was reached."""


def _make_cycle_bot(tmp_path: Path, broker, **overrides):
    """A bot wired just far enough to execute run_cycle up to decide()."""
    bot = _make_bot(tmp_path, broker, **overrides)
    bot.broker.get_account_snapshot = MagicMock(return_value=_account())
    bot.news_client = MagicMock()
    bot.news_client.fetch_market_headlines = MagicMock(return_value=[])
    bot.entity_news_client = MagicMock()
    bot.entity_news_client.fetch_all = MagicMock(return_value={})
    bot.vix_client = MagicMock()
    bot.vix_client.fetch = MagicMock(return_value=None)
    bot.correlation_client = MagicMock()
    bot.correlation_client.fetch = MagicMock(return_value=None)
    bot.econ_calendar = MagicMock()
    bot.econ_calendar.events_next_hours = MagicMock(return_value=[])
    bot.prorealtime_client = MagicMock()
    bot.prorealtime_client.load_snapshot = MagicMock(return_value=None)
    bot.market_data_client = MagicMock()
    bot.risk_manager = MagicMock()
    bot.risk_manager.build_execution_plans = MagicMock(return_value=[])
    bot.alerts = MagicMock()
    bot.paper_pnl = None
    bot.stop_event = Event()
    bot.wake_event = Event()
    bot.operator_lock = Lock()
    bot.model_lock = Lock()
    bot._current_entity_news = {}
    bot._current_market_vix = None
    bot._current_econ_events = []
    bot._current_cycle_id = "test_cycle"
    bot._cycle_counter = 0
    bot._memory_profile_started = False
    # Trim the cycle to the section under test.
    bot._news_memory_metrics = MagicMock(
        return_value={"size": 0, "oldest_published_at": None}
    )
    bot._refresh_runtime_state = MagicMock()
    bot._get_operator_context = MagicMock(return_value={})
    bot._fetch_macro_context_cached = MagicMock(return_value=None)
    bot._compute_entry_block_lite = MagicMock(
        return_value={
            "block_new_entries": False,
            "daily_loss_breached": False,
            "block_reasons": [],
            "drawdown_close_only": False,
            "drawdown_pct": 0.0,
        }
    )
    bot._build_market_research = MagicMock(return_value=None)
    bot._build_guardrail_context = MagicMock(return_value={})
    bot._build_symbol_research = MagicMock(return_value=None)
    bot._select_active_symbols = MagicMock(return_value=["BRENT"])
    return bot


def test_run_cycle_reaches_the_llm_for_a_fillable_context(tmp_path):
    """Positive control for the test below: with a fillable context the cycle
    really does get as far as decide(). Without this, "decide was not called"
    proves nothing."""
    broker = _MinSizeBroker(default=0.01)
    bot = _make_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._collect_symbol_contexts = MagicMock(
        return_value={"BRENT": _context("BRENT", price=99.3, atr_14=1.2)}
    )
    captured: dict = {}

    def _decide(**kwargs):
        captured["contexts"] = dict(kwargs["symbol_contexts"])
        raise _Reached

    bot.decision_engine.decide = _decide

    with pytest.raises(_Reached):
        bot.run_cycle()

    assert list(captured["contexts"]) == ["BRENT"]


def test_run_cycle_drops_the_unfillable_context_before_the_llm(tmp_path):
    """The token saving: an unfillable symbol is gone before decide() runs.

    This is the test that fails if the filter is never called from run_cycle,
    however correct the predicate is in isolation.
    """
    broker = _MinSizeBroker(default=1.0)
    bot = _make_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._collect_symbol_contexts = MagicMock(
        return_value={"BRENT": _brent_breaching()}
    )
    bot.decision_engine.decide = MagicMock(
        side_effect=AssertionError("decide() saw a structurally unfillable symbol")
    )

    results = bot.run_cycle()

    assert results == []
    bot.decision_engine.decide.assert_not_called()
    # Not vacuous: the cycle really did reach the context-collection stage and
    # really did get a context back, and the broker really was consulted.
    bot._collect_symbol_contexts.assert_called_once()
    assert broker.calls == ["BRENT"]


def test_run_cycle_keeps_a_held_unfillable_symbol_in_the_llm_payload(tmp_path):
    """Safety property 2 through the real cycle: we must be able to close what
    we hold even when it is structurally unfillable to open."""
    broker = _MinSizeBroker(default=1.0)
    bot = _make_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    held = OpenPosition(
        position_id="P1", symbol="BRENT", direction="buy", quantity=1.0,
        current_price=99.3,
    )
    bot.broker.get_account_snapshot = MagicMock(
        return_value=_account(open_positions=[held])
    )
    bot._collect_symbol_contexts = MagicMock(
        return_value={"BRENT": _brent_breaching()}
    )
    captured: dict = {}

    def _decide(**kwargs):
        captured["contexts"] = dict(kwargs["symbol_contexts"])
        raise _Reached

    bot.decision_engine.decide = _decide

    with pytest.raises(_Reached):
        bot.run_cycle()

    assert list(captured["contexts"]) == ["BRENT"]
