from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from trading_bot.config import Settings
from trading_bot.instruments import get_instrument, list_supported_instruments
from trading_bot.json_log import JsonEventLogger
from trading_bot.models import AccountSnapshot, ExecutionPlan, ExecutionResult, OpenPosition
from trading_bot.session_manager import IGSession

LOGGER = logging.getLogger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class IGRestBridge:
    def __init__(self, settings: Settings, *, event_logger: JsonEventLogger | None = None):
        self.settings = settings
        self.session_mgr = IGSession(settings)
        self.current_account_id: str | None = None
        self.account_currency: str | None = None
        self._login_payload: dict[str, Any] = {}
        self._position_cache: dict[str, dict[str, Any]] = {}
        self._symbol_to_epic = dict(settings.ig_epics)
        self._epic_to_symbol = {epic: symbol for symbol, epic in self._symbol_to_epic.items()}
        # Optional event logger to emit risk_gate_degraded events (proposal #11).
        # Lazily built from settings.json_log_path if not injected.
        self._event_logger = event_logger

    def connect(self) -> None:
        if self.current_account_id:
            return
        if not self.settings.has_ig_credentials:
            raise RuntimeError("IG credentials and API key are required")
        self.session_mgr.ensure_fresh()
        payload = self.session_mgr.login_payload
        self._login_payload = payload
        self.current_account_id = (
            str(payload.get("currentAccountId") or payload.get("accountId") or "").strip() or None
        )
        self.account_currency = self._extract_account_currency(payload)
        if self.settings.ig_account_id and self.settings.ig_account_id != self.current_account_id:
            self._switch_account(self.settings.ig_account_id)
            self.current_account_id = self.settings.ig_account_id
        self._precache_epics()
        LOGGER.info("IG REST bridge connected in %s mode", self.settings.ig_mode)

    def _precache_epics(self) -> None:
        for instrument in list_supported_instruments(self.settings.allowed_asset_classes):
            try:
                epic = self._resolve_epic(instrument.symbol)
                LOGGER.debug("IG EPIC cache: %s -> %s", instrument.symbol, epic)
            except RuntimeError as exc:
                if "exceeded-api-key" in str(exc):
                    LOGGER.warning("IG EPIC pre-cache halted: API allowance exhausted (%s)", exc)
                    break
                LOGGER.warning("IG EPIC cache miss for %s: %s", instrument.symbol, exc)
            except Exception as exc:
                LOGGER.warning("IG EPIC cache miss for %s: %s", instrument.symbol, exc)

    def get_account_snapshot(self) -> AccountSnapshot:
        if not self.current_account_id:
            raise RuntimeError("IG bridge not connected")

        account_payload = self._request("GET", "/accounts", version="1")
        selected_account = self._extract_current_account(account_payload)

        balance = selected_account.get("balance", {})
        total_funds = _as_float(
            balance.get("balance"),
            _as_float(self._login_payload.get("accountInfo", {}).get("balance"), self.settings.simulated_account_balance),
        )
        free_funds = _as_float(
            balance.get("available"),
            _as_float(self._login_payload.get("accountInfo", {}).get("available"), total_funds),
        )
        currency = (
            str(selected_account.get("currency") or selected_account.get("currencyIsoCode") or self.account_currency or "GBP")
            .strip()
            .upper()
        )

        positions_payload = self._request("GET", "/positions", version="2")
        position_items = positions_payload.get("positions", [])
        open_positions: list[OpenPosition] = []
        self._position_cache = {}

        for item in position_items:
            market = item.get("market", {})
            position = item.get("position", {})
            epic = str(market.get("epic", "")).strip()
            symbol = self._epic_to_symbol.get(epic) or self._infer_symbol_from_market(market)
            if not symbol:
                symbol = epic or str(position.get("dealId", ""))
            direction = str(position.get("direction", "")).upper()
            size = _as_float(position.get("size") or position.get("dealSize"))
            signed_size = size
            normalized_direction = "unknown"
            if direction == "BUY":
                normalized_direction = "buy"
            elif direction == "SELL":
                normalized_direction = "sell"
                signed_size = -size

            deal_id = str(position.get("dealId", "")).strip()
            current_price = _as_float(
                market.get("bid") if direction == "SELL" else market.get("offer"),
                default=0.0,
            )

            open_positions.append(
                OpenPosition(
                    symbol=symbol.upper(),
                    position_id=deal_id,
                    quantity=signed_size,
                    current_price=current_price or None,
                    direction=normalized_direction,
                )
            )
            self._position_cache[deal_id] = {
                "symbol": symbol.upper(),
                "epic": epic,
                "expiry": str(market.get("expiry") or position.get("expiry") or "-"),
                "direction": direction,
                "size": size,
                "currency": str(position.get("currency") or currency).upper(),
            }

        return AccountSnapshot(
            mode=self.settings.ig_mode,
            currency=currency,
            total_funds=total_funds,
            free_funds=free_funds,
            open_positions=open_positions,
        )

    def execute(self, plan: ExecutionPlan, dry_run: bool, *, cycle_id: str | None = None) -> ExecutionResult:
        if not self.current_account_id:
            raise RuntimeError("IG bridge not connected")

        submitted_at = datetime.now(timezone.utc).isoformat()
        if dry_run:
            return ExecutionResult(
                plan=plan,
                status="dry_run",
                submitted_at=submitted_at,
                broker_response={"simulated": True, "broker": "ig", "mode": self.settings.ig_mode},
            )

        try:
            if plan.action == "CLOSE":
                payload = self._build_close_payload(plan)
                # Per review 2026-05-02-adhoc-1 #4: log the CLOSE request
                # body so we can identify which field is null-rejected
                # by IG's `validation.null-not-allowed.request` error.
                # The error has fired three times (2026-04-30 WTI;
                # 2026-05-01 BRENT x2) and we have no diagnostic data.
                # Sanitise: log only structural fields, never the full
                # raw request that might contain auth headers.
                LOGGER.debug(
                    "ig_bridge close payload: keys=%s dealId=%r direction=%r "
                    "size=%r expiry=%r timeInForce=%r orderType=%r",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?",
                    (payload or {}).get("dealId"),
                    (payload or {}).get("direction"),
                    (payload or {}).get("size"),
                    (payload or {}).get("expiry"),
                    (payload or {}).get("timeInForce"),
                    (payload or {}).get("orderType"),
                )
                ack = self._request("DELETE", "/positions/otc", version="1", json_body=payload)
            else:
                spread_block = self._check_spread(plan, cycle_id=cycle_id)
                if spread_block:
                    return spread_block
                payload = self._build_open_payload(plan, cycle_id=cycle_id)
                ack = self._request("POST", "/positions/otc", version="2", json_body=payload)

            deal_reference = str(ack.get("dealReference", "")).strip()
            confirmation = self._poll_confirmation(deal_reference) if deal_reference else None

            if confirmation and str(confirmation.get("dealStatus", "")).upper() == "REJECTED":
                reason = str(
                    confirmation.get("reason")
                    or confirmation.get("errorCode")
                    or ""
                ).upper()
                # Per review 2026-05-02-adhoc-1 #4: when the rejection
                # is the recurring `validation.null-not-allowed.request`
                # class on a CLOSE, surface the request body keys at
                # WARNING so we can diagnose which field IG's API is
                # complaining about. This is read-only diagnostic
                # logging — no behavior change.
                if (
                    plan.action == "CLOSE"
                    and "VALIDATION" in reason
                    and "NULL" in reason
                ):
                    LOGGER.warning(
                        "IG close rejected as validation.null: "
                        "payload_keys=%s payload_field_types=%s null_fields=%s "
                        "ack_keys=%s confirm_reason=%r",
                        sorted(payload.keys()) if isinstance(payload, dict) else "?",
                        {k: type(v).__name__ for k, v in (payload or {}).items()},
                        [k for k, v in (payload or {}).items() if v is None or v == ""],
                        sorted(ack.keys()) if isinstance(ack, dict) else "?",
                        confirmation.get("reason") or confirmation.get("errorCode"),
                    )
                # Belt-and-suspenders: if SIZE_INCREMENT slipped past _normalize_size
                # (e.g. stale dealing-rules cache), retry ONCE with a freshly normalised size.
                if "SIZE_INCREMENT" in reason and plan.action != "CLOSE":
                    retry_result = self._retry_after_size_increment(
                        plan,
                        original_payload=payload,
                        original_ack=ack,
                        original_confirm=confirmation,
                        cycle_id=cycle_id,
                        submitted_at=submitted_at,
                    )
                    if retry_result is not None:
                        return retry_result
                return ExecutionResult(
                    plan=plan,
                    status="failed",
                    submitted_at=submitted_at,
                    broker_response={"ack": ack, "confirm": confirmation},
                    error=str(
                        confirmation.get("reason")
                        or confirmation.get("errorCode")
                        or "IG rejected the trade"
                    ),
                )

            return ExecutionResult(
                plan=plan,
                status="submitted",
                submitted_at=submitted_at,
                broker_response={"ack": ack, "confirm": confirmation},
            )
        except Exception as exc:
            return ExecutionResult(
                plan=plan,
                status="failed",
                submitted_at=submitted_at,
                error=str(exc),
            )

    def _retry_after_size_increment(
        self,
        plan: ExecutionPlan,
        *,
        original_payload: dict[str, Any],
        original_ack: dict[str, Any],
        original_confirm: dict[str, Any],
        cycle_id: str | None,
        submitted_at: str,
    ) -> ExecutionResult | None:
        """Retry the order ONCE with a freshly normalised size after a SIZE_INCREMENT
        rejection. Returns None to signal the caller to fall through to its
        original failure path."""
        try:
            symbol = plan.symbol.upper()
            epic = self._resolve_epic(symbol)
            market = self._request("GET", f"/markets/{epic}", version="3")
            dealing_rules = market.get("dealingRules", {})
            raw_size = abs(plan.quantity or 0.0)
            new_size = self._normalize_size(raw_size, dealing_rules)
            if new_size <= 0 or new_size == original_payload.get("size"):
                LOGGER.warning(
                    "SIZE_INCREMENT retry skipped for %s: re-normalised size %s == original %s",
                    symbol, new_size, original_payload.get("size"),
                )
                return None
            tolerance = float(getattr(self.settings, "max_size_overrun_tolerance", 3.0))
            if raw_size > 0 and new_size > raw_size * tolerance:
                LOGGER.warning(
                    "SIZE_INCREMENT retry refused for %s: %s exceeds %sx tolerance",
                    symbol, new_size, tolerance,
                )
                return None
            retry_payload = dict(original_payload)
            retry_payload["size"] = new_size
            retry_payload["dealReference"] = self._make_deal_reference(
                symbol, f"{cycle_id or 'nocycle'}_retry", plan.action
            )
            LOGGER.info(
                "Retrying %s order after SIZE_INCREMENT: size %s -> %s",
                symbol, original_payload.get("size"), new_size,
            )
            ack = self._request("POST", "/positions/otc", version="2", json_body=retry_payload)
            deal_reference = str(ack.get("dealReference", "")).strip()
            confirmation = self._poll_confirmation(deal_reference) if deal_reference else None
            if confirmation and str(confirmation.get("dealStatus", "")).upper() == "REJECTED":
                return ExecutionResult(
                    plan=plan,
                    status="failed",
                    submitted_at=submitted_at,
                    broker_response={
                        "ack": ack,
                        "confirm": confirmation,
                        "first_attempt": {"ack": original_ack, "confirm": original_confirm},
                        "retry": True,
                    },
                    error=str(
                        confirmation.get("reason")
                        or confirmation.get("errorCode")
                        or "IG rejected the retry"
                    ),
                )
            return ExecutionResult(
                plan=plan,
                status="submitted",
                submitted_at=submitted_at,
                broker_response={
                    "ack": ack,
                    "confirm": confirmation,
                    "first_attempt": {"ack": original_ack, "confirm": original_confirm},
                    "retry": True,
                },
            )
        except Exception as exc:
            LOGGER.warning("SIZE_INCREMENT retry for %s raised: %s", plan.symbol, exc)
            return None

    def _switch_account(self, account_id: str) -> None:
        self._request(
            "PUT",
            "/session",
            version="1",
            json_body={"accountId": account_id, "defaultAccount": False},
        )
        self.current_account_id = account_id

    def _check_spread(self, plan: ExecutionPlan, *, cycle_id: str | None = None) -> ExecutionResult | None:
        submitted_at = datetime.now(timezone.utc).isoformat()
        try:
            symbol = plan.symbol.upper()
            epic = self._resolve_epic(symbol)
            market = self._request("GET", f"/markets/{epic}", version="3")
            snapshot = market.get("snapshot", {})
            bid = _as_float(snapshot.get("bid"))
            offer = _as_float(snapshot.get("offer"))
            mid = (bid + offer) / 2.0 if bid > 0 and offer > 0 else 0.0
            spread_abs = max(offer - bid, 0.0) if bid > 0 and offer > 0 else 0.0
            if mid > 0:
                spread_bps = ((offer - bid) / mid) * 10_000.0
                try:
                    inst = get_instrument(symbol)
                    max_spread = float(inst.max_spread_bps)
                except Exception:
                    max_spread = 30.0
                if spread_bps > max_spread:
                    return ExecutionResult(
                        plan=plan,
                        status="failed",
                        submitted_at=submitted_at,
                        error=f"IG spread {spread_bps:.1f} bps exceeds {max_spread} bps cap for {symbol}",
                    )

            # Pre-submit gate: stop_distance vs current spread + IG minimum.
            # Independent of the IG-side STOP_TOO_CLOSE rejection — that is a
            # broker-after-the-fact response; this gate prevents submission of
            # plans where the stop would be eaten by entry slippage.
            stop_block = self._check_stop_vs_spread(
                plan,
                spread_abs=spread_abs,
                market=market,
                submitted_at=submitted_at,
                cycle_id=cycle_id,
            )
            if stop_block is not None:
                return stop_block
        except Exception as exc:
            LOGGER.warning("Spread check failed for %s: %s — proceeding", plan.symbol, exc)
            # Persist a structured risk_gate_degraded event so the watcher can
            # detect risk-gate bypasses during connectivity outages (proposal #11).
            self._emit_risk_gate_degraded(
                gate="spread_check",
                reason=str(exc)[:300],
                symbol=plan.symbol,
                cycle_id=cycle_id,
            )
        return None

    def _check_stop_vs_spread(
        self,
        plan: ExecutionPlan,
        *,
        spread_abs: float,
        market: dict[str, Any],
        submitted_at: str,
        cycle_id: str | None,
    ) -> ExecutionResult | None:
        """Reject plans whose stop_distance is too small relative to the
        current spread or below the IG instrument minimum.

        Incident 2026-04-29: 3× WTI BUYs at stop_distance=12 on a market
        with ~4–8 unit spread were stopped out within 70s. Net loss ~£35.
        """
        if plan.action not in {"BUY", "SELL"}:
            return None
        stop_distance = float(plan.stop_distance or 0.0)
        if stop_distance <= 0:
            return None  # no stop on the plan; nothing to gate

        symbol = plan.symbol.upper()
        multiplier = float(getattr(self.settings, "min_stop_multiplier_of_spread", 0.0) or 0.0)

        # IG's instrument-defined minimum stop distance (in price units).
        ig_min_stop = 0.0
        try:
            dealing_rules = market.get("dealingRules", {}) or {}
            distance_rule = dealing_rules.get("minNormalStopOrLimitDistance", {}) or {}
            unit = str(distance_rule.get("unit", "POINTS")).upper()
            value = _as_float(distance_rule.get("value"))
            if unit == "POINTS":
                ig_min_stop = value
            elif unit == "PERCENTAGE":
                snapshot = market.get("snapshot", {}) or {}
                ref = _as_float(snapshot.get("offer")) or _as_float(snapshot.get("bid"))
                if ref > 0:
                    ig_min_stop = ref * value / 100.0
        except Exception:
            ig_min_stop = 0.0

        spread_floor = spread_abs * multiplier if multiplier > 0 else 0.0
        required = max(spread_floor, ig_min_stop)
        if required <= 0:
            return None

        if stop_distance < required:
            reason_parts = []
            if multiplier > 0 and spread_abs > 0:
                reason_parts.append(
                    f"spread={spread_abs:.4f} × {multiplier:.2f}={spread_floor:.4f}"
                )
            if ig_min_stop > 0:
                reason_parts.append(f"ig_min={ig_min_stop:.4f}")
            error = (
                f"STOP_TOO_TIGHT_VS_SPREAD: stop_distance={stop_distance:.4f} "
                f"< required={required:.4f} ({'; '.join(reason_parts) or 'gate'}) for {symbol}"
            )
            LOGGER.warning("%s — refusing to submit", error)
            self._emit_risk_block(
                reason="STOP_TOO_TIGHT_VS_SPREAD",
                symbol=symbol,
                detail={
                    "stop_distance": stop_distance,
                    "spread_abs": spread_abs,
                    "spread_floor": spread_floor,
                    "ig_min_stop": ig_min_stop,
                    "required": required,
                    "multiplier": multiplier,
                    "action": plan.action,
                },
                cycle_id=cycle_id,
            )
            return ExecutionResult(
                plan=plan,
                status="failed",
                submitted_at=submitted_at,
                error=error,
            )
        return None

    def _emit_risk_block(
        self, *, reason: str, symbol: str, detail: dict[str, Any], cycle_id: str | None
    ) -> None:
        """Best-effort emission of a risk_block event when a pre-submit gate
        rejects a plan."""
        try:
            logger = self._event_logger
            if logger is None:
                logger = JsonEventLogger(path=self.settings.json_log_path)
                self._event_logger = logger
            payload = {"reason": reason, "symbol": str(symbol or "").upper()}
            payload.update(detail)
            logger.log("risk_block", payload, cycle_id=cycle_id)
        except Exception as exc:
            LOGGER.debug("Failed to emit risk_block event: %s", exc)

    def _emit_risk_gate_degraded(
        self, *, gate: str, reason: str, symbol: str, cycle_id: str | None
    ) -> None:
        """Best-effort emission of a risk_gate_degraded event. Builds a default
        JsonEventLogger from settings if one wasn't injected."""
        try:
            logger = self._event_logger
            if logger is None:
                logger = JsonEventLogger(path=self.settings.json_log_path)
                self._event_logger = logger
            logger.log(
                "risk_gate_degraded",
                {
                    "gate": gate,
                    "reason": reason,
                    "symbol": str(symbol or "").upper(),
                },
                cycle_id=cycle_id,
            )
        except Exception as exc:
            LOGGER.debug("Failed to emit risk_gate_degraded event: %s", exc)

    def _build_open_payload(self, plan: ExecutionPlan, *, cycle_id: str | None = None) -> dict[str, Any]:
        symbol = plan.symbol.upper()
        epic = self._resolve_epic(symbol)

        market = self._request("GET", f"/markets/{epic}", version="3")
        snapshot = market.get("snapshot", {})
        if str(snapshot.get("marketStatus", "")).upper() != "TRADEABLE":
            raise RuntimeError(
                f"IG market {epic} is not tradeable right now: {snapshot.get('marketStatus')}"
            )

        instrument = market.get("instrument", {})
        dealing_rules = market.get("dealingRules", {})
        reference_price = _as_float(
            snapshot.get("offer") if plan.action == "BUY" else snapshot.get("bid"),
            default=plan.target_price or 0.0,
        )
        if reference_price <= 0:
            raise RuntimeError(f"Unable to determine a valid execution price for {symbol} on IG")

        raw_size = abs(plan.quantity or 0.0)
        # Pre-submission dealing-rules verification: on the first attempted entry
        # for this symbol in the current process, log the raw-vs-normalised size
        # explicitly so the audit trail proves the size check ran before submit.
        is_first_entry = symbol not in getattr(self, "_validated_symbols", set())
        size = self._normalize_size(raw_size, dealing_rules)
        if is_first_entry:
            min_deal_size = _as_float(dealing_rules.get("minDealSize", {}).get("value"))
            LOGGER.info(
                "PRE_SUBMIT_VERIFY symbol=%s raw_size=%.6f min_deal_size=%s normalized=%s",
                symbol, raw_size, min_deal_size, size,
            )
        # Reject if IG's minDealSize forces a size overrun beyond tolerance
        # (e.g. GOLD: bot requested 0.0637, IG minimum 0.5 → 7.8x overrun).
        tolerance = float(getattr(self.settings, "max_size_overrun_tolerance", 3.0))
        if raw_size > 0 and size > raw_size * tolerance:
            overrun_ratio = size / raw_size
            raise RuntimeError(
                f"SIZE_OVERRUN: IG minimum size {size} for {symbol} exceeds "
                f"requested {raw_size:.4f} by {overrun_ratio:.2f}x "
                f"(tolerance {tolerance:.2f}x); refusing trade to protect risk budget"
            )
        # Mark this symbol as validated for the rest of the session.
        if not hasattr(self, "_validated_symbols"):
            self._validated_symbols = set()
        self._validated_symbols.add(symbol)
        currency_code = self._pick_currency(instrument)
        distance_rule = dealing_rules.get("minNormalStopOrLimitDistance", {})

        payload: dict[str, Any] = {
            "currencyCode": currency_code,
            "dealReference": self._make_deal_reference(symbol, cycle_id, plan.action),
            "direction": "BUY" if plan.action == "BUY" else "SELL",
            "epic": epic,
            "expiry": str(instrument.get("expiry") or "-"),
            "forceOpen": True,
            "guaranteedStop": False,
            "orderType": "MARKET",
            "size": size,
            "timeInForce": "FILL_OR_KILL",
        }
        if plan.stop_distance and plan.stop_distance > 0:
            payload["stopDistance"] = self._normalize_distance(
                raw_distance=plan.stop_distance,
                reference_price=reference_price,
                dealing_rule=distance_rule,
            )
        if plan.take_profit_distance and plan.take_profit_distance > 0:
            payload["limitDistance"] = self._normalize_distance(
                raw_distance=plan.take_profit_distance,
                reference_price=reference_price,
                dealing_rule=distance_rule,
            )
        return payload

    def get_transaction_history(
        self, *, from_iso: str | None = None, to_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent IG transactions/history. Used during startup
        reconciliation to obtain broker-confirmed close levels and pnl for
        positions that vanished during connectivity outages (proposal #12).
        Falls back to an empty list on any API failure."""
        if not self.current_account_id:
            return []
        try:
            params: dict[str, Any] = {"type": "ALL"}
            if from_iso:
                params["from"] = from_iso
            if to_iso:
                params["to"] = to_iso
            payload = self._request("GET", "/history/transactions", version="2", params=params)
            if isinstance(payload, dict):
                items = payload.get("transactions") or []
                return list(items) if isinstance(items, list) else []
            return []
        except Exception as exc:
            LOGGER.warning("IG transaction history fetch failed: %s — using estimated pnl only", exc)
            return []

    def is_tradeable_symbol(self, symbol: str) -> bool:
        try:
            return bool(self._resolve_epic(symbol))
        except Exception:
            return False

    def _build_close_payload(self, plan: ExecutionPlan) -> dict[str, Any]:
        if not plan.position_id:
            raise RuntimeError("A position_id is required to close an IG position")

        position = self._position_cache.get(plan.position_id)
        if not position:
            raise RuntimeError(
                f"Position {plan.position_id} was not found in the current IG position cache"
            )

        current_direction = str(position.get("direction", "")).upper()
        if current_direction not in {"BUY", "SELL"}:
            raise RuntimeError(f"Position {plan.position_id} has no valid IG direction")

        return {
            "dealId": plan.position_id,
            "direction": "SELL" if current_direction == "BUY" else "BUY",
            "orderType": "MARKET",
            "size": abs(_as_float(position.get("size"))),
            "expiry": str(position.get("expiry") or "-"),
            "timeInForce": "FILL_OR_KILL",
        }

    def _pick_currency(self, instrument: dict[str, Any]) -> str:
        currencies = instrument.get("currencies") or []
        preferred = (self.account_currency or "").upper()
        for item in currencies:
            code = str(item.get("code", "")).upper()
            if code and code == preferred:
                return code
        for item in currencies:
            if item.get("isDefault") and item.get("code"):
                return str(item["code"]).upper()
        for item in currencies:
            code = str(item.get("code", "")).upper()
            if code:
                return code
        return preferred or "GBP"

    def _normalize_size(self, raw_size: float, dealing_rules: dict[str, Any]) -> float:
        min_deal_size = _as_float(dealing_rules.get("minDealSize", {}).get("value"))
        if min_deal_size <= 0:
            return round(raw_size, 4)
        # IG rejects sizes that aren't a whole multiple of minDealSize (SIZE_INCREMENT).
        # Round DOWN so we never breach the cash-risk budget upward.
        units = max(1, int(raw_size / min_deal_size))
        normalized = units * min_deal_size
        # Trim float noise (e.g. 0.1 + 0.1 + 0.1 = 0.30000000000000004)
        normalized = round(normalized, 6)
        # Surface size overruns to the supervisor: when minDealSize forces the order
        # to >2x the requested raw size, the per-trade risk budget is being implicitly
        # multiplied. The watcher can pattern-match on OVERRUN_RATIO to alert.
        if raw_size > 0 and normalized > raw_size * 2.0:
            overrun_ratio = normalized / raw_size
            LOGGER.warning(
                "SIZE_OVERRUN OVERRUN_RATIO=%.2fx min_deal_size=%s raw=%.6f normalized=%s",
                overrun_ratio, min_deal_size, raw_size, normalized,
            )
        return normalized

    def _normalize_distance(
        self,
        *,
        raw_distance: float,
        reference_price: float,
        dealing_rule: dict[str, Any],
    ) -> float:
        if raw_distance <= 0:
            raise ValueError("raw_distance must be positive when normalizing IG distances")

        unit = str(dealing_rule.get("unit", "POINTS")).upper()
        minimum = _as_float(dealing_rule.get("value"))

        normalized = raw_distance
        if unit == "PERCENTAGE" and reference_price > 0:
            normalized = (raw_distance / reference_price) * 100.0

        if minimum > 0:
            normalized = max(normalized, minimum)

        return round(normalized, 4)

    def _extract_account_currency(self, payload: dict[str, Any]) -> str | None:
        accounts = payload.get("accounts", [])
        current_account_id = str(
            payload.get("currentAccountId") or payload.get("accountId") or ""
        ).strip()
        for account in accounts:
            if str(account.get("accountId", "")).strip() == current_account_id:
                currency = str(account.get("currencyIsoCode", "")).strip().upper()
                if currency:
                    return currency
        return None

    def _extract_current_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        accounts = payload.get("accounts") if isinstance(payload, dict) else payload
        if not isinstance(accounts, list):
            raise RuntimeError("Unexpected IG /accounts response shape")

        for account in accounts:
            if str(account.get("accountId", "")).strip() == self.current_account_id:
                return account
        for account in accounts:
            if account.get("preferred"):
                return account
        if not accounts:
            raise RuntimeError("IG returned no accounts for the current login")
        return accounts[0]

    def _poll_confirmation(self, deal_reference: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.settings.ig_confirm_timeout_seconds
        while time.monotonic() < deadline:
            response = self.session_mgr.session.get(
                f"{self.settings.ig_base_url}/confirms/{deal_reference}",
                headers={
                    "Version": "1",
                    "CST": self.session_mgr.cst or "",
                    "X-SECURITY-TOKEN": self.session_mgr.x_security_token or "",
                },
                timeout=30,
            )
            if response.status_code == 404:
                time.sleep(self.settings.ig_confirm_poll_seconds)
                continue
            payload = self._handle_response(response)
            if payload:
                return payload
            time.sleep(self.settings.ig_confirm_poll_seconds)
        return None

    def _resolve_epic(self, symbol: str) -> str:
        instrument = get_instrument(symbol)
        if instrument.symbol in self._symbol_to_epic:
            return self._symbol_to_epic[instrument.symbol]

        selected: dict[str, Any] | None = None
        for search_term in self._build_search_terms(instrument):
            search_payload = self._request(
                "GET",
                "/markets",
                version="1",
                params={"searchTerm": search_term},
            )
            markets = search_payload.get("markets", []) if isinstance(search_payload, dict) else []
            if not markets:
                continue

            ranked_markets = sorted(
                markets,
                key=lambda market: self._market_rank(market, instrument, search_term),
            )
            if ranked_markets:
                selected = ranked_markets[0]
                break

        if selected is None:
            raise RuntimeError(f"Could not resolve an IG EPIC for symbol {instrument.symbol}")

        epic = str(selected.get("epic", "")).strip()
        if not epic:
            raise RuntimeError(f"Resolved IG market for {instrument.symbol} had no EPIC")

        self._symbol_to_epic[instrument.symbol] = epic
        self._epic_to_symbol[epic] = instrument.symbol
        return epic

    @staticmethod
    def _market_rank(
        market: dict[str, Any],
        instrument,
        search_term: str,
    ) -> tuple[int, int, int, int, int, str]:
        instrument_name = str(market.get("instrumentName", "")).upper()
        display_name = str(market.get("displayName", "")).upper()
        epic = str(market.get("epic", "")).upper()
        market_status = str(market.get("marketStatus", "")).upper()
        expiry = str(market.get("expiry", "")).upper()
        instrument_type = str(market.get("instrumentType", "")).upper()
        searchable_text = " ".join(
            value for value in [instrument_name, display_name, epic] if value
        )

        preferred_types = IGRestBridge._preferred_instrument_types(instrument.asset_class)
        preferred_type_rank = 0 if instrument_type in preferred_types else 1
        tradeable_rank = 0 if market_status == "TRADEABLE" else 1
        cash_rank = 0 if expiry in {"DFB", "-"} else 1
        search_rank = (
            0 if re.search(rf"\b{re.escape(search_term.upper())}\b", searchable_text) else 1
        )
        display_rank = (
            0
            if re.search(rf"\b{re.escape(instrument.display_name.upper())}\b", searchable_text)
            else 1
        )
        symbol_rank = (
            0 if re.search(rf"\b{re.escape(instrument.symbol)}\b", searchable_text) else 1
        )
        return (preferred_type_rank, tradeable_rank, cash_rank, search_rank, display_rank + symbol_rank, epic)

    @staticmethod
    def _preferred_instrument_types(asset_class: str) -> set[str]:
        mapping = {
            "equity": {"SHARES"},
            "etf": {"SHARES"},
            "index": {"INDICES"},
            "forex": {"FOREX"},
            "metal": {"COMMODITIES"},
            "energy": {"COMMODITIES"},
        }
        return mapping.get(asset_class.lower(), {"SHARES", "INDICES", "FOREX", "COMMODITIES"})

    @staticmethod
    def _build_search_terms(instrument) -> list[str]:
        terms: list[str] = []
        for value in instrument.broker_search_terms + (instrument.display_name, instrument.symbol):
            normalized = str(value).strip()
            if normalized and normalized not in terms:
                terms.append(normalized)
        return terms

    def _infer_symbol_from_market(self, market: dict[str, Any]) -> str | None:
        epic = str(market.get("epic", "")).strip()
        if epic and epic in self._epic_to_symbol:
            return self._epic_to_symbol[epic]

        searchable_text = " ".join(
            str(market.get(field, "")).upper()
            for field in ("instrumentName", "marketName", "epic")
            if str(market.get(field, "")).strip()
        )
        best_symbol: str | None = None
        best_rank: tuple[int, int, str] | None = None

        for instrument in list_supported_instruments(self.settings.allowed_asset_classes):
            terms = [instrument.symbol, instrument.display_name, *instrument.broker_search_terms]
            match_count = sum(
                1
                for term in terms
                if term and re.search(rf"\b{re.escape(term.upper())}\b", searchable_text)
            )
            if match_count <= 0:
                continue
            rank = (
                0 if instrument.asset_class in {"index", "forex", "metal", "energy"} else 1,
                -match_count,
                instrument.symbol,
            )
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_symbol = instrument.symbol

        if best_symbol and epic:
            self._epic_to_symbol[epic] = best_symbol
        return best_symbol

    def _request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        return self.session_mgr.request(
            method, path, version=version, json_body=json_body, params=params
        )

    @staticmethod
    def _handle_response(response: Any) -> dict[str, Any] | list[Any]:
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"raw": response.text}
        if not response.ok:
            error_code = payload.get("errorCode") if isinstance(payload, dict) else None
            detail = error_code or response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"IG API request failed: {detail}")
        return payload if isinstance(payload, (dict, list)) else {"raw": payload}

    @staticmethod
    def _make_deal_reference(symbol: str, cycle_id: str | None = None, side: str | None = None) -> str:
        scope = f"{cycle_id or 'nocycle'}_{side or 'noside'}"
        return f"bot_{symbol[:8]}_{scope}"[:30]
