from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from t212_cfd_bot.config import Settings
from t212_cfd_bot.vix import VixContextClient


def _frame(last_value: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [last_value - 0.5, last_value]},
        index=pd.date_range("2026-04-22", periods=2, freq="D"),
    )


def test_term_structure_contango():
    s = Settings().model_copy(update={"broker": "paper", "vix_context_enabled": True})
    client = VixContextClient(s)
    with patch("t212_cfd_bot.vix.yf.download") as dl:
        dl.side_effect = [_frame(15.0), _frame(14.0), _frame(16.0)]
        ctx = client.fetch()
    assert ctx.vix_spot == 15.0
    assert ctx.vix_9d == 14.0
    assert ctx.vix_3m == 16.0
    assert ctx.term_structure_flag == "contango"


def test_disabled_returns_unknown():
    s = Settings().model_copy(update={"broker": "paper", "vix_context_enabled": False})
    client = VixContextClient(s)
    ctx = client.fetch()
    assert ctx.term_structure_flag == "unknown"
    assert ctx.vix_spot is None
