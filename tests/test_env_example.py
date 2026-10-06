"""Operator-facing kill switches must be discoverable in .env.example."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_example_keys() -> set[str]:
    keys = set()
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


@pytest.mark.parametrize(
    "key",
    [
        "TRADEABILITY_FILTER_ENABLED",
        "FINANCIALJUICE_ENABLED",
        "FED_PRESS_ENABLED",
        "LLM_GUIDED_JSON_ENABLED",
    ],
)
def test_kill_switch_is_documented_and_real(key):
    assert key in _env_example_keys()
    assert key.lower() in Settings.model_fields


def test_config_does_not_point_at_gitignored_reports():
    """.superpowers/ is gitignored scratch; a reference to it is a dead link
    for anyone reading the committed code."""
    assert ".superpowers/" not in (REPO_ROOT / "trading_bot" / "config.py").read_text(
        encoding="utf-8"
    )
