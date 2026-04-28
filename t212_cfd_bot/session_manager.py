from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from t212_cfd_bot.config import Settings

LOGGER = logging.getLogger(__name__)


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
            "User-Agent": "t212-cfd-bot/0.2",
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
        for attempt in range(self.settings.ig_max_retries):
            self.rate_limiter.acquire()
            try:
                response = self.session.request(
                    method=method,
                    url=f"{self.settings.ig_base_url}{path}",
                    headers={
                        "Version": version,
                        "CST": self.cst or "",
                        "X-SECURITY-TOKEN": self.x_security_token or "",
                    },
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
                    "User-Agent": "t212-cfd-bot/0.2",
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
            return self._handle(response)
        raise RuntimeError(
            f"IG request {method} {path} failed after {self.settings.ig_max_retries} retries"
        )

    def _login(self) -> None:
        response = self.session.post(
            f"{self.settings.ig_base_url}/session",
            headers={"Version": "2", "X-IG-API-KEY": self.settings.ig_api_key or ""},
            json={
                "identifier": self.settings.ig_username,
                "password": self.settings.ig_password,
                "encryptedPassword": False,
            },
            timeout=30,
        )
        payload = self._handle(response)
        self.cst = response.headers.get("CST")
        self.x_security_token = response.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.x_security_token:
            raise RuntimeError("IG login failed to return CST/X-SECURITY-TOKEN")
        self.expires_at = self._now() + timedelta(hours=6)
        self.login_payload = payload if isinstance(payload, dict) else {}

    def _handle(self, response: requests.Response) -> dict | list:
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"raw": response.text}
        if not response.ok:
            code = payload.get("errorCode") if isinstance(payload, dict) else None
            raise RuntimeError(f"IG API error: {code or response.status_code}")
        return payload if isinstance(payload, (dict, list)) else {"raw": payload}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
