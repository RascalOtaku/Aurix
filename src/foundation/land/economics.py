"""src/foundation/land/economics.py - TRUE EXPOSURE, carrying cost, revenue, and how sure we are about each number.

    TRUE EXPOSURE = purchase consideration + delinquent taxes + recording/deed costs + required immediate work
                    + mandatory transaction costs + other known acquisition expenses

It is compared with the CASE's authorised maximum (an owner decision recorded per property; the engine default applies only when the
owner has not set one). There is no clever accounting around it:
  - an unknown one-time cost is never zero: exposure becomes a lower bound, and the gate stays locked until it is known
  - exposure that could exceed the maximum (high end over it) locks the gate; exposure that must exceed it (low end over it) rejects
  - the owner's target offer is a negotiation parameter, shown as headroom, never used as a valuation
Every line carries a basis (documented, strongly_supported, estimated, speculative, unknown). "documented" with no source is lowered to
"estimated". Only documented / strongly_supported revenue counts toward recovery; speculative and unknown revenue is never added up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

BASES = ("documented", "strongly_supported", "estimated", "speculative", "unknown")
ONE_TIME_CATEGORIES = ("purchase", "delinquent_taxes", "recording", "immediate_work", "transaction", "acquisition_other")
ANNUAL_CATEGORIES = ("tax", "insurance", "maintenance", "annual_other")
REVENUE_CATEGORIES = ("lease", "sale", "easement", "timber", "revenue_other")
_SUPPORTED = ("documented", "strongly_supported")


@dataclass
class Line:
    label: str
    kind: str                      # one_time | annual
    category: str
    low: Optional[float]
    high: Optional[float]
    basis: str
    sources: List[str] = field(default_factory=list)
    notes: str = ""
    flags: List[str] = field(default_factory=list)


def _normalise(line: Line) -> Line:
    if line.basis == "documented" and not line.sources:
        line.basis = "estimated"
        line.notes = (line.notes + " " if line.notes else "") + "[basis lowered: 'documented' with no source cited]"
    if line.low is None and line.high is None:
        line.basis = "unknown"
    elif line.low is None or line.high is None:
        line.low = line.high = line.low if line.low is not None else line.high
    elif line.low > line.high:
        raise ValueError(f"{line.label}: low > high")
    return line


def _sum(lines: List[Line]) -> Dict[str, object]:
    known = [l for l in lines if l.basis != "unknown"]
    return {"low": sum(l.low for l in known), "high": sum(l.high for l in known),
            "unknown": [l.label for l in lines if l.basis == "unknown"]}


def _bound(s: dict) -> str:
    rng = f"${s['low']:,.0f}" if s["low"] == s["high"] else f"${s['low']:,.0f}–${s['high']:,.0f}"
    return ("at least " if s["unknown"] else "") + rng + (f" + {len(s['unknown'])} unknown item(s)" if s["unknown"] else "")


def summarize(costs: List[Line], revenue: List[Line], authorization: dict) -> dict:
    """authorization: {"max_exposure_usd", "target_offer_usd" (or None), "source": "case" | "engine_default", ...}."""
    cap = float(authorization["max_exposure_usd"])
    costs = [_normalise(l) for l in costs]
    revenue = [_normalise(l) for l in revenue]
    one_time = _sum([l for l in costs if l.kind == "one_time"])
    annual = _sum([l for l in costs if l.kind == "annual"])
    rev_ann = [l for l in revenue if l.kind == "annual"]
    supported = _sum([l for l in rev_ann if l.basis in _SUPPORTED])
    estimated = _sum([l for l in rev_ann if l.basis == "estimated"])

    net_low = supported["low"] - annual["high"]            # the pessimistic end: least revenue, most cost
    if one_time["unknown"] or annual["unknown"]:
        recovery = "not computable: unknown cost items"
    elif net_low <= 0:
        recovery = "no supported path: documented revenue does not cover carrying cost"
    else:
        recovery = f"{one_time['high'] / net_low:.1f} years (pessimistic)"

    target = authorization.get("target_offer_usd")
    non_purchase = _sum([l for l in costs if l.kind == "one_time" and l.category != "purchase"])
    headroom = None
    if target is not None:
        headroom = {"target_offer_usd": target, "room_for_other_costs_usd": cap - target,
                    "other_costs": non_purchase, "other_costs_text": _bound(non_purchase),
                    "fits": (not non_purchase["unknown"]) and target + non_purchase["high"] <= cap}
    if one_time["unknown"]:
        exposure_status = "NOT ESTABLISHED"
    elif one_time["low"] > cap:
        exposure_status = "OVER CAP"
    elif one_time["high"] > cap:
        exposure_status = "MAY EXCEED CAP"
    else:
        exposure_status = "WITHIN CAP"
    return {
        "authorization": authorization, "cap_usd": cap,
        "true_exposure": one_time, "true_exposure_text": _bound(one_time), "true_exposure_status": exposure_status,
        "by_category": {c: _sum([l for l in costs if l.kind == "one_time" and l.category == c]) for c in ONE_TIME_CATEGORIES},
        "annual_carrying": annual, "annual_carrying_text": _bound(annual),
        "revenue_supported": supported, "revenue_estimated": estimated,
        "revenue_excluded": [l.label for l in revenue if l.basis in ("speculative", "unknown")],
        "target": headroom, "recovery": recovery,
        "lines": {"costs": [l.__dict__ for l in costs], "revenue": [l.__dict__ for l in revenue]},
    }
