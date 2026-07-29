from __future__ import annotations

import gc
import json
import logging
import os
import time
import tracemalloc
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

from trading_bot.broker_factory import build_broker
from trading_bot.calendars import EarningsCalendarClient, EconomicCalendarClient, HolidayCalendar
from trading_bot.config import Settings
from trading_bot.correlations import CorrelationContextClient
from trading_bot.decision_log import DecisionLogger, build_decision_record, make_decision_id
from trading_bot.entities import EntityNewsClient, entities_for_symbol
from trading_bot.json_log import AlertSink, JsonEventLogger
from trading_bot.market_data import YahooMarketDataClient
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    ExecutionResult,
    ExecutionPlan,
    Headline,
    IndicatorSnapshot,
    MarketResearchEnvelope,
    OpenPosition,
    OperatorCommandEnvelope,
    ProRealTimeSnapshotEnvelope,
    ProRealTimeSymbolSnapshot,
    SymbolContext,
    SymbolResearchEnvelope,
    TradeDecision,
)
from trading_bot.news import GoogleNewsClient
from trading_bot.news_memory import NewsMemory
from trading_bot.instruments import get_instrument, list_supported_instruments
from trading_bot.openai_decision import OpenAIDecisionEngine
from trading_bot.prorealtime import ProRealTimeDataClient
from trading_bot.risk import RiskManager, degraded_input_reasons
from trading_bot.vix import VixContextClient

LOGGER = logging.getLogger(__name__)


def _tx_float(raw: object) -> float | None:
    """Parse an IG transaction numeric string (e.g. '£-4.08', '9063.9',
    '+0.03', '-') into a float. Returns None when unparseable."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == "-":
        return None
    cleaned = "".join(c for c in s if c.isdigit() or c in ".-")
    # Guard against stray strings like '--' or '.'
    if not cleaned or cleaned in {"-", ".", "-.", ".-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def match_transaction_to_position(
    stale_pos: dict,
    transactions: list[dict],
    *,
    entry_price_tolerance: float = 0.0005,
) -> dict | None:
    """Find the IG transaction-history row that corresponds to a vanished
    position.

    Background: IG ``/history/transactions`` rows are keyed by ``reference``
    (a per-transaction id like ``N7F5MHAV``) and carry NO ``dealId`` /
    ``dealReference``. The position records we hold are keyed by IG
    ``dealId`` (e.g. ``DIAAAAXN7GBPJAW``). The pre-2026-05 reconcile code
    built ``tx_by_deal_id[tx['reference']]`` then looked it up by the
    position's dealId — two different namespaces, so it NEVER matched and
    ``pnl_cash`` was always null / ``estimated=true``.

    This matcher joins on observable trade economics instead:
      1. ``transactionType == 'DEAL'`` (skip fees: 'WITH', interest, admin).
      2. ``openLevel`` ≈ position ``entry_price`` within a relative
         tolerance (primary key — a precise float that uniquely fingerprints
         the fill).
      3. Among entry-price matches, prefer the closest ``openDateUtc`` to the
         position's recorded open time (tiebreak when the same instrument was
         traded at a near-identical price more than once).

    Returns the best-matching transaction dict, or None when no DEAL row
    matches within tolerance.
    """
    entry_price = _tx_float(stale_pos.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        return None

    opened_at = stale_pos.get("opened_at") or stale_pos.get("first_seen_at")
    opened_dt: datetime | None = None
    if opened_at:
        try:
            opened_dt = datetime.fromisoformat(str(opened_at))
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            opened_dt = None

    candidates: list[tuple[float, float, dict]] = []
    for tx in transactions or []:
        if str(tx.get("transactionType", "")).upper() != "DEAL":
            continue
        open_level = _tx_float(tx.get("openLevel"))
        if open_level is None or open_level <= 0:
            continue
        rel_err = abs(open_level - entry_price) / entry_price
        if rel_err > entry_price_tolerance:
            continue
        # Time distance (seconds) as tiebreak; large default when unknown.
        time_dist = float("inf")
        if opened_dt is not None:
            tx_open = tx.get("openDateUtc")
            if tx_open:
                try:
                    tx_dt = datetime.fromisoformat(str(tx_open))
                    if tx_dt.tzinfo is None:
                        tx_dt = tx_dt.replace(tzinfo=timezone.utc)
                    time_dist = abs((tx_dt - opened_dt).total_seconds())
                except (TypeError, ValueError):
                    time_dist = float("inf")
        candidates.append((rel_err, time_dist, tx))

    if not candidates:
        return None
    # Best = smallest entry-price error, then smallest time distance.
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


class TradingBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Build event logger first so subsystems (e.g. market data) can emit events.
        self._early_event_logger = JsonEventLogger(path=settings.json_log_path)
        self.market_data_client = YahooMarketDataClient(
            settings, event_logger=self._early_event_logger
        )
        self.prorealtime_client = ProRealTimeDataClient(settings)
        self.news_client = GoogleNewsClient(settings)
        self.entity_news_client = EntityNewsClient(settings)
        self.vix_client = VixContextClient(settings)
        self.correlation_client = CorrelationContextClient(settings)
        from trading_bot.macro_data import MacroContextClient
        self.macro_client = MacroContextClient(settings)
        self.econ_calendar = EconomicCalendarClient(settings)
        self.earnings_calendar = EarningsCalendarClient(settings)
        self.holiday_calendar = HolidayCalendar()
        self.decision_engine = OpenAIDecisionEngine(settings)
        self.risk_manager = RiskManager(settings)
        self.broker = build_broker(settings)
        self._current_cycle_id: str = ""
        self._current_entity_news: dict[str, list[Headline]] = {}
        self._current_market_vix = None
        self._current_correlation_context = None
        self._current_macro_context = None
        self._current_econ_events: list = []
        # Per review 2026-04-30-token-burn #C: cross-cycle TTL caches for the
        # two heaviest repeat-LLM subagents. Each entry holds:
        #   {"at": datetime, "value": Envelope, "breaking_count": int,
        #    "active_symbols": tuple[str, ...] | None}
        # Macro themes don't shift every 15-30 minutes; news synth is more
        # volatile so it has its own (shorter) TTL.
        self._market_research_cache: dict | None = None
        self._news_synthesis_cache: dict | None = None
        self._macro_context_cache: dict | None = None
        self.last_trade_times: dict[str, datetime] = {}
        self.operator_lock = Lock()
        self.model_lock = Lock()
        self.wake_event = Event()
        self.stop_event = Event()
        self.operator_notes: list[str] = []
        self.focused_symbols: set[str] = set()
        self.excluded_symbols: set[str] = set()
        self.forced_close_symbols: set[str] = set()
        self.paused = False
        self.last_cycle_report: dict | None = None
        self.runtime_state = self._load_runtime_state()
        self.json_log = self._early_event_logger
        self.decision_logger = DecisionLogger(
            records_path=settings.decision_records_path,
            raw_io_path=settings.llm_raw_io_path,
        )
        self.alerts = AlertSink(directory=settings.alerts_dir, webhook_url=settings.alert_webhook_url)
        # Per review 2026-05-02-adhoc-1 #6: lightweight memory diagnostic.
        # tracemalloc adds ~10-15% overhead but only fires every N cycles
        # (default 10) so the impact across 24h is negligible.
        self._cycle_counter: int = 0
        self._memory_profile_started: bool = False
        # Per review 2026-05-05-adhoc-1 #6: soft-restart trigger.
        # _soft_restart_requested is set when current RSS exceeds
        # memory_soft_restart_threshold × memory_max_bytes; the cycle
        # loop checks the flag at the next boundary and raises
        # SystemExit(143) so systemd's Restart=on-failure cleanly
        # bounces the process. Rate-limited via _last_soft_restart_at.
        self._soft_restart_requested: bool = False
        self._last_soft_restart_at: datetime | None = None
        from trading_bot.paper_pnl import PaperPnL
        self.paper_pnl: PaperPnL | None = PaperPnL(settings.paper_pnl_path) if settings.paper_pnl_simulation_enabled else None
        # Track-A: deterministic signals-only twin (no LLM), separate paper
        # book so forward_eval can compare LLM-track vs mechanical-track and
        # isolate the LLM's value-add. Paper mode only.
        self.track_a_pnl: PaperPnL | None = (
            PaperPnL(settings.track_a_pnl_path)
            if settings.track_a_enabled and settings.dry_run
            else None
        )

    def run(self, once: bool = False) -> None:
        if self.settings.execution_mode == "live" and not self.settings.allow_live_trading:
            raise RuntimeError(
                "Refusing to run in live mode without ALLOW_LIVE_TRADING=true"
            )

        self.broker.connect()
        self._reconcile_on_startup()

        # Start tracemalloc before the cycle loop so the first snapshot has
        # a baseline. Per review 2026-05-02-adhoc-1 #6.
        if (
            getattr(self.settings, "memory_profile_enabled", False)
            and int(getattr(self.settings, "memory_profile_cycles_per_snapshot", 0) or 0) > 0
            and not tracemalloc.is_tracing()
        ):
            try:
                tracemalloc.start()
                self._memory_profile_started = True
                LOGGER.info("tracemalloc enabled (every %d cycles)",
                            self.settings.memory_profile_cycles_per_snapshot)
            except Exception as exc:
                LOGGER.warning("Failed to start tracemalloc: %s", exc)

        while not self.stop_event.is_set():
            if self.settings.kill_switch_file and Path(self.settings.kill_switch_file).exists():
                LOGGER.warning("Kill switch file present — flattening and exiting")
                try:
                    self._flatten_all_positions()
                except Exception as exc:
                    LOGGER.exception("Flatten-all failed during kill switch: %s", exc)
                self._write_alert("kill_switch_triggered", {"path": str(self.settings.kill_switch_file)})
                raise SystemExit(0)

            if self._is_paused():
                self.wake_event.wait(timeout=1.0)
                self.wake_event.clear()
                continue

            try:
                self.run_cycle()
            except Exception:
                LOGGER.exception("Trading cycle failed")
                if once:
                    raise

            # Per review 2026-05-02-adhoc-1 #6: every-N-cycles tracemalloc
            # snapshot + gc.collect. Logs top-N allocators so we can
            # see which subsystem holds memory across cycles.
            self._cycle_counter += 1
            self._maybe_emit_memory_snapshot()

            # Per review 2026-05-05-adhoc-1 #6: if the snapshot crossed
            # the soft-restart threshold, exit cleanly here (between
            # cycles, NOT mid-LLM-call). systemd's Restart=on-failure
            # bounces the process. Exit code 143 = 128 + SIGTERM (15)
            # by convention.
            if self._soft_restart_requested:
                LOGGER.warning(
                    "Memory soft-restart triggered — exiting cleanly "
                    "(cycle %d). systemd will restart.",
                    self._cycle_counter,
                )
                raise SystemExit(143)

            if once:
                return

            self.wake_event.wait(timeout=self.settings.poll_interval_seconds)
            self.wake_event.clear()

    def _maybe_emit_memory_snapshot(self) -> None:
        if not getattr(self.settings, "memory_profile_enabled", False):
            return
        every_n = int(
            getattr(self.settings, "memory_profile_cycles_per_snapshot", 0) or 0
        )
        if every_n <= 0:
            return
        if (self._cycle_counter % every_n) != 0:
            return
        if not tracemalloc.is_tracing():
            return
        try:
            gc.collect()
            snapshot = tracemalloc.take_snapshot()
            stats = snapshot.statistics("filename")
            top_n = max(int(getattr(self.settings, "memory_profile_top_n", 5) or 5), 1)
            top_allocators = []
            total_kb = 0.0
            for stat in stats[:top_n]:
                size_kb = stat.size / 1024.0
                total_kb += size_kb
                # Compact the filename to just the last 2 path segments
                frame = stat.traceback[0]
                fname = frame.filename
                short = "/".join(fname.split("/")[-2:]) if fname else fname
                top_allocators.append({
                    "filename": short,
                    "lineno": frame.lineno,
                    "size_kb": round(size_kb, 1),
                    "count": stat.count,
                })
            tracemalloc_total_kb = round(
                sum(s.size for s in stats) / 1024.0, 1
            )
            payload = {
                "cycle_counter": self._cycle_counter,
                "tracemalloc_total_kb": tracemalloc_total_kb,
                "top_allocators": top_allocators,
            }
            self.json_log.log(
                "memory_snapshot",
                payload,
                cycle_id=getattr(self, "_current_cycle_id", None) or None,
            )
            LOGGER.info(
                "Memory snapshot @ cycle %d: total tracemalloc=%.1f KB, top=%s",
                self._cycle_counter,
                tracemalloc_total_kb,
                ", ".join(
                    f"{a['filename']}:{a['lineno']}={a['size_kb']:.1f}KB"
                    for a in top_allocators[:3]
                ),
            )
            # Per review 2026-05-05-adhoc-1 #6: check whether RSS has
            # crossed the soft-restart threshold, and if so request a
            # graceful exit at the next cycle boundary.
            self._maybe_request_memory_soft_restart()
        except Exception as exc:
            LOGGER.warning("memory snapshot failed: %s", exc)

    def _news_memory_metrics(self) -> dict[str, Any]:
        """Return ``{size, oldest_published_at}`` for the live news_memory.

        Per review 2026-05-05-adhoc-1 #7. Cheap O(N) over the items list
        (typically <500 entries). Used to enrich every cycle_heartbeat
        event so the watcher's news_memory_growth_anomaly rule has
        something to fire on.

        - ``size`` is the count of items currently retained.
        - ``oldest_published_at`` is the earliest ``published_at`` ISO
          string across all items (falls back to
          ``first_seen_cycle_ts`` when published_at is missing). None
          if the store is empty.
        """
        try:
            nm = (self.runtime_state or {}).get("news_memory") or {}
            items = nm.get("items") or []
        except Exception:
            return {"size": 0, "oldest_published_at": None}
        if not items:
            return {"size": 0, "oldest_published_at": None}
        oldest: str | None = None
        for it in items:
            ts = it.get("published_at") or it.get("first_seen_cycle_ts")
            if not ts:
                continue
            ts_str = str(ts)
            if oldest is None or ts_str < oldest:
                oldest = ts_str
        return {"size": len(items), "oldest_published_at": oldest}

    @staticmethod
    def _get_rss_bytes() -> int | None:
        """Return current process RSS in bytes from /proc/self/status, or
        None if unavailable (non-Linux, or read fails). Per review
        2026-05-05-adhoc-1 #6.
        """
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Format: "VmRSS:    179680 kB"
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
                        break
        except (OSError, ValueError):
            return None
        return None

    def _maybe_request_memory_soft_restart(self) -> None:
        """If the current RSS exceeds
        ``memory_soft_restart_threshold × memory_max_bytes``, emit an
        event and set ``_soft_restart_requested`` so the cycle loop
        exits cleanly at the next boundary. Rate-limited via
        ``memory_soft_restart_min_interval_seconds``.

        Per review 2026-05-05-adhoc-1 #6.
        """
        threshold = float(
            getattr(self.settings, "memory_soft_restart_threshold", 0.0) or 0.0
        )
        if threshold <= 0.0:
            return  # disabled
        max_bytes = int(
            getattr(self.settings, "memory_max_bytes", 0) or 0
        )
        if max_bytes <= 0:
            return
        rss_bytes = self._get_rss_bytes()
        if rss_bytes is None:
            return  # /proc unavailable
        ratio = rss_bytes / max_bytes
        if ratio < threshold:
            return

        # Rate-limit. If a soft-restart was requested recently and the
        # bot is still running, suppress further triggers — but emit a
        # WARN so operators see the persistent pressure.
        now = datetime.now(timezone.utc)
        min_interval = int(
            getattr(
                self.settings,
                "memory_soft_restart_min_interval_seconds",
                3600,
            ) or 0
        )
        if self._last_soft_restart_at is not None and min_interval > 0:
            elapsed = (now - self._last_soft_restart_at).total_seconds()
            if elapsed < min_interval:
                LOGGER.warning(
                    "Memory pressure persisted: rss=%.1fMB ratio=%.2f%% "
                    "(threshold %.0f%%); soft-restart suppressed (last "
                    "request %.0fs ago, min interval %ds)",
                    rss_bytes / 1024 / 1024, ratio * 100,
                    threshold * 100, elapsed, min_interval,
                )
                return

        self._soft_restart_requested = True
        self._last_soft_restart_at = now
        LOGGER.warning(
            "Memory pressure soft-restart requested: rss=%.1fMB / max=%.1fMB "
            "(%.1f%% ≥ threshold %.0f%%). Process will exit at next cycle "
            "boundary; systemd Restart=on-failure will bring it back up.",
            rss_bytes / 1024 / 1024, max_bytes / 1024 / 1024,
            ratio * 100, threshold * 100,
        )
        try:
            self.json_log.log(
                "memory_pressure_soft_restart_requested",
                {
                    "rss_bytes": rss_bytes,
                    "memory_max_bytes": max_bytes,
                    "ratio": round(ratio, 3),
                    "threshold": threshold,
                    "cycle_counter": self._cycle_counter,
                },
                cycle_id=getattr(self, "_current_cycle_id", None) or None,
            )
        except Exception as exc:
            LOGGER.debug("Failed to emit memory_pressure event: %s", exc)

    def run_cycle(self) -> list[ExecutionResult]:
        cycle_started_at = datetime.now(timezone.utc)
        self._current_cycle_id = f"c_{uuid.uuid4().hex[:8]}"
        # Per review 2026-04-29-adhoc-1 #7: emit a positive heartbeat at
        # cycle start. Watcher's heartbeat_silence rule replaces the
        # noisy file-mtime-based log_silence detector.
        # Per review 2026-05-05-adhoc-1 #7: include news_memory metrics
        # so the watcher can detect unbounded growth or stale-entry
        # accumulation across cycles.
        nm_metrics = self._news_memory_metrics()
        self.json_log.log(
            "cycle_heartbeat",
            {
                "cycle_id": self._current_cycle_id,
                "phase": "start",
                "news_memory_size": nm_metrics["size"],
                "news_memory_oldest_published_at": nm_metrics["oldest_published_at"],
            },
            cycle_id=self._current_cycle_id,
        )
        account_snapshot = self.broker.get_account_snapshot()
        self._refresh_runtime_state(account_snapshot, cycle_started_at)
        operator_context = self._get_operator_context(account_snapshot)
        self.json_log.log("cycle_start", {"cycle_id": self._current_cycle_id}, cycle_id=self._current_cycle_id)

        news_memory_state = self.runtime_state.setdefault(
            "news_memory", {"items": [], "last_purged_at": None}
        )
        news_memory = NewsMemory(
            state=news_memory_state,
            retention_hours=self.settings.news_memory_hours,
            max_items=self.settings.news_memory_max_items,
        )

        market_headlines = self.news_client.fetch_market_headlines()
        market_headlines = news_memory.classify(market_headlines, cycle_ts=cycle_started_at.isoformat())

        entity_news = (
            self.entity_news_client.fetch_all() if self.settings.entity_tracking_enabled else {}
        )
        for key in list(entity_news.keys()):
            entity_news[key] = news_memory.classify(
                entity_news[key], cycle_ts=cycle_started_at.isoformat()
            )
        self._current_entity_news = entity_news
        self._current_market_vix = self.vix_client.fetch()
        try:
            self._current_correlation_context = self.correlation_client.fetch()
        except Exception as exc:  # pragma: no cover - network/defensive
            LOGGER.warning("correlation context fetch failed: %s", exc)
            self._current_correlation_context = None
        # Phase 4 macro context. Cached with a TTL because the underlying
        # data updates daily (FRED) / weekly (COT) — re-fetching every cycle
        # would waste the free Finnhub 60 req/min budget for no new signal.
        try:
            self._current_macro_context = self._fetch_macro_context_cached(cycle_started_at)
        except Exception as exc:  # pragma: no cover - network/defensive
            LOGGER.warning("macro context fetch failed: %s", exc)
            self._current_macro_context = None
        self._current_econ_events = self.econ_calendar.events_next_hours(
            hours=self.settings.econ_calendar_lookahead_hours,
            impact_min="medium",
            currencies=None,
            now=cycle_started_at,
        )

        prorealtime_snapshot = self.prorealtime_client.load_snapshot()

        # Per review 2026-05-02-adhoc-1 #7: cooperative shutdown — bail
        # out before the expensive LLM phase if a stop has been requested.
        if self.stop_event.is_set():
            LOGGER.info("Stop requested — abandoning cycle before LLM phase")
            return []

        # Per review 2026-04-30-token-burn #A: short-circuit the LLM phase
        # when no new entries are possible (drawdown breach, outside entry
        # window, PRT flatten_all). Skips the four subagent LLM calls
        # entirely; CLOSE plans are emitted deterministically from
        # close_requests. Saves ~80% of token spend on bad-loss days.
        #
        # Per review 2026-05-05-adhoc-1 #5: exception to the short-circuit
        # rule. When the ONLY block reason is drawdown breach AND there
        # are open positions, run the LLM in CLOSE-ONLY mode (set
        # ``close_only_mode=True`` on guardrail_context). This gives the
        # LLM a chance to time the close intelligently; the risk gate
        # still rejects any non-CLOSE/HOLD plan with reason
        # ``drawdown_close_only_mode``. When no positions are open we
        # keep short-circuiting unchanged.
        entry_block_lite = self._compute_entry_block_lite(
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
            now=cycle_started_at,
        )
        close_only_mode = entry_block_lite.get("drawdown_close_only", False)
        # Only the drawdown-breach block reason is eligible for the
        # close-only LLM path. Other block reasons (outside entry window,
        # PRT flatten_all, all-sessions-closed) keep the short-circuit
        # behaviour unchanged.
        only_drawdown_blocks = (
            entry_block_lite["block_new_entries"]
            and entry_block_lite["daily_loss_breached"]
            and len(entry_block_lite["block_reasons"]) == 1
            and close_only_mode
        )
        if entry_block_lite["block_new_entries"] and not only_drawdown_blocks:
            return self._run_short_circuit_cycle(
                cycle_started_at=cycle_started_at,
                account_snapshot=account_snapshot,
                prorealtime_snapshot=prorealtime_snapshot,
                entry_block=entry_block_lite,
                operator_context=operator_context,
            )

        market_research = self._build_market_research(
            account_snapshot=account_snapshot,
            market_headlines=market_headlines,
            operator_context=operator_context,
            prorealtime_snapshot=prorealtime_snapshot,
        )
        active_symbols = self._select_active_symbols(
            account_snapshot=account_snapshot,
            market_headlines=market_headlines,
            market_research=market_research,
            operator_context=operator_context,
            prorealtime_snapshot=prorealtime_snapshot,
        )
        symbol_contexts = self._collect_symbol_contexts(
            active_symbols,
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
        )

        if not symbol_contexts:
            LOGGER.warning(
                "No symbol contexts were collected; skipping decision and execution for this cycle"
            )
            return []

        guardrail_context = self._build_guardrail_context(
            account_snapshot=account_snapshot,
            symbol_contexts=symbol_contexts,
            prorealtime_snapshot=prorealtime_snapshot,
            now=cycle_started_at,
        )
        # Per review 2026-05-05-adhoc-1 #5: when we entered the LLM phase
        # despite a drawdown breach (because positions are open), tag
        # the context so the prompt builder appends a CLOSE-ONLY MODE
        # clause and the risk gate rejects any non-CLOSE/HOLD plan.
        if close_only_mode:
            guardrail_context["close_only_mode"] = True
            LOGGER.info(
                "CLOSE-ONLY mode active: drawdown breached (%.2f%%) with "
                "%d open position(s); LLM will be constrained to "
                "CLOSE/HOLD plans only.",
                entry_block_lite["drawdown_pct"] * 100.0,
                len(account_snapshot.open_positions),
            )

        news_synthesis = None
        if self.settings.news_synthesiser_enabled:
            news_synthesis = self._invoke_news_synthesiser(
                market_headlines=market_headlines,
                symbol_contexts=symbol_contexts,
                account_snapshot=account_snapshot,
            )
        self._current_news_synthesis = news_synthesis

        symbol_research = self._build_symbol_research(
            account_snapshot=account_snapshot,
            symbol_contexts=symbol_contexts,
            market_headlines=market_headlines,
            market_research=market_research,
            news_synthesis=news_synthesis,
            operator_context=operator_context,
            guardrail_context=guardrail_context,
        )
        # Per review 2026-04-29-adhoc-1 #10: feed recent execution outcomes
        # into the Portfolio Manager so the LLM can avoid re-proposing
        # plans that just got rejected by the broker / pre-submit gate.
        recent_outcomes = self.runtime_state.get("recent_execution_outcomes", [])
        with self.model_lock:
            decision = self.decision_engine.decide(
                account_snapshot=account_snapshot,
                symbol_contexts=symbol_contexts,
                market_headlines=market_headlines,
                market_research=market_research,
                symbol_research=symbol_research,
                news_synthesis=news_synthesis,
                regime_history=self.runtime_state.get("regime_history", []),
                open_position_invalidations=self.runtime_state.get("open_position_invalidations", {}),
                operator_context=operator_context,
                guardrail_context=guardrail_context,
                recent_execution_outcomes=recent_outcomes,
            )
        # Per review 2026-04-30-adhoc-1 #8: dedupe consecutive identical
        # regime tags and cap retention to 100 entries (was 16).
        # Pre-fix: 16 identical "energy-supply-shock" tokens consumed
        # the entire history, leaving zero regime-change signal for
        # the LLM to reason about. With dedupe, 100 entries can span
        # multiple regime transitions; cycle_at on each entry now
        # captures FIRST appearance, plus a last_seen_at and a count
        # so the LLM can see "energy-supply-shock since 09:30, last
        # confirmed 17 cycles later" rather than 17 identical rows.
        regime_hist = self.runtime_state.setdefault("regime_history", [])
        new_entry = {
            "cycle_at": cycle_started_at.isoformat(),
            "regime": decision.market_regime,
            "last_seen_at": cycle_started_at.isoformat(),
            "count": 1,
        }
        if regime_hist and regime_hist[-1].get("regime") == decision.market_regime:
            # Repeat — bump last_seen_at and count; do NOT append a row.
            last = regime_hist[-1]
            last["last_seen_at"] = cycle_started_at.isoformat()
            last["count"] = int(last.get("count") or 1) + 1
        else:
            regime_hist.append(new_entry)
        self.runtime_state["regime_history"] = regime_hist[-100:]
        decision = self._apply_operator_overrides(
            decision=decision,
            account_snapshot=account_snapshot,
            operator_context=operator_context,
        )
        decision = self._apply_guardrail_overrides(
            decision=decision,
            account_snapshot=account_snapshot,
            guardrail_context=guardrail_context,
        )
        self._log_prorealtime_snapshot(prorealtime_snapshot)
        self._log_market_research(market_research)
        self._log_guardrail_context(guardrail_context)
        self._log_decision_summary(decision)
        self.json_log.log("decision", decision.model_dump(mode="json"), cycle_id=self._current_cycle_id)
        plans = self.risk_manager.build_execution_plans(
            decision=decision,
            symbol_contexts=symbol_contexts,
            account_snapshot=account_snapshot,
            last_trade_times=self.last_trade_times,
            guardrail_context=guardrail_context,
        )
        self._log_execution_plans(plans)
        self._log_decision_records(
            decision=decision,
            symbol_contexts=symbol_contexts,
            plans=plans,
            account_snapshot=account_snapshot,
            guardrail_context=guardrail_context,
            cycle_started_at=cycle_started_at,
        )

        results: list[ExecutionResult] = []
        blocked_symbols: set[str] = set()
        for plan in plans:
            if plan.symbol in blocked_symbols and plan.action != "CLOSE":
                results.append(
                    ExecutionResult(
                        plan=plan,
                        status="failed",
                        submitted_at=datetime.now(timezone.utc).isoformat(),
                        error=(
                            f"Skipped {plan.action} for {plan.symbol} because a required close "
                            "failed or did not confirm safely in this cycle"
                        ),
                    )
                )
                LOGGER.warning(
                    "Skipping %s %s because a required close failed or did not confirm safely",
                    plan.action,
                    plan.symbol,
                )
                continue

            LOGGER.info("Executing %s %s: %s", plan.action, plan.symbol, plan.reason)
            result = self.broker.execute(plan=plan, dry_run=self.settings.dry_run, cycle_id=self._current_cycle_id)
            results.append(result)
            if result.status in {"submitted", "dry_run"}:
                self.last_trade_times[plan.symbol] = cycle_started_at
            if plan.action == "CLOSE" and not self._close_result_allows_follow_on_entry(result):
                blocked_symbols.add(plan.symbol)
            self._log_execution_result(result)

        invalidations = self.runtime_state.setdefault("open_position_invalidations", {})
        for result in results:
            if result.plan.action in {"BUY", "SELL"} and result.status in {"submitted", "dry_run"}:
                pid = (
                    ((result.broker_response or {}).get("ack") or {}).get("dealReference")
                    or f"{result.plan.symbol}:{cycle_started_at.isoformat()}"
                )
                invalidations[pid] = {
                    "symbol": result.plan.symbol,
                    "condition": result.plan.reason,
                    "entry_at": result.submitted_at,
                    "cycle_id": self._current_cycle_id,
                }
            if result.plan.action == "CLOSE" and result.status in {"submitted", "dry_run"}:
                invalidations.pop(result.plan.position_id or "", None)

        self.json_log.log(
            "execution",
            [r.model_dump(mode="json") for r in results],
            cycle_id=self._current_cycle_id,
        )

        # Per review 2026-04-29-adhoc-1 #10: keep the last 3 cycles'
        # execution outcomes so the next decide() call can reference
        # them. We only persist the minimum signal the prompt needs:
        # symbol, action, status, error/reason. Avoids burning prompt
        # tokens on full plan dumps.
        cycle_outcomes = []
        for r in results:
            plan = r.plan
            cycle_outcomes.append(
                {
                    "symbol": plan.symbol,
                    "action": plan.action,
                    "status": r.status,
                    "error": (r.error or "")[:200],
                    "submitted_at": r.submitted_at,
                }
            )
        if cycle_outcomes:
            history = self.runtime_state.setdefault("recent_execution_outcomes", [])
            history.append(
                {
                    "cycle_id": self._current_cycle_id,
                    "cycle_started_at": cycle_started_at.isoformat(),
                    "outcomes": cycle_outcomes,
                }
            )
            # Keep only the last 3 cycles.
            self.runtime_state["recent_execution_outcomes"] = history[-3:]

        if self.paper_pnl is not None and self.settings.dry_run:
            for result in results:
                if result.status == "dry_run" and result.plan.action in {"BUY", "SELL"}:
                    try:
                        instrument = get_instrument(result.plan.symbol)
                        side = "buy" if result.plan.action == "BUY" else "sell"
                        entry = result.plan.target_price or 0.0
                        stop = entry - (result.plan.stop_distance or 0.0) if side == "buy" else entry + (result.plan.stop_distance or 0.0)
                        tp = entry + (result.plan.take_profit_distance or 0.0) if side == "buy" else entry - (result.plan.take_profit_distance or 0.0)
                        # Phase 3 forward-walk eval: carry the decision join
                        # key + confidence (for Brier calibration) and the
                        # cost params (adverse fills + financing) into the
                        # paper simulator so the eval is not optimistically
                        # biased.
                        trade = next(
                            (t for t in decision.trades if t.symbol == result.plan.symbol),
                            None,
                        )
                        self.paper_pnl.record_entry(
                            symbol=result.plan.symbol,
                            side=side,
                            entry=entry,
                            stop=stop,
                            tp=tp,
                            opened_at=result.submitted_at,
                            data_symbol=instrument.data_symbol,
                            decision_id=make_decision_id(
                                self._current_cycle_id or "", result.plan.symbol
                            ),
                            confidence=float(trade.confidence) if trade else None,
                            spread_bps=float(instrument.max_spread_bps),
                            slippage_bps=float(self.settings.slippage_buffer_bps),
                            swap_annualised_bps=float(self.settings.swap_annualised_bps_default),
                            max_holding_hours=float(
                                (trade.max_holding_hours if trade else None)
                                or self.settings.default_max_holding_hours
                            ),
                        )
                    except Exception as exc:
                        LOGGER.warning("paper_pnl record_entry failed: %s", exc)
            try:
                self.paper_pnl.evaluate()
            except Exception as exc:
                LOGGER.warning("paper_pnl evaluate failed: %s", exc)

        # Track-A mechanical twin: trades the same symbol contexts through
        # its own paper book — entries are decided by the deterministic
        # daily-trend rules, never by the LLM. Best-effort.
        if self.track_a_pnl is not None and self.settings.dry_run:
            try:
                from trading_bot.track_a import run_track_a_cycle

                run_track_a_cycle(symbol_contexts, self.track_a_pnl, self.settings)
            except Exception as exc:
                LOGGER.warning("track_a cycle failed: %s", exc)

        self._refresh_runtime_state(account_snapshot, cycle_started_at)
        self.prorealtime_client.export_watchlist(
            active_symbols=active_symbols,
            market_research=market_research,
            decision=decision,
            plans=plans,
        )
        self._write_cycle_log(
            cycle_started_at=cycle_started_at,
            cycle_id=self._current_cycle_id,
            account_snapshot=account_snapshot.model_dump(mode="json"),
            market_headlines=[headline.model_dump(mode="json") for headline in market_headlines],
            symbol_contexts={
                symbol: context.model_dump(mode="json")
                for symbol, context in symbol_contexts.items()
            },
            active_symbols=active_symbols,
            operator_context=operator_context,
            prorealtime_snapshot=(
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            guardrail_context=guardrail_context,
            market_research=market_research.model_dump(mode="json") if market_research else None,
            news_synthesis=(
                news_synthesis.model_dump(mode="json") if news_synthesis else None
            ),
            symbol_research=symbol_research.model_dump(mode="json") if symbol_research else None,
            decision=decision.model_dump(mode="json"),
            plans=[plan.model_dump(mode="json") for plan in plans],
            results=[result.model_dump(mode="json") for result in results],
        )
        self.last_cycle_report = {
            "cycle_started_at": cycle_started_at.isoformat(),
            "active_symbols": active_symbols,
            "operator_context": operator_context,
            "prorealtime_snapshot": (
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            "guardrail_context": guardrail_context,
            "market_research": market_research.model_dump(mode="json") if market_research else None,
            "symbol_research": symbol_research.model_dump(mode="json") if symbol_research else None,
            "decision": decision.model_dump(mode="json"),
            "plans": [plan.model_dump(mode="json") for plan in plans],
            "results": [result.model_dump(mode="json") for result in results],
            "account_snapshot": account_snapshot.model_dump(mode="json"),
        }
        news_memory.purge(now=cycle_started_at)
        self._save_runtime_state()
        # Per review 2026-04-29-adhoc-1 #7: positive heartbeat on cycle
        # completion. The watcher uses absence of these for >2× normal
        # cycle interval as a hang signal.
        # Per review 2026-05-05-adhoc-1 #7: news_memory metrics enriched.
        nm_metrics = self._news_memory_metrics()
        self.json_log.log(
            "cycle_heartbeat",
            {
                "cycle_id": self._current_cycle_id,
                "phase": "end",
                "plans": len(plans),
                "results": len(results),
                "news_memory_size": nm_metrics["size"],
                "news_memory_oldest_published_at": nm_metrics["oldest_published_at"],
            },
            cycle_id=self._current_cycle_id,
        )
        LOGGER.info(
            "Cycle complete: %d decisions approved, %d results recorded",
            len(plans),
            len(results),
        )
        return results

    def _select_active_symbols(
        self,
        *,
        account_snapshot: AccountSnapshot,
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None = None,
        operator_context: dict | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> list[str]:
        configured_symbols = [symbol.upper() for symbol in self.settings.symbols]
        discovered_symbols: list[str] = []
        prorealtime_symbols = [
            item.symbol.upper()
            for item in (prorealtime_snapshot.symbols[: self.settings.max_discovered_symbols] if prorealtime_snapshot else [])
            if item.symbol.strip()
        ]
        focused_symbols = [
            symbol.upper() for symbol in (operator_context or {}).get("focused_symbols", [])
        ]
        excluded_symbols = {
            symbol.upper() for symbol in (operator_context or {}).get("excluded_symbols", [])
        }
        forced_close_symbols = {
            symbol.upper() for symbol in (operator_context or {}).get("forced_close_symbols", [])
        }

        if self.settings.auto_discover_symbols or not configured_symbols:
            if market_research is not None:
                discovered_symbols = [
                    candidate.symbol.upper()
                    for candidate in market_research.candidates[: self.settings.max_discovered_symbols]
                ]
            else:
                try:
                    discovered_symbols = self.decision_engine.discover_symbols(
                        market_headlines=market_headlines,
                        max_symbols=self.settings.max_discovered_symbols,
                        existing_symbols=configured_symbols,
                        operator_context=operator_context,
                        prorealtime_snapshot=prorealtime_snapshot,
                    )
                except Exception as exc:
                    LOGGER.warning("Automatic symbol discovery failed: %s", exc)

        open_position_symbols = [
            position.symbol.upper()
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        ]
        open_position_symbol_set = set(open_position_symbols)

        candidate_symbols = self._dedupe_symbols(
            configured_symbols
            + focused_symbols
            + discovered_symbols
            + prorealtime_symbols
            + open_position_symbols
            + list(forced_close_symbols)
        )
        if not candidate_symbols:
            fallback_symbols = [
                instrument.symbol.upper()
                for instrument in list_supported_instruments(self.settings.allowed_asset_classes)[
                    : self.settings.max_discovered_symbols
                ]
            ]
            candidate_symbols = self._dedupe_symbols(open_position_symbols + fallback_symbols)
            LOGGER.warning(
                "No candidate symbols were available after discovery; falling back to liquid universe: %s",
                ", ".join(candidate_symbols),
            )

        # Per review 2026-05-02-adhoc-1 #1: filter symbols whose exchange
        # session is closed (weekends, holidays, outside trading hours)
        # before they reach the LLM payload. Open positions bypass — we
        # still want to consider closes when the market is closed.
        now = datetime.now(timezone.utc)
        tradeable_symbols: list[str] = []
        for symbol in candidate_symbols:
            if (
                symbol in excluded_symbols
                and symbol not in open_position_symbol_set
                and symbol not in forced_close_symbols
            ):
                LOGGER.info("Skipping %s because the operator excluded it from new trading", symbol)
                continue
            if symbol in open_position_symbol_set:
                tradeable_symbols.append(symbol)
                continue
            # Session-open filter (defense-in-depth alongside the lite
            # entry-block check). Skip silently — log at debug to avoid
            # noise on weekend cycles.
            try:
                inst = get_instrument(symbol)
                if not self.holiday_calendar.is_session_open(inst.exchange_code, now):
                    LOGGER.debug(
                        "Skipping %s because exchange %s session is closed",
                        symbol, inst.exchange_code,
                    )
                    continue
            except Exception:
                # Unknown symbol or calendar miss — be permissive
                pass
            if self._is_tradeable_symbol(symbol):
                tradeable_symbols.append(symbol)
            else:
                LOGGER.info(
                    "Skipping %s because the broker could not resolve it as tradeable",
                    symbol,
                )

        # supervisor blacklist (skip even if otherwise tradeable)
        from trading_bot.supervisor_blacklist import read_active_blacklist
        blacklisted = read_active_blacklist(self.settings.supervisor_blacklist_path)
        if blacklisted:
            kept = []
            for sym in tradeable_symbols:
                if sym in blacklisted and sym not in open_position_symbol_set:
                    LOGGER.info("Skipping %s — supervisor blacklist", sym)
                    continue
                kept.append(sym)
            tradeable_symbols = kept

        if not tradeable_symbols:
            LOGGER.warning(
                "No tradeable symbols remained after broker filtering; cycle will be skipped"
            )
            return []

        LOGGER.info("Active symbols for this cycle: %s", ", ".join(tradeable_symbols))
        return tradeable_symbols

    def _build_market_research(
        self,
        *,
        account_snapshot: AccountSnapshot,
        market_headlines: list[Headline],
        operator_context: dict | None = None,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> MarketResearchEnvelope | None:
        if not self.settings.enable_research_subagents:
            return None
        # Per review 2026-04-30-token-burn #C: TTL cache. Macro themes
        # are sticky; re-running this every cycle wastes tokens.
        now = datetime.now(timezone.utc)
        cached = self._market_research_cache_lookup(market_headlines, now)
        if cached is not None:
            self.json_log.log(
                "llm_cache_hit",
                {"subagent": "market_research"},
                cycle_id=getattr(self, "_current_cycle_id", None) or None,
            )
            return cached
        try:
            with self.model_lock:
                value = self.decision_engine.research_market(
                    market_headlines,
                    max_symbols=self.settings.max_discovered_symbols,
                    account_snapshot=account_snapshot,
                    existing_symbols=self.settings.symbols,
                    operator_context=operator_context,
                    prorealtime_snapshot=prorealtime_snapshot,
                )
            self._market_research_cache_store(value, market_headlines, now)
            return value
        except Exception as exc:
            LOGGER.warning("Market research subagent failed: %s", exc)
            self._emit_subagent_failure("market_research", exc)
            return None

    def _count_breaking_high_priority(self, market_headlines: list[Headline]) -> int:
        """Count 'new' headlines from top-tier sources (priority <= 2).

        Used to invalidate market_research / news_synthesis caches when
        meaningful breaking news arrives. Source priority comes from
        the news client's source_priority map (Bloomberg/Reuters/CNBC
        typically 1-2; lesser outlets 3+).
        """
        count = 0
        for h in market_headlines:
            if getattr(h, "novelty", "new") != "new":
                continue
            priority = getattr(h, "source_priority", None)
            if priority is None:
                continue
            try:
                if int(priority) <= 2:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def _market_research_cache_lookup(
        self,
        market_headlines: list[Headline],
        now: datetime,
    ) -> MarketResearchEnvelope | None:
        cache = self._market_research_cache
        if not cache:
            return None
        ttl = max(int(self.settings.market_research_cache_minutes), 0)
        if ttl <= 0:
            return None
        try:
            age_minutes = (now - cache["at"]).total_seconds() / 60.0
        except (TypeError, AttributeError):
            return None
        if age_minutes >= ttl:
            return None
        threshold = max(int(self.settings.breaking_news_invalidation_threshold), 1)
        new_high = self._count_breaking_high_priority(market_headlines)
        if new_high >= cache.get("breaking_count", 0) + threshold:
            return None
        return cache.get("value")

    def _market_research_cache_store(
        self,
        value: MarketResearchEnvelope,
        market_headlines: list[Headline],
        now: datetime,
    ) -> None:
        if value is None:
            return
        self._market_research_cache = {
            "at": now,
            "value": value,
            "breaking_count": self._count_breaking_high_priority(market_headlines),
        }

    def _news_synthesis_cache_lookup(
        self,
        market_headlines: list[Headline],
        active_symbols: list[str],
        now: datetime,
    ):
        cache = self._news_synthesis_cache
        if not cache:
            return None
        ttl = max(int(self.settings.news_synthesis_cache_minutes), 0)
        if ttl <= 0:
            return None
        try:
            age_minutes = (now - cache["at"]).total_seconds() / 60.0
        except (TypeError, AttributeError):
            return None
        if age_minutes >= ttl:
            return None
        # Active-symbol set must match — otherwise the cached payload's
        # per_symbol_summaries don't cover the new symbol.
        if tuple(sorted(active_symbols)) != cache.get("active_symbols"):
            return None
        threshold = max(int(self.settings.breaking_news_invalidation_threshold), 1)
        new_high = self._count_breaking_high_priority(market_headlines)
        if new_high >= cache.get("breaking_count", 0) + threshold:
            return None
        return cache.get("value")

    def _news_synthesis_cache_store(
        self,
        value,
        market_headlines: list[Headline],
        active_symbols: list[str],
        now: datetime,
    ) -> None:
        if value is None:
            return
        self._news_synthesis_cache = {
            "at": now,
            "value": value,
            "active_symbols": tuple(sorted(active_symbols)),
            "breaking_count": self._count_breaking_high_priority(market_headlines),
        }

    def _build_symbol_research(
        self,
        *,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        market_headlines: list[Headline],
        market_research: MarketResearchEnvelope | None,
        news_synthesis=None,
        operator_context: dict | None = None,
        guardrail_context: dict | None = None,
    ) -> SymbolResearchEnvelope | None:
        if not self.settings.enable_research_subagents:
            return None
        try:
            with self.model_lock:
                return self.decision_engine.research_symbols(
                    account_snapshot=account_snapshot,
                    symbol_contexts=symbol_contexts,
                    market_headlines=market_headlines,
                    market_research=market_research,
                    news_synthesis=news_synthesis,
                    operator_context=operator_context,
                    guardrail_context=guardrail_context,
                )
        except Exception as exc:
            LOGGER.warning("Symbol research subagent failed: %s", exc)
            self._emit_subagent_failure("symbol_research", exc)
            return None

    def _collect_symbol_contexts(
        self,
        symbols: list[str],
        *,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None = None,
    ) -> dict[str, SymbolContext]:
        symbol_contexts: dict[str, SymbolContext] = {}
        open_positions_by_symbol = {
            position.symbol.upper(): position
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        for symbol in symbols:
            prorealtime_symbol = self.prorealtime_client.get_symbol_snapshot(
                symbol=symbol,
                snapshot=prorealtime_snapshot,
            )
            try:
                per_symbol_headlines = self.news_client.fetch_symbol_headlines(symbol)
            except Exception as exc:
                LOGGER.warning("Headline collection degraded for %s: %s", symbol, exc)
                per_symbol_headlines = []

            entity_items: list[Headline] = []
            for entity in entities_for_symbol(symbol):
                entity_items.extend(self._current_entity_news.get(entity.key, []))
            headlines: list[Headline] = per_symbol_headlines + entity_items

            try:
                symbol_contexts[symbol] = self.market_data_client.build_symbol_context(
                    symbol=symbol,
                    headlines=headlines,
                    prorealtime=prorealtime_symbol,
                )
                continue
            except Exception as exc:
                if prorealtime_symbol and (prorealtime_symbol.last_price or 0.0) > 0:
                    LOGGER.warning(
                        "Using ProRealTime fallback context for %s due to market-data error: %s",
                        symbol,
                        exc,
                    )
                    symbol_contexts[symbol] = self.market_data_client.build_fallback_symbol_context(
                        symbol=symbol,
                        fallback_price=float(prorealtime_symbol.last_price or 0.0),
                        headlines=headlines,
                        note=(
                            "Fallback context from ProRealTime sidecar snapshot because Yahoo "
                            "Finance data was unavailable"
                        ),
                        prorealtime=prorealtime_symbol,
                    )
                    continue
                open_position = open_positions_by_symbol.get(symbol.upper())
                fallback_context = self._build_position_fallback_context(
                    symbol=symbol,
                    open_position=open_position,
                    headlines=headlines,
                    prorealtime=prorealtime_symbol,
                )
                if fallback_context is not None:
                    LOGGER.warning(
                        "Using fallback broker snapshot context for %s due to market-data error: %s",
                        symbol,
                        exc,
                    )
                    symbol_contexts[symbol] = fallback_context
                    continue
                LOGGER.warning("Skipping %s due to data collection error: %s", symbol, exc)
        return symbol_contexts

    def _build_position_fallback_context(
        self,
        *,
        symbol: str,
        open_position: OpenPosition | None,
        headlines: list[Headline],
        prorealtime: ProRealTimeSymbolSnapshot | None = None,
    ) -> SymbolContext | None:
        fallback_price = 0.0
        if prorealtime and (prorealtime.last_price or 0.0) > 0:
            fallback_price = float(prorealtime.last_price or 0.0)
        elif open_position is not None and (open_position.current_price or 0.0) > 0:
            fallback_price = float(open_position.current_price or 0.0)

        if fallback_price <= 0:
            return None

        instrument = get_instrument(symbol)
        price = fallback_price
        indicators = IndicatorSnapshot(
            price=price,
            sma_20=price,
            sma_50=price,
            ema_12=price,
            ema_26=price,
            rsi_14=50.0,
            macd=0.0,
            macd_signal=0.0,
            atr_14=0.0,
            volatility_20d=0.0,
            return_1d=0.0,
            return_5d=0.0,
        )
        return SymbolContext(
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            as_of=datetime.now(timezone.utc).isoformat(),
            indicators=indicators,
            headlines=headlines,
            chart_note=(
                "Fallback context from broker open-position snapshot because market data was unavailable"
                if open_position is not None and (open_position.current_price or 0.0) > 0
                else "Fallback context from ProRealTime sidecar snapshot because market data was unavailable"
            ),
            prorealtime=prorealtime,
        )

    def add_operator_note(self, note: str) -> None:
        normalized = note.strip()
        if not normalized:
            return
        with self.operator_lock:
            self.operator_notes.append(normalized)

    def add_focus_symbols(self, symbols: list[str]) -> list[str]:
        normalized = self._normalize_symbol_list(symbols)
        if not normalized:
            return []
        with self.operator_lock:
            self.focused_symbols.update(normalized)
        return normalized

    def add_excluded_symbols(self, symbols: list[str]) -> list[str]:
        normalized = self._normalize_symbol_list(symbols)
        if not normalized:
            return []
        with self.operator_lock:
            self.excluded_symbols.update(normalized)
        return normalized

    def queue_forced_close(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("A symbol is required for close requests")
        with self.operator_lock:
            self.forced_close_symbols.add(normalized)
        return normalized

    def clear_operator_guidance(self) -> None:
        with self.operator_lock:
            self.operator_notes.clear()
            self.focused_symbols.clear()
            self.excluded_symbols.clear()
            self.forced_close_symbols.clear()

    def pause(self) -> None:
        with self.operator_lock:
            self.paused = True
        self.wake_event.set()

    def resume(self) -> None:
        with self.operator_lock:
            self.paused = False
        self.wake_event.set()

    def request_run_now(self) -> None:
        self.wake_event.set()

    def request_stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def answer_operator_question(self, question: str) -> str:
        runtime_state = self.build_runtime_state()
        with self.model_lock:
            return self.decision_engine.answer_operator_question(
                question,
                runtime_state=runtime_state,
            )

    def interpret_operator_input(self, text: str) -> OperatorCommandEnvelope:
        runtime_state = self.build_runtime_state()
        with self.model_lock:
            return self.decision_engine.interpret_operator_input(
                text,
                runtime_state=runtime_state,
            )

    def build_runtime_state(self) -> dict:
        with self.operator_lock:
            operator_context = {
                "notes": list(self.operator_notes),
                "focused_symbols": sorted(self.focused_symbols),
                "excluded_symbols": sorted(self.excluded_symbols),
                "forced_close_symbols": sorted(self.forced_close_symbols),
                "paused": self.paused,
            }
        return {
            "settings": {
                "broker": self.settings.broker,
                "mode": self.settings.execution_mode,
                "dry_run": self.settings.dry_run,
                "allow_shorts": self.settings.allow_shorts,
                "max_open_positions": self.settings.max_open_positions,
                "max_new_trades_per_cycle": self.settings.max_new_trades_per_cycle,
                "prt_enabled": self.settings.prt_enabled,
                "prt_confirmation_mode": self.settings.prt_confirmation_mode,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "default_max_holding_hours": self.settings.default_max_holding_hours,
            },
            "operator_context": operator_context,
            "runtime_state": self.runtime_state,
            "last_cycle_report": self.last_cycle_report,
        }

    def get_status_report(self) -> str:
        runtime_state = self.build_runtime_state()
        last_cycle = runtime_state.get("last_cycle_report") or {}
        operator_context = runtime_state["operator_context"]
        lines = [
            f"paused: {operator_context['paused']}",
            f"notes: {len(operator_context['notes'])}",
            f"focused_symbols: {', '.join(operator_context['focused_symbols']) or '-'}",
            f"excluded_symbols: {', '.join(operator_context['excluded_symbols']) or '-'}",
            f"forced_close_symbols: {', '.join(operator_context['forced_close_symbols']) or '-'}",
            f"tracked_positions: {len((runtime_state.get('runtime_state') or {}).get('positions', {}))}",
        ]
        daily_guardrail = (runtime_state.get("runtime_state") or {}).get("daily", {})
        if daily_guardrail:
            lines.append(
                "daily_guardrail: "
                f"date={daily_guardrail.get('date', '-')}, "
                f"starting_equity={daily_guardrail.get('starting_equity', '-')}"
            )
        if last_cycle:
            lines.extend(
                [
                    f"last_cycle_started_at: {last_cycle.get('cycle_started_at', '-')}",
                    f"last_active_symbols: {', '.join(last_cycle.get('active_symbols', [])) or '-'}",
                    f"last_plan_count: {len(last_cycle.get('plans', []))}",
                    f"last_result_count: {len(last_cycle.get('results', []))}",
                ]
            )
            for plan in last_cycle.get("plans", []):
                lines.append(
                    f"plan: {plan.get('action')} {plan.get('symbol')} because {plan.get('reason', '')}"
                )
            for result in last_cycle.get("results", []):
                plan = result.get("plan", {})
                lines.append(
                    f"result: {result.get('status')} {plan.get('action')} {plan.get('symbol')} "
                    f"{result.get('error') or ''}".strip()
                )
        else:
            lines.append("last_cycle: no cycles have completed yet")
        return "\n".join(lines)

    def _get_operator_context(self, account_snapshot: AccountSnapshot) -> dict:
        open_symbols = {
            position.symbol.upper()
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        with self.operator_lock:
            self.forced_close_symbols.intersection_update(open_symbols)
            return {
                "notes": list(self.operator_notes),
                "focused_symbols": sorted(self.focused_symbols),
                "excluded_symbols": sorted(self.excluded_symbols),
                "forced_close_symbols": sorted(self.forced_close_symbols),
                "paused": self.paused,
            }

    def _apply_operator_overrides(
        self,
        *,
        decision: DecisionEnvelope,
        account_snapshot: AccountSnapshot,
        operator_context: dict,
    ) -> DecisionEnvelope:
        forced_close_symbols = {
            symbol.upper() for symbol in operator_context.get("forced_close_symbols", [])
        }
        if not forced_close_symbols:
            return decision

        open_positions_by_symbol = {
            position.symbol.upper(): position
            for position in account_snapshot.open_positions
            if position.symbol.strip()
        }
        trades = list(decision.trades)
        existing_close_symbols = {
            trade.symbol.upper()
            for trade in trades
            if trade.action == "CLOSE"
        }
        for symbol in sorted(forced_close_symbols):
            if symbol not in open_positions_by_symbol or symbol in existing_close_symbols:
                continue
            trades.append(
                TradeDecision(
                    symbol=symbol,
                    action="CLOSE",
                    confidence=1.0,
                    thesis="Operator-requested close from terminal interaction",
                    news_bias="neutral",
                    invalidate_if="Operator request withdrawn",
                    close_position_id=open_positions_by_symbol[symbol].position_id,
                )
            )
        if len(trades) == len(decision.trades):
            return decision
        return DecisionEnvelope(
            market_regime=decision.market_regime,
            summary=decision.summary,
            trades=trades,
        )

    def _load_runtime_state(self) -> dict:
        path = self.settings.runtime_state_path
        if not path.exists():
            return {"daily": {}, "positions": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Failed to load runtime state from %s: %s", path, exc)
            return {"daily": {}, "positions": {}}
        if not isinstance(payload, dict):
            return {"daily": {}, "positions": {}}
        payload.setdefault("daily", {})
        payload.setdefault("positions", {})
        return payload

    def _save_runtime_state(self) -> None:
        path = self.settings.runtime_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.runtime_state, handle, indent=2)
        os.replace(tmp, path)

    def _flatten_all_positions(self) -> None:
        snapshot = self.broker.get_account_snapshot()
        for pos in snapshot.open_positions:
            plan = ExecutionPlan(
                symbol=pos.symbol,
                action="CLOSE",
                confidence=1.0,
                reason="kill switch flatten",
                target_price=pos.current_price,
                position_id=pos.position_id,
            )
            self.broker.execute(plan=plan, dry_run=False)

    def _write_alert(self, event: str, data: dict) -> None:
        self.alerts.alert(event, data)

    def _reconcile_on_startup(self) -> None:
        LOGGER.info("Reconciling runtime state with broker on startup")
        try:
            snapshot = self.broker.get_account_snapshot()
        except Exception as exc:
            LOGGER.warning("Startup reconcile skipped (broker call failed): %s", exc)
            return
        broker_ids = {p.position_id for p in snapshot.open_positions if p.position_id}
        positions_state = self.runtime_state.setdefault("positions", {})
        stale = [pid for pid in positions_state if pid not in broker_ids]
        # Fetch broker transaction history once to resolve confirmed close levels
        # for any vanished positions (proposal #12). Best-effort; falls back to
        # estimated pnl if the broker call fails or doesn't expose the helper.
        transactions: list[dict] = []
        if stale:
            try:
                history_fn = getattr(self.broker, "get_transaction_history", None)
                if callable(history_fn):
                    transactions = history_fn() or []
            except Exception as exc:
                LOGGER.warning("Transaction history reconcile failed: %s", exc)
                transactions = []
        for pid in stale:
            LOGGER.info("Dropping stale local position %s (not at broker)", pid)
            stale_pos = positions_state.get(pid) or {}
            # Match by trade economics (openLevel≈entry_price + open-time),
            # NOT by dealId — IG tx rows carry no dealId. See
            # match_transaction_to_position docstring.
            broker_tx = match_transaction_to_position(stale_pos, transactions)
            # Emit a synthesised trade_close event so the supervisor's
            # consecutive_losses / same_symbol_stop_streak rules don't go
            # blind during connectivity outages. Per review
            # 2026-05-05-adhoc-1 #2, this caller is the startup path.
            self._emit_reconciled_trade_close(
                pid, stale_pos, broker_tx,
                caller_context="startup",
            )
            positions_state.pop(pid, None)
        # Per review 2026-05-02-adhoc-1 #3: aggressively purge ghost
        # ``open_position_invalidations`` records on startup. The
        # invalidation keys are dealReferences (what the bot SENDS to IG)
        # while ``broker_ids`` are dealIds / position_ids (what IG ASSIGNS
        # post-fill); the two ID spaces rarely match so the previous
        # ``if pid not in broker_ids`` cleanup almost never fires. Also
        # purge by symbol — if no current broker position covers a given
        # invalidation's symbol, the invalidation is by definition stale.
        invalidations = self.runtime_state.setdefault("open_position_invalidations", {})
        broker_symbols = {
            (p.symbol or "").upper()
            for p in snapshot.open_positions
            if (p.symbol or "").strip()
        }
        purged_invalidations = 0
        for pid in list(invalidations.keys()):
            entry = invalidations.get(pid) or {}
            entry_symbol = str(entry.get("symbol") or "").upper()
            stale_id = pid not in broker_ids
            stale_symbol = bool(entry_symbol) and entry_symbol not in broker_symbols
            if stale_id and stale_symbol:
                invalidations.pop(pid, None)
                purged_invalidations += 1
            elif stale_id and not entry_symbol:
                # No symbol on the entry to cross-check — fall back to
                # the legacy id-only purge (will match if dealReference
                # equals position_id by accident, which can happen for
                # paper broker fills).
                invalidations.pop(pid, None)
                purged_invalidations += 1
        if purged_invalidations:
            LOGGER.info(
                "Purged %d ghost open_position_invalidations on startup reconcile",
                purged_invalidations,
            )
        self._save_runtime_state()

    def _emit_reconciled_trade_close(
        self,
        position_id: str,
        stale_pos: dict,
        broker_tx: dict | None = None,
        *,
        caller_context: str = "startup",
    ) -> None:
        """Persist a best-effort trade_close event for a position that vanished
        from the broker while we were offline. If a matching IG transaction is
        provided (proposal #12), populate confirmed close level / pnl and tag
        estimated=false.

        Per review 2026-05-05-adhoc-1 #2: ``caller_context`` distinguishes
        the startup-reconcile path from the mid-session
        ``_refresh_runtime_state`` path. When ``broker_tx`` is provided, we
        still tag ``ig_transaction_history`` (the broker confirmation is
        the canonical source). When estimated, we tag
        ``reconciled_on_startup`` only on the actual startup path; the
        mid-session path now emits ``reconciled_mid_session`` so
        downstream consumers can tell a real broker-driven mid-session
        close from a post-restart catch-up.

        Per review 2026-04-30-adhoc-1 #1: when the vanished position was
        opened on a prior UTC date (overnight broker-driven close), the
        cash P&L is tagged ``overnight=True`` and credited to
        ``runtime_state.daily.overnight_pnl_credit`` so the guardrail
        context can subtract it from today's drawdown calc. Otherwise a
        single overnight loss consumes the next day's loss budget and
        freezes the bot for an entire trading session.
        """
        try:
            entry_price = stale_pos.get("entry_price")
            last_price = stale_pos.get("last_price") or stale_pos.get("highest_price")
            symbol = str(stale_pos.get("symbol") or "").upper()
            direction = str(stale_pos.get("direction") or "").lower()
            first_seen_at = stale_pos.get("first_seen_at")
            estimated = True
            close_level: float | None = None
            pnl_pct = 0.0
            pnl_cash: float | None = None  # broker-confirmed cash P&L (account currency)
            # Per review 2026-05-05-adhoc-1 #2: pick the literal based on
            # the caller. ``broker_tx`` overrides this when available
            # because IG's transaction history is the canonical source.
            if caller_context == "mid_session":
                close_source = "reconciled_mid_session"
            else:
                close_source = "reconciled_on_startup"
            if broker_tx:
                estimated = False
                close_source = "ig_transaction_history"
                # IG /history/transactions DEAL rows expose closeLevel /
                # openLevel / profitAndLoss (e.g. '£-4.08'). Older field
                # guesses ('level', 'instrumentValue', 'pnl', 'profit') do
                # not exist on this endpoint and silently produced null.
                close_level = _tx_float(
                    broker_tx.get("closeLevel")
                    if broker_tx.get("closeLevel") not in (None, "", "-")
                    else broker_tx.get("level")
                )
                pnl_value = _tx_float(
                    broker_tx.get("profitAndLoss")
                    if broker_tx.get("profitAndLoss") not in (None, "", "-")
                    else (broker_tx.get("pnl") or broker_tx.get("profit"))
                )
                if pnl_value is not None:
                    pnl_cash = pnl_value
                    # Prefer a price-derived pnl_pct from open/close levels
                    # (a true return). Fall back to pnl_cash/entry only when
                    # close level is unavailable.
                    open_level = _tx_float(broker_tx.get("openLevel")) or (
                        float(entry_price) if entry_price else None
                    )
                    if (
                        close_level is not None
                        and open_level is not None
                        and open_level > 0
                    ):
                        raw_ret = (close_level - open_level) / open_level
                        # Direction sign: a SELL profits when close < open.
                        if direction == "sell":
                            raw_ret = -raw_ret
                        pnl_pct = raw_ret
                    elif entry_price and float(entry_price) > 0:
                        pnl_pct = pnl_value / float(entry_price)
                    else:
                        pnl_pct = pnl_value
            else:
                try:
                    if entry_price and last_price and float(entry_price) > 0:
                        raw = (float(last_price) - float(entry_price)) / float(entry_price)
                        pnl_pct = -abs(raw) if direction == "buy" else -abs(raw)
                except (TypeError, ValueError):
                    pnl_pct = 0.0

            # Overnight detection: position first_seen_at on an earlier
            # UTC date than now ⇒ this P&L was incurred yesterday but the
            # bot only learned about it today. Tag for the drawdown gate
            # to credit it back to today's budget.
            now_utc = datetime.now(timezone.utc)
            opened_date: str | None = None
            overnight = False
            try:
                if first_seen_at:
                    opened_dt = datetime.fromisoformat(str(first_seen_at))
                    if opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                    opened_date = opened_dt.date().isoformat()
                    overnight = opened_dt.date() < now_utc.date()
            except (TypeError, ValueError):
                opened_date = None

            close_payload = {
                "position_id": position_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "last_known_price": last_price,
                "close_level": close_level,
                "pnl_pct": pnl_pct,
                "pnl_cash": pnl_cash,
                "reason": "stop",
                "close_source": close_source,
                "estimated": estimated,
                "opened_at": str(first_seen_at) if first_seen_at else None,
                "opened_date": opened_date,
                "overnight": overnight,
            }
            self.json_log.log("trade_close", close_payload)
            # Per review 2026-04-29-adhoc-1 #6: also append to runtime_state
            # so /bot-status and the supervisor see closed_trades.
            try:
                closed = self.runtime_state.setdefault("closed_trades", [])
                closed.append(
                    {
                        **close_payload,
                        "closed_at": now_utc.isoformat(),
                    }
                )
                # Cap to avoid unbounded growth.
                if len(closed) > 500:
                    self.runtime_state["closed_trades"] = closed[-500:]
            except Exception as exc:
                LOGGER.debug("Failed to append closed_trades for %s: %s", position_id, exc)

            # Per review 2026-04-30-adhoc-1 #1: credit overnight cash P&L
            # to today's drawdown bucket. Only credit losses (negative
            # cash P&L) — overnight gains are a windfall and should not
            # increase today's loss budget. If pnl_cash is unavailable
            # (no broker_tx), best-effort estimate from pnl_pct ×
            # entry_price is too unreliable for risk decisions; skip.
            if overnight and pnl_cash is not None and pnl_cash < 0:
                try:
                    daily_state = self.runtime_state.setdefault("daily", {})
                    credit = float(daily_state.get("overnight_pnl_credit") or 0.0)
                    # Add the loss magnitude (positive number) so the
                    # guardrail can subtract it cleanly.
                    credit += abs(float(pnl_cash))
                    daily_state["overnight_pnl_credit"] = credit
                    LOGGER.info(
                        "Overnight close %s on %s: crediting %.2f back to today's drawdown budget "
                        "(opened %s, closed today, total credit=%.2f)",
                        symbol, position_id, abs(float(pnl_cash)),
                        opened_date, credit,
                    )
                except Exception as exc:
                    LOGGER.debug("Failed to record overnight credit for %s: %s", position_id, exc)
        except Exception as exc:
            LOGGER.warning("Failed to emit reconciled trade_close for %s: %s", position_id, exc)

    def _emit_subagent_failure(
        self,
        subagent: str,
        error: BaseException,
        *,
        attempt: int | None = None,
    ) -> None:
        """Persist a structured ``subagent_failure`` event so the watcher's
        ``subagent_failure_streak`` rule can correlate degraded-decision
        windows. Per review 2026-04-29-adhoc-1 #9. Best-effort; never
        raises (we don't want event-logging to mask the original failure).
        """
        try:
            error_class = type(error).__name__
            payload = {
                "provider": self.settings.llm_provider,
                "subagent": subagent,
                "error_class": error_class,
                "error_message": str(error)[:500],
            }
            if attempt is not None:
                payload["attempt"] = int(attempt)
            self.json_log.log(
                "subagent_failure",
                payload,
                cycle_id=getattr(self, "_current_cycle_id", None) or None,
            )
        except Exception as exc:
            LOGGER.debug("Failed to emit subagent_failure event: %s", exc)

    def _invoke_news_synthesiser(
        self,
        *,
        market_headlines: list,
        symbol_contexts: dict,
        account_snapshot,
    ):
        """Run the news synthesiser with headline-payload truncation, one retry
        on parse failure, and a fallback to the previous cycle's synthesis.
        Emits a `news_synthesiser_failed` event when the cycle ultimately falls
        back to degraded research (proposal #9).

        Per review 2026-04-30-token-burn #C: TTL cache hit short-circuits
        all the retry/truncation/LLM-call work below.
        """
        # Cache check (TTL + active-symbol match + breaking-news guard)
        now = datetime.now(timezone.utc)
        active_symbols = list(symbol_contexts.keys())
        cached = self._news_synthesis_cache_lookup(market_headlines, active_symbols, now)
        if cached is not None:
            self.json_log.log(
                "llm_cache_hit",
                {"subagent": "news_synthesis"},
                cycle_id=getattr(self, "_current_cycle_id", None) or None,
            )
            return cached

        max_market = 20
        max_per_symbol = 12
        max_chars = 400

        def _truncate_headlines(items: list) -> list:
            out = []
            for h in items[:max_market]:
                try:
                    if hasattr(h, "model_copy"):
                        title = (getattr(h, "title", "") or "")[:max_chars]
                        summary = (getattr(h, "summary", "") or "")[:max_chars]
                        out.append(h.model_copy(update={"title": title, "summary": summary}))
                    else:
                        out.append(h)
                except Exception:
                    out.append(h)
            return out

        truncated_market = _truncate_headlines(market_headlines)
        truncated_per_symbol = {
            sym: _truncate_headlines((getattr(ctx, "headlines", []) or [])[:max_per_symbol])
            for sym, ctx in symbol_contexts.items()
        }

        def _attempt(tighten: bool = False):
            with self.model_lock:
                return self.decision_engine.synthesise_news(
                    market_headlines=truncated_market,
                    per_symbol_headlines=truncated_per_symbol,
                    entity_news=self._current_entity_news,
                    open_positions=list(account_snapshot.open_positions),
                    regime_history=self.runtime_state.get("regime_history", []),
                    active_symbols=list(symbol_contexts.keys()),
                    tighten=tighten,
                )

        try:
            value = _attempt()
            self._news_synthesis_cache_store(value, market_headlines, active_symbols, now)
            return value
        except Exception as first_exc:
            LOGGER.warning("News synthesiser failed (attempt 1): %s — retrying once", first_exc)
            self._emit_subagent_failure("news_synthesiser", first_exc, attempt=1)
            try:
                # Per review 2026-05-02-adhoc-1 #2: retry with the
                # tightened directive ('OUTPUT MUST BE LESS THAN 2000
                # TOKENS. Truncate.'), forcing the model to constrain
                # output. Pre-fix, the retry produced *more* output
                # than the first attempt, which made things worse.
                value = _attempt(tighten=True)
                self._news_synthesis_cache_store(value, market_headlines, active_symbols, now)
                return value
            except Exception as second_exc:
                LOGGER.warning("News synthesiser failed (attempt 2): %s — falling back", second_exc)
                self._emit_subagent_failure("news_synthesiser", second_exc, attempt=2)
                try:
                    self.json_log.log(
                        "news_synthesiser_failed",
                        {
                            "first_error": str(first_exc)[:500],
                            "second_error": str(second_exc)[:500],
                            "active_symbols": list(symbol_contexts.keys()),
                        },
                        cycle_id=self._current_cycle_id,
                    )
                except Exception:
                    pass
                return getattr(self, "_current_news_synthesis", None)

    def _fetch_macro_context_cached(self, now: datetime):
        """Fetch macro context, honouring a TTL cache so FRED/Finnhub/CFTC
        are not hit every cycle (their data changes daily/weekly). Returns
        the cached MacroContext within the TTL, else refetches."""
        ttl_min = float(getattr(self.settings, "macro_context_refresh_minutes", 180.0) or 0.0)
        cache = self._macro_context_cache
        if cache and ttl_min > 0:
            age = (now - cache["at"]).total_seconds() / 60.0
            if age < ttl_min:
                return cache["value"]
        ctx = self.macro_client.fetch(now=now)
        self._macro_context_cache = {"at": now, "value": ctx}
        return ctx

    def _refresh_runtime_state(
        self,
        account_snapshot: AccountSnapshot,
        now: datetime,
    ) -> None:
        # Per review 2026-04-29-adhoc-1 #6: bind the supervisor-visible
        # account_balance / starting_balance / daily_pnl fields to the
        # same source the Guardrails logger uses every cycle. The
        # supervisor's /bot-status and watcher rules read this file and
        # were getting nulls all day on 2026-04-29 despite the bot's
        # internal accounting being correct.
        account_view = self.runtime_state.setdefault("account", {})
        account_view["balance"] = float(account_snapshot.total_funds or 0.0)
        account_view["free_funds"] = float(account_snapshot.free_funds or 0.0)
        account_view["currency"] = str(account_snapshot.currency or "GBP")
        account_view["mode"] = str(account_snapshot.mode or "demo")
        account_view["updated_at"] = now.isoformat()
        # Mirror in legacy keys so external tools that read either name
        # see the same number.
        self.runtime_state["account_balance"] = account_view["balance"]

        # Sync starting balance from daily guardrail so we have a single
        # source of truth. _build_guardrail_context populates daily.
        daily_state = self.runtime_state.setdefault("daily", {})
        starting = daily_state.get("starting_equity")
        if starting is not None:
            try:
                self.runtime_state["starting_balance"] = float(starting)
                self.runtime_state["daily_pnl"] = (
                    float(account_snapshot.total_funds or 0.0) - float(starting)
                )
                # Per review 2026-04-30-adhoc-1 #1: surface the overnight
                # credit at the top level so /bot-status and the
                # supervisor watcher can see why an apparent "5% drawdown"
                # is not blocking entries.
                overnight_credit = float(daily_state.get("overnight_pnl_credit") or 0.0)
                if overnight_credit:
                    self.runtime_state["overnight_pnl_credit"] = overnight_credit
            except (TypeError, ValueError):
                pass

        positions_state = self.runtime_state.setdefault("positions", {})
        open_position_ids: set[str] = set()

        for position in account_snapshot.open_positions:
            if not position.position_id:
                continue
            open_position_ids.add(position.position_id)
            current_price = float(position.current_price or 0.0)
            item = positions_state.setdefault(
                position.position_id,
                {
                    "symbol": position.symbol.upper(),
                    "first_seen_at": now.isoformat(),
                    "entry_price": current_price or None,
                    "highest_price": current_price or None,
                    "lowest_price": current_price or None,
                    "max_holding_hours": self.settings.default_max_holding_hours,
                },
            )
            item["symbol"] = position.symbol.upper()
            item["direction"] = self._normalize_position_direction(position)
            item["updated_at"] = now.isoformat()
            if current_price > 0:
                if not item.get("entry_price"):
                    item["entry_price"] = current_price
                item["highest_price"] = max(float(item.get("highest_price") or current_price), current_price)
                item["lowest_price"] = min(float(item.get("lowest_price") or current_price), current_price)
                item["last_price"] = current_price

        # Per review 2026-04-29-adhoc-1 #5: when a tracked position has
        # vanished from the broker mid-session (normal broker-driven
        # stop-out), emit a `trade_close` event so the supervisor's
        # consecutive_losses / same_symbol_stop_streak rules see it.
        # The previous reconcile fix (#2 from 2026-04-28-adhoc-1) only
        # ran at startup; on 2026-04-29 three WTI fills opened and
        # closed within the same session and produced 0 trade_close
        # events. We attempt to fetch broker transaction history once
        # to get a confirmed close level, falling back to estimated
        # pnl if the helper is unavailable.
        vanished = [pid for pid in positions_state if pid not in open_position_ids]
        if vanished:
            transactions: list[dict] = []
            try:
                history_fn = getattr(self.broker, "get_transaction_history", None)
                if callable(history_fn):
                    transactions = history_fn() or []
            except Exception as exc:
                LOGGER.warning(
                    "Mid-session reconcile transaction history failed: %s — using estimated pnl",
                    exc,
                )
            for pid in vanished:
                stale_pos = positions_state.get(pid) or {}
                # Match by trade economics, not dealId (IG tx rows have none).
                broker_tx = match_transaction_to_position(stale_pos, transactions)
                # Per review 2026-05-05-adhoc-1 #2: this caller runs every
                # cycle, NOT only at startup. Tag accordingly so consumers
                # don't conflate post-restart catch-up with mid-session
                # broker-driven closes (e.g. silent stop-outs).
                self._emit_reconciled_trade_close(
                    pid, stale_pos, broker_tx,
                    caller_context="mid_session",
                )
                positions_state.pop(pid, None)
            # Per review 2026-05-02-adhoc-1 #3: also purge any
            # ``open_position_invalidations`` whose symbol is no longer
            # in the broker's open positions. This was the source of
            # the BRENT phantom that polluted prompts for ~32h.
            invalidations = self.runtime_state.setdefault(
                "open_position_invalidations", {}
            )
            broker_symbols = {
                (p.symbol or "").upper()
                for p in account_snapshot.open_positions
                if (p.symbol or "").strip()
            }
            for pid in list(invalidations.keys()):
                entry = invalidations.get(pid) or {}
                entry_symbol = str(entry.get("symbol") or "").upper()
                if entry_symbol and entry_symbol not in broker_symbols:
                    invalidations.pop(pid, None)

    def _compute_entry_block_lite(
        self,
        *,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        now: datetime,
    ) -> dict:
        """Cheap pre-check that determines if the LLM phase can be skipped.

        Returns the same ``block_new_entries`` and ``close_requests`` fields
        as :meth:`_build_guardrail_context` but does NOT require
        ``symbol_contexts`` — so it can be called BEFORE the four
        expensive subagent / decision LLM calls. Per review
        2026-04-30-token-burn #A.

        Important: this method WRITES to ``runtime_state.daily`` (resets
        the daily bucket on date roll) so the canonical
        ``_build_guardrail_context`` doesn't double-reset later. The
        full guardrail context is only built when we actually need it
        for the LLM decision; on short-circuit cycles, this lite output
        is the sole source of truth.
        """
        daily_state = self.runtime_state.setdefault("daily", {})
        current_date = now.date().isoformat()
        if daily_state.get("date") != current_date:
            daily_state.clear()
            daily_state["date"] = current_date
            daily_state["starting_equity"] = float(account_snapshot.total_funds)

        starting_equity = max(
            float(daily_state.get("starting_equity") or account_snapshot.total_funds), 0.01
        )
        overnight_credit = max(float(daily_state.get("overnight_pnl_credit") or 0.0), 0.0)
        gross_drawdown_cash = max(starting_equity - float(account_snapshot.total_funds), 0.0)
        drawdown_cash = max(gross_drawdown_cash - overnight_credit, 0.0)
        drawdown_pct = drawdown_cash / starting_equity if starting_equity > 0 else 0.0
        daily_loss_breached = drawdown_pct >= self.settings.max_daily_loss_pct
        entry_window_open = self._entry_window_is_open(now)

        block_reasons: list[str] = []
        if daily_loss_breached:
            block_reasons.append(
                f"daily drawdown {drawdown_pct:.2%} breached limit "
                f"{self.settings.max_daily_loss_pct:.2%}"
            )
        if not entry_window_open:
            block_reasons.append(
                f"new entries are outside the configured UTC window "
                f"{self.settings.entry_start_hour_utc:02d}:00-"
                f"{self.settings.entry_end_hour_utc:02d}:00"
            )
        if prorealtime_snapshot and prorealtime_snapshot.flatten_all:
            block_reasons.append("ProRealTime sidecar requested flatten_all")

        # Per review 2026-05-02-adhoc-1 #1: detect "all candidate sessions
        # closed" to skip the LLM phase on weekends / outside-hours cycles
        # where every BUY plan would be deterministically rejected by the
        # risk gate. This adds a 4th block reason to the existing trio
        # (drawdown / entry-window / PRT flatten_all). Open positions
        # bypass this check — we still want to consider closes even if
        # exchanges are closed.
        all_sessions_closed = self._all_candidate_sessions_closed(
            account_snapshot=account_snapshot,
            now=now,
        )
        if all_sessions_closed and not account_snapshot.open_positions:
            block_reasons.append(
                "all candidate-symbol sessions are closed (weekend / holiday / "
                "outside trading hours) and no open positions to manage"
            )

        close_requests = self._collect_guardrail_close_requests(
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
            now=now,
            daily_loss_breached=daily_loss_breached,
        )

        # Per review 2026-05-05-adhoc-1 #5: when daily drawdown is
        # breached AND positions are open, route the cycle to a
        # CLOSE-ONLY LLM mode rather than skipping the LLM entirely.
        # Rationale: today (2026-05-05) the bot was pinned in
        # short-circuit from 13:03 onwards with no positions, but if a
        # phantom-open position were present, there'd be no path to
        # clear it without operator intervention. Even when the
        # auto-flatten path already issues CLOSEs (existing behaviour),
        # giving the LLM a chance to time the close (e.g. wait for a
        # bounce, prefer one of multiple positions) is preferable to
        # blanket flatten-all. When no positions are open, we keep
        # short-circuiting (current behaviour, ~80% token saving).
        drawdown_close_only = bool(
            daily_loss_breached and account_snapshot.open_positions
        )

        return {
            "block_new_entries": bool(block_reasons),
            "block_reasons": block_reasons,
            "close_requests": close_requests,
            "daily_loss_breached": daily_loss_breached,
            "drawdown_pct": drawdown_pct,
            "starting_equity": starting_equity,
            "drawdown_cash": drawdown_cash,
            "current_date": current_date,
            "all_sessions_closed": all_sessions_closed,
            "drawdown_close_only": drawdown_close_only,
        }

    def _all_candidate_sessions_closed(
        self,
        *,
        account_snapshot: AccountSnapshot,
        now: datetime,
    ) -> bool:
        """Return True iff EVERY symbol in the candidate universe has its
        exchange session closed right now.

        Per review 2026-05-02-adhoc-1 #1. The candidate universe is:

        1. Configured ``settings.symbols`` if non-empty, OR
        2. The supported instrument list filtered to allowed asset
           classes (the same fallback ``_select_active_symbols`` uses
           when discovery returns nothing).

        Uses ``self.holiday_calendar.is_session_open(exchange_code, now)``
        — a deterministic local lookup (no network, no LLM).

        Open positions bypass this check at the call site so the bot
        can still consider closes even when sessions are closed.
        """
        configured_symbols = [s.upper() for s in self.settings.symbols if s.strip()]
        if configured_symbols:
            candidate_symbols = configured_symbols
        else:
            try:
                candidate_symbols = [
                    inst.symbol.upper()
                    for inst in list_supported_instruments(
                        self.settings.allowed_asset_classes
                    )
                ]
            except Exception:
                # If we can't enumerate the universe, don't block.
                return False
        if not candidate_symbols:
            return False
        for sym in candidate_symbols:
            try:
                inst = get_instrument(sym)
                if self.holiday_calendar.is_session_open(inst.exchange_code, now):
                    return False
            except Exception:
                # Unknown symbol or calendar miss — be permissive (don't block).
                return False
        return True

    def _run_short_circuit_cycle(
        self,
        *,
        cycle_started_at: datetime,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        entry_block: dict,
        operator_context: dict,
    ) -> list[ExecutionResult]:
        """Run a cycle without any LLM calls.

        Used when entry conditions are blocked (daily-loss guardrail,
        outside entry window, PRT flatten_all, or — per review
        2026-05-02-adhoc-1 #1 — all candidate sessions closed).
        Skips the four subagent / decision LLM calls entirely and
        emits a synthetic HOLD decision plus deterministic CLOSE
        plans for any ``close_requests``. Per review
        2026-04-30-token-burn #A.
        """
        LOGGER.info(
            "Short-circuiting cycle (no LLM): %s",
            "; ".join(entry_block["block_reasons"]),
        )
        self.json_log.log(
            "llm_phase_skipped",
            {
                "block_reasons": entry_block["block_reasons"],
                "close_requests_count": len(entry_block["close_requests"]),
                "drawdown_pct": entry_block["drawdown_pct"],
                "open_positions": len(account_snapshot.open_positions),
                "all_sessions_closed": entry_block.get("all_sessions_closed", False),
            },
            cycle_id=self._current_cycle_id,
        )

        # Build a synthetic decision; guardrail overrides will append
        # CLOSE trades for any close_requests.
        synthetic_decision = DecisionEnvelope(
            market_regime="event-risk-block",
            summary=(
                "LLM phase skipped (no new entries possible): "
                + "; ".join(entry_block["block_reasons"])
            ),
            trades=[],
        )
        # Manufacture a guardrail_context shaped like
        # ``_build_guardrail_context``'s output so the existing
        # ``_apply_guardrail_overrides`` and risk-manager paths work
        # unchanged.
        guardrail_context = {
            "daily_loss": {
                "date": entry_block["current_date"],
                "starting_equity": entry_block["starting_equity"],
                "drawdown_cash": entry_block["drawdown_cash"],
                "drawdown_pct": entry_block["drawdown_pct"],
                "breached": entry_block["daily_loss_breached"],
            },
            "entry_window": {
                "start_hour_utc": self.settings.entry_start_hour_utc,
                "end_hour_utc": self.settings.entry_end_hour_utc,
                "open": self._entry_window_is_open(cycle_started_at),
            },
            "prorealtime": {
                "enabled": self.settings.prt_enabled,
                "confirmation_mode": self.settings.prt_confirmation_mode,
                "flatten_all": bool(prorealtime_snapshot and prorealtime_snapshot.flatten_all),
                "source": prorealtime_snapshot.source if prorealtime_snapshot else None,
            },
            "block_new_entries": True,
            "block_reasons": entry_block["block_reasons"],
            "close_requests": entry_block["close_requests"],
            "position_guardrails": [],
            "symbol_count": 0,
            "econ_events": [],
            "earnings_events": {},
            "session_open": {},
            "vix_context": None,
            "correlation_context": None,
            "macro_context": None,
            "consecutive_loss_state": {
                "pause_until": self.runtime_state.get("consecutive_loss_pause_until"),
            },
        }
        decision = self._apply_guardrail_overrides(
            decision=synthetic_decision,
            account_snapshot=account_snapshot,
            guardrail_context=guardrail_context,
        )
        plans = self.risk_manager.build_execution_plans(
            decision=decision,
            symbol_contexts={},
            account_snapshot=account_snapshot,
            last_trade_times=self.last_trade_times,
            guardrail_context=guardrail_context,
        )
        self._log_decision_summary(decision)
        self.json_log.log(
            "decision",
            decision.model_dump(mode="json"),
            cycle_id=self._current_cycle_id,
        )
        self._log_execution_plans(plans)

        results: list[ExecutionResult] = []
        for plan in plans:
            LOGGER.info("Executing %s %s: %s", plan.action, plan.symbol, plan.reason)
            result = self.broker.execute(
                plan=plan, dry_run=self.settings.dry_run, cycle_id=self._current_cycle_id
            )
            results.append(result)
            if result.status in {"submitted", "dry_run"}:
                self.last_trade_times[plan.symbol] = cycle_started_at
            self._log_execution_result(result)

        self.json_log.log(
            "execution",
            [r.model_dump(mode="json") for r in results],
            cycle_id=self._current_cycle_id,
        )

        # Track the recent execution outcomes (mirrors the full-cycle path
        # so the LLM has continuity once it can run again).
        cycle_outcomes = []
        for r in results:
            cycle_outcomes.append(
                {
                    "symbol": r.plan.symbol,
                    "action": r.plan.action,
                    "status": r.status,
                    "error": (r.error or "")[:200],
                    "submitted_at": r.submitted_at,
                }
            )
        if cycle_outcomes:
            history = self.runtime_state.setdefault("recent_execution_outcomes", [])
            history.append(
                {
                    "cycle_id": self._current_cycle_id,
                    "cycle_started_at": cycle_started_at.isoformat(),
                    "outcomes": cycle_outcomes,
                }
            )
            self.runtime_state["recent_execution_outcomes"] = history[-3:]

        self._refresh_runtime_state(account_snapshot, cycle_started_at)
        # Per review 2026-05-02-adhoc-1 #10: also purge news_memory on
        # short-circuit cycles. Without this, weekend/holiday cycles
        # (which all short-circuit when sessions are closed) never
        # purge stale items, and 60h-old "Iran war" headlines linger
        # to anchor the next live-cycle prompt.
        try:
            news_memory_state = self.runtime_state.get("news_memory")
            if isinstance(news_memory_state, dict):
                news_memory = NewsMemory(
                    state=news_memory_state,
                    retention_hours=self.settings.news_memory_hours,
                    max_items=self.settings.news_memory_max_items,
                )
                news_memory.purge(now=cycle_started_at)
        except Exception as exc:
            LOGGER.debug("news_memory purge skipped on short-circuit: %s", exc)
        self.last_cycle_report = {
            "cycle_started_at": cycle_started_at.isoformat(),
            "active_symbols": [],
            "operator_context": operator_context,
            "prorealtime_snapshot": (
                prorealtime_snapshot.model_dump(mode="json") if prorealtime_snapshot else None
            ),
            "guardrail_context": guardrail_context,
            "market_research": None,
            "symbol_research": None,
            "decision": decision.model_dump(mode="json"),
            "plans": [plan.model_dump(mode="json") for plan in plans],
            "results": [result.model_dump(mode="json") for result in results],
            "account_snapshot": account_snapshot.model_dump(mode="json"),
            "llm_phase_skipped": True,
        }
        self._save_runtime_state()
        # Per review 2026-05-05-adhoc-1 #7: news_memory metrics on
        # short-circuit heartbeats too.
        nm_metrics = self._news_memory_metrics()
        self.json_log.log(
            "cycle_heartbeat",
            {
                "cycle_id": self._current_cycle_id,
                "phase": "end",
                "plans": len(plans),
                "results": len(results),
                "llm_phase_skipped": True,
                "news_memory_size": nm_metrics["size"],
                "news_memory_oldest_published_at": nm_metrics["oldest_published_at"],
            },
            cycle_id=self._current_cycle_id,
        )
        LOGGER.info(
            "Cycle complete (LLM skipped): %d plans, %d results",
            len(plans),
            len(results),
        )
        return results

    def _build_guardrail_context(
        self,
        *,
        account_snapshot: AccountSnapshot,
        symbol_contexts: dict[str, SymbolContext],
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        now: datetime,
    ) -> dict:
        daily_state = self.runtime_state.setdefault("daily", {})
        current_date = now.date().isoformat()
        if daily_state.get("date") != current_date:
            daily_state.clear()
            daily_state["date"] = current_date
            daily_state["starting_equity"] = float(account_snapshot.total_funds)

        starting_equity = max(float(daily_state.get("starting_equity") or account_snapshot.total_funds), 0.01)
        # Per review 2026-04-30-adhoc-1 #1: subtract any overnight P&L
        # credit from the gross drawdown. Positions opened on a prior
        # UTC date and reconciled today have their loss attributed to
        # ``overnight_pnl_credit``; the loss "happened yesterday and we
        # just learned about it today" so it should not consume today's
        # daily-loss budget. Without this, a single overnight broker
        # close can freeze the bot for an entire trading session
        # (observed 2026-04-30: WTI loss of £519 took dd from 0.00% to
        # 5.39% within minutes of date roll, blocking 14h of cycles).
        overnight_credit = max(float(daily_state.get("overnight_pnl_credit") or 0.0), 0.0)
        gross_drawdown_cash = max(starting_equity - float(account_snapshot.total_funds), 0.0)
        drawdown_cash = max(gross_drawdown_cash - overnight_credit, 0.0)
        drawdown_pct = drawdown_cash / starting_equity if starting_equity > 0 else 0.0
        daily_loss_breached = drawdown_pct >= self.settings.max_daily_loss_pct
        entry_window_open = self._entry_window_is_open(now)

        block_reasons: list[str] = []
        if daily_loss_breached:
            block_reasons.append(
                f"daily drawdown {drawdown_pct:.2%} breached limit {self.settings.max_daily_loss_pct:.2%}"
            )
        if not entry_window_open:
            block_reasons.append(
                f"new entries are outside the configured UTC window {self.settings.entry_start_hour_utc:02d}:00-"
                f"{self.settings.entry_end_hour_utc:02d}:00"
            )
        if prorealtime_snapshot and prorealtime_snapshot.flatten_all:
            block_reasons.append("ProRealTime sidecar requested flatten_all")

        close_requests = self._collect_guardrail_close_requests(
            account_snapshot=account_snapshot,
            prorealtime_snapshot=prorealtime_snapshot,
            now=now,
            daily_loss_breached=daily_loss_breached,
        )

        position_guardrails = []
        for position in account_snapshot.open_positions:
            state = self.runtime_state.get("positions", {}).get(position.position_id, {})
            position_guardrails.append(
                {
                    "symbol": position.symbol.upper(),
                    "position_id": position.position_id,
                    "age_hours": self._position_age_hours(state, now),
                    "max_holding_hours": state.get("max_holding_hours") or self.settings.default_max_holding_hours,
                    "current_price": position.current_price,
                }
            )

        from trading_bot.instruments import get_instrument as _get_inst
        econ_events = [e.model_dump(mode="json") for e in (self._current_econ_events or [])]
        earnings_events: dict[str, dict] = {}
        for sym in symbol_contexts:
            try:
                event = self.earnings_calendar.next_earnings_date(sym)
                if event is not None:
                    earnings_events[sym] = event.model_dump(mode="json")
            except Exception:
                pass
        session_state: dict[str, bool] = {}
        for sym in symbol_contexts:
            try:
                inst = _get_inst(sym)
                session_state[sym] = self.holiday_calendar.is_session_open(inst.exchange_code, now)
            except Exception:
                session_state[sym] = True

        # Price-feed degradation, surfaced to the LLM prompt and the decision
        # log. Deliberately NOT added to ``block_reasons``: degradation raises
        # the per-entry confidence bar in RiskManager (degraded_inputs_min_
        # confidence), it does not trip the global new-entry kill-switch.
        degraded_inputs = degraded_input_reasons(
            {sym: ctx.data_quality for sym, ctx in symbol_contexts.items()}
        )

        return {
            "daily_loss": {
                "date": current_date,
                "starting_equity": starting_equity,
                "drawdown_cash": drawdown_cash,
                "drawdown_pct": drawdown_pct,
                "breached": daily_loss_breached,
            },
            "entry_window": {
                "start_hour_utc": self.settings.entry_start_hour_utc,
                "end_hour_utc": self.settings.entry_end_hour_utc,
                "open": entry_window_open,
            },
            "prorealtime": {
                "enabled": self.settings.prt_enabled,
                "confirmation_mode": self.settings.prt_confirmation_mode,
                "flatten_all": bool(prorealtime_snapshot and prorealtime_snapshot.flatten_all),
                "source": prorealtime_snapshot.source if prorealtime_snapshot else None,
            },
            "block_new_entries": bool(block_reasons),
            "block_reasons": block_reasons,
            "degraded_inputs": degraded_inputs,
            "close_requests": close_requests,
            "position_guardrails": position_guardrails,
            "symbol_count": len(symbol_contexts),
            "econ_events": econ_events,
            "earnings_events": earnings_events,
            "session_open": session_state,
            "vix_context": (
                self._current_market_vix.model_dump(mode="json") if self._current_market_vix else None
            ),
            "correlation_context": (
                self._current_correlation_context.model_dump(mode="json")
                if getattr(self, "_current_correlation_context", None) else None
            ),
            "macro_context": (
                self._current_macro_context.model_dump(mode="json")
                if getattr(self, "_current_macro_context", None) else None
            ),
            "consecutive_loss_state": {
                "pause_until": self.runtime_state.get("consecutive_loss_pause_until"),
            },
        }

    def _collect_guardrail_close_requests(
        self,
        *,
        account_snapshot: AccountSnapshot,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
        now: datetime,
        daily_loss_breached: bool,
    ) -> list[dict]:
        close_requests: list[dict] = []
        seen_position_ids: set[str] = set()

        def add_close(position: OpenPosition, reason: str) -> None:
            if not position.position_id or position.position_id in seen_position_ids:
                return
            seen_position_ids.add(position.position_id)
            close_requests.append(
                {
                    "symbol": position.symbol.upper(),
                    "position_id": position.position_id,
                    "reason": reason,
                }
            )

        if prorealtime_snapshot and prorealtime_snapshot.flatten_all:
            for position in account_snapshot.open_positions:
                add_close(position, "ProRealTime sidecar requested flatten_all")

        if daily_loss_breached and self.settings.auto_flatten_on_daily_loss:
            for position in account_snapshot.open_positions:
                add_close(position, "Daily loss guardrail breached; flattening open exposure")

        for position in account_snapshot.open_positions:
            state = self.runtime_state.get("positions", {}).get(position.position_id, {})
            age_hours = self._position_age_hours(state, now)
            max_holding_hours = int(
                state.get("max_holding_hours") or self.settings.default_max_holding_hours
            )
            if age_hours is not None and age_hours >= max_holding_hours:
                add_close(
                    position,
                    f"Max holding time exceeded: {age_hours:.1f}h >= {max_holding_hours}h",
                )
                continue

            trailing_reason = self._trailing_exit_reason(position, state)
            if trailing_reason:
                add_close(position, trailing_reason)

        return close_requests

    def _apply_guardrail_overrides(
        self,
        *,
        decision: DecisionEnvelope,
        account_snapshot: AccountSnapshot,
        guardrail_context: dict,
    ) -> DecisionEnvelope:
        close_requests = guardrail_context.get("close_requests") or []
        if not close_requests:
            return decision

        open_positions_by_id = {
            position.position_id: position
            for position in account_snapshot.open_positions
            if position.position_id
        }
        trades = list(decision.trades)
        existing_close_ids = {
            trade.close_position_id
            for trade in trades
            if trade.action == "CLOSE" and trade.close_position_id
        }

        for item in close_requests:
            position_id = str(item.get("position_id") or "").strip()
            position = open_positions_by_id.get(position_id)
            if not position or position_id in existing_close_ids:
                continue
            trades.append(
                TradeDecision(
                    symbol=position.symbol.upper(),
                    action="CLOSE",
                    confidence=1.0,
                    thesis=str(item.get("reason") or "Guardrail-triggered close"),
                    news_bias="neutral",
                    invalidate_if="Guardrail condition cleared before execution",
                    close_position_id=position_id,
                )
            )

        if len(trades) == len(decision.trades):
            return decision
        return DecisionEnvelope(
            market_regime=decision.market_regime,
            summary=decision.summary,
            trades=trades,
        )

    @staticmethod
    def _position_age_hours(state: dict, now: datetime) -> float | None:
        first_seen_at = str(state.get("first_seen_at") or "").strip()
        if not first_seen_at:
            return None
        normalized = first_seen_at[:-1] + "+00:00" if first_seen_at.endswith("Z") else first_seen_at
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((now - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.0)

    def _trailing_exit_reason(self, position: OpenPosition, state: dict) -> str | None:
        activation = self.settings.trailing_exit_activation_pct
        trailing = self.settings.trailing_exit_pct
        current_price = float(position.current_price or 0.0)
        entry_price = float(state.get("entry_price") or 0.0)
        if activation is None or trailing is None or current_price <= 0 or entry_price <= 0:
            return None

        direction = self._normalize_position_direction(position)
        highest_price = float(state.get("highest_price") or entry_price)
        lowest_price = float(state.get("lowest_price") or entry_price)

        if direction == "buy":
            if highest_price < entry_price * (1.0 + activation):
                return None
            if current_price <= highest_price * (1.0 - trailing):
                return (
                    f"Trailing exit for long position: current price {current_price:.4f} retraced "
                    f"from peak {highest_price:.4f}"
                )
            return None

        if direction == "sell":
            if lowest_price > entry_price * (1.0 - activation):
                return None
            if current_price >= lowest_price * (1.0 + trailing):
                return (
                    f"Trailing exit for short position: current price {current_price:.4f} rebounded "
                    f"from low {lowest_price:.4f}"
                )
        return None

    def _entry_window_is_open(self, now: datetime) -> bool:
        return self.settings.entry_start_hour_utc <= now.hour < self.settings.entry_end_hour_utc

    @staticmethod
    def _normalize_position_direction(position: OpenPosition) -> str:
        if position.direction in {"buy", "sell"}:
            return position.direction
        if position.quantity < 0:
            return "sell"
        return "buy"

    def _log_market_research(self, market_research: MarketResearchEnvelope | None) -> None:
        if market_research is None:
            return
        LOGGER.info("Market regime: %s", market_research.market_regime)
        LOGGER.info("Research summary: %s", market_research.summary)

    def _log_prorealtime_snapshot(
        self,
        prorealtime_snapshot: ProRealTimeSnapshotEnvelope | None,
    ) -> None:
        if prorealtime_snapshot is None:
            return
        LOGGER.info(
            "ProRealTime sidecar: source=%s rows=%d flatten_all=%s",
            prorealtime_snapshot.source or "unknown",
            len(prorealtime_snapshot.symbols),
            prorealtime_snapshot.flatten_all,
        )

    def _log_guardrail_context(self, guardrail_context: dict) -> None:
        daily_loss = guardrail_context.get("daily_loss") or {}
        entry_window = guardrail_context.get("entry_window") or {}
        LOGGER.info(
            "Guardrails: daily_drawdown=%.2f%% breached=%s entry_window_open=%s",
            float(daily_loss.get("drawdown_pct", 0.0)) * 100.0,
            daily_loss.get("breached"),
            entry_window.get("open"),
        )
        for reason in guardrail_context.get("block_reasons") or []:
            LOGGER.info("Entry block: %s", reason)
        for request in guardrail_context.get("close_requests") or []:
            LOGGER.info(
                "Guardrail close: %s because %s",
                request.get("symbol"),
                request.get("reason"),
            )

    def _log_decision_summary(self, decision: DecisionEnvelope) -> None:
        LOGGER.info("Decision summary: %s", decision.summary)
        for trade in decision.trades:
            LOGGER.info(
                "Model trade: %s %s (confidence %.2f) because %s",
                trade.action,
                trade.symbol,
                trade.confidence,
                trade.thesis,
            )

    def _log_decision_records(
        self,
        *,
        decision: DecisionEnvelope,
        symbol_contexts: dict[str, SymbolContext],
        plans: list[ExecutionPlan],
        account_snapshot: AccountSnapshot,
        guardrail_context: dict[str, Any] | None,
        cycle_started_at: datetime,
    ) -> None:
        """Emit one flat decision record per trade decision (forward-walk eval
        precondition). Best-effort: never raises into the cycle."""
        try:
            plans_by_symbol: dict[str, ExecutionPlan] = {}
            for p in plans:
                # First plan per symbol is enough for the join (one decision
                # per symbol per cycle in practice).
                plans_by_symbol.setdefault(p.symbol, p)
            corr = (guardrail_context or {}).get("correlation_context")
            acct = account_snapshot.model_dump(mode="json") if account_snapshot else {}
            model = getattr(self.settings, "openai_model", None)
            for trade in decision.trades:
                ctx = symbol_contexts.get(trade.symbol)
                sig = (
                    ctx.signals.model_dump(mode="json")
                    if ctx and ctx.signals else None
                )
                ind = (
                    ctx.indicators.model_dump(mode="json")
                    if ctx and ctx.indicators else None
                )
                plan = plans_by_symbol.get(trade.symbol)
                pl = plan.model_dump(mode="json") if plan else None
                record = build_decision_record(
                    cycle_id=self._current_cycle_id,
                    cycle_ts=cycle_started_at.isoformat(),
                    symbol=trade.symbol,
                    decision=trade.model_dump(mode="json"),
                    signals=sig,
                    indicators=ind,
                    correlation_context=corr,
                    plan=pl,
                    account=acct,
                    regime=decision.market_regime,
                    prompt_hash=getattr(self, "_current_prompt_sha", None),
                    model=model,
                    dry_run=self.settings.dry_run,
                )
                self.decision_logger.log_record(record)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("decision record logging failed: %s", exc)

    def _log_execution_plans(self, plans: list[ExecutionPlan]) -> None:
        if not plans:
            LOGGER.info("No execution plans were approved for this cycle")
            return
        for plan in plans:
            LOGGER.info(
                "Approved plan: %s %s qty=%s stop=%s take_profit=%s reason=%s",
                plan.action,
                plan.symbol,
                plan.quantity,
                plan.stop_distance,
                plan.take_profit_distance,
                plan.reason,
            )

    @staticmethod
    def _log_execution_result(result: ExecutionResult) -> None:
        plan = result.plan
        if result.status == "failed":
            LOGGER.warning(
                "Execution failed: %s %s because %s",
                plan.action,
                plan.symbol,
                result.error or "unknown error",
            )
            return
        LOGGER.info(
            "Execution %s: %s %s",
            result.status,
            plan.action,
            plan.symbol,
        )

    @staticmethod
    def _normalize_symbol_list(symbols: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            item = symbol.strip().upper()
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized

    def _is_paused(self) -> bool:
        with self.operator_lock:
            return self.paused

    def _close_result_allows_follow_on_entry(self, result: ExecutionResult) -> bool:
        if result.plan.action != "CLOSE":
            return True
        if result.status == "dry_run":
            return True
        if result.status != "submitted":
            return False
        if self.settings.broker != "ig":
            return True

        broker_response = result.broker_response or {}
        if not isinstance(broker_response, dict):
            return False
        confirmation = broker_response.get("confirm")
        if not isinstance(confirmation, dict):
            return False
        return str(confirmation.get("dealStatus", "")).upper() == "ACCEPTED"

    def _is_tradeable_symbol(self, symbol: str) -> bool:
        checker = getattr(self.broker, "is_tradeable_symbol", None)
        if callable(checker):
            return bool(checker(symbol))
        return True

    @staticmethod
    def _dedupe_symbols(symbols: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            normalized = symbol.strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _write_cycle_log(
        self,
        *,
        cycle_started_at: datetime,
        account_snapshot: dict,
        market_headlines: list[dict],
        symbol_contexts: dict[str, dict],
        active_symbols: list[str],
        operator_context: dict,
        prorealtime_snapshot: dict | None,
        guardrail_context: dict,
        market_research: dict | None,
        symbol_research: dict | None,
        decision: dict,
        plans: list[dict],
        results: list[dict],
        news_synthesis: dict | None = None,
        cycle_id: str | None = None,
    ) -> None:
        log_dir = self.settings.decision_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = cycle_started_at.strftime("%Y%m%dT%H%M%SZ.json")
        payload = {
            "cycle_id": cycle_id,
            "cycle_started_at": cycle_started_at.isoformat(),
            "settings": {
                "broker": self.settings.broker,
                "symbols": self.settings.symbols,
                "auto_discover_symbols": self.settings.auto_discover_symbols,
                "allowed_asset_classes": self.settings.allowed_asset_classes,
                "enable_research_subagents": self.settings.enable_research_subagents,
                "mode": self.settings.execution_mode,
                "dry_run": self.settings.dry_run,
                "allow_shorts": self.settings.allow_shorts,
                "allow_position_reversals": self.settings.allow_position_reversals,
                "prt_enabled": self.settings.prt_enabled,
                "prt_confirmation_mode": self.settings.prt_confirmation_mode,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "default_max_holding_hours": self.settings.default_max_holding_hours,
            },
            "operator_context": operator_context,
            "prorealtime_snapshot": prorealtime_snapshot,
            "guardrail_context": guardrail_context,
            "runtime_state": self.runtime_state,
            "active_symbols": active_symbols,
            "account_snapshot": account_snapshot,
            "market_headlines": market_headlines,
            "market_research": market_research,
            "symbol_contexts": symbol_contexts,
            # Per review 2026-05-02-news-synth-json: include news_synthesis
            # in the cycle log so we can verify the subagent output offline
            # and inspect the produced JSON (themes, per-symbol summaries,
            # regime implications, position assessments).
            "news_synthesis": news_synthesis,
            "symbol_research": symbol_research,
            "decision": decision,
            "execution_plans": plans,
            "execution_results": results,
        }
        with (log_dir / filename).open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
