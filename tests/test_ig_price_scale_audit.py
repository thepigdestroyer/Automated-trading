"""IG price-scale audit (review 2026-04-30-adhoc-1 #2).

Per the 2026-04-29 #1 unit fix, ``InstrumentDefinition.ig_price_scale``
multiplies plan distances when handing off to the IG executor, so a
$1.38 stop on WTI becomes 138 IG points. The original fix only set
scales for WTI/BRENT/NATGAS; this audit covers the remaining symbols
(metals + FX) which the next regime shift will exercise.

Conventions used (verified against IG dealing-rules and Yahoo data):
- WTI/BRENT/NATGAS: 1pt = 1¢ ⇒ scale = 100  (already set in #1 of yesterday)
- GOLD/PLATINUM:    1pt = $0.10 ⇒ scale = 10
- SILVER/COPPER:    1pt = 1¢ ⇒ scale = 100
- EUR/GBP/AUD/CHF FX: 1pt = 1 pip = 0.0001 ⇒ scale = 10000
- USDJPY:           1pt = 1 pip = 0.01 ⇒ scale = 100  (JPY is the quote ccy)
- Equities/indices/ETFs: scale = 1.0 (unchanged)
"""

from __future__ import annotations

import pytest

from trading_bot.instruments import get_instrument


# --- Energy (already set; sanity confirmation) -----------------------


def test_wti_scale_is_100():
    assert get_instrument("WTI").ig_price_scale == 100.0


def test_brent_scale_is_100():
    assert get_instrument("BRENT").ig_price_scale == 100.0


def test_natgas_scale_is_100():
    assert get_instrument("NATGAS").ig_price_scale == 100.0


# --- Metals ----------------------------------------------------------


def test_gold_scale_is_10():
    """GOLD: IG quotes 1pt = $0.10 ⇒ ×10."""
    assert get_instrument("GOLD").ig_price_scale == 10.0


def test_silver_scale_is_100():
    """SILVER: IG quotes 1pt = 1¢ ⇒ ×100."""
    assert get_instrument("SILVER").ig_price_scale == 100.0


def test_copper_scale_is_100():
    """COPPER: IG quotes 1pt = 1¢ ⇒ ×100."""
    assert get_instrument("COPPER").ig_price_scale == 100.0


def test_platinum_scale_is_10():
    """PLATINUM: IG quotes 1pt = $0.10 ⇒ ×10."""
    assert get_instrument("PLATINUM").ig_price_scale == 10.0


# --- FX --------------------------------------------------------------


def test_eurusd_scale_is_10000():
    """EURUSD: 1pt = 1 pip = 0.0001 ⇒ ×10000."""
    assert get_instrument("EURUSD").ig_price_scale == 10000.0


def test_gbpusd_scale_is_10000():
    assert get_instrument("GBPUSD").ig_price_scale == 10000.0


def test_audusd_scale_is_10000():
    assert get_instrument("AUDUSD").ig_price_scale == 10000.0


def test_usdchf_scale_is_10000():
    assert get_instrument("USDCHF").ig_price_scale == 10000.0


def test_usdjpy_scale_is_100():
    """USDJPY is the FX exception: JPY is the quote currency, so 1 pip = 0.01."""
    assert get_instrument("USDJPY").ig_price_scale == 100.0


# --- Equities / indices / ETFs (must remain unscaled) ----------------


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "SPX", "NAS100"])
def test_equity_index_etf_unscaled(symbol):
    """Stocks/indices/ETFs are quoted natively in IG units; no rescale."""
    assert get_instrument(symbol).ig_price_scale == 1.0


# --- Coverage check: every metal and FX has a scale != 1 ------------


def test_no_metal_or_fx_left_with_default_scale():
    """A regression guard: if a new metal or FX pair is added without
    explicit ig_price_scale, this test will catch it."""
    from trading_bot.instruments import list_supported_instruments

    for inst in list_supported_instruments():
        if inst.asset_class in ("metal", "forex"):
            assert inst.ig_price_scale != 1.0, (
                f"{inst.symbol} ({inst.asset_class}) has default ig_price_scale=1.0; "
                f"set it explicitly per review 2026-04-30-adhoc-1 #2."
            )


# --- Real-world plan distances after scaling -------------------------


@pytest.mark.parametrize(
    "symbol,price,stop_pct,expected_post_scale",
    [
        # WTI: $1.38 stop → 138 IG pts
        ("WTI", 65.42, 0.0211, 138.0),
        # GOLD: $23.5 stop on $4700 → ×10 = 235 IG pts
        ("GOLD", 4700.0, 0.005, 235.0),
        # EURUSD: 0.005 stop (50 pips) → ×10000 = 50.0 IG pts
        ("EURUSD", 1.083, 0.00462, 50.0),
        # USDJPY: 0.5 stop on 160 → ×100 = 50.0 IG pts
        ("USDJPY", 160.0, 0.003125, 50.0),
        # COPPER: $0.05 stop on $4.50 → ×100 = 5.0 IG pts (still tight, but in correct units)
        ("COPPER", 4.50, 0.01111, 5.0),
    ],
)
def test_post_scale_distance_in_ig_points(symbol, price, stop_pct, expected_post_scale):
    """Sanity: plan stop distance in price-units × ig_price_scale lands in IG points."""
    inst = get_instrument(symbol)
    raw_distance = price * stop_pct
    scaled = raw_distance * inst.ig_price_scale
    assert scaled == pytest.approx(expected_post_scale, rel=0.02), (
        f"{symbol}: raw {raw_distance} × scale {inst.ig_price_scale} = {scaled}, "
        f"expected ~{expected_post_scale}"
    )
