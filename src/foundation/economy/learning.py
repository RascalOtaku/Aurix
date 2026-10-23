"""economy/learning.py - compare what was forecast with what happened, and let that change the next decision.

When the owner approves a venture, its forecast (projected monthly net, owner hours, months to first revenue) is frozen in `forecasts`.
Once it has been launched for at least one complete month, the ledger says what really happened. Two uses:
    comparisons()   per venture: forecast vs actual net per month and hours per week - shown in the weekly review
    calibration()   the median actual/forecast ratio over every venture with enough history, clamped to 0.2..1.0, multiplied into
                    every NEW projection by the scorecard. Optimism that reality has not backed up gets discounted automatically;
                    it never inflates a projection above what was written down. Fewer than two data points: 1.0 (no evidence yet).
"""
from __future__ import annotations

import statistics
from typing import List, Optional

from src.foundation.economy import ledger, store

MIN_SAMPLES = 2
MONTH_SECONDS = 30.4 * 86400


def _launched_at(opp_id: str) -> Optional[float]:
    r = store.one("SELECT at FROM stage_log WHERE opp_id = ? AND to_stage = 'launched' ORDER BY at LIMIT 1", (opp_id,))
    return r["at"] if r else None


def comparisons(now: Optional[float] = None) -> List[dict]:
    now = now or store.now()
    out = []
    for f in store.rows("SELECT * FROM forecasts ORDER BY at"):
        launched = _launched_at(f["opp_id"])
        if launched is None:
            continue
        months = (now - launched) / MONTH_SECONDS
        if months < 1:
            continue
        tot = ledger.venture_totals(f["opp_id"])
        weeks = max(1.0, (now - launched) / (7 * 86400))
        out.append({"opp_id": f["opp_id"], "months": round(months, 1), "forecast_net": f["net_mid"],
                    "actual_net": round(tot["net"] / months, 2), "forecast_hours": f["owner_hours"],
                    "actual_hours": round(tot["hours"] / weeks, 1)})
    latest = {}
    for c in out:                              # a venture approved twice (e.g. re-scaled) keeps only its latest forecast
        latest[c["opp_id"]] = c
    return list(latest.values())


def calibration(now: Optional[float] = None) -> float:
    ratios = [c["actual_net"] / c["forecast_net"] for c in comparisons(now) if c["forecast_net"] > 0]
    if len(ratios) < MIN_SAMPLES:
        return 1.0
    return round(max(0.2, min(1.0, statistics.median(ratios))), 2)
