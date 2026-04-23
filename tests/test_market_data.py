from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from t212_cfd_bot.config import Settings
from t212_cfd_bot.market_data import YahooMarketDataClient


def _ohlcv(length: int = 200, close_gap_at: int | None = None, volume: float = 1000.0) -> pd.DataFrame:
    np.random.seed(0)
    idx = pd.date_range("2025-01-01", periods=length, freq="h")
    close = 100 + np.cumsum(np.random.normal(0, 0.5, length))
    if close_gap_at is not None:
        close[close_gap_at] = close[close_gap_at - 1] * 2.0
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(length, volume),
        },
        index=idx,
    )


def test_sanity_filter_rejects_extreme_price_gap():
    s = Settings().model_copy(update={"broker": "paper"})
    client = YahooMarketDataClient(s)
    bad = _ohlcv(length=200, close_gap_at=150)
    with patch("t212_cfd_bot.market_data.yf.download", return_value=bad):
        with pytest.raises(ValueError, match="price gap"):
            client.fetch_history("SPY")


def test_sanity_filter_rejects_all_zero_volume():
    s = Settings().model_copy(update={"broker": "paper"})
    client = YahooMarketDataClient(s)
    zero_vol = _ohlcv(length=200, volume=0.0)
    with patch("t212_cfd_bot.market_data.yf.download", return_value=zero_vol):
        with pytest.raises(ValueError, match="zero volume"):
            client.fetch_history("SPY")


def test_sanity_filter_passes_clean_data():
    s = Settings().model_copy(update={"broker": "paper"})
    client = YahooMarketDataClient(s)
    good = _ohlcv(length=200)
    with patch("t212_cfd_bot.market_data.yf.download", return_value=good):
        result = client.fetch_history("SPY")
    assert len(result) == 200
