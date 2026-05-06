from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.config import Settings
from trading_bot.session_manager import (
    IGSession,
    TokenBucket,
    _diagnostic_payload_dump,
    _is_sensitive_key,
    _truncate_response_body,
)


def test_token_bucket_enforces_rate():
    bucket = TokenBucket(rate_per_second=2.0, capacity=2)
    t0 = time.monotonic()
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()  # third should block ~0.5s
    assert time.monotonic() - t0 >= 0.4


def test_ig_session_retries_on_500():
    s = Settings().model_copy(
        update={
            "broker": "ig",
            "ig_username": "u",
            "ig_password": "p",
            "ig_api_key": "k",
            "ig_max_retries": 2,
        }
    )
    sess = IGSession(s)
    sess.cst = "CST"
    sess.x_security_token = "XST"
    sess.expires_at = sess._now() + sess.refresh_margin * 10
    responses = [
        MagicMock(status_code=500, content=b"", text="bad", ok=False, headers={}),
        MagicMock(
            status_code=200,
            content=b'{"ok":true}',
            ok=True,
            headers={},
            json=lambda: {"ok": True},
        ),
    ]
    with patch.object(sess.session, "request", side_effect=responses):
        result = sess.request("GET", "/accounts", version="1")
    assert result == {"ok": True}


# --- Per review 2026-05-05-adhoc-1 #4 (β scope): IG 4xx diagnostic dump ---


def _ig_settings(**overrides) -> Settings:
    base = dict(
        broker="ig",
        ig_username="u",
        ig_password="p",
        ig_api_key="k",
        ig_max_retries=1,
    )
    base.update(overrides)
    return Settings().model_copy(update=base)


def _make_session_ready_for_request() -> IGSession:
    sess = IGSession(_ig_settings())
    sess.cst = "CST_REDACTED_VAL"
    sess.x_security_token = "XST_REDACTED_VAL"
    sess.expires_at = sess._now() + sess.refresh_margin * 10
    return sess


def test_is_sensitive_key_matches_ig_and_generic_secrets():
    assert _is_sensitive_key("X-IG-API-KEY")
    assert _is_sensitive_key("CST")
    assert _is_sensitive_key("X-SECURITY-TOKEN")
    assert _is_sensitive_key("authorization")
    assert _is_sensitive_key("Authorization")
    assert _is_sensitive_key("api_key")
    assert _is_sensitive_key("password")
    assert _is_sensitive_key("auth_token")
    # Non-sensitive
    assert not _is_sensitive_key("dealId")
    assert not _is_sensitive_key("epic")
    assert not _is_sensitive_key("size")
    assert not _is_sensitive_key("direction")


def test_diagnostic_payload_dump_returns_keys_types_nulls():
    payload = {
        "dealId": "DI001",
        "direction": "SELL",
        "size": 1.0,
        "expiry": None,
        "level": None,
        "orderType": "MARKET",
    }
    dump = _diagnostic_payload_dump(payload)
    assert dump["keys"] == sorted(payload.keys())
    assert dump["field_types"]["dealId"] == "str"
    assert dump["field_types"]["size"] == "float"
    assert dump["field_types"]["expiry"] == "NoneType"
    assert set(dump["null_fields"]) == {"expiry", "level"}


def test_diagnostic_payload_dump_masks_sensitive_keys():
    payload = {
        "dealId": "DI001",
        "X-IG-API-KEY": "REAL_SECRET_VALUE",
        "api_key": "another_secret",
        "size": 2.0,
    }
    dump = _diagnostic_payload_dump(payload)
    assert dump["field_types"]["X-IG-API-KEY"] == "***"
    assert dump["field_types"]["api_key"] == "***"
    assert dump["field_types"]["dealId"] == "str"  # not sensitive
    assert dump["field_types"]["size"] == "float"


def test_diagnostic_payload_dump_handles_non_dict():
    dump = _diagnostic_payload_dump(None)
    assert dump["keys"] == []
    assert dump["field_types"] == {}
    assert dump["null_fields"] == []
    assert dump["non_dict_type"] == "NoneType"


def test_truncate_response_body_clamps_at_limit():
    long = "x" * 1000
    out = _truncate_response_body(long, limit=500)
    assert len(out) <= 500 + len("...[truncated]")
    assert out.endswith("...[truncated]")


def test_truncate_response_body_preserves_short_strings():
    assert _truncate_response_body("hi", limit=500) == "hi"
    assert _truncate_response_body(None, limit=500) == ""


def test_4xx_emits_diagnostic_warning_and_reraises(caplog):
    """The actual production failure path: HTTP 4xx with errorCode in
    body. Must emit structured WARN with masked sensitive keys, then
    raise RuntimeError unchanged.
    """
    sess = _make_session_ready_for_request()
    payload_with_null = {
        "dealId": "DI001",
        "direction": "SELL",
        "size": 1.0,
        "expiry": None,        # the suspected culprit on null-not-allowed
        "level": None,         # ditto
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
    }
    fake_response = MagicMock(
        status_code=400,
        content=b'{"errorCode":"validation.null-not-allowed.request"}',
        text='{"errorCode":"validation.null-not-allowed.request"}',
        ok=False,
        headers={},
        json=lambda: {"errorCode": "validation.null-not-allowed.request"},
    )
    with patch.object(sess.session, "request", return_value=fake_response):
        with caplog.at_level(logging.WARNING, logger="trading_bot.session_manager"):
            with pytest.raises(RuntimeError) as exc_info:
                sess.request(
                    "DELETE", "/positions/otc", version="1",
                    json_body=payload_with_null,
                )
    # Original error is preserved (caller depends on substring match)
    assert "validation.null-not-allowed.request" in str(exc_info.value)
    # Diagnostic was emitted
    diag_logs = [
        rec for rec in caplog.records
        if "IG 4xx diagnostic" in rec.getMessage()
    ]
    assert len(diag_logs) == 1, (
        f"expected exactly 1 diagnostic log line; got {len(diag_logs)}"
    )
    msg = diag_logs[0].getMessage()
    assert "status=400" in msg
    assert "method=DELETE" in msg
    assert "path=/positions/otc" in msg
    assert "validation.null-not-allowed.request" in msg
    # Null fields surfaced (the suspected culprits on the production
    # 2026-04-30 / 2026-05-01 / 2026-05-05 BRENT/WTI CLOSE failures)
    assert "'expiry'" in msg
    assert "'level'" in msg
    # Sensitive secrets must NOT leak (they're not in this payload but the
    # masking must be visible if we sneak one in).


def test_4xx_diagnostic_masks_sensitive_payload_values(caplog):
    """If a sensitive-keyed field somehow appears in a request payload,
    its TYPE must be replaced with '***' in the dump (value never leaks)."""
    sess = _make_session_ready_for_request()
    payload = {
        "dealId": "DI001",
        "api_key": "SHOULD_NOT_APPEAR_IN_LOG",
        "size": 1.0,
    }
    fake_response = MagicMock(
        status_code=400,
        content=b'{"errorCode":"x"}',
        text='{"errorCode":"x"}',
        ok=False,
        headers={},
        json=lambda: {"errorCode": "x"},
    )
    with patch.object(sess.session, "request", return_value=fake_response):
        with caplog.at_level(logging.WARNING, logger="trading_bot.session_manager"):
            with pytest.raises(RuntimeError):
                sess.request(
                    "POST", "/positions/otc", version="2", json_body=payload,
                )
    msg = [r.getMessage() for r in caplog.records
           if "IG 4xx diagnostic" in r.getMessage()][0]
    # The literal value must not leak
    assert "SHOULD_NOT_APPEAR_IN_LOG" not in msg
    # The masked type must appear
    assert "'***'" in msg or '"***"' in msg


def test_2xx_does_not_emit_diagnostic(caplog):
    """Successful responses must NOT emit the diagnostic line."""
    sess = _make_session_ready_for_request()
    fake_response = MagicMock(
        status_code=200,
        content=b'{"dealReference":"REF001"}',
        text='{"dealReference":"REF001"}',
        ok=True,
        headers={},
        json=lambda: {"dealReference": "REF001"},
    )
    with patch.object(sess.session, "request", return_value=fake_response):
        with caplog.at_level(logging.WARNING, logger="trading_bot.session_manager"):
            result = sess.request(
                "POST", "/positions/otc", version="2",
                json_body={"epic": "X", "direction": "BUY", "size": 1},
            )
    assert result == {"dealReference": "REF001"}
    assert not any(
        "IG 4xx diagnostic" in rec.getMessage() for rec in caplog.records
    )


def test_5xx_does_not_emit_4xx_diagnostic(caplog):
    """5xx is a server / retry path, not the 4xx validation path. Must
    not emit the (4xx-targeted) diagnostic."""
    sess = _make_session_ready_for_request()
    # 500s are retried; after exhausting retries, the request loop
    # raises a different RuntimeError without going through _handle's
    # 4xx branch.
    fake_response = MagicMock(
        status_code=503, content=b"down", text="down", ok=False, headers={},
    )
    with patch.object(sess.session, "request", return_value=fake_response):
        with caplog.at_level(logging.WARNING, logger="trading_bot.session_manager"):
            with pytest.raises(RuntimeError):
                sess.request(
                    "GET", "/accounts", version="1", json_body={"k": "v"},
                )
    assert not any(
        "IG 4xx diagnostic" in rec.getMessage() for rec in caplog.records
    )
