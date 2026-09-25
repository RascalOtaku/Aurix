#!/usr/bin/env python3
"""print_cost.py - estimate and compare the cost of producing a 3D-printed part.

Two jobs:
  1. ESTIMATE from a mesh report (ct_to_stl.py's report.json: volume, area, extents) using a
     dated static price table. These are ballparks to shortlist options - they are NOT quotes.
  2. RANK real quotes the agent collected from vendors (quotes.json) by total landed cost,
     flagging incomplete/duplicate/implausible entries so a bad row cannot win.

    python3 print_cost.py --report report.json [--quotes quotes.json] [--out cost.md]
    python3 print_cost.py --selftest

Stdlib only. All prices are USD.
"""
import argparse
import json
import math
import sys

PRICE_TABLE_DATE = "2026-09"
# tech -> ballpark model. price_per_cm3 is what online services typically charge per cm3 of
# MODEL volume (they price on volume); setup is a per-order fee; material_g_cm3 is density.
# Editable: override with --prices file.json of the same shape.
PRICE_TABLE = {
    "fdm_pla_home":  {"label": "FDM PLA, print at home (material only)", "home": True, "usd_per_kg": 22.0,
                      "density": 1.24, "flow_mm3_s": 8.0, "build_mm": (220, 220, 250), "setup": 0.0},
    "fdm_pla_service": {"label": "FDM PLA, online service", "price_per_cm3": (0.10, 0.30), "setup": (5.0, 15.0),
                        "build_mm": (250, 250, 250), "lead_days": (3, 8)},
    "resin_sla_service": {"label": "SLA resin, online service", "price_per_cm3": (0.25, 0.70), "setup": (8.0, 20.0),
                          "build_mm": (145, 145, 175), "lead_days": (3, 8)},
    "sls_nylon_service": {"label": "SLS nylon (PA12), online service", "price_per_cm3": (0.30, 0.90),
                          "setup": (10.0, 25.0), "build_mm": (300, 300, 300), "lead_days": (4, 10)},
    "mjf_nylon_service": {"label": "MJF nylon (PA12), online service", "price_per_cm3": (0.25, 0.75),
                          "setup": (10.0, 25.0), "build_mm": (380, 280, 380), "lead_days": (4, 10)},
}
DISCLAIMER = (f"Estimates come from a static price table dated {PRICE_TABLE_DATE}. They shortlist options; "
              "only real vendor quotes are prices.")


def fits(extents_mm, build_mm):
    """Does the part fit the build volume in some orientation? (sorted-dimension comparison)"""
    return all(p <= b for p, b in zip(sorted(extents_mm), sorted(build_mm)))


def scale_to_fit(extents_mm, build_mm):
    """Largest uniform scale (<= 1.0) at which the part fits the build volume."""
    if fits(extents_mm, build_mm):
        return 1.0
    return min(b / p for p, b in zip(sorted(extents_mm), sorted(build_mm)) if p > 0)


def fdm_effective_cm3(volume_cm3, area_cm2, wall_mm=1.2, infill=0.20):
    """Material actually deposited: printed walls + infill inside them."""
    shell = min(volume_cm3, area_cm2 * (wall_mm / 10.0))
    return shell + infill * max(0.0, volume_cm3 - shell)


def estimate(report, tech_id, table=None, infill=0.20, scale=1.0):
    """One option's estimate: {'tech','label','low','high','notes',...} (low==high for home)."""
    table = table or PRICE_TABLE
    t = table[tech_id]
    s3 = scale ** 3
    vol = float(report["volume_cm3"]) * s3
    area = float(report.get("area_cm2", 0.0)) * scale ** 2
    ext = [float(e) * scale for e in report["extents_mm"]]
    notes = []
    if not fits(ext, t["build_mm"]):
        fit = scale_to_fit(ext, t["build_mm"])
        notes.append(f"does not fit the {t['build_mm']} mm build volume at this scale; "
                     f"would need to be scaled to {fit:.0%} or printed in parts")
    if t.get("home"):
        eff = fdm_effective_cm3(vol, area, infill=infill)
        grams = eff * t["density"]
        material = grams / 1000.0 * t["usd_per_kg"]
        hours = (eff * 1000.0) / t["flow_mm3_s"] / 3600.0
        notes.append(f"~{grams:.0f} g of filament, ~{hours:.1f} h of print time at {infill:.0%} infill")
        return {"tech": tech_id, "label": t["label"], "low": round(material, 2), "high": round(material, 2),
                "notes": notes, "fits": fits(ext, t["build_mm"]), "grams": round(grams), "hours": round(hours, 1)}
    lo_p, hi_p = t["price_per_cm3"]
    lo_s, hi_s = t["setup"]
    return {"tech": tech_id, "label": t["label"], "low": round(vol * lo_p + lo_s, 2),
            "high": round(vol * hi_p + hi_s, 2), "notes": notes, "fits": fits(ext, t["build_mm"]),
            "lead_days": list(t.get("lead_days", ()))}


def estimate_all(report, table=None, **kw):
    table = table or PRICE_TABLE
    return sorted((estimate(report, k, table, **kw) for k in table), key=lambda e: (not e["fits"], e["low"]))


# ---------------------------------------------------------------------------
# real quotes
# ---------------------------------------------------------------------------

REQUIRED_QUOTE_FIELDS = ("vendor", "tech", "price")


def validate_quote(q):
    """List of problems with one quote row (empty list = usable)."""
    problems = []
    for f in REQUIRED_QUOTE_FIELDS:
        if q.get(f) in (None, ""):
            problems.append(f"missing {f}")
    for f in ("price", "shipping"):
        v = q.get(f)
        if v not in (None, ""):
            try:
                if float(v) < 0:
                    problems.append(f"negative {f}")
            except (TypeError, ValueError):
                problems.append(f"{f} is not a number")
    if not q.get("source"):
        problems.append("no source URL/reference (cannot be verified)")
    return problems


def rank_quotes(quotes, report=None, table=None):
    """Sort usable quotes by total landed cost. Returns (ranked, rejected, warnings).

    A quote far below the estimate table's LOW bound for its tech is flagged (likely a wrong
    scale, quantity, or material) but kept, so the owner decides."""
    table = table or PRICE_TABLE
    ranked, rejected, warnings = [], [], []
    seen = set()
    for q in quotes:
        problems = validate_quote(q)
        if problems:
            rejected.append({"quote": q, "problems": problems})
            continue
        key = (str(q["vendor"]).strip().lower(), str(q["tech"]).strip().lower(), float(q["price"]))
        if key in seen:
            rejected.append({"quote": q, "problems": ["duplicate of an earlier quote"]})
            continue
        seen.add(key)
        total = round(float(q["price"]) + float(q.get("shipping") or 0.0), 2)
        row = dict(q, total=total)
        tech = str(q["tech"]).strip().lower()
        if report and tech in table and not table[tech].get("home"):
            low = estimate(report, tech, table)["low"]
            if total < 0.5 * low:
                row["flag"] = f"total ${total} is far below the ~${low} ballpark: check scale, quantity, material"
                warnings.append(f"{q['vendor']}: {row['flag']}")
        ranked.append(row)
    ranked.sort(key=lambda r: (r["total"], r.get("lead_days") or 999))
    return ranked, rejected, warnings


def render_markdown(report, estimates, ranked=None, rejected=None, warnings=None):
    L = ["# Print cost comparison", "",
         f"Part: {report['volume_cm3']:.1f} cm3, extents {' x '.join(f'{e:.0f}' for e in report['extents_mm'])} mm"
         f", watertight: {report.get('watertight', 'unknown')}", "", f"_{DISCLAIMER}_", "",
         "## Estimates (ballpark, not quotes)", "", "| Option | Est. low | Est. high | Fits | Notes |", "|---|---|---|---|---|"]
    for e in estimates:
        L.append(f"| {e['label']} | ${e['low']:.2f} | ${e['high']:.2f} | {'yes' if e['fits'] else 'NO'} | "
                 f"{'; '.join(e['notes']) or '-'} |")
    if ranked is not None:
        L += ["", "## Real quotes, cheapest first", "", "| # | Vendor | Tech / material | Price | Shipping | Total | Lead | Source |",
              "|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(ranked, 1):
            L.append(f"| {i} | {r['vendor']} | {r['tech']} {r.get('material', '')} | ${float(r['price']):.2f} | "
                     f"${float(r.get('shipping') or 0):.2f} | **${r['total']:.2f}** | {r.get('lead_days', '?')} d | "
                     f"{r.get('source', '')} |")
        if not ranked:
            L.append("| - | no usable quotes | | | | | | |")
    for w in warnings or []:
        L.append(f"\n> WARNING: {w}")
    if rejected:
        L += ["", "## Rejected quote rows", ""]
        L += [f"- {r['quote'].get('vendor', '?')}: {', '.join(r['problems'])}" for r in rejected]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------

def _selftest():
    sphere = {"volume_cm3": 268.0, "area_cm2": 201.0, "extents_mm": [80, 80, 80], "watertight": True}
    ests = estimate_all(sphere)
    assert all(e["low"] <= e["high"] for e in ests)
    assert ests[0]["tech"] == "fdm_pla_home", ests[0]
    big = {"volume_cm3": 900.0, "area_cm2": 700.0, "extents_mm": [400, 300, 300], "watertight": True}
    assert not estimate(big, "resin_sla_service")["fits"]
    assert abs(scale_to_fit([400, 300, 300], (145, 145, 175)) - 175 / 400) < 1e-9   # the 400 mm axis is the limit
    ranked, rejected, _ = rank_quotes([
        {"vendor": "A", "tech": "sls_nylon_service", "price": 90, "shipping": 10, "source": "url"},
        {"vendor": "B", "tech": "sls_nylon_service", "price": 70, "source": "url"},
        {"vendor": "C", "tech": "sls_nylon_service", "price": 10},
    ], sphere)
    assert [r["vendor"] for r in ranked] == ["B", "A"] and len(rejected) == 1
    print("print_cost selftest OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", help="ct_to_stl report.json")
    ap.add_argument("--quotes", help="quotes.json: list of {vendor, tech, material, price, shipping, lead_days, source}")
    ap.add_argument("--prices", help="override price table (same shape as PRICE_TABLE)")
    ap.add_argument("--infill", type=float, default=0.20)
    ap.add_argument("--scale", type=float, default=1.0, help="uniform print scale (1.0 = as scanned)")
    ap.add_argument("--out", help="write markdown here (default: stdout)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.report:
        ap.error("--report is required")
    report = json.load(open(a.report, encoding="utf-8"))
    table = json.load(open(a.prices, encoding="utf-8")) if a.prices else PRICE_TABLE
    ests = estimate_all(report, table, infill=a.infill, scale=a.scale)
    ranked = rejected = warnings = None
    if a.quotes:
        ranked, rejected, warnings = rank_quotes(json.load(open(a.quotes, encoding="utf-8")), report, table)
    md = render_markdown(report, ests, ranked, rejected, warnings)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(md)
        print(f"wrote {a.out}")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
