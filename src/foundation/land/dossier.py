"""src/foundation/land/dossier.py - LAND-001: turn a property reference plus evidence into a structured, versioned dossier.

    IDENTIFY -> COLLECT -> NORMALIZE -> PROVENANCE -> FIND GAPS -> UNDERWRITE -> EXIT ANALYSIS -> RED TEAM -> GATE -> STOP

build() is pure (no I/O besides what adapters do) and deterministic for a given `today`, so the same evidence always yields the
same dossier version: the sha256 of its canonical content, including the exposure authorisation it was evaluated under. An owner
approval (hub.py) names that version, so it can never silently apply to a dossier that changed after the owner read it.

STOP is a successful outcome: "we cannot establish legal access, tax exposure or lien status; gate locked" means LAND-001 worked.
Automation level 0: this module researches and recommends. It never contacts anyone, never spends, never commits.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import date
from typing import Dict, List, Optional, Sequence

from src.foundation.land import schema
from src.foundation.land.adapters import SALE_EXITS, Bundle, PropertyRef, SourceAdapter
from src.foundation.land.economics import summarize
from src.foundation.land.evidence import Claim, Source, SourceType, Tier, grade, meets, resolve
from src.foundation.land.screen import BLOCK, WARN, Finding, recommend, red_team, screen

ENGINE_VERSION = "land-001/0.2"
AUTOMATION_LEVEL = 0
DEFAULT_MAX_EXPOSURE_USD = 500.0


class NetworkNotAuthorised(PermissionError):
    """LandPilot's core never does network I/O. Web research runs as an ordinary Foundation mission (the land_intelligence
    domain pack) through the approval gate and mission contract; what it finds comes back as an evidence file."""


def default_authorization() -> dict:
    return {"source": "engine_default", "max_exposure_usd": DEFAULT_MAX_EXPOSURE_USD, "target_offer_usd": None}


def _merge(bundles: Sequence[Bundle]) -> Bundle:
    out = Bundle()
    seen: Dict[str, Source] = {}
    for b in bundles:
        for s in b.sources:
            if s.id in seen and seen[s.id] != s:
                raise ValueError(f"two adapters define source id {s.id!r} differently")
            seen[s.id] = s
        for k in ("claims", "exits", "tax_ledger", "adjacent", "counterparties", "costs", "revenue"):
            getattr(out, k).extend(getattr(b, k))
    out.sources = list(seen.values())
    return out


def _listed(fields: Dict[str, dict], name: str, use: str, *, min_tier: Tier) -> bool:
    f = fields.get(name)
    if not f or f["value"] is None or not meets(Tier(f["tier"]), min_tier):
        return False
    items = f["value"] if isinstance(f["value"], list) else [f["value"]]
    u = use.strip().lower()
    return bool(u) and any((x := str(i).strip().lower()) and (u in x or x in u) for i in items)


def _legal_use_status(x: dict, fields: Dict[str, dict]) -> str:
    """Only official zoning text (VERIFIED_SECONDARY or better) can make a use legal or prohibited."""
    if x["exit_type"] in SALE_EXITS:
        return "n/a (sale)"
    use = x["use"] or x["exit_type"]
    ok = Tier.VERIFIED_SECONDARY
    if _listed(fields, "prohibited_uses", use, min_tier=ok):
        return "prohibited"
    if _listed(fields, "permitted_uses", use, min_tier=ok):
        return "as_of_right"
    if _listed(fields, "conditional_uses", use, min_tier=ok):
        return "conditional"
    return "not_established"


def build(ref: PropertyRef, adapters: Sequence[SourceAdapter], *, mode: str = "candidate", authorization: Optional[dict] = None,
          today: Optional[date] = None) -> dict:
    for a in adapters:
        if a.network:
            raise NetworkNotAuthorised(f"adapter {a.name!r} needs the network; run that research as a mission instead")
    today = today or date.today()
    authorization = dict(authorization or default_authorization())
    b = copy.deepcopy(_merge([a.collect(ref) for a in adapters]))     # grading annotates claims; never mutate adapter state
    sources = {s.id: s for s in b.sources}

    by_field: Dict[str, List[Claim]] = {}
    for c in b.claims:
        spec = schema.FIELDS[c.field]
        c.unit = c.unit or spec.unit
        grade(c, sources, physical=c.field in schema.PHYSICAL, max_age_days=spec.max_age_days, today=today)
        by_field.setdefault(c.field, []).append(c)

    fields: Dict[str, dict] = {}
    for name, spec in schema.FIELDS.items():
        claims = by_field.get(name)
        if not claims:
            if not spec.material:
                continue
            claims = [grade(Claim(name, None, [], unit=spec.unit, notes="no evidence collected yet"), sources)]
        st = resolve(claims)
        if st.resolution_required:
            for c in claims:
                c.flags.append("conflicts with another claim on this field: resolution required")
        fields[name] = {"section": spec.section, "label": spec.label, "material": spec.material, "unit": spec.unit,
                        "required": spec.required.value if spec.required else None, "tier": st.tier.value, "status": st.status,
                        "value": st.value, "resolution_required": st.resolution_required, "disqualified": st.disqualified,
                        "leads": st.leads, "claims": [asdict(c) | {"tier": c.tier.value} for c in claims]}

    ledger_spec = schema.FIELDS["tax_balance_due"]
    ledger = [asdict(grade(r, sources, max_age_days=ledger_spec.max_age_days, today=today)) for r in b.tax_ledger]
    for r in ledger:
        r["tier"] = Tier(r["tier"]).value

    exits = []
    for x in b.exits:
        support = grade(Claim("exit", x["id"], list(x["sources"])), sources)
        legal = _legal_use_status(x, fields)
        evidenced = (legal in ("n/a (sale)", "as_of_right", "conditional") and x["demonstrated_need"] in ("high", "moderate")
                     and meets(support.tier, Tier.STRONGLY_INDICATED))
        exits.append(x | {"legal_use_status": legal, "evidence_tier": support.tier.value, "counterparty_identified": bool(x["counterparty"]),
                          "evidenced": evidenced})

    counterparties = [dict(c, interest="untested") for c in b.counterparties]
    counterparties += [{"name": a["owner"] or "(owner not recorded)", "type": "adjacent owner",
                        "basis": f"owns adjacent parcel {a['parcel_id']} ({a['land_use'] or 'use unknown'})",
                        "sources": a["sources"], "flags": a["flags"], "interest": "untested"} for a in b.adjacent]
    econ = summarize(b.costs, b.revenue, authorization)
    findings = screen(fields, exits, econ)

    steps, seen = [], set()
    for f in (f for f in findings if f.outcome == BLOCK):                          # already in priority order (screen.py)
        spec = schema.FIELDS.get(f.field)
        r = spec.resolve if spec else None
        if f.path in seen:
            continue
        seen.add(f.path)
        steps.append({"field": f.field, "why": f.reason, "action": f.path, "remote": f.remote,
                      "needs_web_mission": bool(r and r.network), "est_cost_usd": [r.cost_low, r.cost_high] if r else None})
    for name, f in fields.items():
        r = schema.FIELDS[name].resolve
        if r and not f["material"] and f["tier"] in ("UNKNOWN", "UNVERIFIED", "STALE", "CONTRADICTED") and f"{r.action} ({r.where})" not in seen:
            seen.add(f"{r.action} ({r.where})")
            steps.append({"field": name, "why": f"{f['label']}: {f['tier']} (not blocking)", "action": f"{r.action} ({r.where})",
                          "remote": r.remote, "needs_web_mission": r.network, "est_cost_usd": [r.cost_low, r.cost_high]})
    field_only = [s for s in steps if s["remote"] is False]
    inspection = ({"needed": True, "why": [s["why"] for s in field_only], "verify": [s["action"] for s in field_only]}
                  if field_only else
                  {"needed": False, "why": ["every open item has a remote resolution path; decide after those are exhausted"]})

    flagged = [(c["field"], fl) for f in fields.values() for c in f["claims"] for fl in c["flags"] if "conflict" not in fl]
    flagged += [(f"exit:{x['id']}", fl) for x in exits for fl in x["flags"]]
    flagged += [(f"{k}:{x.get('name') or x.get('parcel_id')}", fl) for k, xs in (("counterparty", b.counterparties), ("adjacent", b.adjacent))
                for x in xs for fl in x["flags"]]
    flagged += [(f"cost:{l.label}", fl) for l in b.costs + b.revenue for fl in l.flags]
    flagged += [(f"ledger:{r['field']}", fl) for r in ledger for fl in r["flags"]]
    flagged += [(f"source:{s.id}", "contains instruction-like text (kept as data, not acted on)") for s in b.sources
                if "instruction-like" in s.notes]
    for where, fl in flagged:
        if "instruction-like" in fl:
            findings.append(Finding(WARN, "untrusted input", f"{where}: record contains instruction-like text; shown as data, "
                                                           f"not acted on. Check the source for tampering or fraud."))
    llm_sources = [s.id for s in b.sources if s.type is SourceType.LLM_REPORT]
    dossier = {
        "schema": schema.SCHEMA_VERSION, "engine": ENGINE_VERSION, "automation_level": AUTOMATION_LEVEL,
        "property": asdict(ref) | {"key": ref.key}, "mode": mode, "evaluated_on": today.isoformat(),
        "fields": fields, "exits": exits, "tax_ledger": ledger, "adjacent": b.adjacent, "counterparties": counterparties,
        "economics": econ,
        "unknowns": [{"field": n, "label": f["label"], "tier": f["tier"], "material": f["material"], "required": f["required"],
                      "resolution_required": f["resolution_required"], "leads": f["leads"]}
                     for n, f in fields.items() if f["tier"] in ("UNKNOWN", "CONTRADICTED", "STALE", "UNVERIFIED")],
        "sources": [asdict(s) | {"type": s.type.value} for s in b.sources],
        "llm_sources_treated_as_leads": llm_sources,
        "findings": [asdict(f) for f in findings],
        "red_team": red_team(fields, econ, exits),
        "recommendation": recommend(findings, mode),
        "next_steps": steps, "inspection": inspection,
        "untrusted_flags": [{"where": w, "flag": fl} for w, fl in flagged],
    }
    dossier["version"] = version(dossier)
    return dossier


def canonical(d: dict) -> str:
    return json.dumps({k: v for k, v in d.items() if k != "version"}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def version(d: dict) -> str:
    return hashlib.sha256(canonical(d).encode("utf-8")).hexdigest()
