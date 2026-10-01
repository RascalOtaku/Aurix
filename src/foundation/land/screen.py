"""src/foundation/land/screen.py - the kill list, the red team, and the acquisition gate's lock.

Each finding is one of:
  KILL   a known deal killer backed by a primary record, or exposure that MUST exceed the case's authorised maximum
  BLOCK  something that must be established before the gate can open: a material field below its required tier, a conflict that
         needs resolving, an exposure that is unknown or could exceed the maximum, no evidenced exit
  WARN   something the owner must see that does not by itself lock the gate

The gate is LOCKED unless there is no KILL and no BLOCK - and even then it only becomes ELIGIBLE: nothing opens it except a separate,
explicit owner approval of one card for one dossier version (hub.py). A recommendation, a high tier or "no major risks found" never
unlocks anything. A BLOCK says how to resolve it, whether it can be done remotely, and roughly what it costs, which is how a
complicated-but-solvable parcel is told apart from a complicated-and-uneconomic one.

The red team is deterministic: rules in code that attack the deal from the evidence, each with what would disprove it. No model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from src.foundation.land import schema
from src.foundation.land.evidence import Tier, meets

KILL, BLOCK, WARN = "KILL", "BLOCK", "WARN"
CARRY_WARN_RATIO = 0.5

# Plain-language names for the kill-list categories in the project brief.
_CATEGORY = {
    "legal_access": "legal access", "owner_of_record": "ownership", "vesting_instrument": "ownership",
    "title_chain": "title", "deeds_of_trust": "liens", "dot_releases": "liens", "judgments": "liens", "tax_liens": "liens",
    "foreclosure": "title", "annual_tax": "tax burden", "tax_delinquent": "tax burden", "tax_balance_due": "tax burden",
    "tax_sale_status": "tax burden", "zoning_district": "use legality", "permitted_uses": "use legality",
    "flood_zone": "environmental restriction", "acreage": "parcel geometry", "parcel_id": "identity", "county": "identity",
    "state": "identity",
}


@dataclass
class Finding:
    outcome: str
    rule: str
    reason: str
    field: str = ""
    path: str = ""                 # for BLOCK: how it gets resolved
    remote: Optional[bool] = None
    cost_high: int = 0


def screen(fields: Dict[str, dict], exits: List[dict], econ: dict) -> List[Finding]:
    out: List[Finding] = []
    for name, f in fields.items():
        spec = schema.FIELDS[name]
        if not spec.material:
            continue
        cat, tier, req = _CATEGORY.get(name, name), Tier(f["tier"]), Tier(f["required"])
        r = spec.resolve
        path = f"{r.action} ({r.where})" if r else "no standard resolution path; needs analyst judgement"
        if f["disqualified"]:
            out.append(Finding(KILL, cat, f"{spec.label}: {f['value']} (primary record)", name))
        elif tier is Tier.CONTRADICTED:
            out.append(Finding(BLOCK, cat, f"{spec.label}: sources conflict; resolve with {req.value} evidence", name, path,
                               r.remote if r else None, r.cost_high if r else 0))
        elif not meets(tier, req):
            have = "unknown" if tier is Tier.UNKNOWN else tier.value
            out.append(Finding(BLOCK, cat, f"{spec.label}: needs {req.value}, have {have}", name, path,
                               r.remote if r else None, r.cost_high if r else 0))

    cap, st = econ["cap_usd"], econ["true_exposure_status"]
    exp, carry = econ["true_exposure"], econ["annual_carrying"]
    if st == "OVER CAP":
        out.append(Finding(KILL, "capital beyond authorised exposure",
                           f"true exposure is at least ${exp['low']:,.0f}, over the ${cap:,.0f} maximum for this case"))
    elif st == "MAY EXCEED CAP":
        out.append(Finding(BLOCK, "true exposure may exceed the maximum",
                           f"true exposure ranges up to ${exp['high']:,.0f}, over the ${cap:,.0f} maximum for this case",
                           path="Pin down the high-end cost items", remote=True))
    elif st == "NOT ESTABLISHED":
        out.append(Finding(BLOCK, "true exposure not established",
                           f"{len(exp['unknown'])} one-time cost item(s) unknown: " + "; ".join(exp["unknown"]),
                           path="Get each unknown cost from its source (treasurer payoff, clerk fees, settlement quote)", remote=True))
    if carry["low"] >= cap:
        out.append(Finding(KILL, "uneconomic carrying cost",
                           f"annual carrying cost is at least ${carry['low']:,.0f}, at or above the ${cap:,.0f} maximum"))
    elif carry["high"] >= CARRY_WARN_RATIO * cap:
        out.append(Finding(WARN, "carrying cost vs exposure",
                           f"annual carrying cost up to ${carry['high']:,.0f} is {carry['high'] / cap:.0%} of the ${cap:,.0f} maximum, every year"))
    t = econ.get("target")
    if t and not t["fits"]:
        out.append(Finding(WARN, "target offer vs other costs",
                           f"at the ${t['target_offer_usd']:,.0f} target offer, other one-time costs ({t['other_costs_text']}) must stay "
                           f"within ${t['room_for_other_costs_usd']:,.0f}; not yet shown"))

    if exits and all(x["legal_use_status"] == "prohibited" for x in exits):
        out.append(Finding(KILL, "every exit prohibited", "every proposed exit is a prohibited use"))
    elif not any(x["evidenced"] for x in exits):
        out.append(Finding(BLOCK, "no evidenced exit",
                           "no exit is legal, needed (high/moderate) and supported by at least STRONGLY_INDICATED evidence",
                           path="Map adjacent owners and land users; confirm the use is allowed (LAND-005)", remote=True))

    def priority(f: Finding) -> int:                  # what matters most first: conflicts, then primary-evidence gates, then the rest
        if f.outcome != BLOCK:
            return 0
        spec = schema.FIELDS.get(f.field)
        return 1 if "conflict" in f.reason else 2 if spec and spec.required is Tier.VERIFIED_PRIMARY else 3
    out.sort(key=priority)

    by_where: Dict[str, int] = {}                    # one clerk subscription covers every clerk search: count each custodian once
    for f in out:
        if f.outcome == BLOCK:
            where = f.path.rsplit("(", 1)[-1]
            by_where[where] = max(by_where.get(where, 0), f.cost_high)
    research = sum(by_where.values())
    if research > cap:
        out.append(Finding(WARN, "research cost vs exposure",
                           f"resolving the blocking items may cost up to ${research:,.0f}, more than the ${cap:,.0f} maximum: "
                           f"likely complicated AND uneconomic at this cap"))
    return out


# ------------------------------------------------------------------------------------------------------------------------------
# red team
# ------------------------------------------------------------------------------------------------------------------------------

def _below(fields: Dict[str, dict], name: str) -> bool:
    f = fields.get(name)
    return f is None or not meets(Tier(f["tier"]), Tier(f["required"] or Tier.VERIFIED_SECONDARY))


def _known(fields: Dict[str, dict], name: str) -> bool:
    f = fields.get(name)
    return bool(f) and f["value"] is not None


_RED_TEAM: List[tuple] = [
    (lambda F, E, X: _known(F, "deeds_of_trust") and _below(F, "dot_releases"),
     "A recorded deed of trust may still be a live lien. Buying could mean inheriting a payoff demand, or losing the land to foreclosure.",
     "A recorded release or certificate of satisfaction for each deed of trust."),
    (lambda F, E, X: _below(F, "tax_balance_due") or _below(F, "tax_sale_status"),
     "Delinquent taxes grow with penalties, interest and collection costs; a judicial tax sale could transfer the parcel before or during a purchase.",
     "A current certified payoff and written confirmation of the sale/suit status from the Treasurer or tax-sale counsel."),
    (lambda F, E, X: _below(F, "legal_access"),
     "Road adjacency is not legal access. Without a recorded right of way the parcel may be landlocked and close to unsellable.",
     "Deed/plat frontage on a publicly maintained road, or a recorded access easement."),
    (lambda F, E, X: F.get("zoning_district", {}).get("tier") == Tier.CONTRADICTED.value or _below(F, "permitted_uses"),
     "The intended use may not be allowed. Zoning sources disagree or the ordinance has not been read.",
     "The official zoning map and the district's use table from the county."),
    (lambda F, E, X: _below(F, "flood_zone"),
     "Part of the land may be in a flood hazard area, limiting structures and some uses and affecting value.",
     "The FEMA flood map panel for the parcel."),
    (lambda F, E, X: _below(F, "title_chain") or _below(F, "vesting_instrument"),
     "The seller may not be able to convey clean title (chain gaps, recording discrepancies, entity or estate issues).",
     "A title search or report covering the chain back 40-60 years."),
    (lambda F, E, X: _known(F, "hazards"),
     "Known hazards on site create premises liability and cleanup duties for the next owner.",
     "A costed plan to secure or remove the hazards, and insurance quotes."),
    (lambda F, E, X: not any(x["counterparty"] for x in X),
     "No buyer or user has been identified; the exit is a hypothesis, not a plan.",
     "An identified counterparty with a demonstrated need (contacted only after owner approval)."),
    (lambda F, E, X: E["true_exposure_status"] != "WITHIN CAP",
     "True exposure is not proven to fit the authorised maximum; hidden costs are how cheap land becomes expensive.",
     "Every one-time cost line known and summing within the case maximum."),
    (lambda F, E, X: any(f["status"] == "stale" for f in F.values() if f["material"]),
     "Some material facts come from old or undated data; the situation may have changed.",
     "Fresh retrievals with the data's own as-of date."),
]


def red_team(fields: Dict[str, dict], econ: dict, exits: List[dict]) -> List[dict]:
    return [{"attack": attack, "disproved_by": disprove} for cond, attack, disprove in _RED_TEAM if cond(fields, econ, exits)]


def recommend(findings: List[Finding], mode: str) -> dict:
    kills = [f for f in findings if f.outcome == KILL]
    blocks = [f for f in findings if f.outcome == BLOCK]
    if kills:
        verdict, headline = "REJECT", "Rejected: " + "; ".join(sorted({f.rule for f in kills}))
    elif blocks:
        verdict, headline = "RESEARCH_MORE", f"{len(blocks)} item(s) must be established before the gate can open"
    else:
        verdict, headline = "PRESENT_TO_OWNER", "Nothing blocks: eligible for an acquisition card (Gate 1)"
    gate = "ELIGIBLE: awaiting an explicit owner approval" if verdict == "PRESENT_TO_OWNER" and mode != "research_fixture" else "LOCKED"
    rec = {"verdict": verdict, "headline": headline, "gate": gate,
           "reasons": [f"{f.outcome} · {f.rule} · {f.reason}" for f in findings],
           "authority": "Recommendation only. AURIX does not contact anyone, make offers, or buy. The owner decides."}
    if mode == "research_fixture":
        rec["restriction"] = "RESEARCH FIXTURE: no contact with the owner, no offer, no purchase, regardless of the verdict."
    return rec
