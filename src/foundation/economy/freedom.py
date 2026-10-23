"""economy/freedom.py - the freedom number: how much dependable income and reserve a life without a conventional job needs.

    monthly costs     = essential monthly costs + irregular annual costs / 12
    target income     = monthly costs / (1 - safety margin)       (a 20% margin: $2,250 of costs needs $2,813 coming in)
    emergency fund    = monthly costs x reserve months
    gap               = target income - dependable income           (dependable = ledger.dependable(), never a forecast)

Three milestones, in order:
    breathing room            savings reach the emergency fund target
    work optionality          dependable income covers half of monthly costs
    financial independence    dependable income reaches the target AND the emergency fund is in place

Profile values start as clearly-labelled PLACEHOLDERS until the owner sets them (`freedom set essentials 1800`). The same profile
holds the hard limits every opportunity is checked against (test cash, max loss, weekly hours).
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from src.foundation import audit
from src.foundation.economy import ledger, store

# key: (default, what it means, low, high)
FIELDS: Dict[str, Tuple[float, str, float, float]] = {
    "essentials": (2000.0, "essential monthly living costs ($)", 0, 100000),
    "irregular": (3000.0, "irregular expenses per YEAR ($)", 0, 1000000),
    "margin": (20.0, "safety margin (%)", 0, 80),
    "reserve_months": (6.0, "emergency reserve (months of costs)", 0, 36),
    "saved": (0.0, "cash already set aside as a reserve ($)", 0, 10000000),
    "tax": (25.0, "tax reserve on venture profit (%)", 0, 60),
    "wage": (0.0, "what an hour of your current job pays, after tax ($/h)", 0, 1000),
    "test_cash": (50.0, "HARD LIMIT: most cash one experiment may risk before validation ($)", 0, 1000000),
    "max_loss": (100.0, "HARD LIMIT: largest credible loss any one venture may carry ($)", 0, 10000000),
    "hours": (10.0, "HARD LIMIT: hours per week you will give all ventures together", 0, 80),
}
ALIASES = {"costs": "essentials", "monthly": "essentials", "annual": "irregular", "reserve": "reserve_months",
           "savings": "saved", "cash": "saved", "tax_rate": "tax", "hourly": "wage", "test": "test_cash", "loss": "max_loss",
           "time": "hours"}


def profile() -> Dict[str, float]:
    got = {r["key"]: r["value"] for r in store.rows("SELECT key, value FROM profile")}
    return {k: float(got.get(k, d[0])) for k, d in FIELDS.items()}


def is_set(key: str) -> bool:
    return store.one("SELECT 1 AS x FROM profile WHERE key = ?", (key,)) is not None


def set_value(key: str, value: float) -> str:
    key = ALIASES.get(key.lower(), key.lower())
    if key not in FIELDS:
        return "Unknown setting. One of: " + ", ".join(FIELDS)
    lo, hi = FIELDS[key][2], FIELDS[key][3]
    if not lo <= value <= hi:
        return f"{key} must be between {lo:g} and {hi:g}."
    store.execute("INSERT INTO profile(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, float(value)))
    audit.append("economy_profile", key=key, value=float(value))
    return f"Set <b>{FIELDS[key][1]}</b> to <b>{value:,.2f}</b>."


def compute(now: Optional[float] = None) -> Dict[str, float]:
    p = profile()
    monthly = p["essentials"] + p["irregular"] / 12.0
    target = monthly / max(1e-9, 1 - p["margin"] / 100.0)
    fund = monthly * p["reserve_months"]
    dep, months = ledger.dependable(now)
    return {"monthly_costs": round(monthly, 2), "target": round(target, 2), "emergency_fund": round(fund, 2),
            "dependable": round(dep, 2), "gap": round(max(0.0, target - dep), 2), "saved": p["saved"],
            "breathing_room": min(1.0, p["saved"] / fund) if fund else 1.0,
            "optionality": min(1.0, dep / (monthly * 0.5)) if monthly else 1.0,
            "independence": min(1.0, dep / target) if target else 1.0,
            "job_hours_replaced": round(dep / p["wage"], 1) if p["wage"] else 0.0}


def milestone(c: Dict[str, float]) -> str:
    if c["independence"] >= 1 and c["breathing_room"] >= 1:
        return "financial independence"
    if c["optionality"] >= 1:
        return "work optionality (next: financial independence)"
    if c["breathing_room"] >= 1:
        return "breathing room (next: work optionality)"
    return "building breathing room"


def placeholders() -> list:
    return [k for k in ("essentials", "irregular", "saved", "wage") if not is_set(k)]
