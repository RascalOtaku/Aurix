"""src/foundation/land/adapters.py - where evidence comes from, and the strict gate it passes on the way in.

A SourceAdapter turns one property reference into a Bundle: sources, claims, exits (the Evidenced Exit Schema), tax-ledger rows, adjacent
parcels, counterparties, cost and revenue lines. Adapters read local evidence files only. Anything that needs the network is a Foundation
mission (land_intelligence pack, approval gate, audited); an adapter that declares `network = True` is always refused.

parse_bundle() is the only way raw input becomes a Bundle, and it is strict on purpose:
  - unknown keys are errors, not ignored - including `confidence` / `tier`: input cannot assign its own evidence tier (evidence.py
    computes it), and an evidence file can never carry a spending cap (only the owner sets one, see hub.authorize_case)
  - every string goes through untrusted.clean_value; anything flagged there is recorded on the item

Provenance survives promotion. A research mission writes only a DRAFT in its own workspace; the owner promotes it into the evidence
store (hub.promote), which stamps an `origin` block. A file whose origin is a mission draft is model output: every source in it is
loaded as an llm_report LEAD, whatever type the draft declared (kept as `claimed_type` so the owner can see what to go and verify).
Promotion moves a file; it never upgrades what the file proves.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Protocol, Tuple

from src.foundation.land import schema
from src.foundation.land.economics import ANNUAL_CATEGORIES, BASES, ONE_TIME_CATEGORIES, REVENUE_CATEGORIES, Line
from src.foundation.land.evidence import Claim, Source, SourceType
from src.foundation.land.untrusted import clean, clean_value

MODES = ("candidate", "research_fixture")
EXIT_TYPES = ("consolidation", "assemblage", "boundary_adjustment", "resale", "utility_easement", "conservation", "lease", "timber",
              "recreation", "other")
SALE_EXITS = ("consolidation", "assemblage", "boundary_adjustment", "resale")
NEED_LEVELS = ("high", "moderate", "low", "speculative")
PROXIMITY = ("directly_adjacent", "sub_market", "unknown")
CONTACT_STATUS = ("not_contacted",)            # LAND-001 never contacts anyone; later layers may add states
# Fields whose values are record identifiers: never redacted (see untrusted.py), only flagged.
IDENTIFIER_FIELDS = frozenset({"parcel_id", "rpc", "tacs", "legal_description", "vesting_instrument", "prior_conveyances",
                               "deeds_of_trust", "dot_releases"})
_ID_RX = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


@dataclass(frozen=True)
class PropertyRef:
    state: str
    county: str
    parcel_id: str
    label: str = ""

    @property
    def key(self) -> str:
        """Filesystem-safe stable id, e.g. VA-botetourt-18_2_1D. Also the case id for authorisations."""
        slug = lambda s: re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
        return f"{slug(self.state).upper()}-{slug(self.county).lower()}-{slug(self.parcel_id)}"


@dataclass
class Bundle:
    sources: List[Source] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    exits: List[dict] = field(default_factory=list)
    tax_ledger: List[Claim] = field(default_factory=list)
    adjacent: List[dict] = field(default_factory=list)
    counterparties: List[dict] = field(default_factory=list)
    costs: List[Line] = field(default_factory=list)
    revenue: List[Line] = field(default_factory=list)


class SourceAdapter(Protocol):
    name: str
    network: bool

    def collect(self, ref: PropertyRef) -> Bundle: ...


def _only(d: Any, allowed: set, where: str) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"{where}: expected an object")
    extra = set(d) - allowed
    if extra:
        raise ValueError(f"{where}: unknown key(s) {sorted(extra)}")
    return d


def _text(d: dict, key: str, flags: List[str], default: str = "", identifier: bool = False) -> str:
    s, f = clean(d.get(key, default) or "", identifier=identifier)
    flags += [f"{key}: {x}" for x in f]
    return s


def _ids(v: Any, where: str) -> List[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and _ID_RX.match(x) for x in v):
        raise ValueError(f"{where}: sources must be a list of source ids")
    return list(v)


def _num(v: Any, where: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError(f"{where}: amounts must be non-negative numbers or null")
    return float(v)


def _choice(v: Any, allowed: tuple, where: str, default: Optional[str] = None) -> str:
    v = default if v is None else v
    if v not in allowed:
        raise ValueError(f"{where}: must be one of {allowed}")
    return v


def _claim(c: dict, w: str, field_name: str, identifier: bool) -> Claim:
    fl: List[str] = []
    value, vf = clean_value(c.get("value"), identifier=identifier)
    fl += [f"value: {x}" for x in vf]
    if c.get("disqualifying") not in (None, True, False):
        raise ValueError(f"{w}: disqualifying must be true or false")
    return Claim(field=field_name, value=value, sources=_ids(c.get("sources", []), w), unit=_text(c, "unit", fl),
                 as_of=_text(c, "as_of", fl), notes=_text(c, "notes", fl), disqualifying=bool(c.get("disqualifying")), flags=fl)


def parse_bundle(raw: Any) -> Bundle:
    raw = _only(raw, {"sources", "claims", "exits", "tax_ledger", "adjacent", "counterparties", "costs", "revenue"}, "bundle")
    b = Bundle()
    for i, s in enumerate(raw.get("sources", [])):
        w = f"sources[{i}]"
        _only(s, {"id", "type", "title", "custodian", "retrieved_at", "as_of", "locator", "notes"}, w)
        if not _ID_RX.match(str(s.get("id", ""))):
            raise ValueError(f"{w}: bad id")
        fl: List[str] = []
        b.sources.append(Source(id=s["id"], type=SourceType(s["type"]), title=_text(s, "title", fl),
                                custodian=_text(s, "custodian", fl), retrieved_at=_text(s, "retrieved_at", fl),
                                as_of=_text(s, "as_of", fl), locator=_text(s, "locator", fl),
                                notes=_text(s, "notes", fl) + (" [" + "; ".join(fl) + "]" if fl else "")))
    for i, c in enumerate(raw.get("claims", [])):
        w = f"claims[{i}]"
        _only(c, {"field", "value", "unit", "sources", "as_of", "notes", "disqualifying"}, w)
        if c.get("field") not in schema.FIELDS:
            raise ValueError(f"{w}: unknown field {c.get('field')!r}")
        b.claims.append(_claim(c, w, c["field"], c["field"] in IDENTIFIER_FIELDS))
    for i, r in enumerate(raw.get("tax_ledger", [])):
        w = f"tax_ledger[{i}]"
        _only(r, {"item", "value", "unit", "sources", "as_of", "notes"}, w)
        fl: List[str] = []
        item = _text(r, "item", fl)
        if not item:
            raise ValueError(f"{w}: item is required")
        b.tax_ledger.append(_claim(r, w, item, False))
    for i, x in enumerate(raw.get("exits", [])):
        w = f"exits[{i}]"
        _only(x, {"id", "exit_type", "description", "use", "counterparty", "proximity", "demonstrated_need", "value_low", "value_high",
                  "value_basis", "months_low", "months_high", "dependencies", "sources"}, w)
        fl = []
        cp = x.get("counterparty")
        if cp is not None:
            _only(cp, {"name", "parcel_id", "contact_status"}, f"{w}.counterparty")
            cp = {"name": _text(cp, "name", fl), "parcel_id": _text(cp, "parcel_id", fl, identifier=True),
                  "contact_status": _choice(cp.get("contact_status"), CONTACT_STATUS, f"{w}.counterparty.contact_status", "not_contacted")}
        deps = x.get("dependencies", [])
        if not isinstance(deps, list):
            raise ValueError(f"{w}: dependencies must be a list")
        b.exits.append({
            "id": _text(x, "id", fl) or f"X{i + 1}", "exit_type": _choice(x.get("exit_type"), EXIT_TYPES, f"{w}.exit_type"),
            "description": _text(x, "description", fl), "use": _text(x, "use", fl), "counterparty": cp,
            "proximity": _choice(x.get("proximity"), PROXIMITY, f"{w}.proximity", "unknown"),
            "demonstrated_need": _choice(x.get("demonstrated_need"), NEED_LEVELS, f"{w}.demonstrated_need", "speculative"),
            "value_low": _num(x.get("value_low"), w), "value_high": _num(x.get("value_high"), w),
            "value_basis": _choice(x.get("value_basis"), BASES, f"{w}.value_basis", "unknown"),
            "months_low": _num(x.get("months_low"), w), "months_high": _num(x.get("months_high"), w),
            "dependencies": [clean(d)[0] for d in deps], "sources": _ids(x.get("sources", []), w), "flags": fl})
    for i, a in enumerate(raw.get("adjacent", [])):
        w = f"adjacent[{i}]"
        _only(a, {"parcel_id", "owner", "land_use", "relation", "sources", "notes"}, w)
        fl = []
        b.adjacent.append({k: _text(a, k, fl, identifier=(k == "parcel_id")) for k in ("parcel_id", "owner", "land_use", "relation", "notes")}
                          | {"sources": _ids(a.get("sources", []), w), "flags": fl})
    for i, p in enumerate(raw.get("counterparties", [])):
        w = f"counterparties[{i}]"
        _only(p, {"name", "type", "basis", "sources"}, w)
        fl = []
        b.counterparties.append({k: _text(p, k, fl) for k in ("name", "type", "basis")}
                                | {"sources": _ids(p.get("sources", []), w), "flags": fl})
    for key, target in (("costs", b.costs), ("revenue", b.revenue)):
        for i, l in enumerate(raw.get(key, [])):
            w = f"{key}[{i}]"
            _only(l, {"label", "kind", "category", "low", "high", "basis", "sources", "notes"}, w)
            kind = _choice(l.get("kind"), ("one_time", "annual"), f"{w}.kind")
            cats = REVENUE_CATEGORIES if key == "revenue" else ONE_TIME_CATEGORIES if kind == "one_time" else ANNUAL_CATEGORIES
            fl = []
            target.append(Line(label=_text(l, "label", fl), kind=kind, category=_choice(l.get("category"), cats, f"{w}.category"),
                               low=_num(l.get("low"), w), high=_num(l.get("high"), w),
                               basis=_choice(l.get("basis"), BASES, f"{w}.basis"), sources=_ids(l.get("sources", []), w),
                               notes=_text(l, "notes", fl), flags=fl))
    return b


class FixtureAdapter:
    """A local JSON evidence file for one property. Offline."""
    name = "fixture"
    network = False

    def __init__(self, path: Path):
        self.path = Path(path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        _only(raw, {"property", "mode", "evidence", "origin"}, str(self.path))
        p = _only(raw.get("property"), {"state", "county", "parcel_id", "label"}, "property")
        self.ref = PropertyRef(**{k: clean(p.get(k, ""), max_len=120, identifier=True)[0] for k in ("state", "county", "parcel_id", "label")})
        self.mode = _choice(raw.get("mode"), MODES, "mode", "candidate")
        self._bundle = parse_bundle(raw.get("evidence", {}))
        self.origin = raw.get("origin") or {}
        if self.origin.get("kind") == "mission_draft":
            self._bundle.sources = [Source(id=s.id, type=SourceType.LLM_REPORT, title=s.title, custodian=s.custodian,
                                           retrieved_at=s.retrieved_at, as_of=s.as_of, locator=s.locator,
                                           notes=(s.notes + " " if s.notes else "") + f"[from mission draft {self.origin.get('mission', '?')}: "
                                                 f"declared {s.type.value}, loaded as a lead until verified]",
                                           claimed_type=s.type.value) for s in self._bundle.sources]

    def collect(self, ref: PropertyRef) -> Bundle:
        if ref.key != self.ref.key:
            raise ValueError(f"fixture {self.path.name} is for {self.ref.key}, not {ref.key}")
        return self._bundle


def load_fixture(path: Path) -> Tuple[PropertyRef, str, FixtureAdapter]:
    a = FixtureAdapter(path)
    return a.ref, a.mode, a
