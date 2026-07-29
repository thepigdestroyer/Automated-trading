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
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Settings
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import AccountSnapshot, OpenPosition


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
