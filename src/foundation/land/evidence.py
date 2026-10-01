"""src/foundation/land/evidence.py - facts with provenance, and the tier each fact has EARNED from its sources.

    SOURCE  ->  CLAIM  ->  EVIDENCE RULES (code)  ->  TIER  ->  DOSSIER

Nobody assigns a tier: not an analyst, not an adapter, not a model. Input claims carry a value, a unit, source ids and an as-of date;
this module computes the tier from the TYPE of the sources behind it:

    VERIFIED_PRIMARY    a primary record (recorded instrument, certified ledger, ordinance text), or a dated first-hand observation of a
                        physical fact
    VERIFIED_SECONDARY  an official secondary source: county GIS layer, official portal readout, published legal notice
    STRONGLY_INDICATED  two or more independent non-official sources agree
    UNVERIFIED          one non-official source
    STALE               the data's own date (as_of, not when we retrieved it) is too old for a time-sensitive field, or unknown
    CONTRADICTED        (field level) claims disagree; each claim is kept, resolution by stronger evidence is REQUIRED
    UNKNOWN             no value, or only a model's report of one

An LLM report is a LEAD, never evidence: it can raise a conflict (something is off, go and check), but it never supports a value, never
counts as a second source, and a field backed only by LLM reports stays UNKNOWN.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, List, Optional, Sequence


class Tier(str, Enum):
    VERIFIED_PRIMARY = "VERIFIED_PRIMARY"
    VERIFIED_SECONDARY = "VERIFIED_SECONDARY"
    STRONGLY_INDICATED = "STRONGLY_INDICATED"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"


# Tiers that SUPPORT a value, strongest first. STALE, CONTRADICTED and UNKNOWN never satisfy a requirement.
SUPPORTING = (Tier.VERIFIED_PRIMARY, Tier.VERIFIED_SECONDARY, Tier.STRONGLY_INDICATED, Tier.UNVERIFIED)
_ORDER = SUPPORTING + (Tier.STALE, Tier.CONTRADICTED, Tier.UNKNOWN)


def meets(tier: Tier, required: Tier) -> bool:
    return tier in SUPPORTING and SUPPORTING.index(tier) <= SUPPORTING.index(required)


def strongest(tiers: Sequence[Tier]) -> Tier:
    return min(tiers, key=_ORDER.index)


class SourceType(str, Enum):
    PRIMARY_RECORD = "primary_record"          # the record itself from its custodian
    OFFICIAL_SECONDARY = "official_secondary"  # official but derived: GIS layer, portal readout, published notice
    OBSERVATION = "observation"                # dated first-hand observation (primary for PHYSICAL facts only)
    THIRD_PARTY = "third_party"                # listings, aggregators, news, historical narrative
    TRANSCRIBED = "transcribed"                # our own notes copying a record whose original was not saved
    REPORTED = "reported"                      # someone said so (includes the owner's brief)
    LLM_REPORT = "llm_report"                  # a model's research output: a lead, never evidence


@dataclass(frozen=True)
class Source:
    id: str
    type: SourceType
    title: str
    custodian: str = ""
    retrieved_at: str = ""     # when WE looked
    as_of: str = ""            # the date the data describes; a record retrieved today may describe 2017
    locator: str = ""
    notes: str = ""
    claimed_type: str = ""     # what a mission draft SAID this source was; kept for the owner, never trusted (see adapters.py)


@dataclass
class Claim:
    field: str
    value: object              # None = "we do not know": a valid, permanent state until evidence arrives
    sources: List[str]
    unit: str = ""
    as_of: str = ""
    notes: str = ""
    disqualifying: bool = False    # "this fact kills the deal" - only honoured when the claim is VERIFIED_PRIMARY
    # computed by grade()
    tier: Tier = Tier.UNKNOWN
    status: str = "unknown"        # current | stale | unknown | lead
    why: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


def parse_date(s: str) -> Optional[date]:
    """'YYYY-MM-DD', 'YYYY-MM' or 'YYYY'. Partial dates resolve to the END of the period (the most generous reading of freshness)."""
    s = (s or "").strip()
    try:
        if len(s) == 4:
            return date(int(s), 12, 31)
        if len(s) == 7:
            y, m = int(s[:4]), int(s[5:7])
            return date(y, m, calendar.monthrange(y, m)[1])
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def grade(claim: Claim, sources: Dict[str, Source], *, physical: bool = False, max_age_days: Optional[int] = None,
          today: Optional[date] = None) -> Claim:
    """Compute claim.tier / status / why from its sources. Mutates and returns the claim."""
    missing = [s for s in claim.sources if s not in sources]
    if missing:
        claim.flags.append(f"cites unknown source id(s) {', '.join(missing)}; ignored")
        claim.sources = [s for s in claim.sources if s in sources]
    if claim.value is None:
        claim.tier, claim.status = Tier.UNKNOWN, "unknown"
        claim.why.append("value not known")
        return claim
    srcs = [sources[s] for s in claim.sources]
    evidence = [s for s in srcs if s.type is not SourceType.LLM_REPORT]
    types = {s.type for s in evidence}
    if not evidence:
        claim.tier, claim.status = Tier.UNVERIFIED, "lead"
        claim.why.append("only a model's report: a lead to check, not evidence" if srcs else "no source cited")
        return claim
    if SourceType.PRIMARY_RECORD in types:
        claim.tier = Tier.VERIFIED_PRIMARY
        claim.why.append("primary record")
    elif SourceType.OBSERVATION in types and physical:
        claim.tier = Tier.VERIFIED_PRIMARY
        claim.why.append("dated first-hand observation of a physical fact")
    elif SourceType.OFFICIAL_SECONDARY in types:
        claim.tier = Tier.VERIFIED_SECONDARY
        claim.why.append("official secondary source")
    elif len({s.id for s in evidence}) >= 2:
        claim.tier = Tier.STRONGLY_INDICATED
        claim.why.append(f"{len(evidence)} independent non-official sources agree")
    else:
        claim.tier = Tier.UNVERIFIED
        claim.why.append(f"single {evidence[0].type.value.replace('_', ' ')} source")
    claim.status = "current"
    if max_age_days is not None:
        vintage = parse_date(claim.as_of) or min((d for d in (parse_date(s.as_of) for s in evidence) if d), default=None)
        today = today or date.today()
        if vintage is None:
            claim.tier, claim.status = Tier.STALE, "stale"
            claim.why.append("time-sensitive and the data's own date is unknown (retrieval date is not the data's date)")
        elif (today - vintage).days > max_age_days:
            claim.tier, claim.status = Tier.STALE, "stale"
            claim.why.append(f"data as of {vintage.isoformat()} is older than {max_age_days} days for this field")
    if claim.disqualifying and claim.tier is not Tier.VERIFIED_PRIMARY:
        claim.flags.append("marked disqualifying, but only a primary record can disqualify; held as blocking instead")
    return claim


def _norm(v: object) -> object:
    return " ".join(str(v).lower().split()) if isinstance(v, str) else v


@dataclass
class FieldState:
    tier: Tier
    value: object
    status: str                                           # current | stale | conflicted | unknown
    resolution_required: bool = False
    disqualified: bool = False
    leads: List[object] = field(default_factory=list)     # values only a model reported: to be checked


def resolve(claims: Sequence[Claim]) -> FieldState:
    """One field's state from its graded claims. Conflicts are never settled by picking a side."""
    known = [c for c in claims if c.value is not None]
    leads = [c.value for c in known if c.status == "lead"]
    if len({repr(_norm(c.value)) for c in known}) > 1:
        return FieldState(Tier.CONTRADICTED, None, "conflicted", resolution_required=True, leads=leads)
    support = [c for c in known if c.status in ("current", "stale")]
    if not support:
        return FieldState(Tier.UNKNOWN, None, "unknown", leads=leads)
    best = min(support, key=lambda c: _ORDER.index(c.tier))
    return FieldState(best.tier, best.value, best.status, leads=leads,
                      disqualified=any(c.disqualifying and c.tier is Tier.VERIFIED_PRIMARY for c in support))
