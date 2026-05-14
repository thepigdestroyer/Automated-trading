from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from trading_bot.config import Settings

LOGGER = logging.getLogger(__name__)


# Per review 2026-05-05-adhoc-1 #4: regex matching field names whose
# values must be masked when the diagnostic dump logs a request payload
# or response body. Case-insensitive substring match; covers IG-specific
# headers (CST, X-SECURITY-TOKEN, X-IG-API-KEY) plus the generic
# token/key/secret/password/authorization family.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(token|key|secret|password|authorization|cst|x-ig-api)"
)


def _is_sensitive_key(name: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(str(name or "")))


def _diagnostic_payload_dump(payload: Any) -> dict[str, Any]:
    """Return ``{keys, field_types, null_fields}`` for a request payload.

    Sensitive field types are replaced with ``"***"``; values are never
    serialised. ``null_fields`` lists keys whose value is ``None``
    (critical for diagnosing IG's ``validation.null-not-allowed.request``
    family).

    Non-dict payloads return a degenerate shape so callers can log
    without branching.
    """
    if not isinstance(payload, dict):
        return {
            "keys": [],
            "field_types": {},
            "null_fields": [],
            "non_dict_type": type(payload).__name__,
        }
    field_types: dict[str, str] = {}
    null_fields: list[str] = []
    for k, v in payload.items():
        if _is_sensitive_key(k):
            field_types[str(k)] = "***"
        else:
            field_types[str(k)] = type(v).__name__
        if v is None:
            null_fields.append(str(k))
    return {
        "keys": sorted(payload.keys()),
        "field_types": field_types,
        "null_fields": null_fields,
    }


def _truncate_response_body(body: Any, *, limit: int = 500) -> str:
    """Stringify a response body and clamp to ``limit`` chars. Never
    returns more than ``limit`` characters even if the body contains
    sensitive substrings — the caller is expected to log this only in
    failure paths where the operator needs to see the raw IG response."""
    if body is None:
        return ""
    try:
        s = body if isinstance(body, str) else str(body)
    except Exception:
        return "<unstringifiable>"
    if len(s) > limit:
        return s[:limit] + "...[truncated]"
    return s


class TokenBucket:
    def __init__(self, *, rate_per_second: float, capacity: int):
        self.rate_per_second = rate_per_second
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, amount: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                wait = (amount - self._tokens) / self.rate_per_second
            time.sleep(max(0.01, wait))


class IGSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json",
            "X-IG-API-KEY": settings.ig_api_key or "",
            "User-Agent": "trading-bot/0.2",
        })
        self.cst: str | None = None
        self.x_security_token: str | None = None
        self.expires_at: datetime = self._now()
        self.login_payload: dict[str, Any] = {}
        self.refresh_margin = timedelta(seconds=settings.ig_session_refresh_margin_seconds)
        self.rate_limiter = TokenBucket(
            rate_per_second=settings.ig_rate_limit_per_minute / 60.0,
            capacity=max(5, settings.ig_rate_limit_per_minute // 4),
        )

    def ensure_fresh(self) -> None:
        if self.cst is None or self._now() + self.refresh_margin >= self.expires_at:
            self._login()

    def request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict | list:
        self.ensure_fresh()
        # IG REST gateway quirk: DELETE /positions/otc (and a few other
        # endpoints) silently drops the JSON body, yielding
        # `validation.null-not-allowed.request` errors with non-null
        # payloads. The official trading-ig Python client
        # (ig-python/trading-ig rest.py IGSessionCRUD.delete) works around
        # this by submitting POST with an `_method: DELETE` header
        # instead. Apply the same transport-layer transform here for
        # any DELETE-with-body request; bodyless DELETEs (e.g. session
        # logout) still use raw DELETE. Evidence: 11+ failed CLOSE
        # attempts since 2026-04-30 with zero successes.
        effective_method = method
        effective_headers = {
            "Version": version,
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.x_security_token or "",
        }
        if method == "DELETE" and json_body is not None:
            effective_method = "POST"
            effective_headers["_method"] = "DELETE"
        for attempt in range(self.settings.ig_max_retries):
            self.rate_limiter.acquire()
            try:
                response = self.session.request(
                    method=effective_method,
                    url=f"{self.settings.ig_base_url}{path}",
                    headers=effective_headers,
                    json=json_body,
                    params=params,
                    timeout=30,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # IG load balancer drops idle keep-alive connections after ~10-15min;
                # rebuild the underlying TCP session and retry.
                backoff = 0.5 * (2 ** attempt)
                LOGGER.warning(
                    "IG transport error on %s (%s) — resetting session, retrying in %.1fs",
                    path, type(exc).__name__, backoff,
                )
                try:
                    self.session.close()
                except Exception:
                    pass
                self.session = requests.Session()
                self.session.headers.update({
                    "Accept": "application/json; charset=UTF-8",
                    "Content-Type": "application/json",
                    "X-IG-API-KEY": self.settings.ig_api_key or "",
                    "User-Agent": "trading-bot/0.2",
                })
                time.sleep(backoff)
                continue
            if response.status_code == 401:
                LOGGER.warning("IG 401 — re-authenticating")
                self._login()
                continue
            if 500 <= response.status_code < 600:
                backoff = 0.5 * (2 ** attempt)
                LOGGER.warning("IG %s on %s — retrying in %.1fs", response.status_code, path, backoff)
                time.sleep(backoff)
                continue
            return self._handle(
                response,
                request_method=method,
                request_path=path,
                request_json_body=json_body,
            )
        raise RuntimeError(
            f"IG request {method} {path} failed after {self.settings.ig_max_retries} retries"
        )

    def _login(self) -> None:
        login_body = {
            "identifier": self.settings.ig_username,
            "password": self.settings.ig_password,
            "encryptedPassword": False,
        }
        response = self.session.post(
            f"{self.settings.ig_base_url}/session",
            headers={"Version": "2", "X-IG-API-KEY": self.settings.ig_api_key or ""},
            json=login_body,
            timeout=30,
        )
        payload = self._handle(
            response,
            request_method="POST",
            request_path="/session",
            request_json_body=login_body,
        )
        self.cst = response.headers.get("CST")
        self.x_security_token = response.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.x_security_token:
            raise RuntimeError("IG login failed to return CST/X-SECURITY-TOKEN")
        self.expires_at = self._now() + timedelta(hours=6)
        self.login_payload = payload if isinstance(payload, dict) else {}

    def _handle(
        self,
        response: requests.Response,
        *,
        request_method: str | None = None,
        request_path: str | None = None,
        request_json_body: dict | None = None,
    ) -> dict | list:
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"raw": response.text}
        if not response.ok:
            code = payload.get("errorCode") if isinstance(payload, dict) else None
            # Per review 2026-05-05-adhoc-1 #4 (β scope): on any IG 4xx,
            # emit a structured WARN-level diagnostic dump of the
            # request/response shape so operators can identify which
            # field IG considers null/invalid. Logging-only — the
            # RuntimeError is raised unchanged immediately after.
            #
            # The recurring class is ``validation.null-not-allowed.request``
            # on CLOSE attempts (4 occurrences across 2026-04-30 ..
            # 2026-05-05). Today no payload data is captured anywhere.
            try:
                if 400 <= response.status_code < 500:
                    diag = _diagnostic_payload_dump(request_json_body)
                    LOGGER.warning(
                        "IG 4xx diagnostic: status=%s method=%s path=%s "
                        "error_code=%r request_payload_keys=%s "
                        "request_payload_field_types=%s null_fields=%s "
                        "response_body=%r",
                        response.status_code,
                        request_method,
                        request_path,
                        code,
                        diag["keys"],
                        diag["field_types"],
                        diag["null_fields"],
                        _truncate_response_body(response.text, limit=500),
                    )
            except Exception as exc:
                # Diagnostic must never mask the real error.
                LOGGER.debug("IG 4xx diagnostic dump failed: %s", exc)
            raise RuntimeError(f"IG API error: {code or response.status_code}")
        return payload if isinstance(payload, (dict, list)) else {"raw": payload}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
