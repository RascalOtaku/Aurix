"""economy/seed.py - the first five candidates, so the engine has something real to rank on day one.

Every number below is a PLACEHOLDER ASSUMPTION written down so it can be argued with and replaced by evidence - not research, not a
promise. All five start at `discovered` with no demand evidence, which the scorecard discounts heavily. Land is included on purpose:
with the default limits it is blocked by the hard constraints, which is the honest answer for limited starting cash.

`opp import <money id>` pulls any idea from the money catalog (money/opportunities.json) in the same way.
"""
from __future__ import annotations

from typing import List

from src.foundation.economy import pipeline, store

ASSUMPTION = "Placeholder assumption (seeded 2026-10): replace with evidence."

SEEDS = [
    dict(opp_id="property_dossiers", name="Property due-diligence packages", lane="research",
         problem="Buyers of cheap rural or tax-sale parcels skip checks (access, zoning, back taxes, flood) and get burned.",
         customer="first-time land buyers, small investors", startup_cash=30, max_loss=60, months_to_revenue=1.5, net_low=150,
         net_high=600, owner_hours=4, automation=0.6, durability=0.5, reversibility=0.9, strategic=0.9,
         next_action="Build one sample dossier for a public listing with LandPilot; price it from what competitors charge.",
         notes=ASSUMPTION + " Reuses the LandPilot dossier pipeline. Permitted use of public records must be checked first."),
    dict(opp_id="market_monitor", name="Recurring market-monitoring reports", lane="research",
         problem="Small businesses do not track competitors' prices, reviews and listings; they find out late.",
         customer="local service businesses, niche sellers", startup_cash=20, max_loss=40, months_to_revenue=2, net_low=100,
         net_high=500, owner_hours=3, automation=0.7, durability=0.5, reversibility=0.9, strategic=0.8,
         next_action="Pick one niche and draft a sample monthly report from public data only.", notes=ASSUMPTION),
    dict(opp_id="smb_automation", name="Small-business workflow automation", lane="automation",
         problem="Owners re-type the same intake forms, quotes and reports every week.",
         customer="small offices and trades", startup_cash=0, max_loss=50, months_to_revenue=2.5, net_low=200,
         net_high=1200, owner_hours=8, automation=0.5, durability=0.6, reversibility=0.8, strategic=0.8,
         next_action="Build a demo intake-to-report workflow in the sandbox with fake data.",
         notes=ASSUMPTION + " Support and reliability are real obligations once someone depends on it."),
    dict(opp_id="research_templates", name="Due-diligence checklists and templates", lane="products",
         problem="People researching a purchase want a proven checklist, not a blank page.",
         customer="DIY buyers and researchers", startup_cash=15, max_loss=30, months_to_revenue=3, net_low=20,
         net_high=200, owner_hours=2, automation=0.9, durability=0.3, reversibility=1.0, strategic=0.5,
         next_action="Wait for lane 1: turn what customers actually asked for into the first template.",
         notes=ASSUMPTION + " Publishing a product is not demand; distribution has to be tested."),
    dict(opp_id="land_parcel", name="A small productive parcel", lane="land",
         problem="Housing and food costs; long-term resilience.", customer="you",
         startup_cash=1500, max_loss=3000, months_to_revenue=12, net_low=0, net_high=150, owner_hours=4, automation=0.2,
         durability=0.8, reversibility=0.4, strategic=0.9,
         next_action="Research only (LandPilot): lowers living costs later; not a first way to turn limited cash into income.",
         notes=ASSUMPTION + " Capital-heavy: expected to be blocked by your limits until ventures fund it."),
]


def seed_if_empty() -> List[str]:
    """Seed the five candidates the first time the engine opens. Returns the ids created."""
    if store.meta_get("seeded") or store.one("SELECT 1 AS x FROM opportunities"):
        return []
    made = []
    for s in SEEDS:
        s = dict(s)
        pipeline.create(s.pop("opp_id"), s.pop("name"), s.pop("lane"), origin="seed", **s)
        made.append(SEEDS[len(made)]["opp_id"])
    store.meta_set("seeded", "1")
    return made


def import_money_idea(money_id: str) -> str:
    from src.foundation import money
    it = money.get(money_id)
    if not it:
        return f"No money idea called <code>{money_id}</code>. <code>money</code> lists them."
    if pipeline.get(it["id"]):
        return f"<code>{it['id']}</code> is already in the engine: <code>opp {it['id']}</code>."
    lane = "land" if "land" in it["id"] else {"services": "automation", "products": "products"}.get(str(it.get("category", "")), "other")
    pipeline.create(it["id"], it["name"], lane, origin="catalog", problem=it.get("summary", ""), startup_cash=it.get("capital", 0),
                    max_loss=it.get("capital", 0), months_to_revenue=it.get("months_to_first_dollar", 3), net_low=it.get("est_low", 0),
                    net_high=it.get("est_high", 0), owner_hours=it.get("hours_per_week", 5),
                    automation=max(0.0, min(1.0, float(it.get("automation", 0.3) or 0))),
                    next_action=it.get("first_step", ""),
                    notes="Imported from the money catalog: its numbers are estimates from general knowledge, not evidence.")
    return f"📥 Imported <b>{it['name']}</b> as <code>{it['id']}</code> (stage: discovered)."
