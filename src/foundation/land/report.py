"""src/foundation/land/report.py - the human-readable dossier. Every section, every fact with its tier, status and sources.

All adapter text was already cleaned on the way in; here it is additionally escaped for Markdown/HTML so a record cannot inject
links, markup or table breaks into the owner's report.
"""
from __future__ import annotations

import html
from typing import List

from src.foundation.land import schema

_FIELD_SECTIONS = ("identity", "ownership", "tax", "title", "zoning", "environment", "access", "physical")


def _e(v: object) -> str:
    if v is None:
        return "UNKNOWN"
    if isinstance(v, list):
        v = "; ".join(map(str, v))
    elif isinstance(v, dict):
        v = "; ".join(f"{k}: {x}" for k, x in v.items())
    return html.escape(str(v), quote=False).replace("|", "\\|").replace("\n", " ").replace("[", "\\[").replace("`", "'")


def _money(lo, hi) -> str:
    return f"${lo:,.0f}" if lo == hi else f"${lo:,.0f}–${hi:,.0f}"


def _field_rows(d: dict, section: str) -> List[str]:
    rows = []
    for name, f in d["fields"].items():
        if f["section"] != section:
            continue
        srcs, notes = set(), []
        for c in f["claims"]:
            srcs.update(c["sources"])
            if f["resolution_required"] and c["value"] is not None:
                notes.append(f"claim [{c['tier']}{', lead' if c['status'] == 'lead' else ''}]: {_e(c['value'])} ({', '.join(c['sources']) or 'no source'})")
            notes += [_e(c["notes"])] if c["notes"] else []
            notes += [f"tier: {_e(w)}" for w in c["why"] if c["status"] in ("stale", "lead")]
            notes += [f"⚠ {_e(x)}" for x in c["flags"] if "conflicts" not in x]
        if f["leads"] and not f["resolution_required"]:
            notes.append("model-reported lead (not evidence): " + _e(f["leads"]))
        label = f["label"] + (" *" if f["material"] else "")
        value = "CONFLICT: resolution required" if f["resolution_required"] else _e(f["value"]) + (f" {f['unit']}" if f["unit"] and f["value"] is not None else "")
        need = f" (needs {f['required']})" if f["required"] else ""
        rows.append(f"| {f['tier']}{need} | {_e(label)} | {value} | {', '.join(sorted(srcs)) or '—'} | {' · '.join(notes)} |")
    if not rows:
        return ["_No evidence collected for this section._"]
    return ["| Tier | Field | Value | Sources | Notes |", "|---|---|---|---|---|"] + rows


def _economics(d: dict) -> List[str]:
    econ = d["economics"]
    a = econ["authorization"]
    src = (f"case authorisation by {a.get('authorized_by', 'owner')} on {a.get('authorized_at', '?')}"
           if a["source"] == "case" else "engine default (no case authorisation recorded)")
    out = [f"- Maximum exposure for this case: **${econ['cap_usd']:,.0f}** ({_e(src)})",
           f"- TRUE EXPOSURE: **{econ['true_exposure_text']}**: **{econ['true_exposure_status']}**",
           "  - " + "; ".join(f"{c.replace('_', ' ')}: " + (_money(s['low'], s['high']) if not s["unknown"] else "UNKNOWN")
                              for c, s in econ["by_category"].items() if s["low"] or s["high"] or s["unknown"])]
    t = econ.get("target")
    if t:
        out.append(f"- Target offer (negotiation parameter, not a valuation): ${t['target_offer_usd']:,.0f}; leaves "
                   f"${t['room_for_other_costs_usd']:,.0f} for other one-time costs, which are {t['other_costs_text']}")
    out += [f"- Annual carrying cost: **{econ['annual_carrying_text']}**",
            f"- Supported revenue (documented/strong): {_money(econ['revenue_supported']['low'], econ['revenue_supported']['high'])}/yr",
            f"- Capital recovery: {econ['recovery']}", "",
            "| Line | Kind | Category | Amount | Basis | Sources | Notes |", "|---|---|---|---|---|---|---|"]
    for l in econ["lines"]["costs"] + econ["lines"]["revenue"]:
        amt = "UNKNOWN" if l["basis"] == "unknown" else _money(l["low"], l["high"])
        out.append(f"| {_e(l['label'])} | {l['kind']} | {l['category']} | {amt} | {l['basis']} | {', '.join(l['sources']) or '—'} | {_e(l['notes'])} |")
    if d["tax_ledger"]:
        out += ["", "**Tax ledger** (payoff needs VERIFIED_PRIMARY: a certified Treasurer/TACS figure)", "",
                "| Item | Amount | Tier | Sources | Notes |", "|---|---|---|---|---|"]
        out += [f"| {_e(r['field'])} | {('$' + format(r['value'], ',.2f')) if isinstance(r['value'], (int, float)) else _e(r['value'])} | "
                f"{r['tier']} | {', '.join(r['sources']) or '—'} | {_e(r['notes'])} |" for r in d["tax_ledger"]]
    return out


def render(d: dict) -> str:
    p, rec = d["property"], d["recommendation"]
    out = [f"# Land dossier: {_e(p['label'] or p['parcel_id'])}",
           f"`{p['key']}` · version `{d['version'][:12]}` · evaluated {d['evaluated_on']} · automation level {d['automation_level']} · {d['engine']}", ""]
    if rec.get("restriction"):
        out += [f"> **{rec['restriction']}**", ""]
    out += [f"**Verdict: {rec['verdict']}**: {_e(rec['headline'])}  ", f"**Acquisition gate: {rec['gate']}**", "",
            "Tiers are computed from source types, never asserted. A model's report is a lead, not evidence. `*` = material field.", ""]
    for key, title in schema.SECTIONS:
        out.append(f"## {title}")
        if key in _FIELD_SECTIONS:
            out += _field_rows(d, key)
        elif key == "uses":
            out += ["Evidenced exit matrix:", "",
                    "| Id | Exit | Legal use | Need | Counterparty | Value | Months | Evidence | Evidenced | Depends on |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
            for x in d["exits"]:
                cp = x["counterparty"]
                cps = f"{_e(cp['name'])} ({_e(cp['parcel_id'])}, {cp['contact_status']})" if cp else "not identified"
                val = "UNKNOWN" if x["value_basis"] == "unknown" or x["value_low"] is None else f"{_money(x['value_low'], x['value_high'] or x['value_low'])} ({x['value_basis']})"
                mon = "?" if x["months_low"] is None else f"{x['months_low']:.0f}–{(x['months_high'] or x['months_low']):.0f}"
                out.append(f"| {_e(x['id'])} | {x['exit_type']}: {_e(x['description'])} | {x['legal_use_status']} | {x['demonstrated_need']} | "
                           f"{cps} | {val} | {mon} | {x['evidence_tier']} | {'yes' if x['evidenced'] else 'no'} | {_e(x['dependencies']) if x['dependencies'] else '—'} |")
            if not d["exits"]:
                out.append("| — | _No exits proposed._ | | | | | | | | |")
        elif key == "adjacent":
            out += [f"- {_e(a['parcel_id'])}: {_e(a['owner']) or 'owner not recorded'}; {_e(a['land_use']) or 'use unknown'} [{', '.join(a['sources'])}]"
                    for a in d["adjacent"]] or ["_Adjacent parcels not mapped yet (LAND-005)._"]
        elif key == "counterparties":
            out += [f"- {_e(c['name'])} ({_e(c['type'])}): {_e(c['basis'])}. Interest: **{c['interest']}**" for c in d["counterparties"]] \
                or ["_None identified. Interest is never assumed; it is tested only after an owner approval, and AURIX sends nothing itself._"]
        elif key == "economics":
            out += _economics(d)
        elif key == "unknowns":
            out += [f"- {u['tier']} · {_e(u['label'])}{' (material, needs ' + u['required'] + ')' if u['material'] else ''}"
                    f"{': resolution required' if u['resolution_required'] else ''}"
                    f"{'; model-reported lead: ' + _e(u['leads']) if u['leads'] and not u['resolution_required'] else ''}"
                    for u in d["unknowns"]] or ["_None._"]
        elif key == "evidence":
            out += ["| Id | Type | Source | Custodian | Retrieved | Data as of | Locator |", "|---|---|---|---|---|---|---|"]
            out += [f"| {s['id']} | {s['type']} | {_e(s['title'])} | {_e(s['custodian'])} | {_e(s['retrieved_at'])} | {_e(s['as_of']) or 'not stated'} | {_e(s['locator'])} |"
                    for s in d["sources"]]
            if d["llm_sources_treated_as_leads"]:
                out += ["", f"Model reports treated as leads only: {', '.join(d['llm_sources_treated_as_leads'])}."]
            if d["untrusted_flags"]:
                out += ["", "**Untrusted-input flags:**"] + [f"- {_e(x['where'])}: {_e(x['flag'])}" for x in d["untrusted_flags"]]
        elif key == "risk":
            out += [f"- **{f['outcome']}** · {_e(f['rule'])}: {_e(f['reason'])}" for f in d["findings"]] or ["_No findings._"]
        elif key == "recommendation":
            out += [f"**{rec['verdict']}**: {_e(rec['headline'])}", "", f"**Acquisition gate: {rec['gate']}**", "", f"_{rec['authority']}_"]
        elif key == "next_steps":
            for i, s in enumerate(d["next_steps"], 1):
                cost = s["est_cost_usd"]
                out.append(f"{i}. {_e(s['action'])}. _Why:_ {_e(s['why'])}. "
                           f"{'Remote' if s['remote'] else 'Resolution path not known' if s['remote'] is None else 'Needs someone on site'}"
                           f"{'; web research runs as an approved mission' if s['needs_web_mission'] else ''}"
                           f"{'' if not cost else '; no fee expected' if not cost[1] else f'; est. ${cost[0]}–${cost[1]} (planning estimate, not a quote)'}.")
            ins = d["inspection"]
            out += ["", f"**Physical inspection:** {'needed' if ins['needed'] else 'not yet justified'}: " + "; ".join(map(_e, ins["why"]))]
        elif key == "red_team":
            out += [f"- **{_e(r['attack'])}** _Disproved only by:_ {_e(r['disproved_by'])}" for r in d["red_team"]] or ["_No attack survived the evidence._"]
        out.append("")
    return "\n".join(out)
