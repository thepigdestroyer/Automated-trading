"""The guided-JSON probe decides whether a production flag gets enabled, so its
two arms must be measured on equal terms. No network: the client is faked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Settings

PROBE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "probe_guided_json.py"

_VALID = json.dumps(
    {"market_regime": "risk-off", "summary": "hold", "trades": []}
)


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_guided_json", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(*, content: str = "", reasoning_content: str = "", usage: Any = None):
    message = SimpleNamespace(
        content=content, reasoning=None, reasoning_content=reasoning_content
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
    )


class _FakeEngine:
    def __init__(self, settings: Settings, response: Any) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

        def create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return response

        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )


def _settings(**over) -> Settings:
    return Settings(
        _env_file=None, llm_provider="openrouter", openrouter_api_key="test", **over
    )


def test_exits_2_without_a_key_and_makes_no_call(monkeypatch):
    probe = _load_probe()
    engine_cls = MagicMock()
    monkeypatch.setattr(probe, "OpenAIDecisionEngine", engine_cls)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["probe_guided_json.py"])

    assert probe.main() == 2
    engine_cls.assert_not_called()


def test_off_arm_gets_the_bots_reasoning_channel_fallback():
    """Empty content with the JSON in reasoning_content is what the bot parses
    successfully today. Scoring it as unparseable would skew the comparison
    toward guided JSON."""
    probe = _load_probe()
    engine = _FakeEngine(
        _settings(), _response(reasoning_content="thinking... " + _VALID)
    )

    row = probe._probe_once(engine=engine, guided=False, max_tokens=8000)

    assert row["json_source"] == "reasoning_content"
    assert row["first_try_parse"] is True
    assert row["schema_valid"] is True


def test_both_arms_are_scored_identically():
    probe = _load_probe()
    response = _response(content="```json\n" + _VALID + "\n```")
    rows = [
        probe._probe_once(
            engine=_FakeEngine(_settings(), response), guided=guided, max_tokens=1
        )
        for guided in (False, True)
    ]

    keys = ("json_source", "first_try_parse", "repair_parse", "schema_valid")
    assert [tuple(r[k] for k in keys) for r in rows] == [
        ("content", True, False, True)
    ] * 2


@pytest.mark.parametrize(
    "usage, expected",
    [
        (SimpleNamespace(completion_tokens=900,
                         completion_tokens_details=SimpleNamespace(reasoning_tokens=700)), 700),
        (SimpleNamespace(completion_tokens=900,
                         completion_tokens_details={"reasoning_tokens": 650}), 650),
        (SimpleNamespace(completion_tokens=900, completion_tokens_details=None,
                         reasoning_tokens=500), 500),
    ],
    ids=["nested_object", "nested_dict", "top_level_fallback"],
)
def test_reasoning_tokens_are_read_from_completion_tokens_details(usage, expected):
    probe = _load_probe()
    engine = _FakeEngine(_settings(), _response(content=_VALID, usage=usage))

    row = probe._probe_once(engine=engine, guided=True, max_tokens=8000)

    assert row["reasoning_tokens"] == expected


def test_max_tokens_defaults_to_the_bots_decision_budget(monkeypatch, capsys):
    probe = _load_probe()
    settings = _settings()
    engines: list[_FakeEngine] = []

    def _engine(s):
        engines.append(_FakeEngine(s, _response(content=_VALID)))
        return engines[-1]

    monkeypatch.setattr(probe, "Settings", lambda: settings)
    monkeypatch.setattr(probe, "OpenAIDecisionEngine", _engine)
    monkeypatch.setattr(sys, "argv", ["probe_guided_json.py", "--repeat", "1"])

    assert probe.main() == 0
    sent = {call["max_tokens"] for call in engines[0].calls}
    assert sent == {settings.decision_max_output_tokens}
    assert settings.decision_max_output_tokens == 8000
