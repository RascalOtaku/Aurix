"""src/foundation/land/buyer.py - the customer-facing BUYER'S DOSSIER, rendered from a LandPilot dossier.

LandPilot's own dossier (report.py) is an internal acquisition tool: exposure caps, exits, gates. A buyer needs something else - what
the records establish, what they do not, and exactly who can settle each open question - and it must never say more than the evidence
does. So this is a VIEW over the same evidence, never a second opinion:

    Confirmed      VERIFIED_PRIMARY / VERIFIED_SECONDARY: an official record or source says so (linked)
    Indicated      STRONGLY_INDICATED / UNVERIFIED, or an official source that is weaker than the field needs (a GIS map showing
                   frontage is not the recorded deed or easement that decides legal access): our reading, to be verified
    Open           UNKNOWN / STALE / CONTRADICTED, and anything only a model reported: a question, with who resolves it

Seven sections (parcel identity · ownership & title · legal access · land use · physical constraints · financial exposure · decision
summary). A report whose honest conclusion is "not enough evidence to decide" is a valid, useful report.

metrics() measures the sample itself - findings, how many rest on an official source, open material questions, record discrepancies,
machine time - so the Economic Engine's `sample` record is measured, not guessed.

SCOPE is printed at the top and the bottom: desktop research is not a title opinion, a survey, a legal opinion, an environmental
assessment or a buildability determination, and the report says so plainly.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.foundation.land import schema, store
from src.foundation.land.evidence import Tier, meets
from src.foundation.land.report import _e

SCOPE = ("**Scope:** desktop research from public records and official online sources. It is NOT a title opinion or title "
         "insurance, a boundary survey, a legal opinion, an environmental assessment, or a determination that anything can be built "
         "or used. Items marked *Open* must be settled by the named authority or a qualified professional before relying on them.")
CONFIRMED = ("VERIFIED_PRIMARY", "VERIFIED_SECONDARY")
INDICATED = ("STRONGLY_INDICATED", "UNVERIFIED")
SECTIONS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("identity", "1. Parcel identity", ("identity",)),
    ("title", "2. Ownership and title", ("ownership", "title")),
    ("access", "3. Legal access", ("access",)),
    ("use", "4. Land use", ("zoning",)),
    ("physical", "5. Physical constraints", ("environment", "physical")),
    ("money", "6. Financial exposure", ("tax",)),
)
# Who settles an open question when the field has no specific resolution step.
PROFESSIONAL = {"identity": "a licensed land surveyor (boundaries, acreage) and the county assessor",
                "ownership": "a title company or real-estate attorney", "title": "a title company or real-estate attorney",
                "access": "the county recorder (recorded deed/plat/easement) and a real-estate attorney",
                "zoning": "the county planning & zoning office", "environment": "the county health department or a wetlands professional",
                "physical": "an on-site inspection", "tax": "the county treasurer"}


def _group(f: dict) -> str:
    if f["resolution_required"] or f["tier"] in ("CONTRADICTED", "STALE", "UNKNOWN") or f["value"] is None:
        return "open"
    if f.get("required") and not meets(Tier(f["tier"]), Tier(f["required"])):
        return "short"                  # e.g. a GIS map shows road frontage, but the recorded deed/easement is what decides access
    return "confirmed" if f["tier"] in CONFIRMED else "indicated"


def _sources(f: dict, by_id: Dict[str, dict]) -> str:
    ids = sorted({s for c in f["claims"] if c["status"] != "lead" for s in c["sources"]})
    out = []
    for i in ids:
        s = by_id.get(i, {})
        loc = s.get("locator") or ""
        title = _e(s.get("title") or i)
        out.append(f"[{title}]({loc})" if loc.startswith("http") else title)
    return "; ".join(out) or "no source"


def _who(name: str, f: dict) -> str:
    spec = schema.FIELDS.get(name)
    if spec and spec.resolve:
        return f"{_e(spec.resolve.where)}: {_e(spec.resolve.action)}"
    return PROFESSIONAL.get(f["section"], "a qualified professional")


def _section(d: dict, sections: Tuple[str, ...], by_id: Dict[str, dict]) -> List[str]:
    rows = {"confirmed": [], "indicated": [], "open": []}
    for name, f in d["fields"].items():
        if f["section"] not in sections:
            continue
        g = _group(f)
        if g == "open":
            if not f["material"] and not f["leads"] and not f["resolution_required"]:
                continue                                   # an unknown minor detail is not a question worth the buyer's time
            why = ("records DISAGREE" if f["resolution_required"] else "out of date" if f["tier"] == "STALE" else "not established")
            lead = f" A model's research suggested {_e(f['leads'])}: a hypothesis to check, not a fact." if f["leads"] else ""
            rows["open"].append(f"- **{_e(f['label'])}**{' (material)' if f['material'] else ''}: {why}.{lead} "
                                f"*Who resolves it:* {_who(name, f)}.")
        else:
            unit = f" {f['unit']}" if f["unit"] else ""
            line = f"- **{_e(f['label'])}**: {_e(f['value'])}{unit} ({_sources(f, by_id)})"
            if g == "short":
                rows["indicated"].append(line + f" - *not yet confirmed by the deciding record:* {_who(name, f)}.")
            else:
                rows[g].append(line)
    out = []
    if rows["confirmed"]:
        out += ["*Confirmed in official records:*"] + rows["confirmed"]
    if rows["indicated"]:
        out += ["*Indicated, not yet confirmed (our interpretation - verify before relying on it):*"] + rows["indicated"]
    if rows["open"]:
        out += ["*Open questions:*"] + rows["open"]
    return out or ["_No records collected for this section yet._"]


def _access_check(d: dict) -> List[str]:
    legal, phys = d["fields"].get("legal_access"), d["fields"].get("physical_access")
    if phys and phys["value"] is not None and (not legal or _group(legal) != "confirmed"):
        return ["- ⚠ **Apparent access is not recorded access.** Something suggests a way onto the parcel, but no recorded deed, plat "
                "or easement checked so far establishes a legal right to use it. Landlocked land can be very hard to use, finance or resell."]
    return []


def metrics(d: dict, seconds: float = 0.0) -> Dict[str, float]:
    fields = d["fields"].values()
    findings = [f for f in fields if f["value"] is not None and not f["resolution_required"]]
    official = [f for f in findings if _group(f) == "confirmed" and f["tier"] in CONFIRMED]
    sourced = [f for f in findings if f["tier"] in CONFIRMED + INDICATED]
    open_material = [f for f in fields if f["material"] and _group(f) in ("open", "short")]
    discrepancies = [f for f in fields if f["resolution_required"]]
    return {"findings": len(findings), "sourced": len(sourced), "official": len(official), "unresolved": len(open_material),
            "discrepancies": len(discrepancies), "machine_min": round(seconds / 60.0, 2)}


def render(d: dict) -> str:
    p = d["property"]
    by_id = {s["id"]: s for s in d["sources"]}
    m = metrics(d)
    out = [f"# Buyer's dossier: {_e(p['label'] or p['parcel_id'])}",
           f"Parcel `{_e(p['parcel_id'])}` · {_e(p.get('county') or '')} {_e(p.get('state') or '')} · evidence as evaluated on "
           f"{d['evaluated_on']} · version `{d['version'][:12]}`", "", SCOPE, "",
           f"**At a glance:** {m['findings']} findings ({m['official']} from official sources) · **{m['unresolved']} material open "
           f"question(s)** · {m['discrepancies']} record discrepancy(ies).", ""]
    for _, title, sections in SECTIONS:
        out.append(f"## {title}")
        if "access" in sections:
            out += _access_check(d)
        out += _section(d, sections, by_id)
        if "tax" in sections:
            econ = d.get("economics") or {}
            if econ.get("annual_carrying_text"):
                out.append(f"- **Ongoing cost of ownership (estimate):** {_e(econ['annual_carrying_text'])} per year, from the cost "
                           "lines on record; unknown lines are shown as unknown, never as zero.")
        out.append("")
    out.append("## 7. Decision summary")
    flags = [f for f in d["findings"] if f["outcome"] in ("BLOCK", "WARN", "REJECT", "DISQUALIFY")] or d["findings"]
    out += ["**Red flags:**"] + ([f"- {_e(f['reason'])}" for f in flags[:8]] or ["- None found in the records collected."])
    if d.get("red_team"):
        out += ["", "**How this purchase could hurt you** (and what would rule it out):"]
        out += [f"- {_e(r['attack'])} *Ruled out only by:* {_e(r['disproved_by'])}" for r in d["red_team"][:6]]
    open_material = [(n, f) for n, f in d["fields"].items() if f["material"] and _group(f) in ("open", "short")]
    out += ["", "**Verify before buying, in this order:**"]
    out += [f"{i}. {_e(f['label'])} - {_who(n, f)}" for i, (n, f) in enumerate(open_material[:12], 1)] or ["- Nothing material is open."]
    verdict = ("**Not enough evidence to decide yet.** That is a finding, not a failure: the open questions above are exactly what to settle."
               if m["unresolved"] else "The material questions this desktop research can answer are answered; the scope limits below still apply.")
    out += ["", verdict, "", "## Sources", ""]
    out += [f"- {_e(s['title'])} ({_e(s['type'])}{', data as of ' + _e(s['as_of']) if s.get('as_of') else ''})"
            + (f": {s['locator']}" if str(s.get("locator", "")).startswith("http") else "") for s in d["sources"]] or ["- none"]
    out += ["", SCOPE]
    return "\n".join(out)


def save(d: dict) -> Path:
    folder = store.data_dir() / "dossiers" / d["property"]["key"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"buyer-{d['version'][:12]}.md"
    path.write_text(render(d), encoding="utf-8")
    return path


def produce(d: dict) -> Tuple[Path, Dict[str, float]]:
    """Render and save one buyer's dossier; returns (path, measured metrics incl. machine minutes)."""
    t0 = time.monotonic()
    path = save(d)
    return path, metrics(d, time.monotonic() - t0)


def summary_text(d: dict, path: Path, m: Dict[str, float], opp: Optional[str] = "property_dossiers") -> str:
    import html
    return "\n".join([
        f"📄 <b>Buyer's dossier</b> for {html.escape(d['property']['label'] or d['property']['parcel_id'])} saved "
        f"(<code>{html.escape(path.name)}</code>).",
        f"{m['findings']} findings · {m['official']} from official sources · <b>{m['unresolved']} material open question(s)</b> · "
        f"{m['discrepancies']} discrepancy(ies) · rendered in {m['machine_min'] * 60:.1f} s",
        "To record this as the sample (fill in YOUR minutes, any cost, and how much it helps a buyer decide):",
        f"<code>sample {opp}: owner=?? machine={m['machine_min']} cost=0 findings={m['findings']} sourced={m['official']} "
        f"unresolved={m['unresolved']} value=low|medium|high what a buyer learns that free tools would not show</code>",
        "<i>sourced counts official-source findings only. machine= is the render; add the research mission's minutes if one ran.</i>"])


def command(ref: str) -> Tuple[str, Optional[Path]]:
    """`land buyer <key>`: (Telegram text, path of the saved report or None)."""
    import html
    from src.foundation.land import hub
    hits = hub._find(ref)
    if not hits:
        return f"No dossier matches <code>{html.escape(ref)}</code>. Build one first (<code>land build &lt;evidence file&gt;</code>).", None
    if len(hits) > 1:
        return "Several match: " + ", ".join(f"<code>{html.escape(d['property']['key'])}</code>" for d in hits[:8]), None
    path, m = produce(hits[0])
    from src.foundation import audit
    audit.append("land_buyer_dossier", property=hits[0]["property"]["key"], version=hits[0]["version"][:12], **{k: m[k] for k in
                 ("findings", "official", "unresolved", "discrepancies")})
    return summary_text(hits[0], path, m), path
