from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

from t212_cfd_bot.config import Settings
from t212_cfd_bot.models import AccountSnapshot, ExecutionPlan, ExecutionResult, OpenPosition

LOGGER = logging.getLogger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_firefox_binary(configured_path: str | None) -> str | None:
    if configured_path:
        candidate = Path(configured_path)
        if os.name == "nt" and candidate.exists():
            return str(candidate)
        if candidate.suffix.lower() != ".exe" and candidate.exists():
            return str(candidate)

    if os.name == "nt":
        windows_candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "Mozilla Firefox" / "firefox.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Mozilla Firefox" / "firefox.exe",
            Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "Firefox.exe",
        ]
        for candidate in windows_candidates:
            if str(candidate) and candidate.exists():
                return str(candidate)
        return None

    cache_root = Path.home() / ".cache" / "selenium" / "firefox" / "linux64"
    if not cache_root.exists():
        return None

    candidates = sorted(
        (path / "firefox" for path in cache_root.iterdir() if (path / "firefox").exists()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return str(candidates[0])


def _build_firefox_ld_library_path(firefox_binary: str | None, repo_root: Path) -> str | None:
    segments: list[str] = []
    if firefox_binary:
        segments.append(str(Path(firefox_binary).resolve().parent))

    local_alsa_dir = repo_root / "local_deps" / "extracted" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_alsa_dir.exists():
        segments.append(str(local_alsa_dir))

    if not segments:
        return None

    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if existing:
        segments.append(existing)
    return ":".join(segments)


class Trading212LegacyBridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._api = None
        self._cfd = None

    def connect(self) -> None:
        if self._cfd is not None:
            return
        if not self.settings.has_trading212_credentials:
            raise RuntimeError("Trading212 credentials are required for the legacy bridge")

        repo_path = self.settings.trading212_repo_path.resolve()
        if not repo_path.exists():
            raise FileNotFoundError(f"Trading212 repo path does not exist: {repo_path}")

        repo_path_str = str(repo_path)
        if repo_path_str not in sys.path:
            sys.path.insert(0, repo_path_str)

        firefox_binary = _resolve_firefox_binary(self.settings.firefox_binary_path)
        if firefox_binary:
            os.environ["T212_FIREFOX_BINARY"] = firefox_binary
        os.environ["T212_HEADLESS"] = "1" if self.settings.trading212_headless else "0"
        os.environ["T212_IMPORT_FIREFOX_COOKIES"] = (
            "1" if self.settings.trading212_import_firefox_cookies else "0"
        )
        if self.settings.trading212_firefox_profile_path:
            os.environ["T212_FIREFOX_PROFILE_PATH"] = self.settings.trading212_firefox_profile_path

        firefox_ld_library_path = _build_firefox_ld_library_path(firefox_binary, repo_path.parent)
        if firefox_ld_library_path:
            os.environ["LD_LIBRARY_PATH"] = firefox_ld_library_path

        module = import_module("apit212_legacy.apit212")
        Apit212 = getattr(module, "Apit212")
        CFD = getattr(module, "CFD")

        self._api = Apit212()
        self._api._saveCookies = self.settings.trading212_save_cookies
        status = self._api.setup(
            username=self.settings.trading212_username,
            password=self.settings.trading212_password,
            mode=self.settings.trading212_mode,
            _beauty=False,
        )
        if status != 200:
            detail = getattr(getattr(self._api, "constant", None), "error_msg", "") or "unknown error"
            raise RuntimeError(
                f"Trading212 authentication failed with status {status}: {detail}"
            )

        self._cfd = CFD(cred=self._api)
        LOGGER.info("Trading212 legacy CFD bridge connected in %s mode", self.settings.trading212_mode)

    def get_account_snapshot(self) -> AccountSnapshot:
        if self._cfd is None:
            raise RuntimeError("Trading212 bridge not connected")

        funds_payload = self._cfd.get_funds()
        first_account = next(iter(funds_payload.values()), {})
        summary = self._cfd.get_summary() or {}
        open_items = summary.get("open", {}).get("items", [])

        open_positions: list[OpenPosition] = []
        for item in open_items:
            quantity = _as_float(item.get("quantity"))
            direction = "unknown"
            if quantity > 0:
                direction = "buy"
            elif quantity < 0:
                direction = "sell"
            open_positions.append(
                OpenPosition(
                    symbol=str(item.get("code", "")).upper(),
                    position_id=str(item.get("positionId", "")),
                    quantity=quantity,
                    current_price=_as_float(item.get("currentPrice"), default=0.0) or None,
                    pnl=_as_float(item.get("ppl"), default=0.0),
                    direction=direction,
                )
            )

        return AccountSnapshot(
            mode=self.settings.trading212_mode,
            currency=str(first_account.get("currency", "GBP")),
            total_funds=_as_float(first_account.get("total"), self.settings.simulated_account_balance),
            free_funds=_as_float(first_account.get("freeForWithdraw"), self.settings.simulated_account_balance),
            open_positions=open_positions,
        )

    def execute(self, plan: ExecutionPlan, dry_run: bool) -> ExecutionResult:
        if self._cfd is None:
            raise RuntimeError("Trading212 bridge not connected")

        submitted_at = datetime.now(timezone.utc).isoformat()
        if dry_run:
            return ExecutionResult(
                plan=plan,
                status="dry_run",
                submitted_at=submitted_at,
                broker_response={"simulated": True, "mode": self.settings.trading212_mode},
            )

        try:
            if plan.action == "CLOSE":
                close_price = plan.target_price
                if close_price is None or close_price <= 0:
                    close_price = _as_float(self._cfd.fast_price(plan.symbol))
                if close_price is None or close_price <= 0:
                    raise RuntimeError(
                        f"Trading212 could not determine a valid close price for {plan.symbol}"
                    )
                response = self._cfd.close_position(
                    position_id=plan.position_id,
                    current_price=close_price,
                )
            else:
                response = self._cfd.market_order(
                    instrument=plan.symbol,
                    target_price=plan.target_price,
                    quantity=plan.quantity,
                    take_profit=plan.take_profit_distance,
                    stop_loss=plan.stop_distance,
                )
        except Exception as exc:
            return ExecutionResult(
                plan=plan,
                status="failed",
                submitted_at=submitted_at,
                error=str(exc),
            )

        return ExecutionResult(
            plan=plan,
            status="submitted",
            submitted_at=submitted_at,
            broker_response=response,
        )

    def is_tradeable_symbol(self, symbol: str) -> bool:
        if self._cfd is None:
            return True
        try:
            return (_as_float(self._cfd.fast_price(symbol)) or 0.0) > 0
        except Exception:
            return False
