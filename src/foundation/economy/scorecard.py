"""economy/scorecard.py - every opportunity measured the same way, so the newest idea never wins just by being new.

HARD CONSTRAINTS come first and no score can override them (limits from the freedom profile):
    startup cash      <= test_cash      cash that could be lost before validation
    max credible loss <= max_loss
    owner hours       fit inside `hours` together with every venture already running
    legal             not known to be a problem (and must be CHECKED - evidence `legal_ok` - before approval)

Then a 0-100 score from the same ten questions for everyone (weights in WEIGHTS, printed with every score):
    demand evidence   none 0 · interest .3 · presale .6 · paid 1 sale .85 · paid repeatedly 1.0   (from evidence + the ledger)
    net contribution  projected monthly net x calibration x (0.25 + 0.75 x demand): an unproven projection is mostly discounted,
                      and `calibration` is how past forecasts compared with what really happened (learning.py)
    speed             time to first revenue
    hours fit         owner hours against the weekly budget
    automation, durability, reversibility, strategic value   0..1 judgements, shown so they can be argued with
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.foundation.economy import freedom, ledger, store

EVIDENCE_KINDS = ("research", "competitor", "customer_interest", "presale", "payment", "cost_quote", "legal_ok",
                  "legal_problem", "build", "risk", "lesson", "hypothesis")
# Observed facts must say where they were observed: research/competitor evidence without a source is kept, but it does not open a
# stage gate. `hypothesis` is the honest home for a guess - recorded, shown, and never counted as evidence of anything.
NEEDS_SOURCE = ("research", "competitor")
DEMAND_LEVELS = {"none": 0.0, "interest": 0.3, "presale": 0.6, "paid": 0.85, "recurring": 1.0}
WEIGHTS = {"demand": 25, "contribution": 20, "speed": 10, "hours_fit": 10, "automation": 10, "durability": 10,
           "reversibility": 5, "strategic": 10}
RUNNING = ("approved", "built", "launched", "measured", "scaled", "modified")


def evidence(opp_id: str) -> List[dict]:
    return store.rows("SELECT * FROM evidence WHERE opp_id = ? ORDER BY at", (opp_id,))


def demand(opp_id: str) -> str:
    kinds = [x["kind"] for x in evidence(opp_id)]
    paid_months = {ledger.month_of(r["at"]) for r in store.rows(
        "SELECT at FROM transactions WHERE opp_id = ? AND kind = 'revenue' AND status = 'collected'", (opp_id,))}
    payments = kinds.count("payment") + len(paid_months)
    if len(paid_months) >= 2 or kinds.count("payment") >= 2:
        return "recurring"
    if payments:
        return "paid"
    if "presale" in kinds:
        return "presale"
    if "customer_interest" in kinds:
        return "interest"
    return "none"


def legal(opp_id: str) -> str:
    kinds = {x["kind"] for x in evidence(opp_id)}
    return "problem" if "legal_problem" in kinds else ("checked" if "legal_ok" in kinds else "unchecked")


def hours_committed(exclude: Optional[str] = None) -> float:
    return sum(r["owner_hours"] for r in store.rows("SELECT id, owner_hours, stage FROM opportunities")
               if r["stage"] in RUNNING and r["id"] != exclude)


def constraints(opp: dict, p: Optional[Dict[str, float]] = None) -> List[str]:
    """Hard-constraint failures (empty list = passes). A high score never overrides these."""
    p = p or freedom.profile()
    out = []
    if opp["startup_cash"] > p["test_cash"]:
        out.append(f"risks ${opp['startup_cash']:,.0f} before validation; your test limit is ${p['test_cash']:,.0f}")
    if opp["max_loss"] > p["max_loss"]:
        out.append(f"could lose ${opp['max_loss']:,.0f}; your loss limit is ${p['max_loss']:,.0f}")
    used = hours_committed(exclude=opp["id"])
    if used + opp["owner_hours"] > p["hours"]:
        out.append(f"needs {opp['owner_hours']:g} h/week; only {max(0.0, p['hours'] - used):g} of your {p['hours']:g} h are free")
    if legal(opp["id"]) == "problem":
        out.append("a legal problem is on record")
    return out


def score(opp: dict, p: Optional[Dict[str, float]] = None, calibration: Optional[float] = None) -> Tuple[float, Dict[str, float]]:
    from src.foundation.economy import learning
    p = p or freedom.profile()
    cal = learning.calibration() if calibration is None else calibration
    d = DEMAND_LEVELS[demand(opp["id"])]
    mid = (opp["net_low"] + opp["net_high"]) / 2.0
    effective = mid * cal * (0.25 + 0.75 * d)
    target = freedom.compute()["target"]
    parts = {
        "demand": d,
        "contribution": max(0.0, min(1.0, effective / max(1.0, 0.25 * target))),
        "speed": 1.0 / (1.0 + max(0.0, opp["months_to_revenue"]) / 2.0),
        "hours_fit": max(0.0, 1.0 - opp["owner_hours"] / p["hours"]) if p["hours"] else 0.0,
        "automation": _unit(opp["automation"]), "durability": _unit(opp["durability"]),
        "reversibility": _unit(opp["reversibility"]), "strategic": _unit(opp["strategic"]),
    }
    total = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
    parts["effective_net"] = round(effective, 2)
    return total, parts


def _unit(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def next_test(opp: dict) -> str:
    """The cheapest next experiment that removes the biggest uncertainty - a $30 test before a $300 build."""
    d, lg, stage = demand(opp["id"]), legal(opp["id"]), opp["stage"]
    kinds = {x["kind"] for x in evidence(opp["id"])}
    if stage == "discovered":
        return "desk research (free, no contact): who sells this now, at what price, and what customers complain about"
    if lg == "unchecked":
        return "check the rules first (free): permitted use of the records/data, licensing, platform terms - log `legal_ok` or `legal_problem`"
    if d == "none":
        return "a demand test: one sample deliverable plus a small, approved outreach or listing to see if anyone asks for it"
    if d in ("interest", "presale") and "build" not in kinds:
        return "a paid pilot: deliver one real order by hand before automating anything"
    if d == "paid":
        return "repeatability: a second paying customer, and the real hours each delivery took"
    return "standardise and automate the steps that repeat, then measure hours saved"


def ranked() -> List[dict]:
    p = freedom.profile()
    from src.foundation.economy import learning
    cal = learning.calibration()
    out = []
    for opp in store.rows("SELECT * FROM opportunities WHERE stage != 'closed'"):
        total, parts = score(opp, p, cal)
        out.append({**opp, "score": total, "parts": parts, "blocked": constraints(opp, p), "demand": demand(opp["id"]),
                    "legal": legal(opp["id"])})
    return sorted(out, key=lambda r: (bool(r["blocked"]), -r["score"], r["id"]))
