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
    # Set in TradingBot.__init__; needed here because these tests build the bot
    # via __new__, and the filter tags its drop events with the cycle id exactly
    # as every other json_log call site in bot.py does.
    bot._current_cycle_id = ""
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
    # Always set in TradingBot.__init__ (bot.py:347); needed explicitly here
    # because these tests build the bot via __new__. Default off so a cycle that
    # returns early does not silently depend on Track-A being wired.
    bot.track_a_pnl = None
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


# =============================================================================
# Review findings on ae91764 (F1-F4). See .superpowers/sdd/task-4b-report.md,
# section "Fix pass (review findings)".
# =============================================================================


# --- F3: the "strictly more permissive than the broker" invariant ------------


def test_confidence_scaling_above_one_must_not_over_remove():
    """F3. The predicate's safety argument must be self-enforcing.

    ``structural_cash_risk_breach`` is only sound if its budget is >= the one
    the real sizer will use, otherwise it drops symbols the broker would have
    happily filled -- a silent false drop, which is the worse failure direction
    (a missed drop is still caught at submit time; a false drop leaves no trade
    and no trace).

    The sizer scales its budget by ``confidence_multiplier``, which is bounded
    above by ``risk_confidence_scaling_max`` (risk.py). At the shipped default
    of 1.0 that can only shrink the budget, so ignoring it was safe. But
    ``tests/test_risk_sizing.py`` already constructs 2.5, and at 2.5 the real
    sizer's budget is 2.5x the predicate's: BRENT's raw_size becomes 0.9689,
    min_deal_size 1.0 barely inflates it, and the forced risk of GBP 450 is
    well inside the real ceiling of GBP 545. The predicate must agree and KEEP
    it. Hence ``max(1.0, settings.risk_confidence_scaling_max)``.
    """
    from trading_bot.bot import structural_cash_risk_breach

    kwargs = dict(
        instrument=get_instrument("BRENT"),
        price=99.3,
        atr_14=3.0,
        equity=6976.0,
        min_deal_size=1.0,
    )

    # At the default (1.0) this symbol is unfillable and must be dropped.
    breach, diag = structural_cash_risk_breach(settings=_settings(), **kwargs)
    assert breach is True
    assert diag["budget_ceiling"] == pytest.approx(218.0)

    # Raise only the confidence-scaling ceiling. The real sizer can now afford
    # the forced size, so the filter must no longer remove it.
    scaled = _settings(risk_confidence_scaling_max=2.5)
    assert scaled.risk_confidence_scaling_max == 2.5      # the knob really moved
    breach, diag = structural_cash_risk_breach(settings=scaled, **kwargs)
    assert diag["target_cash_risk"] == pytest.approx(6976.0 * 0.025 * 2.5)
    assert diag["budget_ceiling"] == pytest.approx(545.0)
    assert diag["forced_cash_risk"] == pytest.approx(450.0)
    assert breach is False, "predicate over-removed a symbol the sizer can fill"


def test_confidence_scaling_below_one_is_clamped_to_one():
    """F3, other direction: the clamp must not let a *small* scaling_max
    shrink the predicate's budget below the sizer's.

    ``confidence_multiplier`` is ``1.0 + t * (max - 1.0)`` for t in [0, 1], so
    with max < 1.0 the sizer's multiplier ranges over [max, 1.0] -- the largest
    it can be is still 1.0. Budgeting off 0.5 here would halve the ceiling and
    start dropping fillable symbols, so the ``max(1.0, ...)`` floor matters as
    much as the ceiling.
    """
    from trading_bot.bot import structural_cash_risk_breach

    _, diag = structural_cash_risk_breach(
        settings=_settings(risk_confidence_scaling_max=0.5),
        instrument=get_instrument("BRENT"),
        price=99.3, atr_14=3.0, equity=6976.0, min_deal_size=1.0,
    )
    assert diag["target_cash_risk"] == pytest.approx(174.4)   # not 87.2
    assert diag["budget_ceiling"] == pytest.approx(218.0)


def test_budget_pct_takes_the_larger_of_the_two_risk_knobs():
    """F3. The sizer budgets from ``risk_per_trade_pct_base`` (risk.py:330),
    which is a *different knob* from ``risk_per_trade_pct``. They are equal by
    default; if the base is ever configured higher, the predicate must follow it
    up or it will under-budget and over-remove.
    """
    from trading_bot.bot import structural_cash_risk_breach

    s = _settings(risk_per_trade_pct_base=0.05)
    assert s.risk_per_trade_pct == 0.025 and s.risk_per_trade_pct_base == 0.05
    _, diag = structural_cash_risk_breach(
        settings=s,
        instrument=get_instrument("BRENT"),
        price=99.3, atr_14=3.0, equity=6976.0, min_deal_size=1.0,
    )
    assert diag["target_cash_risk"] == pytest.approx(6976.0 * 0.05)


# --- F4: the instrument must be looked up PER SYMBOL -------------------------


def test_filter_looks_up_the_instrument_for_each_symbol(tmp_path):
    """F4. Pins the per-symbol ``get_instrument(symbol)`` lookup.

    Every other filter-level test uses only BRENT and WTI, which share
    ``ig_price_scale == 100``, so a mutant hard-coding ``get_instrument("BRENT")``
    survived the whole suite. That is precisely the 100x mis-scaling failure
    mode, unguarded at the wiring layer.

    GOLD is quoted by IG in 1/10 dollars (scale 10), so its verdict FLIPS on the
    lookup. At $4,700 with ATR 10.0 the 0.5% floor gives a 23.5-unit stop:
      * true scale 10  -> 235 IG pts, raw_size 0.742 > min 0.1, no inflation,
        forced risk GBP 174.40 <= GBP 218 ceiling  -> KEPT
      * BRENT's scale 100 -> 2,350 IG pts, raw_size 0.074 forced up to 0.1,
        forced risk GBP 235.00 >  GBP 218 ceiling  -> WRONGLY DROPPED
    """
    from trading_bot.bot import structural_cash_risk_breach

    # The scales this test depends on, so an instruments-table change fails
    # loudly rather than silently weakening the assertion.
    assert get_instrument("GOLD").ig_price_scale == 10.0
    assert get_instrument("BRENT").ig_price_scale == 100.0

    gold_args = dict(price=4700.0, atr_14=10.0, equity=6976.0, min_deal_size=0.1)
    # The counterfactual, asserted directly: the verdict really does depend on
    # which instrument row is used, so the wiring assertion below has teeth.
    right, right_diag = structural_cash_risk_breach(
        settings=_settings(), instrument=get_instrument("GOLD"), **gold_args
    )
    wrong, wrong_diag = structural_cash_risk_breach(
        settings=_settings(), instrument=get_instrument("BRENT"), **gold_args
    )
    assert right is False and wrong is True
    assert right_diag["stop_ig_points"] == pytest.approx(235.0)
    assert wrong_diag["stop_ig_points"] == pytest.approx(2350.0)

    # Now through the filter, which must pick GOLD's own row. BRENT rides along
    # as a positive control: it proves the filter is live and really is dropping
    # things on this call, so "GOLD was kept" is not vacuous.
    broker = _MinSizeBroker({"GOLD": 0.1, "BRENT": 1.0})
    bot = _make_bot(tmp_path, broker)
    contexts = {
        "GOLD": _context("GOLD", price=4700.0, atr_14=10.0),
        "BRENT": _brent_breaching(),
    }

    kept = bot._drop_unfillable_contexts(contexts, account_snapshot=_account())

    assert list(kept) == ["GOLD"], (
        "GOLD was dropped -- the filter is not using GOLD's own ig_price_scale"
    )
    assert broker.calls == ["GOLD", "BRENT"]


# --- F2: a structural exclusion must be countable, not just greppable --------


def test_drop_emits_a_structured_event_with_diagnostics(tmp_path):
    """F2. The human-readable log line cannot be aggregated. On a branch whose
    purpose is an evidence loop, "how many symbols did this filter remove, and
    why" must be answerable from the event journal alone.
    """
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    bot.json_log = MagicMock()
    bot._current_cycle_id = "cycle-42"

    bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )

    events = [c for c in bot.json_log.log.call_args_list
              if c.args and c.args[0] == "structural_cash_risk_drop"]
    assert len(events) == 1, bot.json_log.log.call_args_list
    payload = events[0].args[1]
    assert payload["symbol"] == "BRENT"
    assert payload["min_deal_size"] == pytest.approx(1.0)
    assert payload["equity"] == pytest.approx(6976.0)
    # The reason, not just the fact: the same diagnostics the log line carries.
    assert payload["stop_ig_points"] == pytest.approx(450.0)
    assert payload["forced_size"] == pytest.approx(1.0)
    assert payload["forced_cash_risk"] == pytest.approx(450.0)
    assert payload["target_cash_risk"] == pytest.approx(174.4)
    assert payload["budget_ceiling"] == pytest.approx(218.0)
    # Same convention as every sibling json_log call in bot.py.
    assert events[0].kwargs.get("cycle_id") == "cycle-42"


def test_no_event_is_emitted_when_nothing_is_dropped(tmp_path):
    """F2. The counter must count drops, not cycles."""
    broker = _MinSizeBroker(default=0.01)
    bot = _make_bot(tmp_path, broker)
    bot.json_log = MagicMock()

    bot._drop_unfillable_contexts(
        {"WTI": _wti_fillable()}, account_snapshot=_account()
    )

    assert [c for c in bot.json_log.log.call_args_list
            if c.args and c.args[0] == "structural_cash_risk_drop"] == []


def test_drop_event_reaches_the_real_event_journal(tmp_path):
    """F2. End-to-end through the real JsonEventLogger: the diagnostics must
    actually serialise to JSON, not just reach a mock."""
    import json

    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)

    bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    drops = [r for r in records if r.get("event") == "structural_cash_risk_drop"]
    assert len(drops) == 1, records
    assert drops[0]["data"]["symbol"] == "BRENT"
    assert drops[0]["data"]["forced_cash_risk"] == pytest.approx(450.0)


def test_a_broken_event_logger_must_not_resurrect_a_dropped_symbol(tmp_path):
    """F2. Telemetry must never change the verdict.

    The emit sits inside the method's keep-on-error handler, so an exception
    escaping it would fall through to ``kept[symbol] = context`` and a logging
    failure would silently un-drop an unfillable symbol.
    """
    broker = _MinSizeBroker(default=1.0)
    bot = _make_bot(tmp_path, broker)
    bot.json_log = MagicMock()
    bot.json_log.log = MagicMock(side_effect=RuntimeError("disk full"))

    kept = bot._drop_unfillable_contexts(
        {"BRENT": _brent_breaching()}, account_snapshot=_account()
    )

    assert list(kept) == []


# --- F1: Track-A must NOT be narrowed by this filter -------------------------


def _make_whole_cycle_bot(tmp_path: Path, broker, **overrides):
    """`_make_cycle_bot`, extended far enough to run a cycle to completion.

    The Track-A call site sits *after* decide() and execution, so unlike the
    tests above this cycle cannot stop at the LLM.
    """
    from trading_bot.models import DecisionEnvelope

    bot = _make_cycle_bot(tmp_path, broker, **overrides)
    bot.decision_logger = MagicMock()
    bot._log_decision_records = MagicMock()
    bot._save_runtime_state = MagicMock()
    bot._write_cycle_log = MagicMock()
    bot.last_trade_times = {}
    bot.track_a_pnl = None
    bot.decision_engine.decide = MagicMock(
        return_value=DecisionEnvelope(
            market_regime="mixed", summary="test", trades=[]
        )
    )
    return bot


def test_track_a_gets_the_unfiltered_universe_while_the_llm_does_not(
    tmp_path, monkeypatch
):
    """F1. Track-A is the deterministic, LLM-free twin: it exists to measure
    whether the mechanical rules have edge, as the baseline the LLM track is
    scored against. It trades a PAPER BOOK, where IG's ``min_deal_size`` does
    not exist, so live-broker fillability is irrelevant to it.

    Narrowing its universe with this filter would corrupt the very measurement
    it exists to produce -- silently, and worst of all *selectively*: the drops
    land exactly on the volatile instruments, so the mechanical-vs-LLM
    comparison would no longer be over the same symbols.

    BRENT is unfillable this cycle and WTI is not. The LLM must see WTI only;
    Track-A must see both.
    """
    import trading_bot.track_a as track_a_module

    broker = _MinSizeBroker({"BRENT": 1.0, "WTI": 0.01})
    bot = _make_whole_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._select_active_symbols = MagicMock(return_value=["BRENT", "WTI"])
    bot._collect_symbol_contexts = MagicMock(
        return_value={"BRENT": _brent_breaching(), "WTI": _wti_fillable()}
    )
    bot.track_a_pnl = MagicMock()

    seen: dict = {}

    def _fake_track_a_cycle(symbol_contexts, paper, settings, **kwargs):
        seen["contexts"] = dict(symbol_contexts)
        return []

    monkeypatch.setattr(track_a_module, "run_track_a_cycle", _fake_track_a_cycle)

    bot.run_cycle()

    # The LLM path IS narrowed -- this is the whole point of the filter.
    llm_contexts = bot.decision_engine.decide.call_args.kwargs["symbol_contexts"]
    assert list(llm_contexts) == ["WTI"]

    # Track-A is NOT.
    assert "contexts" in seen, "run_track_a_cycle was never reached"
    assert list(seen["contexts"]) == ["BRENT", "WTI"], (
        "Track-A's universe was narrowed by the live-broker fillability filter; "
        "it trades a paper book and this biases the mechanical-vs-LLM comparison"
    )
    # Same objects, not rebuilt copies.
    assert seen["contexts"]["WTI"] is llm_contexts["WTI"]


def test_track_a_still_runs_when_every_symbol_is_dropped(tmp_path, monkeypatch):
    """F1, worst case. When the filter empties the universe, ``run_cycle``
    returns early -- and before this fix that early return sat *above* the
    Track-A call, so an all-breach cycle silently cost the mechanical baseline a
    whole cycle of signals.

    That is the maximal form of the bias F1 exists to prevent: not merely a
    narrowed universe but a missing observation, and one that lands
    preferentially on volatile days. Track-A trades a paper book, so the LLM
    path having nothing left is no reason for it to skip the cycle.
    """
    import trading_bot.track_a as track_a_module

    broker = _MinSizeBroker(default=1.0)          # every symbol unfillable
    bot = _make_whole_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._select_active_symbols = MagicMock(return_value=["BRENT"])
    bot._collect_symbol_contexts = MagicMock(return_value={"BRENT": _brent_breaching()})
    bot.track_a_pnl = MagicMock()

    seen: dict = {}

    def _fake_track_a_cycle(symbol_contexts, paper, settings, **kwargs):
        seen["contexts"] = dict(symbol_contexts)
        return []

    monkeypatch.setattr(track_a_module, "run_track_a_cycle", _fake_track_a_cycle)

    results = bot.run_cycle()

    # The LLM path really did short-circuit -- otherwise this test is vacuous.
    assert results == []
    bot.decision_engine.decide.assert_not_called()

    # ...but Track-A still got its full, unfiltered cycle.
    assert seen.get("contexts") is not None, (
        "Track-A lost a whole cycle because every symbol was unfillable for the "
        "LIVE broker; it trades a paper book and must still be measured"
    )
    assert list(seen["contexts"]) == ["BRENT"]


def test_cycle_log_reconciles_the_dropped_symbol_with_active_symbols(tmp_path):
    """F2. The cycle record must not contradict itself.

    ``active_symbols`` is the *selection* record and still lists BRENT, while
    the ``symbol_contexts`` written alongside it no longer contains BRENT. Left
    as-is the pair is unreadable; ``dropped_symbols`` is what reconciles them.
    """
    broker = _MinSizeBroker({"BRENT": 1.0, "WTI": 0.01})
    bot = _make_whole_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._select_active_symbols = MagicMock(return_value=["BRENT", "WTI"])
    bot._collect_symbol_contexts = MagicMock(
        return_value={"BRENT": _brent_breaching(), "WTI": _wti_fillable()}
    )
    bot.track_a_pnl = None

    bot.run_cycle()

    kwargs = bot._write_cycle_log.call_args.kwargs
    assert kwargs["active_symbols"] == ["BRENT", "WTI"]
    assert list(kwargs["symbol_contexts"]) == ["WTI"]
    assert kwargs["dropped_symbols"] == ["BRENT"]


def test_cycle_log_dropped_symbols_is_empty_when_nothing_is_dropped(tmp_path):
    """F2. Not a constant: the field is empty on a clean cycle."""
    broker = _MinSizeBroker(default=0.01)
    bot = _make_whole_cycle_bot(tmp_path, broker, news_synthesiser_enabled=False)
    bot._select_active_symbols = MagicMock(return_value=["WTI"])
    bot._collect_symbol_contexts = MagicMock(return_value={"WTI": _wti_fillable()})
    bot.track_a_pnl = None

    bot.run_cycle()

    assert bot._write_cycle_log.call_args.kwargs["dropped_symbols"] == []
