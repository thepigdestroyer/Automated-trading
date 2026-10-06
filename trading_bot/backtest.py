"""Vectorised Track-A backtester (Phase 3 evidence loop).

Track-A is a pure function of price history, so unlike the LLM track it can
be backtested without training-cutoff contamination (ADR 0003). This module
replays the exact Track-A entry rule over years of daily bars to get a
provisional verdict in hours instead of waiting for ~60 forward-walk trades.

Signals come from `compute_trend_frame` (row t = settled-close verdict for
bar t, no lookahead; ADR 0007) and `entry_signals` reproduces
`track_a.decide_track_a`. Realism rules, each load-bearing:

  1. a signal on bar t's settled close fills at bar t+1's OPEN;
  2. a still-forming last bar (today's session) is dropped before anything
     else, so the backtest never sees a settled close early;
  3. a bar whose range crosses both stop and target records the STOP;
  4. a bar that OPENS beyond the stop (or target) fills at the open, not the
     level -- gap-through slippage is real (live paid GBP 227 for one);
  5. time stop after `default_max_holding_hours`, resolved at the first
     daily open at or after the deadline;
  6. one open position per symbol, `track_a_cooldown_hours` between entries;
  7. TP = stop distance x `risk_reward_ratio`.

Costs mirror `paper_pnl`: `_adverse_fill` (half-spread + slippage against the
trade on both legs) plus swap pro-rated by holding time.

Stop regimes (`stop_mode`): live sizes Track-A stops from an HOURLY ATR on a
DAILY signal. "daily_atr" is the strategy as designed; "live_hourly_equiv"
approximates what live actually runs as daily_ATR14 / r_sym, where r_sym is
the measured daily-to-hourly ATR ratio (Yahoo 60m history is ~730 days, too
short for a 5-year hourly replay).

Run:  python -m trading_bot.backtest --years 5 [--shorts]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from trading_bot.config import Settings
from trading_bot.daily_trend import compute_trend_frame
from trading_bot.forward_eval import MIN_INDEPENDENT_TRADES, bootstrap_expectancy_ci, wilson_ci
from trading_bot.indicators import _atr
from trading_bot.instruments import get_instrument, list_supported_instruments
from trading_bot.paper_pnl import _HOURS_PER_YEAR, _adverse_fill

STOP_MODES = ("daily_atr", "live_hourly_equiv")
_OHLC = ["Open", "High", "Low", "Close"]
# Daily bars carry no intrabar timing. An exit inside a bar (or at the last
# close) is timed at mid-session for swap accrual only; the error is at most
# ~0.3 bps of financing at 200 bps/yr.
_INTRABAR_HOURS = 12.0


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str              # "buy" | "sell"
    entry_at: pd.Timestamp # bar whose Open filled the entry (signal bar + 1)
    entry: float           # adverse-filled entry price
    stop: float
    tp: float
    exit_at: pd.Timestamp
    exit: float            # adverse-filled exit price
    reason: str            # "stop" | "tp" | "time" | "end_of_data"
    r_multiple: float      # realised P&L / initial risk
    pnl_pct: float         # net of spread + slippage, signed
    hold_hours: float = 0.0
    gap: bool = False      # exit filled at a bar's open beyond the stop/target
    stop_pct: float = 0.0  # initial stop distance / mid entry


# --- data hygiene ------------------------------------------------------------


def _clean(daily: pd.DataFrame) -> pd.DataFrame:
    """OHLC as floats, sorted, de-duplicated, without unusable rows (any NaN,
    infinite or non-positive price). Such a bar can be neither filled nor
    judged, so it is dropped rather than imputed."""
    missing = [c for c in _OHLC if c not in getattr(daily, "columns", [])]
    if missing:
        raise ValueError(f"daily frame missing columns {missing}")
    frame = daily[_OHLC].astype(float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    values = frame.to_numpy()
    usable = np.isfinite(values).all(axis=1) & (values > 0).all(axis=1)
    return frame[usable]


def drop_unsettled_bar(daily: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop bars whose session has not settled: any bar dated today or later
    in the bar's own timezone (UTC when the index is naive).

    yfinance's daily download includes today's still-forming bar; live acts on
    it, but a backtest that treated its partial close as settled would be
    reading the future.
    """
    if daily is None or len(daily) == 0:
        return daily
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    idx = pd.DatetimeIndex(daily.index)
    if idx.tz is None:
        today = now_ts.tz_convert("UTC").tz_localize(None).normalize()
    else:
        today = now_ts.tz_convert(idx.tz).normalize()
    return daily[idx.normalize() < today]


def _align_ts(ts: Any, idx: pd.DatetimeIndex) -> pd.Timestamp:
    out = pd.Timestamp(ts)
    if idx.tz is not None and out.tzinfo is None:
        return out.tz_localize("UTC")
    if idx.tz is None and out.tzinfo is not None:
        return out.tz_convert("UTC").tz_localize(None)
    return out


# --- signals -----------------------------------------------------------------


def entry_signals(trend: pd.DataFrame, settings: Settings, *, allow_shorts: bool) -> np.ndarray:
    """Per-bar Track-A entry: +1 long, -1 short, 0 none.

    Exactly `track_a.decide_track_a` applied to row t of the trend frame:
    long on direction up + Donchian breakout up, short on the mirror, both
    gated on trend_strength >= min_trend_strength. The strength is rounded to
    0.1 first because that is the value live compares (`build_daily_trend`
    rounds it into the DailyTrend).
    """
    min_strength = float(getattr(settings, "min_trend_strength", 0.0) or 0.0)
    strength = np.array([round(float(x), 1) for x in trend["trend_strength"]], dtype=float)
    strong = strength >= min_strength
    direction = trend["direction"].to_numpy(dtype=object)
    breakout = trend["donchian_breakout"].to_numpy(dtype=object)
    sig = np.zeros(len(trend), dtype=int)
    sig[strong & (direction == "up") & (breakout == "up")] = 1
    if allow_shorts:
        sig[strong & (direction == "down") & (breakout == "down")] = -1
    return sig


# --- simulation --------------------------------------------------------------


def simulate_symbol(
    daily: pd.DataFrame,
    symbol: str,
    settings: Settings,
    *,
    spread_bps: float,
    slippage_bps: float,
    swap_annualised_bps: float | None = None,
    stop_mode: str = "daily_atr",
    hourly_ratio: Mapping[str, float] | None = None,
    allow_shorts: bool | None = None,
    start: Any = None,
    now: datetime | None = None,
) -> list[BacktestTrade]:
    """Replay Track-A on one symbol's daily bars.

    `start` ignores signals on bars before it (indicator warm-up stays in the
    frame). `now` decides which bar is still forming (default: wall clock).
    """
    if stop_mode not in STOP_MODES:
        raise ValueError(f"unknown stop_mode {stop_mode!r}; expected one of {STOP_MODES}")
    ratio = 1.0
    if stop_mode == "live_hourly_equiv":
        raw = (hourly_ratio or {}).get(symbol)
        if raw is None or not math.isfinite(float(raw)) or float(raw) <= 0:
            raise ValueError(f"stop_mode='live_hourly_equiv' needs hourly_ratio[{symbol!r}] > 0")
        ratio = float(raw)
    if allow_shorts is None:
        allow_shorts = bool(getattr(settings, "paper_allow_shorts", False))
    swap = float(
        settings.swap_annualised_bps_default if swap_annualised_bps is None else swap_annualised_bps
    )

    frame = drop_unsettled_bar(_clean(daily), now)
    n = len(frame)
    if n < 2:
        return []
    idx = pd.DatetimeIndex(frame.index)
    sig = entry_signals(compute_trend_frame(frame), settings, allow_shorts=allow_shorts)
    atr = _atr(frame).to_numpy() / ratio
    o, h, lo, c = (frame[col].to_numpy() for col in _OHLC)

    start_ts = _align_ts(start, idx) if start is not None else None
    max_hold = timedelta(hours=float(settings.default_max_holding_hours))
    cooldown = timedelta(hours=float(getattr(settings, "track_a_cooldown_hours", 0.0) or 0.0))
    mult = float(settings.atr_stop_multiplier)
    floor = float(settings.stop_loss_pct_floor)
    rr = float(settings.risk_reward_ratio)

    trades: list[BacktestTrade] = []
    last_entry: pd.Timestamp | None = None
    t = 0
    while t < n - 1:
        side = int(sig[t])
        e = t + 1
        if (
            side == 0
            or not math.isfinite(atr[t])
            or (start_ts is not None and idx[t] < start_ts)
            or (last_entry is not None and idx[e] - last_entry < cooldown)
        ):
            t += 1
            continue

        entry_mid = float(o[e])
        dist = max(float(atr[t]) * mult, entry_mid * floor, 0.01)
        if side > 0:
            stop, tp = entry_mid - dist, entry_mid + dist * rr
        else:
            stop, tp = entry_mid + dist, entry_mid - dist * rr

        exit_j, exit_mid, reason, gap, hold = _walk_exit(
            side, stop, tp, e, idx, o, h, lo, c, max_hold
        )
        side_str = "buy" if side > 0 else "sell"
        fill_entry = _adverse_fill(
            entry_mid, side_str, leg="entry", spread_bps=spread_bps, slippage_bps=slippage_bps
        )
        fill_exit = _adverse_fill(
            exit_mid, side_str, leg="exit", spread_bps=spread_bps, slippage_bps=slippage_bps
        )
        financing = fill_entry * (swap / 10_000.0) * (hold / _HOURS_PER_YEAR)
        pnl_units = side * (fill_exit - fill_entry) - financing
        trades.append(
            BacktestTrade(
                symbol=symbol,
                side=side_str,
                entry_at=idx[e],
                entry=fill_entry,
                stop=stop,
                tp=tp,
                exit_at=idx[exit_j],
                exit=fill_exit,
                reason=reason,
                r_multiple=pnl_units / dist,
                pnl_pct=pnl_units / fill_entry,
                hold_hours=hold,
                gap=gap,
                stop_pct=dist / entry_mid,
            )
        )
        last_entry = idx[e]
        # Flat from the exit bar on: its own settled close may signal again.
        t = exit_j
    return trades


def _walk_exit(side, stop, tp, e, idx, o, h, lo, c, max_hold):
    """(exit bar, exit mid, reason, filled-at-open-beyond-level, hold hours)."""
    n = len(idx)
    for j in range(e, n):
        held = idx[j] - idx[e]
        if j > e:
            # At the open: a gap through a level fills at the open; otherwise
            # the time stop closes at the first open at or after the deadline.
            if (side > 0 and o[j] <= stop) or (side < 0 and o[j] >= stop):
                return j, float(o[j]), "stop", True, _hours(held)
            if (side > 0 and o[j] >= tp) or (side < 0 and o[j] <= tp):
                return j, float(o[j]), "tp", True, _hours(held)
            if held >= max_hold:
                return j, float(o[j]), "time", False, _hours(held)
        if side > 0:
            stop_hit, tp_hit = lo[j] <= stop, h[j] >= tp
        else:
            stop_hit, tp_hit = h[j] >= stop, lo[j] <= tp
        if stop_hit:  # pessimistic: stop wins a bar that touched both
            return j, float(stop), "stop", False, _hours(held) + _INTRABAR_HOURS
        if tp_hit:
            return j, float(tp), "tp", False, _hours(held) + _INTRABAR_HOURS
    last = n - 1
    return last, float(c[last]), "end_of_data", False, _hours(idx[last] - idx[e]) + _INTRABAR_HOURS


def _hours(delta: pd.Timedelta) -> float:
    return float(pd.Timedelta(delta).total_seconds()) / 3600.0


# --- hourly-vs-daily ATR ratio -------------------------------------------------


def _local_dates(index: pd.Index, tz: Any = None) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        if tz is not None:
            idx = idx.tz_convert(tz)
        idx = idx.tz_localize(None)
    return idx.normalize().as_unit("ns")


def measure_hourly_ratio(
    daily: pd.DataFrame,
    hourly: pd.DataFrame,
    *,
    period: int = 14,
    min_samples: int = 20,
) -> float | None:
    """Median of daily_ATR / hourly_ATR as the live bot experiences it.

    Each hourly bar is one moment live could size a stop: its hourly ATR-14
    is set against the daily ATR-14 of the most recent settled daily bar
    (dated strictly before the hourly bar's local date, at most 5 days old).
    None when fewer than `min_samples` hourly bars overlap.
    """
    d = _clean(daily)
    hr = _clean(hourly)
    if len(d) <= period or len(hr) <= period:
        return None
    left = pd.DataFrame(
        {"date": _local_dates(hr.index, pd.DatetimeIndex(d.index).tz), "h_atr": _atr(hr, period).to_numpy()}
    ).dropna()
    right = pd.DataFrame(
        {"date": _local_dates(d.index), "d_atr": _atr(d, period).to_numpy()}
    ).dropna()
    if left.empty or right.empty:
        return None
    merged = pd.merge_asof(
        left.sort_values("date"),
        right.sort_values("date"),
        on="date",
        direction="backward",
        allow_exact_matches=False,
        tolerance=pd.Timedelta(days=5),
    ).dropna()
    merged = merged[merged["h_atr"] > 0]
    ratios = (merged["d_atr"] / merged["h_atr"]).to_numpy()
    ratios = ratios[np.isfinite(ratios) & (ratios > 0)]
    if len(ratios) < min_samples:
        return None
    return float(np.median(ratios))


# --- aggregation -------------------------------------------------------------


def _verdict(n: int, r_lo: float, r_hi: float) -> str:
    """Same thresholds as forward_eval.build_report."""
    if n < MIN_INDEPENDENT_TRADES:
        return "insufficient_data"
    if r_lo > 0:
        return "positive_expectancy"
    if r_hi < 0:
        return "negative_expectancy"
    return "indeterminate"


def _summarise(trades: list[BacktestTrade], *, with_ci: bool = True) -> dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    rs = [float(t.r_multiple) for t in trades]
    pcts = [float(t.pnl_pct) for t in trades]
    win_lo, win_hi = wilson_ci(wins, n)
    out: dict[str, Any] = {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n if n else 0.0,
        "win_rate_ci": [round(win_lo, 4), round(win_hi, 4)],
        "expectancy_pct": sum(pcts) / n if n else 0.0,
        "expectancy_r": sum(rs) / n if n else 0.0,
    }
    if with_ci:
        _, p_lo, p_hi = bootstrap_expectancy_ci(pcts)
        _, r_lo, r_hi = bootstrap_expectancy_ci(rs)
        out["expectancy_ci"] = [p_lo, p_hi]
        out["expectancy_r_ci"] = [r_lo, r_hi]
        out["verdict"] = _verdict(n, r_lo, r_hi)
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    out.update(
        {
            "exit_reasons": dict(Counter(t.reason for t in trades)),
            "gap_exits": sum(1 for t in trades if t.gap),
            "avg_stop_pct": sum(t.stop_pct for t in trades) / n if n else 0.0,
            "avg_hold_hours": sum(t.hold_hours for t in trades) / n if n else 0.0,
            "avg_win_r": gains / wins if wins else 0.0,
            "avg_loss_r": -losses / (n - wins) if n - wins else 0.0,
            "profit_factor_r": (gains / losses) if losses > 0 else None,
            "worst_r": min(rs) if rs else None,
        }
    )
    return out


def run_backtest(
    frames: Mapping[str, pd.DataFrame],
    settings: Settings,
    *,
    stop_mode: str = "daily_atr",
    hourly_ratio: Mapping[str, float] | None = None,
    allow_shorts: bool | None = None,
    start: Any = None,
    now: datetime | None = None,
    trades_out: list[BacktestTrade] | None = None,
) -> dict[str, Any]:
    """Backtest every symbol (keyed by bot symbol) and aggregate.

    Costs per symbol: spread = instrument.max_spread_bps, slippage =
    settings.slippage_buffer_bps, swap = settings.swap_annualised_bps_default
    -- the inputs Track-A's paper book uses. Win rate (Wilson) and
    expectancy (bootstrap, in R and in % of price) use forward_eval's
    estimators so the numbers compare directly with the forward-walk report.
    A symbol that cannot be simulated is listed under `skipped`, never
    silently dropped.
    """
    if stop_mode not in STOP_MODES:
        raise ValueError(f"unknown stop_mode {stop_mode!r}; expected one of {STOP_MODES}")
    by_symbol_trades: dict[str, list[BacktestTrade]] = {}
    skipped: dict[str, str] = {}
    for symbol, daily in frames.items():
        instrument = get_instrument(symbol)
        try:
            by_symbol_trades[symbol] = simulate_symbol(
                daily,
                symbol,
                settings,
                spread_bps=float(instrument.max_spread_bps),
                slippage_bps=float(settings.slippage_buffer_bps),
                stop_mode=stop_mode,
                hourly_ratio=hourly_ratio,
                allow_shorts=allow_shorts,
                start=start,
                now=now,
            )
        except ValueError as exc:
            skipped[symbol] = str(exc)

    trades = [t for ts in by_symbol_trades.values() for t in ts]
    trades.sort(key=lambda t: (pd.Timestamp(t.entry_at).value, t.symbol))
    if trades_out is not None:
        trades_out.extend(trades)

    report = _summarise(trades)
    report["stop_mode"] = stop_mode
    report["allow_shorts"] = bool(
        getattr(settings, "paper_allow_shorts", False) if allow_shorts is None else allow_shorts
    )
    report["skipped"] = skipped
    report["by_symbol"] = {
        sym: _summarise(ts, with_ci=True) for sym, ts in by_symbol_trades.items()
    }
    report["by_side"] = {
        side: _summarise([t for t in trades if t.side == side], with_ci=False)
        for side in ("buy", "sell")
        if any(t.side == side for t in trades)
    }
    years: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        years.setdefault(str(pd.Timestamp(t.entry_at).year), []).append(t)
    report["by_year"] = {y: _summarise(ts, with_ci=False) for y, ts in sorted(years.items())}
    counts = [len(ts) for ts in by_symbol_trades.values()]
    report["symbol_concentration"] = (max(counts) / len(trades)) if trades else 0.0
    return report


# --- CLI -----------------------------------------------------------------------

DISCLOSURES = [
    "live_hourly_equiv APPROXIMATES live's hourly-ATR stop as daily_ATR14 / r_sym; r_sym is "
    "the median ratio of (prior settled daily ATR14) / (hourly ATR14) over every overlapping "
    "Yahoo 60m bar (~730 days), applied as a constant to the whole window.",
    "Signals use the SETTLED daily close and fill at the next daily open. Live acts intraday on "
    "a still-forming daily bar, so live entries can differ from these.",
    "Costs: spread = instrument.max_spread_bps (a cap, so likely conservative), slippage = "
    "slippage_buffer_bps on each leg, swap = swap_annualised_bps_default pro-rated by hold.",
    "Daily bars hide the intrabar path: a bar touching stop and target is scored as a stop; "
    "the time stop resolves at the first daily open at or after the deadline.",
    "Futures are Yahoo continuous front-month series (CL=F etc.); contract rolls show up as "
    "price jumps and can create spurious gap fills in either direction.",
    "Each symbol is simulated independently: no portfolio caps (max_open_positions, "
    "correlation cap), no IG dealing-rule / min-deal-size rejections, no cost-edge gate. "
    "Correlated symbols (WTI/BRENT, indices, mega-caps) make the effective N smaller than "
    "the raw trade count.",
    "R-expectancy is what matters for a fixed-fractional account (each trade risks "
    "risk_per_trade_pct of equity); % expectancy is per unit of notional.",
]


def _download(ticker: str, *, interval: str, start: Any = None, period: str | None = None,
              cache: Path | None = None) -> pd.DataFrame | None:  # pragma: no cover - network
    import yfinance as yf

    path = None
    if cache is not None:
        safe = "".join(ch if ch.isalnum() else "_" for ch in ticker)
        path = cache / f"{safe}_{interval}.pkl"
        if path.exists():
            return pd.read_pickle(path)
    try:
        frame = yf.download(
            tickers=ticker,
            start=start,
            period=period,
            interval=interval,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
    except Exception:
        return None
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(path)
    return frame


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ci(pair: Iterable[float], fmt) -> str:
    lo, hi = list(pair)
    return f"[{fmt(lo)}, {fmt(hi)}]"


def format_comparison(reports: Mapping[str, Mapping[str, Any]], ratios: Mapping[str, float]) -> str:
    """Human-readable side-by-side of the stop regimes plus per-symbol rows."""
    modes = [m for m in STOP_MODES if m in reports]
    r_fmt = lambda v: f"{v:+.3f}"  # noqa: E731
    p_fmt = lambda v: f"{v * 100:+.3f}%"  # noqa: E731
    rows = [
        ("trades", lambda r: str(r["trades"])),
        ("win rate", lambda r: _pct(r["win_rate"])),
        ("win rate 95% CI", lambda r: _ci(r["win_rate_ci"], _pct)),
        ("expectancy R", lambda r: r_fmt(r["expectancy_r"])),
        ("expectancy R 95% CI", lambda r: _ci(r["expectancy_r_ci"], r_fmt)),
        ("expectancy % / trade", lambda r: p_fmt(r["expectancy_pct"])),
        ("expectancy % 95% CI", lambda r: _ci(r["expectancy_ci"], p_fmt)),
        ("verdict (R, n>=60)", lambda r: r["verdict"]),
        ("avg stop distance", lambda r: f"{r['avg_stop_pct'] * 100:.2f}%"),
        ("avg win / loss R", lambda r: f"{r['avg_win_r']:+.2f} / {r['avg_loss_r']:+.2f}"),
        ("profit factor (R)", lambda r: "n/a" if r["profit_factor_r"] is None else f"{r['profit_factor_r']:.2f}"),
        ("worst trade R", lambda r: "n/a" if r["worst_r"] is None else f"{r['worst_r']:+.2f}"),
        ("exits stop/tp/time/eod", lambda r: "/".join(
            str(r["exit_reasons"].get(k, 0)) for k in ("stop", "tp", "time", "end_of_data"))),
        ("gap-through exits", lambda r: str(r["gap_exits"])),
        ("avg hold hours", lambda r: f"{r['avg_hold_hours']:.1f}"),
    ]
    width = 26
    col = 30
    lines = [f"{'metric':<{width}}" + "".join(f"{m:>{col}}" for m in modes)]
    lines.append("-" * (width + col * len(modes)))
    for label, fn in rows:
        lines.append(f"{label:<{width}}" + "".join(f"{fn(reports[m]):>{col}}" for m in modes))

    symbols = sorted({s for m in modes for s in reports[m]["by_symbol"]})
    lines.append("")
    head = f"{'symbol':<9}{'r_sym':>7}"
    for m in modes:
        tag = "daily" if m == "daily_atr" else "live"
        head += f"{tag + ' n':>9}{'win%':>7}{'E[R]':>8}{'stop%':>7}"
    lines.append(head)
    lines.append("-" * len(head))
    for s in symbols:
        ratio = ratios.get(s)
        line = f"{s:<9}{('%.2f' % ratio) if ratio else '-':>7}"
        for m in modes:
            agg = reports[m]["by_symbol"].get(s)
            if not agg:
                line += f"{'-':>9}{'-':>7}{'-':>8}{'-':>7}"
                continue
            line += (
                f"{agg['trades']:>9}{agg['win_rate'] * 100:>6.0f}%{agg['expectancy_r']:>+8.3f}"
                f"{agg['avg_stop_pct'] * 100:>6.2f}%"
            )
        lines.append(line)

    for m in modes:
        by_year = reports[m].get("by_year") or {}
        if by_year:
            lines.append("")
            lines.append(f"{m} by entry year:  " + "  ".join(
                f"{y}: n={v['trades']} win={_pct(v['win_rate'])} E[R]={v['expectancy_r']:+.3f}"
                for y, v in by_year.items()))
        by_side = reports[m].get("by_side") or {}
        if len(by_side) > 1:
            lines.append(f"{m} by side:  " + "  ".join(
                f"{k}: n={v['trades']} win={_pct(v['win_rate'])} E[R]={v['expectancy_r']:+.3f}"
                for k, v in by_side.items()))
    return "\n".join(lines)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return None if not math.isfinite(float(obj)) else float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin CLI over the network
    parser = argparse.ArgumentParser(description="Backtest the mechanical Track-A strategy on daily bars.")
    parser.add_argument("--years", type=float, default=5.0, help="evaluation window length")
    parser.add_argument("--shorts", action="store_true", help="force shorts on (else settings.paper_allow_shorts)")
    parser.add_argument("--stop-mode", choices=("both", *STOP_MODES), default="both")
    parser.add_argument("--symbols", default="", help="comma-separated bot symbols (default: tradeable universe)")
    parser.add_argument("--hourly-period", default="730d", help="Yahoo 60m history used to measure r_sym")
    parser.add_argument("--cache", type=Path, default=None, help="directory to cache downloads (pickles)")
    parser.add_argument("--trades-out", type=Path, default=None, help="write every trade to this CSV")
    args = parser.parse_args(argv)

    settings = Settings()
    now = datetime.now(timezone.utc)
    window_start = pd.Timestamp(now - timedelta(days=365.25 * args.years))
    # Warm-up: 400 calendar days before the window, the same daily depth live
    # downloads, so sma200 exists on the first evaluated bar.
    download_start = (window_start - pd.Timedelta(days=400)).date().isoformat()

    universe = list_supported_instruments(settings.allowed_asset_classes)
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        universe = [i for i in universe if i.symbol in wanted]

    frames: dict[str, pd.DataFrame] = {}
    ratios: dict[str, float] = {}
    failed: dict[str, str] = {}
    data_meta: dict[str, Any] = {}
    for inst in universe:
        daily = _download(inst.data_symbol, interval="1d", start=download_start, cache=args.cache)
        if daily is None or len(daily) == 0:
            failed[inst.symbol] = f"daily download failed ({inst.data_symbol})"
            continue
        settled = drop_unsettled_bar(_clean(daily), now)
        frames[inst.symbol] = daily
        meta = {
            "data_symbol": inst.data_symbol,
            "asset_class": inst.asset_class,
            "daily_bars": int(len(settled)),
            "first_bar": settled.index[0] if len(settled) else None,
            "last_settled_bar": settled.index[-1] if len(settled) else None,
            "forming_bars_dropped": int(len(_clean(daily)) - len(settled)),
            "spread_bps": inst.max_spread_bps,
        }
        hourly = _download(inst.data_symbol, interval="60m", period=args.hourly_period, cache=args.cache)
        ratio = measure_hourly_ratio(daily, hourly) if hourly is not None and len(hourly) else None
        meta["hourly_bars"] = int(len(hourly)) if hourly is not None else 0
        meta["r_sym"] = ratio
        if ratio is None:
            failed[inst.symbol] = f"hourly download/overlap failed ({inst.data_symbol}); no r_sym"
        else:
            ratios[inst.symbol] = ratio
        data_meta[inst.symbol] = meta

    modes = list(STOP_MODES) if args.stop_mode == "both" else [args.stop_mode]
    if "live_hourly_equiv" in modes:
        # Compare regimes on the SAME symbols: those with a measured r_sym.
        frames = {s: f for s, f in frames.items() if s in ratios}

    allow_shorts = True if args.shorts else None
    reports: dict[str, Any] = {}
    all_trades: list[tuple[str, BacktestTrade]] = []
    for mode in modes:
        sink: list[BacktestTrade] = []
        reports[mode] = run_backtest(
            frames, settings, stop_mode=mode, hourly_ratio=ratios,
            allow_shorts=allow_shorts, start=window_start, now=now, trades_out=sink,
        )
        all_trades.extend((mode, t) for t in sink)

    out = {
        "meta": {
            "generated_at": now.isoformat(),
            "years": args.years,
            "window_start": window_start.isoformat(),
            "allow_shorts": reports[modes[0]]["allow_shorts"],
            "symbols_tested": sorted(frames),
            "failed": failed,
            "hourly_ratio": ratios,
            "data": data_meta,
            "settings": {
                k: getattr(settings, k) for k in (
                    "min_trend_strength", "atr_stop_multiplier", "stop_loss_pct_floor",
                    "risk_reward_ratio", "default_max_holding_hours", "track_a_cooldown_hours",
                    "slippage_buffer_bps", "swap_annualised_bps_default",
                )
            },
            "disclosures": DISCLOSURES,
        },
        **reports,
    }
    print(json.dumps(_jsonable(out), indent=2))
    print(format_comparison(reports, ratios), file=sys.stderr)
    if failed:
        print("\nFAILED / EXCLUDED: " + "; ".join(f"{k}: {v}" for k, v in failed.items()), file=sys.stderr)

    if args.trades_out is not None:
        rows = [
            {"stop_mode": mode, **{f: getattr(t, f) for f in BacktestTrade.__dataclass_fields__}}
            for mode, t in all_trades
        ]
        pd.DataFrame(rows).to_csv(args.trades_out, index=False)


if __name__ == "__main__":  # pragma: no cover
    main()
