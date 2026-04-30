"""News synthesiser schema tolerance (review 2026-04-30-adhoc-1 #7).

On 2026-04-30 the bot logged exactly one subagent_failure with:

    ValidationError: open_position_assessments
    Input should be a valid string [type=string_type,
    input_value={}, input_type=dict]

The LLM had returned an empty dict ``{}`` (sensible: no positions to
assess) where the strict pydantic schema demanded a string. Schema-
loosening here accepts dict / list / None / non-string scalars and
coerces them to the string form the rest of the pipeline expects.
"""

from __future__ import annotations

import pytest

from trading_bot.models import NewsSynthesisEnvelope


# --- Top-level happy path -------------------------------------------


def test_strict_string_dict_still_works():
    """Existing valid input continues to validate unchanged."""
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": "Bullish on Iran headlines"},
        "regime_implications": "energy-supply-shock",
        "open_position_assessments": {"DI001": "Stop hit overnight"},
    })
    assert env.per_symbol_summaries["WTI"] == "Bullish on Iran headlines"
    assert env.open_position_assessments["DI001"] == "Stop hit overnight"


# --- Empty dict tolerance (the original 2026-04-30 failure) ---------


def test_empty_dict_value_in_open_position_assessments_coerced(tmp_path):
    """The actual 2026-04-30 failure mode: LLM returned {} for a
    sub-key. With #7's schema loosening the empty dict becomes \"\"."""
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": "ok"},
        "regime_implications": "neutral-wait",
        "open_position_assessments": {"WTI": {}},
    })
    assert env.open_position_assessments["WTI"] == ""


def test_empty_dict_value_in_per_symbol_summaries_coerced():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"GOLD": {}},
        "regime_implications": "",
        "open_position_assessments": {},
    })
    assert env.per_symbol_summaries["GOLD"] == ""


# --- Other tolerant coercions ---------------------------------------


def test_none_value_coerced_to_empty_string():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": None},
        "regime_implications": "",
        "open_position_assessments": {},
    })
    assert env.per_symbol_summaries["WTI"] == ""


def test_list_value_joined_with_semicolons():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": ["Iran tensions", "Hormuz blockade"]},
        "regime_implications": "",
        "open_position_assessments": {},
    })
    assert "Iran tensions" in env.per_symbol_summaries["WTI"]
    assert "Hormuz blockade" in env.per_symbol_summaries["WTI"]


def test_non_empty_dict_value_flattened_to_kv_string():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": {"sentiment": "bullish", "score": 0.78}},
        "regime_implications": "",
        "open_position_assessments": {},
    })
    val = env.per_symbol_summaries["WTI"]
    assert "sentiment=bullish" in val
    assert "score=0.78" in val


def test_int_value_stringified():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {"WTI": 42},
        "regime_implications": "",
        "open_position_assessments": {},
    })
    assert env.per_symbol_summaries["WTI"] == "42"


def test_non_dict_top_level_field_replaced_with_empty():
    """If per_symbol_summaries arrives as e.g. a list (LLM mode collapse),
    the model now produces an empty dict rather than raising."""
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": ["WTI is bullish"],
        "regime_implications": "",
        "open_position_assessments": {},
    })
    assert env.per_symbol_summaries == {}


# --- regime_implications tolerance ----------------------------------


def test_regime_implications_dict_flattened():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {},
        "regime_implications": {"primary": "energy-supply-shock"},
        "open_position_assessments": {},
    })
    assert "primary=energy-supply-shock" in env.regime_implications


def test_regime_implications_list_joined():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {},
        "regime_implications": ["risk-off", "geopolitical"],
        "open_position_assessments": {},
    })
    assert "risk-off" in env.regime_implications
    assert "geopolitical" in env.regime_implications


def test_regime_implications_none_becomes_empty_string():
    env = NewsSynthesisEnvelope.model_validate({
        "themes": [],
        "per_symbol_summaries": {},
        "regime_implications": None,
        "open_position_assessments": {},
    })
    assert env.regime_implications == ""


# --- Default factory paths still work -------------------------------


def test_default_envelope_is_empty():
    env = NewsSynthesisEnvelope()
    assert env.themes == []
    assert env.per_symbol_summaries == {}
    assert env.open_position_assessments == {}
    assert env.regime_implications == ""


def test_alias_keys_still_normalised():
    """Existing alias normalisation (symbols, positions, regime) preserved."""
    env = NewsSynthesisEnvelope.model_validate({
        "topics": [],
        "symbols": {"WTI": "ok"},
        "regime": "neutral-wait",
        "positions": {"DI001": {}},  # also exercises empty-dict coercion
    })
    assert env.per_symbol_summaries["WTI"] == "ok"
    assert env.regime_implications == "neutral-wait"
    assert env.open_position_assessments["DI001"] == ""
