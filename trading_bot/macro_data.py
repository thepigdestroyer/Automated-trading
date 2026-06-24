"""Phase 4 free macro-data sources (2026-06 amended roadmap).

Three zero-cost sources, each a defensive client mirroring vix.py:

  FRED/ALFRED  — vintage-correct macro series (10y yield, 2s10s curve, broad
                 USD index, fed funds, Brent). We pin realtime_start/end to
                 "now" and surface the observation date + staleness so the
                 LLM cannot mistake last month's print for today's news.
  Finnhub      — general market news (60 req/min free) as a corroboration
                 signal; degrades cleanly when a premium endpoint 403s.
  CFTC COT     — Commitments-of-Traders net speculative positioning via the
                 public Socrata API (no key). COT is published Friday for the
                 prior Tuesday, so we surface publication_lag_days explicitly
                 — positioning is a slow context signal, never a timing one.

Every client takes an injectable `json_fetcher` so tests run offline. None of
them ever raises into the cycle — failures become notes on the MacroContext.

The dropped source: Nitter (per the amended roadmap — unreliable/blocked).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from trading_bot.config import Settings
from trading_bot.models import (
    CotPositioning,
    FredSeriesObservation,
    MacroContext,
)

LOGGER = logging.getLogger(__name__)

# (url, params) -> parsed JSON (dict | list) or None.
JsonFetcher = Callable[[str, "dict[str, Any] | None"], Any]

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"
# CFTC legacy futures-only COT, Socrata JSON API (no key required).
CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def _staleness_days(date_str: str | None, *, now: datetime | None = None) -> int | None:
    if not date_str:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(str(date_str)[:10]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (now.date() - d.date()).days


def _to_float(value: Any) -> float | None:
    if value in (None, "", ".", "n/a", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(round(f)) if f is not None else None


def _default_json_fetcher(timeout: float) -> JsonFetcher:
    def fetch(url: str, params: dict[str, Any] | None = None) -> Any:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code in (401, 403):
            # Premium / auth-gated — signal distinctly so callers can note it.
            raise PermissionError(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        return resp.json()

    return fetch


class FredClient:
    def __init__(self, settings: Settings, *, json_fetcher: JsonFetcher | None = None):
        self.settings = settings
        self._fetch = json_fetcher or _default_json_fetcher(settings.macro_http_timeout_seconds)

    def fetch_series(self, series_id: str, *, now: datetime | None = None) -> FredSeriesObservation | None:
        if not self.settings.fred_api_key:
            return None
        now = now or datetime.now(timezone.utc)
        today = now.date().isoformat()
        params = {
            "series_id": series_id,
            "api_key": self.settings.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
            # Vintage correctness: value AS KNOWN today (point-in-time).
            "realtime_start": today,
            "realtime_end": today,
        }
        try:
            data = self._fetch(FRED_URL, params)
        except Exception as exc:
            LOGGER.warning("FRED fetch failed for %s: %s", series_id, exc)
            return None
        obs_list = (data or {}).get("observations") or []
        if not obs_list:
            return None
        obs = obs_list[0]
        date = obs.get("date")
        return FredSeriesObservation(
            series_id=series_id,
            value=_to_float(obs.get("value")),
            date=date,
            vintage_date=obs.get("realtime_start"),
            staleness_days=_staleness_days(date, now=now),
        )


class FinnhubClient:
    def __init__(self, settings: Settings, *, json_fetcher: JsonFetcher | None = None):
        self.settings = settings
        self._fetch = json_fetcher or _default_json_fetcher(settings.macro_http_timeout_seconds)

    def fetch_market_news(self, max_headlines: int = 5) -> dict[str, Any]:
        if not self.settings.finnhub_api_key:
            return {"count": 0, "summary": None, "note": "finnhub: no API key configured"}
        params = {"category": "general", "token": self.settings.finnhub_api_key}
        try:
            data = self._fetch(FINNHUB_NEWS_URL, params)
        except PermissionError as exc:
            return {"count": 0, "summary": None, "note": f"finnhub: endpoint unavailable on free tier ({exc})"}
        except Exception as exc:
            LOGGER.warning("Finnhub news fetch failed: %s", exc)
            return {"count": 0, "summary": None, "note": f"finnhub: fetch failed ({exc})"}
        if not isinstance(data, list) or not data:
            return {"count": 0, "summary": None, "note": "finnhub: no news returned"}
        headlines = [str(item.get("headline") or "").strip() for item in data if item.get("headline")]
        summary = " | ".join(headlines[:max_headlines])
        return {"count": len(data), "summary": summary, "note": None}


class CftcClient:
    def __init__(self, settings: Settings, *, json_fetcher: JsonFetcher | None = None):
        self.settings = settings
        self._fetch = json_fetcher or _default_json_fetcher(settings.macro_http_timeout_seconds)

    def fetch_market(self, market: str, *, now: datetime | None = None) -> CotPositioning | None:
        now = now or datetime.now(timezone.utc)
        params = {
            # Socrata: case-insensitive prefix match on the market name, most
            # recent report first, one row.
            "$where": f"upper(market_and_exchange_names) like upper('{market}%')",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 1,
        }
        try:
            rows = self._fetch(CFTC_URL, params)
        except Exception as exc:
            LOGGER.warning("CFTC fetch failed for %s: %s", market, exc)
            return None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        long_ = _to_int(row.get("noncomm_positions_long_all"))
        short_ = _to_int(row.get("noncomm_positions_short_all"))
        net = (long_ - short_) if (long_ is not None and short_ is not None) else None
        chg_long = _to_int(row.get("change_in_noncomm_long_all"))
        chg_short = _to_int(row.get("change_in_noncomm_short_all"))
        net_chg = (chg_long - chg_short) if (chg_long is not None and chg_short is not None) else None
        report_date = (row.get("report_date_as_yyyy_mm_dd") or "")[:10] or None
        return CotPositioning(
            market=market,
            report_date=report_date,
            publication_lag_days=_staleness_days(report_date, now=now),
            noncommercial_long=long_,
            noncommercial_short=short_,
            net_noncommercial=net,
            net_change_1w=net_chg,
        )


class MacroContextClient:
    """Aggregates the three free sources into one MacroContext. Best-effort:
    each source is independently guarded so one outage cannot blank the
    others, and the whole fetch never raises."""

    def __init__(self, settings: Settings, *, json_fetcher: JsonFetcher | None = None):
        self.settings = settings
        self.fred = FredClient(settings, json_fetcher=json_fetcher)
        self.finnhub = FinnhubClient(settings, json_fetcher=json_fetcher)
        self.cftc = CftcClient(settings, json_fetcher=json_fetcher)

    def fetch(self, *, now: datetime | None = None) -> MacroContext:
        now = now or datetime.now(timezone.utc)
        as_of = now.isoformat()
        if not self.settings.macro_context_enabled:
            return MacroContext(as_of=as_of, notes=["macro_context disabled"])

        notes: list[str] = []
        fred_series: list[FredSeriesObservation] = []
        for sid in self.settings.fred_series_ids:
            try:
                obs = self.fred.fetch_series(sid, now=now)
            except Exception as exc:  # pragma: no cover - defensive
                obs = None
                notes.append(f"fred:{sid} error {exc}")
            if obs is not None:
                fred_series.append(obs)
        if self.settings.fred_series_ids and not fred_series:
            notes.append("fred: no series returned (check FRED_API_KEY)")

        finn_count: int | None = None
        finn_summary: str | None = None
        try:
            finn = self.finnhub.fetch_market_news()
            finn_count = finn.get("count")
            finn_summary = finn.get("summary")
            if finn.get("note"):
                notes.append(finn["note"])
        except Exception as exc:  # pragma: no cover - defensive
            notes.append(f"finnhub: error {exc}")

        cot: list[CotPositioning] = []
        for market in self.settings.cftc_markets:
            try:
                pos = self.cftc.fetch_market(market, now=now)
            except Exception as exc:  # pragma: no cover - defensive
                pos = None
                notes.append(f"cftc:{market} error {exc}")
            if pos is not None:
                cot.append(pos)

        return MacroContext(
            as_of=as_of,
            fred_series=fred_series,
            finnhub_news_count=finn_count,
            finnhub_summary=finn_summary,
            cot=cot,
            notes=notes,
        )
