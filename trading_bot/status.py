from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.config import Settings


def build_status(settings: Settings) -> str:
    lines: list[str] = []
    lines.append(
        f"Bot       trading_bot — broker={settings.broker}, "
        f"mode={settings.execution_mode}, dry_run={settings.dry_run}"
    )
    state: dict = {}
    if settings.runtime_state_path.exists():
        try:
            state = json.loads(settings.runtime_state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    daily = state.get("daily") or {}
    lines.append(f"Daily     {daily}")

    positions = state.get("positions") or {}
    lines.append(f"Book      {len(positions)} open positions tracked")
    for pid, meta in positions.items():
        lines.append(
            f"          {meta.get('symbol')} {meta.get('direction')} "
            f"entry={meta.get('entry_price')} last={meta.get('last_price')}"
        )

    journal = state.get("trade_journal") or []
    if journal:
        last = journal[-1]
        pnl = last.get("pnl_pct", 0.0) or 0.0
        lines.append(
            f"LastTrade {last.get('symbol')} {last.get('direction')} "
            f"pnl_pct={pnl:.4f} at {last.get('exit_at')}"
        )

    ks_path = settings.kill_switch_file
    ks_state = "SET" if ks_path and Path(ks_path).exists() else "not set"
    lines.append(f"KillSwitch {ks_state}")

    alerts_dir = settings.alerts_dir
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alert_file = alerts_dir / f"{day}.log"
    recent: list[str] = []
    if alert_file.exists():
        try:
            with alert_file.open("r", encoding="utf-8") as handle:
                recent = handle.readlines()[-5:]
        except Exception:
            recent = []

    if recent:
        lines.append("Alerts:")
        for a in recent:
            lines.append(f"  {a.strip()}")
    else:
        lines.append("Alerts    none (today)")

    return "\n".join(lines)


def run_status() -> int:
    print(build_status(Settings()))
    return 0
