"""src/foundation/land/schema.py - the dossier's field catalogue: what we track, what is material, what evidence it needs, how stale is
too stale, and how to find out.

A field is MATERIAL when not knowing it could change the legality or economics of a deal. Every material field has a REQUIRED tier;
below it, the field blocks the acquisition gate. The title, tax-payoff and legal-access gates require VERIFIED_PRIMARY (the recorded
instrument or certified ledger itself); other material fields require at least VERIFIED_SECONDARY (an official source). Material fields
with no evidence appear as explicit UNKNOWNs in every dossier, so a gap can never pass as "nothing to report".

Each field also carries the research step that resolves it. Costs there are rough planning estimates (labelled as such in the
report), not quotes, and `network=True` steps need the owner's authorisation before AURIX may run them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from src.foundation.land.evidence import Tier

SCHEMA_VERSION = "land-dossier/2"

SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("identity", "1. Identity"),
    ("ownership", "2. Ownership"),
    ("tax", "3. Tax"),
    ("title", "4. Title & encumbrances"),
    ("zoning", "5. Zoning"),
    ("environment", "6. Flood & environment"),
    ("access", "7. Access"),
    ("physical", "8. Physical characteristics"),
    ("uses", "9. Exits & productive uses (evidenced exit matrix)"),
    ("adjacent", "10. Adjacent properties"),
    ("counterparties", "11. Potential counterparties"),
    ("economics", "12. Economics"),
    ("unknowns", "13. Unknowns"),
    ("evidence", "14. Evidence & provenance"),
    ("risk", "15. Risk summary"),
    ("recommendation", "16. Acquisition recommendation"),
    ("next_steps", "17. Proposed next research steps"),
    ("red_team", "18. Red team: how this deal could hurt you"),
)


@dataclass(frozen=True)
class Resolution:
    action: str
    where: str
    remote: bool = True            # can be done without anyone going to the land
    network: bool = False          # needs AURIX to make network calls (owner authorisation required)
    cost_low: int = 0
    cost_high: int = 0


@dataclass(frozen=True)
class FieldSpec:
    section: str
    label: str
    material: bool = False
    max_age_days: Optional[int] = None     # time-sensitive: older than this, or undated -> STALE
    resolve: Optional[Resolution] = None
    unit: str = ""
    required: Optional[Tier] = None        # set below for material fields


_CLERK = "Circuit Court Clerk land records (in person: free terminals; remote: Secure Remote Access subscription, fee varies by court)"
_TREAS = "County Treasurer (phone, email or online tax lookup)"
_PLAN = "County planning & zoning office; zoning ordinance text and official zoning map"

FIELDS: Dict[str, FieldSpec] = {
    # 1. identity
    "address": FieldSpec("identity", "Address"),
    "county": FieldSpec("identity", "County / city", material=True,
                        resolve=Resolution("Confirm the taxing jurisdiction on the assessment record", "County assessor / GIS")),
    "state": FieldSpec("identity", "State", material=True,
                       resolve=Resolution("Confirm the state on the assessment record", "County assessor / GIS")),
    "parcel_id": FieldSpec("identity", "Parcel / tax map number", material=True,
                           resolve=Resolution("Confirm parcel number on the current assessment record", "County assessor / GIS")),
    "rpc": FieldSpec("identity", "RPC (real property code)"),
    "tacs": FieldSpec("identity", "TACS (treasurer account)"),
    "acreage": FieldSpec("identity", "Acreage", material=True,
                         resolve=Resolution("Compare assessor acreage with the deed/plat description", _CLERK, cost_high=50)),
    "legal_description": FieldSpec("identity", "Legal description",
                                   resolve=Resolution("Pull the vesting deed's full legal description / plat reference", _CLERK, cost_high=50)),
    # 2. ownership
    "owner_of_record": FieldSpec("ownership", "Owner of record", material=True, max_age_days=365,
                                 resolve=Resolution("Confirm grantee on the most recent recorded deed", _CLERK, cost_high=50)),
    "owner_mailing_address": FieldSpec("ownership", "Owner tax-mailing address", max_age_days=365),
    "vesting_instrument": FieldSpec("ownership", "Vesting deed (instrument, dates)", material=True,
                                    resolve=Resolution("Retrieve the vesting deed image; record instrument no., deed date, recording date, grantor", _CLERK, cost_high=50)),
    "prior_conveyances": FieldSpec("ownership", "Prior conveyances",
                                   resolve=Resolution("Walk the grantor index back from the vesting deed", _CLERK, cost_high=50)),
    # 3. tax
    "assessed_value": FieldSpec("tax", "Assessed value", max_age_days=730),
    "annual_tax": FieldSpec("tax", "Annual tax", material=True, max_age_days=400,
                            resolve=Resolution("Get the current-year levy for the parcel", _TREAS)),
    "tax_delinquent": FieldSpec("tax", "Delinquency status", material=True, max_age_days=120,
                                resolve=Resolution("Ask whether the parcel is delinquent and since which year", _TREAS)),
    "tax_balance_due": FieldSpec("tax", "Current tax balance due (with penalties/interest)", material=True, max_age_days=60,
                                 resolve=Resolution("Get the payoff amount including penalties and interest", _TREAS)),
    "tax_sale_status": FieldSpec("tax", "Tax-sale / delinquent-sale status", material=True, max_age_days=120,
                                 resolve=Resolution("Check whether the parcel is on a tax-sale list or in a delinquent-tax suit",
                                                    "County Treasurer; County Attorney / appointed tax-sale counsel")),
    # 4. title
    "deeds_of_trust": FieldSpec("title", "Deeds of trust / mortgages", material=True,
                                resolve=Resolution("Pull every deed of trust recorded against the parcel", _CLERK, cost_high=50)),
    "dot_releases": FieldSpec("title", "Releases / certificates of satisfaction", material=True,
                              resolve=Resolution("Search for releases, satisfactions, assignments and substitutions of trustee referencing each deed of trust", _CLERK, cost_high=50)),
    "judgments": FieldSpec("title", "Judgment liens against owner", material=True, max_age_days=120,
                           resolve=Resolution("Search the judgment lien docket for the owner of record", "Circuit Court Clerk judgment docket")),
    "tax_liens": FieldSpec("title", "Federal / state tax liens", material=True, max_age_days=120,
                           resolve=Resolution("Search recorded federal and state tax liens for the owner of record", _CLERK)),
    "mechanics_liens": FieldSpec("title", "Mechanics' liens", max_age_days=120),
    "foreclosure": FieldSpec("title", "Foreclosure records", material=True, max_age_days=120,
                             resolve=Resolution("Search for trustee's deeds / foreclosure notices", _CLERK)),
    "easements": FieldSpec("title", "Easements & rights-of-way",
                           resolve=Resolution("Read the vesting deed and plats for easements; check recorded utility easements", _CLERK)),
    "restrictions": FieldSpec("title", "Covenants & restrictions"),
    "reservations": FieldSpec("title", "Mineral / timber / water reservations",
                              resolve=Resolution("Read the chain of deeds for reserved mineral, timber or water rights", _CLERK)),
    "title_chain": FieldSpec("title", "Title chain completeness", material=True,
                             resolve=Resolution("Run a 40-60 year chain or order a title report from a settlement attorney",
                                                "Settlement attorney / title company", cost_low=150, cost_high=400)),
    # 5. zoning
    "zoning_district": FieldSpec("zoning", "Zoning district", material=True, max_age_days=730,
                                 resolve=Resolution("Confirm the current district on the official zoning map", _PLAN)),
    "permitted_uses": FieldSpec("zoning", "Permitted uses (by right)", material=True,
                                resolve=Resolution("Read the district's by-right use table; confirm the intended use with planning staff", _PLAN)),
    "prohibited_uses": FieldSpec("zoning", "Prohibited uses"),
    "conditional_uses": FieldSpec("zoning", "Conditional / special uses"),
    "min_lot_size": FieldSpec("zoning", "Minimum lot size / subdivision limits"),
    "setbacks": FieldSpec("zoning", "Setbacks"),
    # 6. environment
    "flood_zone": FieldSpec("environment", "FEMA flood zone", material=True,
                            resolve=Resolution("Look up the parcel on the FEMA National Flood Hazard Layer", "FEMA NFHL (msc.fema.gov / hazards.fema.gov)", network=True)),
    "wetlands": FieldSpec("environment", "Wetlands",
                          resolve=Resolution("Check the USFWS National Wetlands Inventory mapper", "USFWS NWI", network=True)),
    "conservation_easement": FieldSpec("environment", "Conservation / agricultural restrictions",
                                       resolve=Resolution("Search recorded conservation easements; check state easement registry", _CLERK)),
    # 7. access
    "road_frontage": FieldSpec("access", "Road frontage",
                               resolve=Resolution("Measure frontage from GIS; confirm the road is publicly maintained", "County GIS; state DOT road inventory", network=True)),
    "legal_access": FieldSpec("access", "Legal access (ingress/egress)", material=True,
                              resolve=Resolution("Confirm frontage on a public road in the deed/plat, or a recorded access easement", _CLERK, cost_high=50)),
    "physical_access": FieldSpec("access", "Physical access"),
    # 8. physical
    "structures": FieldSpec("physical", "Structures"),
    "structure_condition": FieldSpec("physical", "Structure condition"),
    "hazards": FieldSpec("physical", "Hazards & liabilities"),
    "water_source": FieldSpec("physical", "Water",
                              resolve=Resolution("Check the county/health-department well record; if needed, targeted inspection of the pump house",
                                                 "County health department (environmental health)", )),
    "septic": FieldSpec("physical", "Septic",
                        resolve=Resolution("Request any septic permit on file", "County health department (environmental health)")),
    "utilities": FieldSpec("physical", "Utilities / power proximity"),
    "topography": FieldSpec("physical", "Topography"),
    "land_cover": FieldSpec("physical", "Land cover / agricultural character",
                            resolve=Resolution("Classify cover from recent aerial imagery; check NRCS soil survey", "Esri Wayback / NAIP imagery; NRCS Web Soil Survey", network=True)),
    "timber": FieldSpec("physical", "Timber"),
    "imagery": FieldSpec("physical", "Imagery on file"),
}


# The evidence each material field needs before the acquisition gate can open.
_PRIMARY_GATES = {"owner_of_record", "vesting_instrument", "deeds_of_trust", "dot_releases", "judgments", "tax_liens", "title_chain",
                  "tax_balance_due", "legal_access"}
_UNITS = {"acreage": "acres", "annual_tax": "USD/yr", "tax_balance_due": "USD", "assessed_value": "USD"}
PHYSICAL = frozenset(n for n, f in FIELDS.items() if f.section == "physical") | {"physical_access"}
FIELDS = {n: FieldSpec(f.section, f.label, f.material, f.max_age_days, f.resolve, _UNITS.get(n, ""),
                       (Tier.VERIFIED_PRIMARY if n in _PRIMARY_GATES else Tier.VERIFIED_SECONDARY) if f.material else None)
          for n, f in FIELDS.items()}
