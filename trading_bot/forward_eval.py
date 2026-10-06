"""Forward-walk paper-evaluation statistics (Phase 3 strategy pivot, 2026-05).

The amended roadmap forbids LLM backtesting (training-cutoff contamination):
the ONLY statistically valid evaluation is a forward walk on post-cutoff
dates — frozen prompt, deterministic decoding, real prices, simulated
cost-aware fills (paper_pnl.py). This module turns the accumulated paper
trades + decision records into an honest report:

  - win rate with a Wilson score interval (binomial uncertainty),
  - expectancy in R-multiples with a bootstrap CI,
  - Brier score + calibration table (is LLM confidence informative?),
  - per-symbol breakdown (concentration check — effective N << raw N when
    one instrument dominates),
  - prompt-version drift detection (an eval window is only valid within ONE
    prompt version),
  - progress toward the minimum independent-trade count.

Pure functions over plain dicts; CLI entry at the bottom reads the JSONL /
state files produced by the live bot.

Run:  python -m trading_bot.forward_eval
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

# The adversarial review's power analysis: distinguishing a 55% from a 50%
# win rate needs ~780 trades at p<0.05; we set a pragmatic floor of 60
# INDEPENDENT trades before any verdict is even entertained — and the verdict
# below additionally requires the bootstrap CI to clear zero.
MIN_INDEPENDENT_TRADES = 60

_Z = 1.96  # 95%


def wilson_ci(wins: int, n: int, z: float = _Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. (0,1) when n=0."""
    if n <= 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def bootstrap_expectancy_ci(
    r_multiples: list[float],
    n_boot: int = 2000,
    seed: int = 1234,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """(mean, ci_low, ci_high) of expectancy in R via percentile bootstrap.

    Seeded so reports are reproducible run-to-run on the same data.
    """
    if not r_multiples:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(r_multiples)
    mean = sum(r_multiples) / n
    means = sorted(
        sum(rng.choice(r_multiples) for _ in range(n)) / n for _ in range(n_boot)
    )
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (mean, lo, hi)


def brier_score(pairs: list[tuple[float, bool]]) -> float | None:
    """Mean squared error of (confidence, win) pairs. None when empty.

    0.0 = perfect calibration+resolution, 0.25 = uninformative coin at 0.5,
    1.0 = perfectly anti-calibrated.
    """
    if not pairs:
        return None
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in pairs) / len(pairs)


def calibration_table(pairs: list[tuple[float, bool]]) -> list[dict[str, Any]]:
    """Bucket confidence into 0.1 bins -> realised win rate per bin."""
    bins: dict[int, list[bool]] = {}
    for conf, win in pairs:
        b = min(9, max(0, int(conf * 10)))
        bins.setdefault(b, []).append(win)
    table = []
    for b in sorted(bins):
        outcomes = bins[b]
        table.append(
            {
                "bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}",
                "n": len(outcomes),
                "mean_confidence": round((b / 10) + 0.05, 2),
                "win_rate": sum(outcomes) / len(outcomes),
            }
        )
    return table


def build_report(
    closed: list[dict[str, Any]],
    records: list[dict[str, Any]],
    min_trades: int = MIN_INDEPENDENT_TRADES,
) -> dict[str, Any]:
    """Assemble the forward-walk report from paper closes + decision records.

    Verdict logic (deliberately strict):
      - insufficient_data  : n < min_trades. No conclusion permitted.
      - positive_expectancy: n >= min_trades AND bootstrap CI low > 0.
      - negative_expectancy: n >= min_trades AND bootstrap CI high < 0.
      - indeterminate      : n >= min_trades but CI straddles zero.
    """
    n = len(closed)
    wins = sum(1 for c in closed if c.get("win"))
    rs = [float(c["r_multiple"]) for c in closed if c.get("r_multiple") is not None]
    mean_r, r_lo, r_hi = bootstrap_expectancy_ci(rs)
    win_lo, win_hi = wilson_ci(wins, n)

    # Confidence calibration: join closes to their decision records (closes
    # carry confidence themselves when written by the upgraded simulator;
    # fall back to the record's confidence via decision_id).
    rec_by_id = {r.get("decision_id"): r for r in records if r.get("decision_id")}
    pairs: list[tuple[float, bool]] = []
    for c in closed:
        conf = c.get("confidence")
        if conf is None:
            rec = rec_by_id.get(c.get("decision_id"))
            conf = rec.get("confidence") if rec else None
        if conf is not None and c.get("win") is not None:
            pairs.append((float(conf), bool(c["win"])))

    per_symbol: dict[str, dict[str, Any]] = {}
    for c in closed:
        sym = str(c.get("symbol") or "?")
        agg = per_symbol.setdefault(sym, {"n": 0, "wins": 0, "sum_r": 0.0})
        agg["n"] += 1
        agg["wins"] += 1 if c.get("win") else 0
        agg["sum_r"] += float(c.get("r_multiple") or 0.0)
    for agg in per_symbol.values():
        agg["win_rate"] = agg["wins"] / agg["n"] if agg["n"] else 0.0
        agg["mean_r"] = agg["sum_r"] / agg["n"] if agg["n"] else 0.0

    # Prompt drift: joined records must share ONE prompt_sha for the eval
    # window to be a single experiment.
    joined_shas = {
        rec_by_id[c["decision_id"]].get("prompt_sha")
        for c in closed
        if c.get("decision_id") in rec_by_id
    } - {None}
    prompt_versions = len(joined_shas)

    # Concentration: the largest single-symbol share of trades. Near 1.0
    # means effective N is far below raw N (correlated bets).
    concentration = (
        max(agg["n"] for agg in per_symbol.values()) / n if n else 0.0
    )

    if n < min_trades:
        verdict = "insufficient_data"
    elif r_lo > 0:
        verdict = "positive_expectancy"
    elif r_hi < 0:
        verdict = "negative_expectancy"
    else:
        verdict = "indeterminate"

    return {
        "n_trades": n,
        "wins": wins,
        "win_rate": wins / n if n else 0.0,
        "win_rate_ci": [round(win_lo, 4), round(win_hi, 4)],
        "expectancy_r": round(mean_r, 4),
        "expectancy_r_ci": [round(r_lo, 4), round(r_hi, 4)],
        "brier": brier_score(pairs),
        "calibration": calibration_table(pairs),
        "per_symbol": per_symbol,
        "symbol_concentration": round(concentration, 3),
        "prompt_versions": prompt_versions,
        "prompt_drift_warning": prompt_versions > 1,
        "progress": f"{n}/{min_trades}",
        "verdict": verdict,
    }


def build_track_comparison(
    llm_state: dict[str, Any],
    track_a_state: dict[str, Any],
    records: list[dict[str, Any]],
    min_trades: int = MIN_INDEPENDENT_TRADES,
) -> dict[str, Any]:
    """Head-to-head LLM track vs Track-A, over the SAME symbol universe.

    The cash-risk filter narrows the LLM track only; Track-A deliberately
    trades the unfiltered universe (see bot._run_track_a). Comparing the two
    books as-is would score them over different symbol sets, and the
    difference lands exactly on the volatile instruments the filter drops.
    So the headline ``llm_value_add_r`` excludes Track-A trades tagged
    ``in_llm_universe: False`` (the symbol was dropped for the LLM that
    cycle). The LLM universe is a subset of Track-A's every cycle, so no LLM
    trades need excluding.

    Untagged Track-A rows predate the tag and are kept: before the cash-risk
    filter existed both tracks were handed the same universe. Their count is
    reported so a mixed window is visible.

    Positive delta = the LLM track outperforms the mechanical twin.
    """
    llm_closed = llm_state.get("closed", [])
    track_a_closed = track_a_state.get("closed", [])
    same_universe = [c for c in track_a_closed if c.get("in_llm_universe") is not False]

    out: dict[str, Any] = {}
    out["llm_track"] = build_report(llm_closed, records, min_trades)
    out["llm_track"]["open_positions"] = len(llm_state.get("open", []))
    out["track_a"] = build_report(track_a_closed, records, min_trades)
    out["track_a"]["open_positions"] = len(track_a_state.get("open", []))
    out["track_a_same_universe"] = build_report(same_universe, records, min_trades)
    out["track_a_same_universe"]["excluded_outside_llm_universe"] = (
        len(track_a_closed) - len(same_universe)
    )
    out["track_a_same_universe"]["untagged_rows"] = sum(
        1 for c in track_a_closed if "in_llm_universe" not in c
    )

    llm_r = out["llm_track"]["expectancy_r"]
    out["llm_value_add_r"] = round(llm_r - out["track_a_same_universe"]["expectancy_r"], 4)
    out["llm_value_add_r_basis"] = (
        "same_universe: Track-A trades on symbols the cash-risk filter dropped "
        "from the LLM track that cycle are excluded"
    )
    out["llm_value_add_r_unrestricted"] = round(llm_r - out["track_a"]["expectancy_r"], 4)
    out["llm_value_add_r_unrestricted_basis"] = (
        "all Track-A trades, including symbols the LLM track could not trade"
    )
    out["comparison_valid"] = (
        out["llm_track"]["verdict"] != "insufficient_data"
        and out["track_a_same_universe"]["verdict"] != "insufficient_data"
    )
    return out


# --- CLI ----------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_paper_state(path: Path | None) -> dict[str, Any]:
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"open": [], "closed": []}


def main() -> None:  # pragma: no cover - thin CLI
    from trading_bot.config import Settings

    settings = Settings()
    records = _load_jsonl(Path(settings.decision_records_path)) if settings.decision_records_path else []

    out = build_track_comparison(
        _load_paper_state(settings.paper_pnl_path),
        _load_paper_state(getattr(settings, "track_a_pnl_path", None)),
        records,
    )
    print(json.dumps(out, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
