from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from trading_bot.config import Settings
from trading_bot.session_manager import IGSession, TokenBucket


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
