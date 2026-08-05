#!/usr/bin/env python3
"""Measure whether NVIDIA NIM's ``nvext.guided_json`` suppresses Kimi K2.6's
thinking channel.

WHY THIS EXISTS
---------------
The bot does not read JSON out of the model's ``content`` field. It reads it out
of the *reasoning* channel and repairs it with ``json_repair`` -- which produced
491 ``subagent_failure`` events and 55 watcher restarts over 15 days. Guided
decoding (``nvext.guided_json``) should fix that at the source by constraining
the decode to the schema.

The catch: during the 2026-05-20 OpenRouter -> NVIDIA NIM migration it was
established that ``response_format={"type": "json_object"}`` *silently*
suppresses Kimi K2.6's thinking channel on NIM. No error, no warning -- the
reasoning tokens simply stop. ``nvext.guided_json`` is a different mechanism,
but it plausibly runs through the same chat-template plumbing, and nobody has
measured it. If it kills thinking, turning it on would trade a parsing problem
for a much worse reasoning problem: a Portfolio Manager that no longer thinks.

So ``llm_guided_json_enabled`` ships OFF and this script is the gate.

HOW TO RUN (on the deployment box, where the credentials live)
-------------------------------------------------------------
    export OPENROUTER_API_KEY=...        # read from the environment ONLY
    .venv/bin/python scripts/probe_guided_json.py
    .venv/bin/python scripts/probe_guided_json.py --repeat 5   # recommended

This script NEVER contains a key. It reads ``OPENROUTER_API_KEY`` from the
environment via ``Settings``, exactly like the bot does.

It issues two otherwise-identical ``decide``-shaped calls -- same model, same
system prompt, same schema, same payload, same temperature and seed -- differing
only in ``llm_guided_json_enabled``. For each it reports:

    finish_reason           did the model stop cleanly, or hit the token cap?
    content_len             chars in message.content
    reasoning_content_len   chars in message.reasoning_content (NIM's channel)
    reasoning_len           chars in message.reasoning (OpenRouter's channel)
    reasoning_tokens        usage.reasoning_tokens, when the provider reports it
    first_try_parse         did json.loads() succeed WITHOUT json_repair?
    schema_valid            did the payload validate against DecisionEnvelope?

ADOPTION CRITERIA -- both must hold, or guided JSON stays off
-------------------------------------------------------------
  1. Reasoning length stays non-trivial with guided JSON on. If reasoning
     collapses (particularly to 0) the flag is unsafe at any parse rate: that is
     the json_object failure reproducing itself.
  2. First-try parse rate improves. If it does not, the flag adds provider-
     specific risk and buys nothing.

WARNING: this makes real, billable LLM calls. That is the entire point -- the
question is empirical and cannot be answered from a test suite -- but run it
deliberately, with a small ``--repeat``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading_bot.config import Settings  # noqa: E402
from trading_bot.models import DecisionEnvelope  # noqa: E402
from trading_bot.openai_decision import (  # noqa: E402
    OpenAIDecisionEngine,
    build_nvext_extra_body,
)

# A deliberately small but decide-shaped payload. Realistic enough that the
# model has something to reason about (so a reasoning-length measurement is
# meaningful), small enough that the probe is cheap.
_PROBE_PAYLOAD: dict[str, Any] = {
    "as_of": "2026-05-21T09:00:00+00:00",
    "account": {
        "mode": "demo",
        "currency": "GBP",
        "total_funds": 6976.0,
        "free_funds": 6976.0,
        "open_positions": [],
    },
    "market_headlines": [
        {
            "title": "Brent slips as OPEC+ signals higher output quota",
            "summary": "Traders trimmed long positions ahead of the meeting.",
            "source": "Reuters",
            "published_at": "2026-05-21T07:30:00+00:00",
        }
    ],
    "symbols": [
        {
            "symbol": "BRENT",
            "indicators": {
                "price": 99.3,
                "sma_20": 98.1,
                "sma_50": 97.4,
                "rsi_14": 56.0,
                "atr_14": 1.2,
            },
        }
    ],
    "constraints": {
        "max_open_positions": 3,
        "max_new_trades_per_cycle": 1,
        "allow_shorts": True,
        "min_model_confidence": 0.72,
    },
}


def _system_prompt() -> str:
    """Mirror the decide() prompt shape: instructions + schema + JSON directive.

    This matters. The whole question is whether guided decoding changes model
    behaviour *given the prompt the bot actually sends*, and that prompt already
    contains the schema and a "return only valid JSON" directive.
    """
    schema_json = json.dumps(DecisionEnvelope.model_json_schema(), indent=2)
    return (
        "You are the final Portfolio Manager for a conservative autonomous CFD "
        "trading bot. Use only the supplied account data, headlines and "
        "indicators. Default to HOLD if the signal is weak or conflicting. "
        "Think carefully about whether the setup is real before deciding.\n\n"
        "Required JSON schema:\n"
        + schema_json
        + "\n\nReturn only valid JSON matching the required schema exactly. "
        "Do not wrap the JSON in markdown fences. "
        "Critical: every sibling field must be separated by a comma; "
        "validate the JSON is parseable before returning."
    )


def _probe_once(
    *, engine: OpenAIDecisionEngine, guided: bool, max_tokens: int
) -> dict[str, Any]:
    """One raw chat call. Returns the diagnostics for a single attempt.

    Deliberately calls the client directly rather than going through
    ``_run_openrouter_structured_chat``: that helper returns a validated model
    and discards the raw response, but the raw response IS the measurement.
    ``extra_body`` is built with the shipped ``build_nvext_extra_body`` so the
    probe exercises the real code path rather than a lookalike.
    """
    settings = engine.settings
    chat_kwargs: dict[str, Any] = dict(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": json.dumps(_PROBE_PAYLOAD, indent=2)},
        ],
        extra_body=build_nvext_extra_body(
            guided_json_enabled=guided,
            schema=DecisionEnvelope.model_json_schema(),
        ),
        temperature=float(settings.llm_temperature),
    )
    if settings.llm_seed is not None:
        chat_kwargs["seed"] = int(settings.llm_seed)
    if max_tokens:
        chat_kwargs["max_tokens"] = int(max_tokens)

    result: dict[str, Any] = {
        "guided_json": guided,
        "error": None,
        "finish_reason": None,
        "content_len": 0,
        "reasoning_len": 0,
        "reasoning_content_len": 0,
        "reasoning_tokens": None,
        "completion_tokens": None,
        "first_try_parse": False,
        "repair_parse": False,
        "schema_valid": False,
    }

    try:
        response = engine.client.chat.completions.create(**chat_kwargs)
    except Exception as exc:  # noqa: BLE001 - a probe must report, not crash
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    choices = getattr(response, "choices", None) or []
    if not choices:
        result["error"] = "provider returned no choices"
        return result

    choice = choices[0]
    message = getattr(choice, "message", None)
    result["finish_reason"] = (
        getattr(choice, "finish_reason", None)
        or getattr(choice, "native_finish_reason", None)
        or "?"
    )
    content = (getattr(message, "content", None) or "") or ""
    result["content_len"] = len(content)
    result["reasoning_len"] = len((getattr(message, "reasoning", None) or ""))
    result["reasoning_content_len"] = len(
        (getattr(message, "reasoning_content", None) or "")
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        result["reasoning_tokens"] = getattr(usage, "reasoning_tokens", None)
        result["completion_tokens"] = getattr(usage, "completion_tokens", None)

    # The measurement that matters for adoption: does the payload parse on the
    # FIRST try, with plain json.loads and no json_repair salvage? The bot's
    # current pain is that this is frequently False.
    payload: Any = None
    if content:
        try:
            payload = json.loads(content)
            result["first_try_parse"] = True
        except (json.JSONDecodeError, ValueError):
            try:
                from json_repair import repair_json

                payload = repair_json(content, return_objects=True)
                result["repair_parse"] = bool(payload)
            except Exception:  # noqa: BLE001
                payload = None

    if payload:
        try:
            DecisionEnvelope.model_validate(payload)
            result["schema_valid"] = True
        except Exception:  # noqa: BLE001
            result["schema_valid"] = False

    return result


def _format_row(row: dict[str, Any]) -> str:
    if row["error"]:
        return f"  ERROR: {row['error']}"
    return (
        f"  finish_reason={row['finish_reason']}"
        f" content_len={row['content_len']}"
        f" reasoning_content_len={row['reasoning_content_len']}"
        f" reasoning_len={row['reasoning_len']}"
        f" reasoning_tokens={row['reasoning_tokens']}"
        f" completion_tokens={row['completion_tokens']}"
        f" first_try_parse={row['first_try_parse']}"
        f" repair_parse={row['repair_parse']}"
        f" schema_valid={row['schema_valid']}"
    )


def _summarise(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r["error"]]
    n = len(ok) or 1
    reasoning = [
        max(r["reasoning_content_len"], r["reasoning_len"]) for r in ok
    ]
    summary = {
        "label": label,
        "attempts": len(rows),
        "errors": len(rows) - len(ok),
        "first_try_parse_rate": sum(1 for r in ok if r["first_try_parse"]) / n,
        "schema_valid_rate": sum(1 for r in ok if r["schema_valid"]) / n,
        "mean_reasoning_chars": (sum(reasoning) / n) if reasoning else 0.0,
        "min_reasoning_chars": min(reasoning) if reasoning else 0,
        "zero_reasoning_calls": sum(1 for value in reasoning if value == 0),
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe whether NIM nvext.guided_json suppresses Kimi K2.6 thinking. "
            "Makes real, billable LLM calls."
        )
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="attempts per arm (default 3). Higher = better signal, more cost.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="max_tokens per call (default 4096).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the raw results as JSON on stdout as well.",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print(
            "OPENROUTER_API_KEY is not set. Export it in the shell before "
            "running; this script deliberately contains no credentials.",
            file=sys.stderr,
        )
        return 2

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load Settings: {exc}", file=sys.stderr)
        return 2

    if settings.llm_provider != "openrouter":
        print(
            f"LLM_PROVIDER is {settings.llm_provider!r}; this probe targets the "
            "OpenAI-compatible NIM path. Set LLM_PROVIDER=openrouter.",
            file=sys.stderr,
        )
        return 2

    engine = OpenAIDecisionEngine(settings)

    print("=" * 78)
    print("NIM guided-JSON probe")
    print(f"  started_at   : {datetime.now(timezone.utc).isoformat()}")
    print(f"  model        : {settings.openai_model}")
    print(f"  base_url     : {settings.llm_base_url}")
    print(f"  temperature  : {settings.llm_temperature}  seed: {settings.llm_seed}")
    print(f"  repeat       : {args.repeat} per arm   max_tokens: {args.max_tokens}")
    print(
        "  shipped flag : llm_guided_json_enabled="
        f"{settings.llm_guided_json_enabled}"
    )
    print("=" * 78)

    results: dict[str, list[dict[str, Any]]] = {"off": [], "on": []}
    for attempt in range(1, args.repeat + 1):
        for arm, guided in (("off", False), ("on", True)):
            label = f"guided_json={'ON ' if guided else 'OFF'}"
            print(f"\n[attempt {attempt}/{args.repeat}] {label}")
            row = _probe_once(
                engine=engine, guided=guided, max_tokens=args.max_tokens
            )
            results[arm].append(row)
            print(_format_row(row))

    off = _summarise("guided_json=OFF", results["off"])
    on = _summarise("guided_json=ON", results["on"])

    print("\n" + "=" * 78)
    print("SUMMARY")
    for summary in (off, on):
        print(
            f"  {summary['label']:>16}: "
            f"first_try_parse={summary['first_try_parse_rate']:.0%} "
            f"schema_valid={summary['schema_valid_rate']:.0%} "
            f"mean_reasoning_chars={summary['mean_reasoning_chars']:.0f} "
            f"min={summary['min_reasoning_chars']} "
            f"zero_reasoning_calls={summary['zero_reasoning_calls']}/"
            f"{summary['attempts']} "
            f"errors={summary['errors']}"
        )

    print("\n" + "-" * 78)
    print("ADOPTION CRITERIA")
    reasoning_survives = (
        on["zero_reasoning_calls"] == 0
        and on["mean_reasoning_chars"] >= 0.5 * max(off["mean_reasoning_chars"], 1.0)
    )
    parse_improves = on["first_try_parse_rate"] > off["first_try_parse_rate"]
    print(
        f"  1. reasoning survives with guided JSON : "
        f"{'PASS' if reasoning_survives else 'FAIL'}"
    )
    print(
        f"  2. first-try parse rate improves       : "
        f"{'PASS' if parse_improves else 'FAIL'}"
    )
    if reasoning_survives and parse_improves:
        print(
            "\n  => Both criteria met. Enabling LLM_GUIDED_JSON_ENABLED=true is "
            "justified.\n     Re-run with a higher --repeat first; a 3-sample "
            "result is weak evidence."
        )
    else:
        print(
            "\n  => Criteria NOT met. Leave llm_guided_json_enabled=False.\n"
            "     If criterion 1 failed, guided_json is suppressing the thinking "
            "channel\n     exactly as response_format=json_object did -- do not "
            "enable it."
        )
    print("-" * 78)

    if args.json:
        print("\nRAW_RESULTS_JSON")
        print(json.dumps({"off": results["off"], "on": results["on"]}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
