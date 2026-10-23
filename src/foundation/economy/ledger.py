"""economy/ledger.py - what actually happened to the money, kept apart from what anyone hoped would happen.

Every revenue row has a status, and they are never added together:
    projected   a forecast someone wrote down (never counts toward anything you could spend)
    booked      a sale agreed or invoiced, not paid yet
    collected   cash actually received
Costs are money actually paid out; a refund is money given back to a customer (it reduces collected revenue in the month it is
paid back, not the month of the sale). Monthly close, per month:
    operating profit = collected revenue - refunds - costs     (can be negative: a loss month is shown, never hidden)
    tax reserve      = operating profit x tax rate (profile `tax`, default 25%) - set aside, never "available"
    available        = operating profit - tax reserve   <- the only number the freedom math uses
The tax reserve is only taken from a profitable month; losses are not carried forward against later months (simple and
conservative - the real tax position is for the tax return, not this ledger).

"Dependable income" is a deliberately conservative RULE (the worst of the last three complete months, empty months = $0), not a
statistical forecast: with a handful of months there is no reliable forecast to make. history_months() says how much record there is.

Self-employment income can bring estimated-tax obligations; the reserve is a planning number, not tax advice.
AURIX never moves money: every row is the owner telling AURIX what happened (or a mirror of `earned`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.foundation import audit
from src.foundation.economy import store

KINDS = ("revenue", "refund", "cost", "distribution")
STATUSES = ("projected", "booked", "collected")
DEPENDABLE_MONTHS = 3


def _local(ts: float) -> datetime:
    from src.foundation import standing
    return standing.local_now(ts)


def month_of(ts: float) -> str:
    return _local(ts).strftime("%Y-%m")


def add(kind: str, amount: float, opp_id: Optional[str] = None, status: str = "collected", note: str = "",
        at: Optional[float] = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if kind != "revenue" and status != "collected":
        raise ValueError("costs and distributions are recorded when actually paid")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("amount must be positive")
    row = {"id": store.new_id("t"), "opp_id": opp_id, "kind": kind, "status": status, "amount": amount,
           "note": (note or "")[:200], "at": at or store.now()}
    store.execute("INSERT INTO transactions(id, opp_id, kind, status, amount, note, at) VALUES(?,?,?,?,?,?,?)",
                  (row["id"], opp_id, kind, status, amount, row["note"], row["at"]))
    audit.append("economy_txn", id=row["id"], opp=opp_id or "", kind=kind, status=status, amount=amount)
    return row


# Where the owner's time goes. Production is the work itself; the others are what quietly turns a "profitable" service into a job.
HOUR_CATEGORIES = ("production", "acquisition", "revision", "support", "admin")


def log_hours(hours: float, opp_id: Optional[str] = None, note: str = "", at: Optional[float] = None,
              category: str = "production") -> None:
    hours = round(float(hours), 2)
    if not 0 < hours <= 200:
        raise ValueError("hours must be between 0 and 200")
    if category not in HOUR_CATEGORIES:
        raise ValueError(f"category must be one of {HOUR_CATEGORIES}")
    store.execute("INSERT INTO hours(opp_id, hours, note, at, category) VALUES(?,?,?,?,?)",
                  (opp_id, hours, (note or "")[:200], at or store.now(), category))


def hours_by_category(opp_id: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in store.rows("SELECT hours, category FROM hours WHERE opp_id = ?", (opp_id,)):
        out[r["category"]] = round(out.get(r["category"], 0.0) + r["hours"], 2)
    return out


def _in_month(ym: str, table: str = "transactions") -> List[dict]:
    return [r for r in store.rows(f"SELECT * FROM {table}") if month_of(r["at"]) == ym]


def tax_rate() -> float:
    from src.foundation.economy import freedom
    return freedom.profile()["tax"] / 100.0


def month_summary(ym: str, opp_id: Optional[str] = None) -> Dict[str, float]:
    txns = [r for r in _in_month(ym) if opp_id is None or r["opp_id"] == opp_id]
    rev = {s: round(sum(r["amount"] for r in txns if r["kind"] == "revenue" and r["status"] == s), 2) for s in STATUSES}
    rev["collected"] = round(rev["collected"] - sum(r["amount"] for r in txns if r["kind"] == "refund"), 2)
    costs = round(sum(r["amount"] for r in txns if r["kind"] == "cost"), 2)
    dist = round(sum(r["amount"] for r in txns if r["kind"] == "distribution"), 2)
    profit = round(rev["collected"] - costs, 2)
    reserve = round(max(0.0, profit) * tax_rate(), 2)
    hrs = round(sum(r["hours"] for r in _in_month(ym, "hours") if opp_id is None or r["opp_id"] == opp_id), 2)
    return {"projected": rev["projected"], "booked": rev["booked"], "collected": rev["collected"], "costs": costs,
            "operating_profit": profit, "tax_reserve": reserve, "available": round(profit - reserve, 2),
            "distributions": dist, "hours": hrs}


def previous_months(n: int, now: Optional[float] = None) -> List[str]:
    """The n most recent COMPLETE months before the current one, newest first."""
    d = _local(now or store.now())
    y, m = d.year, d.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out


def dependable(now: Optional[float] = None) -> Tuple[float, List[Tuple[str, float]]]:
    """Dependable monthly income: the LOWEST 'available' of the last 3 complete months (a month with nothing counts as $0).
    Deliberately conservative - one good month is not dependable income, and neither is a forecast."""
    months = [(ym, month_summary(ym)["available"]) for ym in previous_months(DEPENDABLE_MONTHS, now)]
    return max(0.0, min(v for _, v in months)), months


def venture_totals(opp_id: str) -> Dict[str, float]:
    txns = store.rows("SELECT * FROM transactions WHERE opp_id = ?", (opp_id,))
    hrs = store.rows("SELECT hours FROM hours WHERE opp_id = ?", (opp_id,))
    collected = round(sum(r["amount"] for r in txns if r["kind"] == "revenue" and r["status"] == "collected")
                      - sum(r["amount"] for r in txns if r["kind"] == "refund"), 2)
    sales = sum(1 for r in txns if r["kind"] == "revenue" and r["status"] == "collected")
    booked = round(sum(r["amount"] for r in txns if r["kind"] == "revenue" and r["status"] == "booked"), 2)
    costs = round(sum(r["amount"] for r in txns if r["kind"] == "cost"), 2)
    hours = round(sum(r["hours"] for r in hrs), 2)
    net = round(collected - costs, 2)
    return {"collected": collected, "booked": booked, "costs": costs, "net": net, "hours": hours, "sales": sales,
            "per_hour": round(net / hours, 2) if hours else None}


def history_months(now: Optional[float] = None) -> int:
    """Complete months since the first ledger row (0 = less than one full month of records)."""
    first = store.one("SELECT MIN(at) AS t FROM transactions")
    if not first or first["t"] is None:
        return 0
    a, b = _local(first["t"]), _local(now or store.now())
    return max(0, (b.year - a.year) * 12 + (b.month - a.month))


def totals(now: Optional[float] = None) -> Dict[str, float]:
    """All-time: sales collected, cash collected (net of refunds), costs, hours."""
    txns = store.rows("SELECT * FROM transactions")
    collected = sum(r["amount"] for r in txns if r["kind"] == "revenue" and r["status"] == "collected")
    refunds = sum(r["amount"] for r in txns if r["kind"] == "refund")
    return {"sales": sum(1 for r in txns if r["kind"] == "revenue" and r["status"] == "collected"),
            "collected": round(collected - refunds, 2), "costs": round(sum(r["amount"] for r in txns if r["kind"] == "cost"), 2),
            "hours": round(sum(r["hours"] for r in store.rows("SELECT hours FROM hours")), 2)}


def recent(limit: int = 8) -> List[dict]:
    return store.rows("SELECT * FROM transactions ORDER BY at DESC LIMIT ?", (limit,))
