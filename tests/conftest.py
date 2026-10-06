from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Ensure tests don't touch the real .env or filesystem state."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("BROKER", "paper")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setenv("DECISION_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PAPER_PNL_PATH", str(tmp_path / "paper_pnl.json"))
    monkeypatch.setenv("ALERTS_DIR", str(tmp_path / "alerts"))
    # Reset risk-gate floors so the live .env (e.g. MIN_TAKE_PROFIT_DISTANCE=18)
    # does not leak into unit tests that probe defaults. Per review
    # 2026-04-30-adhoc-1 cleanup.
    monkeypatch.delenv("MIN_TAKE_PROFIT_DISTANCE", raising=False)
    # Unit tests must never touch the network. The live RSS news providers
    # (FinancialJuice / Fed press, both default-ON in production) are reached
    # from run_cycle(), so switch them off globally here; the multi-provider
    # fan-out is covered in tests/test_news_sources.py with injected fetchers,
    # and the enabled-by-default config values are asserted there too.
    monkeypatch.setenv("FINANCIALJUICE_ENABLED", "false")
    monkeypatch.setenv("FED_PRESS_ENABLED", "false")
    monkeypatch.chdir(REPO_ROOT)
    yield
