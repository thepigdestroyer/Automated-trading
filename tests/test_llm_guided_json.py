"""Guided JSON must be opt-in and must inject NIM's nvext.guided_json schema.

Guided decoding is shipped OFF. During the 2026-05-20 OpenRouter -> NVIDIA NIM
migration it was established that ``response_format={"type":"json_object"}``
silently suppresses Kimi K2.6's thinking channel on NIM. ``nvext.guided_json``
is a different mechanism but plausibly shares the plumbing, and nobody has
measured it on this stack. Until ``scripts/probe_guided_json.py`` is run on the
deployment box with real credentials, the flag stays off; these tests pin the
default so it cannot drift on by accident.
"""

from __future__ import annotations

from typing import Any

import pydantic

from trading_bot.config import Settings


def test_guided_json_defaults_off():
    """NIM's json_object mode silently suppressed thinking; guided_json is
    unproven on this stack, so it must not be on by default."""
    assert Settings(_env_file=None).llm_guided_json_enabled is False


def test_guided_json_payload_includes_schema_when_enabled():
    from trading_bot.openai_decision import build_nvext_extra_body

    schema = {"type": "object", "properties": {"direction": {"type": "string"}}}
    body = build_nvext_extra_body(guided_json_enabled=True, schema=schema)

    assert body["chat_template_kwargs"] == {"thinking": True}
    assert body["nvext"]["guided_json"] == schema


def test_guided_json_absent_when_disabled():
    from trading_bot.openai_decision import build_nvext_extra_body

    body = build_nvext_extra_body(guided_json_enabled=False, schema={"a": 1})

    assert body == {"chat_template_kwargs": {"thinking": True}}


def test_empty_schema_never_produces_an_nvext_block():
    """A falsy schema with the flag on must not emit ``nvext: {"guided_json":
    {}}``. NIM would receive a constraint matching nothing, which is worse than
    sending no constraint at all."""
    from trading_bot.openai_decision import build_nvext_extra_body

    for schema in (None, {}):
        body = build_nvext_extra_body(guided_json_enabled=True, schema=schema)
        assert body == {"chat_template_kwargs": {"thinking": True}}, schema


# --- Wiring: the builder must actually be called by the chat path ------------
#
# The three tests above exercise ``build_nvext_extra_body`` in isolation. They
# all pass if the function is never wired into ``_run_openrouter_structured_chat``
# at all -- i.e. if the whole feature is dead code. These tests assert on the
# ``extra_body`` the OpenAI-compatible client actually receives.


class _Tiny(pydantic.BaseModel):
    ok: bool


class _FakeCompletions:
    """Captures the kwargs the engine sends and returns a parseable response."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs

        class _Message:
            content = '{"ok": true}'
            reasoning = None
            reasoning_content = None

        class _Choice:
            message = _Message()
            finish_reason = "stop"

        class _Response:
            choices = [_Choice()]
            provider = "nvidia-nim"
            usage = None

        return _Response()


def _engine_with_fake_client(tmp_path, **overrides):
    from trading_bot.openai_decision import OpenAIDecisionEngine

    settings = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key="test",
        broker="paper",
        dry_run=True,
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        **overrides,
    )
    engine = OpenAIDecisionEngine(settings)
    completions = _FakeCompletions()

    class _Chat:
        def __init__(self, c):
            self.completions = c

    class _Client:
        def __init__(self, c):
            self.chat = _Chat(c)

    engine.client = _Client(completions)
    return engine, completions


def _run(engine) -> None:
    engine._run_openrouter_structured_chat(
        schema_model=_Tiny,
        input_items=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ],
        error_label="tiny",
    )


def test_chat_call_omits_nvext_by_default(tmp_path):
    """Default settings: the request the model actually receives must carry
    thinking=True and no guided-decoding constraint."""
    engine, completions = _engine_with_fake_client(tmp_path)
    assert engine.settings.llm_guided_json_enabled is False

    _run(engine)

    assert completions.kwargs["extra_body"] == {
        "chat_template_kwargs": {"thinking": True}
    }


def test_chat_call_includes_the_real_schema_when_enabled(tmp_path):
    """With the flag on, the client must receive the schema of the model being
    requested -- not a placeholder, and not a stale schema."""
    engine, completions = _engine_with_fake_client(
        tmp_path, llm_guided_json_enabled=True
    )

    _run(engine)

    extra_body = completions.kwargs["extra_body"]
    assert extra_body["chat_template_kwargs"] == {"thinking": True}
    assert extra_body["nvext"]["guided_json"] == _Tiny.model_json_schema()
    # Not vacuous: the schema really describes the requested model.
    assert "ok" in extra_body["nvext"]["guided_json"]["properties"]


def test_thinking_stays_enabled_when_guided_json_is_on(tmp_path):
    """The failure mode this flag exists to guard against is losing the
    reasoning channel. Whatever else guided decoding does, the bot must never
    stop *asking* for thinking -- that part is ours to control."""
    engine, completions = _engine_with_fake_client(
        tmp_path, llm_guided_json_enabled=True
    )

    _run(engine)

    assert completions.kwargs["extra_body"]["chat_template_kwargs"]["thinking"] is True
    # And json_object mode -- the proven thinking-killer -- must not reappear.
    assert "response_format" not in completions.kwargs
