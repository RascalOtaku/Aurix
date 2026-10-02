"""src/foundation/land/hub.py - LandPilot's owner surface: what commands.py calls, case authorisations, and the acquisition gate.

    land                                   dossiers and waiting approvals
    land build <name>                      build a dossier from an evidence file (data/land/evidence/, then the bundled fixtures)
    land show <property>                   verdict, gate, true exposure, blockers, next steps
    land cap <property> <max> [<target>]   OWNER ONLY: authorise this case's maximum TRUE exposure (and optional target offer)
    land promote <m-xxxxxx> <name>         OWNER ONLY: move a mission's draft evidence into the evidence store (its sources stay leads)
    land propose <property>                make an acquisition card - only when the gate is ELIGIBLE (never for a research fixture)
    yes land a-xxxxxx / no land a-xxxxxx   decide a card

WHAT A CASE MAXIMUM IS - AND IS NOT. `land cap 14338 2000 1500` sets an UNDERWRITING CONSTRAINT: the dossier treats $2,000 as the most
TRUE EXPOSURE (all one-time acquisition costs) it may model for that property, and $1,500 as the owner's target offer. It is NOT
permission to spend money, not a wallet or budget, and it authorises no transaction of any size: not the purchase, not taxes, recording
fees, inspections, repairs or deposits. Every action still needs its own explicit approval, and AURIX moves no money at all.

Who can set it: only the owner, by typing `land cap` in the verified Telegram chat (commands.py; no button carries it). It is audited
and stored in data/land/cases.json, a protected path no agent tool can write. Evidence files cannot carry one (adapters.py rejects the
key), and no model, mission or automated workflow can raise it. Changing it (up OR down) revokes every open card and every standing
approval for that property. Without a case maximum the engine default ($500) applies.

The gate is LOCKED by default. Research, analysis, a recommendation, a high tier or "no major risks found" never unlock it. Only a
separate, explicit event does: `yes land a-xxxxxx` from the verified owner chat, which records
    decided_by, property, maximum_exposure, action, dossier_version, case authorisation, timestamp
and is refused if the dossier or the case authorisation changed after the card was made. An approval also stops being in force the
moment either changes afterwards (approval_in_force() is what any later action layer must check). A bare "yes" never decides a land
card, and an approval authorises AURIX to RECORD the owner's decision: it never signs, pays, contacts anyone or binds anything.

Protected component (approval_gate): agent tools cannot write src/foundation/land/ or data/land/.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit
from src.foundation import mission as foundation_mission
from src.foundation.land import dossier as engine
from src.foundation.land import store
from src.foundation.land.adapters import load_fixture
from src.foundation.land.evidence import Tier, meets

e = html.escape
PID = re.compile(r"^a-[0-9a-f]{6}$")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
_NAME = re.compile(r"^[A-Za-z0-9_\-]{2,60}$")
MAX_CASE_EXPOSURE_USD = 100_000          # sanity bound on a typed amount, not a policy


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def evidence_dirs() -> List[Path]:
    return [store.data_dir() / "evidence", FIXTURES]


def dossiers() -> List[dict]:
    """Latest dossier per property, newest first."""
    base = store.data_dir() / "dossiers"
    out = [d for d in (store.load_latest(p.name) for p in (base.iterdir() if base.is_dir() else []) if p.is_dir()) if d]
    return sorted(out, key=lambda d: d["evaluated_on"], reverse=True)


def _find(ref: str) -> List[dict]:
    r = (ref or "").lower()
    return [d for d in dossiers() if r in d["property"]["key"].lower() or r in (d["property"]["label"] or "").lower()]


# ------------------------------------------------------------------------------------------------------------------------------
# case authorisations (owner only)
# ------------------------------------------------------------------------------------------------------------------------------

def _cases_path() -> Path:
    return store.data_dir() / "cases.json"


def case_authorization(key: str) -> dict:
    c = _read(_cases_path(), {}).get(key)
    return dict(c, source="case") if isinstance(c, dict) else engine.default_authorization()


def authorize_case(key: str, max_usd: float, target_usd: Optional[float] = None, decided_by: str = "owner:telegram",
                   now: Optional[float] = None) -> str:
    """Record the owner's maximum TRUE exposure (and optional target offer) for one property. Takes effect at the next build."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", key or ""):
        return "That is not a property key (see <code>land</code>)."
    if not (0 < max_usd <= MAX_CASE_EXPOSURE_USD) or (target_usd is not None and not (0 < target_usd <= max_usd)):
        return "The maximum must be a positive amount, and a target offer must not exceed it."
    cases = _read(_cases_path(), {})
    old = cases.get(key, {}).get("max_exposure_usd", engine.DEFAULT_MAX_EXPOSURE_USD)
    rec = {"max_exposure_usd": float(max_usd), "target_offer_usd": None if target_usd is None else float(target_usd),
           "authorized_by": decided_by, "authorized_at": _when(now or time.time())}
    cases[key] = rec
    _atomic(_cases_path(), cases)
    audit.append("land_exposure_authorized", property=key, old_max_usd=old, max_usd=rec["max_exposure_usd"],
                 target_usd=rec["target_offer_usd"], decided_by=decided_by, semantics="underwriting constraint; not permission to spend")
    revoked = _invalidate(key, "CASE_AUTHORIZATION_CHANGED", changed_by=decided_by, old_max_usd=old, new_max_usd=rec["max_exposure_usd"])
    return (f"🔐 Underwriting limit for <code>{e(key)}</code>: at most <b>${max_usd:,.0f}</b> true exposure"
            + (f", target offer ${target_usd:,.0f} (a negotiation parameter, not a valuation)" if target_usd is not None else "")
            + ". This is not permission to spend: every action still needs its own approval, and AURIX moves no money. Logged."
            + (f" {revoked} open card(s)/approval(s) for this property are no longer in force." if revoked else "")
            + " It applies from the next <code>land build</code>.")


def _invalidate(key: str, reason: str, *, changed_by: str, **detail) -> int:
    """Pending cards -> STALE, approvals -> REVOKED, for one property. Nothing is deleted: the card keeps its full history, and the
    audit record carries the reason code, who caused it, the card id, the dossier version it covered and what changed."""
    items, n = proposals(), 0
    for c in items:
        if c["property"] == key and c["status"] in ("pending", "approved"):
            was = c["status"]
            c["status"] = "stale" if was == "pending" else "revoked"
            c["invalidation"] = {"reason": reason, "was": was, "changed_by": changed_by, "at": _when(time.time()), **detail}
            c["invalidated_because"] = reason
            audit.append("land_gate_stale" if was == "pending" else "land_gate_revoked", id=c["id"], property=key,
                         version=c["dossier_version"], card_max_usd=c["max_usd"], reason=reason, changed_by=changed_by, **detail)
            n += 1
    if n:
        _save(items)
    return n


def approval_in_force(key: str) -> Optional[dict]:
    """The owner approval that still covers this property RIGHT NOW, or None. It must be approved, for the latest dossier version,
    under the case maximum as it stands today. Anything that would act on an approval must call this first."""
    latest, auth = store.load_latest(key), case_authorization(key)
    for c in proposals():
        if (c["property"] == key and c["status"] == "approved" and latest is not None and latest["version"] == c["dossier_version"]
                and float(auth["max_exposure_usd"]) == c["max_usd"]
                and (auth.get("authorized_at") if auth["source"] == "case" else None) == c["case_authorized_at"]):
            return c
    return None


def cap_command(arg: str) -> str:
    """`<property> <max> [<target>]` from commands.py."""
    m = re.fullmatch(r"(\S+)\s+\$?([\d,]+(?:\.\d{1,2})?)(?:\s+\$?([\d,]+(?:\.\d{1,2})?))?", (arg or "").strip())
    if not m:
        return "Usage: <code>land cap &lt;property&gt; &lt;max&gt; [&lt;target&gt;]</code>, e.g. <code>land cap botetourt 2000 1500</code>"
    hits = _find(m.group(1))
    if len(hits) != 1:
        return show(m.group(1))
    num = lambda s: float(s.replace(",", ""))
    return authorize_case(hits[0]["property"]["key"], num(m.group(2)), num(m.group(3)) if m.group(3) else None)


# ------------------------------------------------------------------------------------------------------------------------------
# search criteria + the autonomous research-mission trigger (2026-10-01, owner: "we want landpilot to find the
# properties and do the runaround, thats the whole point" - L0/L1 dossiers alone never go looking for anything;
# this is the L2 piece __init__.py already described ("a mission contract for web research") but nothing had
# ever actually proposed one. Finding leads and building a verified dossier stay two separate jobs on purpose:
# a lead-scan mission is cheap and wide (find() in the sense of "what's out there"), a dossier is the narrow,
# evidence-graded "runaround" on ONE property once the owner picks a lead worth pursuing (land build, as today).
# ------------------------------------------------------------------------------------------------------------------------------

RESEARCH_COOLDOWN_DAYS = 7         # L3 (standing/unattended) is deliberately not allowed for this pack - this just
                                    # throttles how often a FRESH proposal is even offered, each one still needs a yes


def _criteria_path() -> Path:
    return store.data_dir() / "criteria.json"


def criteria() -> Optional[dict]:
    return _read(_criteria_path(), None)


def set_criteria(text: str, decided_by: str = "owner:telegram", now: Optional[float] = None) -> str:
    """Owner only, free text on purpose (no rigid region/budget/type fields to parse) - it is handed straight
    into the research mission's own goal description, the same way any other `mission: <goal>` is."""
    text = (text or "").strip()
    if not (8 <= len(text) <= 1000):
        return "Describe what to look for in 8-1000 characters, e.g. regions, budget, and the kind of problem property."
    rec = {"text": text, "set_by": decided_by, "set_at": _when(now or time.time())}
    _atomic(_criteria_path(), rec)
    audit.append("land_criteria_set", decided_by=decided_by, chars=len(text))
    return f"🗺️ Search criteria saved: {e(text)}\nAURIX will propose a research mission against this on its own (at most once every {RESEARCH_COOLDOWN_DAYS} days) - you still approve each one."


def criteria_text() -> str:
    c = criteria()
    return f"Current search criteria: {e(c['text'])}" if c else "No search criteria set yet. <code>land criteria: &lt;what to look for&gt;</code>"


def _research_state_path() -> Path:
    return store.data_dir() / "research_state.json"


async def check_research_trigger(llm, store_: Optional[Any] = None, session_id: Optional[str] = None,
                                  sandboxed: bool = False, now: Optional[float] = None, **presence_kw) -> Optional[str]:
    """The autonomous entry point: if the owner has set criteria, enough time has passed since the last
    proposal, and nothing is already pending, PROPOSE a real land_intelligence mission for it (planner.py's
    normal goal -> contract path, so it gets exactly the same tool/budget/approval rules as a typed
    `mission: <goal>` - never auto-started, land_intelligence's standing_ok is False). Returns owner-facing
    text, or None when there is nothing new to propose."""
    c = criteria()
    if c is None or llm is None:
        return None
    now = now or time.time()
    try:
        last = json.loads(_research_state_path().read_text(encoding="utf-8")).get("ts", 0)
    except (OSError, ValueError):
        last = 0
    if now - float(last) < RESEARCH_COOLDOWN_DAYS * 86400:
        return None
    ms = foundation_mission
    if any(m.status in (ms.MissionStatus.PROPOSED, ms.MissionStatus.ACTIVE) and "landpilot" in (m.objective or "").lower()
           for m in (store_ or ms.MissionStore()).all()):
        return None
    _atomic(_research_state_path(), {"ts": now})
    # 'listings' deliberately avoided below (2026-10-01): it is real_estate_leads's own
    # trigger word, same class of false-positive as the \bflip fix in capabilities.py -
    # this goal only ever means land_intelligence.
    goal = (f"LandPilot: search for cheap, distressed land matching this owner brief: \"{c['text'][:600]}\". "
            "Check county tax-delinquent/tax-sale records, land marketplaces (LandWatch, Land.com, etc.), and "
            "similar public sources. This is a LEAD SCAN, not a full dossier: for each promising candidate, write "
            "one JSON object with state, county, address_or_parcel, asking_price_usd, source_url, why_promising, "
            "known_problem. Save the full list as a JSON array at land_evidence/leads.json in this mission's own "
            "workspace - do not attempt a full evidenced dossier, that is a separate, later step (`land build`) "
            "the owner does once a lead looks worth pursuing.")
    from src.foundation import planner
    audit.append("land_research_triggered", chars=len(c["text"]))
    m = await planner.propose_mission(goal, llm=llm, session_id=session_id, store=store_, sandboxed=sandboxed, **presence_kw)
    return (f"🗺️ <b>Proposed a LandPilot research mission on its own</b>\nAgainst: {e(c['text'][:200])}\n"
            f"<code>approve mission {m.id}</code> / <code>deny mission {m.id}</code>")


def leads(mission_id: str) -> List[dict]:
    """A lead-scan mission's candidate list (land_evidence/leads.json in its workspace) - informational only,
    never auto-promoted. The owner reviews it and runs `land build` on whichever one looks worth the real
    evidence-gathering `land propose`/`land cap` flow needs."""
    if not _MID.match(mission_id or ""):
        return []
    path = Path(foundation_mission.workspace_for(mission_id)) / "land_evidence" / "leads.json"
    data = _read(path, [])
    return data if isinstance(data, list) else []


# ------------------------------------------------------------------------------------------------------------------------------
# promotion: mission workspace -> draft -> validation -> owner promotion -> evidence store
# ------------------------------------------------------------------------------------------------------------------------------

_MID = re.compile(r"^m-[0-9a-f]{6}$")


def promote(mission_id: str, name: str, promoted_by: str = "owner:telegram", now: Optional[float] = None) -> str:
    """Owner only. Copies <mission workspace>/land_evidence/<name>.json into data/land/evidence/, after strict validation, stamped
    with its origin (mission, draft sha256, who, when). The adapter loads every source in it as a LEAD: promotion moves a file into
    the store, it never upgrades what the file proves. Never overwrites an existing evidence file."""
    if not (_MID.match(mission_id or "") and _NAME.match(name or "")):
        return "Usage: <code>land promote m-xxxxxx &lt;name&gt;</code>"
    draft = Path(foundation_mission.workspace_for(mission_id)) / "land_evidence" / f"{name}.json"
    dest = store.data_dir() / "evidence" / f"{name}.json"
    try:
        blob = draft.read_bytes()
    except OSError:
        return f"No draft at <code>{e(mission_id)}/land_evidence/{e(name)}.json</code>."
    if dest.exists():
        audit.append("land_promotion_refused", mission=mission_id, name=name, why="evidence file already exists", attempted_by=promoted_by)
        return f"<code>{e(name)}</code> already exists in the evidence store; pick another name. Nothing was overwritten."
    try:
        raw = json.loads(blob.decode("utf-8"))
        if not isinstance(raw, dict) or "origin" in raw:
            raise ValueError("a draft may not carry its own origin block")
        raw["origin"] = {"kind": "mission_draft", "mission": mission_id, "draft_sha256": hashlib.sha256(blob).hexdigest(),
                         "promoted_by": promoted_by, "promoted_at": _when(now or time.time())}
        tmp = store.data_dir() / "evidence" / f".{name}.validating.json"
        _atomic(tmp, raw)
        try:
            load_fixture(tmp)                                  # the same strict parser a build uses; refuses anything malformed
        finally:
            tmp.unlink(missing_ok=True)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as ex:
        audit.append("land_promotion_refused", mission=mission_id, name=name, why=str(ex)[:160], attempted_by=promoted_by)
        return f"Draft refused, nothing promoted: {e(str(ex)[:300])}"
    _atomic(dest, raw)
    audit.append("land_evidence_promoted", mission=mission_id, name=name, draft_sha256=raw["origin"]["draft_sha256"],
                 promoted_by=promoted_by, trust="leads only (mission output)")
    return (f"📥 Promoted <code>{e(name)}</code> from {e(mission_id)}. Its sources load as <b>leads</b> (a mission's output is model "
            f"output); to become evidence, each fact needs its underlying record. <code>land build {e(name)}</code>")


# ------------------------------------------------------------------------------------------------------------------------------
# reading
# ------------------------------------------------------------------------------------------------------------------------------

def summary(d: dict, paths: Optional[dict] = None) -> str:
    p, rec, econ = d["property"], d["recommendation"], d["economics"]
    lines = [f"🏞️ <b>{e(p['label'] or p['parcel_id'])}</b>  <code>{e(p['key'])}</code>"]
    if rec.get("restriction"):
        lines.append(f"🔒 <i>{e(rec['restriction'])}</i>")
    lines += [f"<b>{e(rec['verdict'])}</b>: {e(rec['headline'])}", f"Gate: <b>{e(rec['gate'])}</b>",
              f"True exposure: {e(econ['true_exposure_text'])} ({e(econ['true_exposure_status'])}) · max ${econ['cap_usd']:,.0f}"
              f" ({'case' if econ['authorization']['source'] == 'case' else 'engine default'}) · carrying {e(econ['annual_carrying_text'])}/yr"]
    kills = [f for f in d["findings"] if f["outcome"] == "KILL"]
    blocks = [f for f in d["findings"] if f["outcome"] == "BLOCK"]
    for f in kills[:3]:
        lines.append(f"⛔ {e(f['rule'])}: {e(f['reason'])}")
    if blocks:
        lines.append(f"🟥 Blocking ({len(blocks)}): " + "; ".join(e(f["reason"].split(":")[0]) for f in blocks[:8]) + ("…" if len(blocks) > 8 else ""))
    conflicts = [f["label"] for f in d["fields"].values() if f["resolution_required"]]
    if conflicts:
        lines.append("⚖️ Conflicts needing resolution: " + ", ".join(e(c) for c in conflicts))
    if d["next_steps"]:
        lines.append("<b>Next:</b>")
        for s in d["next_steps"][:3]:
            lines.append(f"• {e(s['action'])}" + (" <i>(web research: an approved mission)</i>" if s["needs_web_mission"] else ""))
    lines.append(f"<i>{len(d['unknowns'])} open items · {len(d['red_team'])} red-team attacks standing · dossier {d['version'][:12]} · {d['evaluated_on']}</i>")
    if paths:
        lines.append(f"<i>Full report: {e(paths['md'].split('/data/', 1)[-1])}</i>")
    if rec["gate"].startswith("ELIGIBLE"):
        lines.append(f"Ready for an acquisition card: <code>land propose {e(p['key'])}</code>")
    return "\n".join(lines)


def overview() -> str:
    ds, waiting = dossiers(), pending()
    if not ds and not waiting:
        return ("🏞️ <b>LandPilot</b>: no dossiers yet. Research only (automation level 0): nothing is contacted, offered or bought.\n"
                "Try <code>land build botetourt</code> (the 14338 Botetourt Rd research fixture).")
    lines = ["🏞️ <b>LandPilot</b> · research only; the owner decides"]
    for d in ds[:10]:
        lines.append(f"• <b>{e(d['property']['label'] or d['property']['key'])}</b>: {e(d['recommendation']['verdict'])}, gate "
                     f"{e(d['recommendation']['gate'].split(':')[0])} · <code>land show {e(d['property']['key'])}</code>")
    for c in waiting:
        lines.append(f"🟡 Waiting on you: <code>{c['id']}</code> {e(c['label'])} up to ${c['max_usd']:,.0f} · "
                     f"<code>yes land {c['id']}</code> / <code>no land {c['id']}</code>")
    return "\n".join(lines)


def show(ref: str) -> str:
    hits = _find(ref)
    if not hits:
        return f"No dossier matches <code>{e(ref)}</code>. <code>land</code> lists them."
    if len(hits) > 1:
        return "Several match: " + ", ".join(f"<code>{e(d['property']['key'])}</code>" for d in hits[:8])
    return summary(hits[0])


def build(name: str, today: Optional[date] = None) -> str:
    if not _NAME.match(name or ""):
        return "Name the evidence file with letters, digits, - or _ only."
    matches = [p for d in evidence_dirs() if d.is_dir() for p in sorted(d.glob("*.json")) if name.lower() in p.stem.lower()]
    if not matches:
        known = sorted({p.stem for d in evidence_dirs() if d.is_dir() for p in d.glob("*.json")})
        return f"No evidence file matches <code>{e(name)}</code>. Available: " + (", ".join(f"<code>{e(k)}</code>" for k in known) or "none")
    if len({p.stem for p in matches}) > 1:
        return "Several match: " + ", ".join(f"<code>{e(p.stem)}</code>" for p in matches[:8])
    try:
        ref, mode, adapter = load_fixture(matches[0])
        d = engine.build(ref, [adapter], mode=mode, authorization=case_authorization(ref.key), today=today)
        paths = store.save(d)
        for c in proposals():
            if c["property"] == ref.key and c["status"] in ("pending", "approved") and c["dossier_version"] != d["version"]:
                _invalidate(ref.key, "DOSSIER_CHANGED", changed_by="land build", old_version=c["dossier_version"], new_version=d["version"])
                break
    except (ValueError, TypeError, KeyError, RuntimeError, PermissionError) as ex:
        audit.append("land_build_failed", file=matches[0].name, why=str(ex)[:160])
        return f"Could not build that dossier: {e(str(ex)[:300])}"
    return summary(d, paths)


# ------------------------------------------------------------------------------------------------------------------------------
# Gate 1: acquisition cards
# ------------------------------------------------------------------------------------------------------------------------------

def _proposals_path() -> Path:
    return store.data_dir() / "proposals.json"


def proposals() -> List[dict]:
    return _read(_proposals_path(), [])


def pending() -> List[dict]:
    return [c for c in proposals() if c["status"] == "pending"]


def _save(items: List[dict]) -> None:
    _atomic(_proposals_path(), items[-200:])


def _evidence_line(d: dict) -> str:
    """Per material section: OK if every field meets its required tier, else the weakest state found."""
    worst: Dict[str, str] = {}
    rank = ["OK", "UNVERIFIED", "STRONGLY_INDICATED", "VERIFIED_SECONDARY", "STALE", "CONTRADICTED", "UNKNOWN"]
    for f in d["fields"].values():
        if not f["material"]:
            continue
        state = "OK" if meets(Tier(f["tier"]), Tier(f["required"])) else f["tier"]
        s = f["section"]
        worst[s] = max(worst.get(s, "OK"), state, key=rank.index)
    return " · ".join(f"{s.upper()} {c}" for s, c in worst.items())


def card(c: dict, d: dict) -> str:
    econ = d["economics"]
    good = [x for x in d["exits"] if x["evidenced"]]
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━", "<b>LAND ACQUISITION</b>", "━━━━━━━━━━━━━━━━━━",
        f"<b>{e(c['label'])}</b>  <code>{e(c['property'])}</code>",
        f"True exposure: {e(econ['true_exposure_text'])} ({e(econ['true_exposure_status'])})",
        f"Annual carrying: {e(econ['annual_carrying_text'])}",
        "Evidenced exits: " + (", ".join(f"{e(x['exit_type'])} ({e(x['legal_use_status'])})" for x in good) or "none"),
        f"Evidence: {e(_evidence_line(d))}",
        "Red team: " + ("; ".join(e(r["attack"]) for r in d["red_team"][:2]) or "no attack survived the evidence"),
        f"<b>Maximum authorised: ${c['max_usd']:,.0f}</b> (case authorisation {e(c['case_authorized_at'] or 'engine default')})",
        f"<i>Approving records your decision for dossier {c['dossier_version'][:12]} only. It is not permission to spend: AURIX will not sign, pay, move money or contact anyone.</i>",
        f"<code>yes land {c['id']}</code> · <code>no land {c['id']}</code> · <code>land show {e(c['property'])}</code>",
    ])


def propose(ref: str, now: Optional[float] = None) -> str:
    hits = _find(ref)
    if len(hits) != 1:
        return show(ref)
    d = hits[0]
    key, rec = d["property"]["key"], d["recommendation"]
    if d["mode"] == "research_fixture":
        return "🔒 That property is a research fixture: no offer, no contact, no purchase. No card was made."
    if not rec["gate"].startswith("ELIGIBLE"):
        return (f"🔒 Gate LOCKED: the dossier says <b>{e(rec['verdict'])}</b>. No card was made. "
                f"<code>land show {e(key)}</code> lists what is missing.")
    auth = d["economics"]["authorization"]
    items = proposals()
    for c in items:
        if c["status"] == "pending" and c["property"] == key and c["dossier_version"] == d["version"]:
            return card(c, d)
    c = {"id": "a-" + secrets.token_hex(3), "gate": "acquisition", "property": key, "label": d["property"]["label"] or key,
         "dossier_version": d["version"], "max_usd": float(auth["max_exposure_usd"]),
         "case_authorized_at": auth.get("authorized_at") if auth["source"] == "case" else None,
         "action": "Record the owner's go-ahead to prepare an offer whose TRUE exposure stays within the maximum; "
                   "the owner signs and pays, AURIX does neither.",
         "status": "pending", "created": now or time.time()}
    items.append(c)
    _save(items)
    audit.append("land_gate_proposed", id=c["id"], gate="acquisition", property=key, version=d["version"], max_usd=c["max_usd"])
    return card(c, d)


def _decide(pid: str, approve: bool, decided_by: str, now: Optional[float]) -> str:
    if not PID.match(pid or ""):
        return "That is not a land card id (a-xxxxxx)."
    items = proposals()
    c = next((x for x in items if x["id"] == pid), None)
    if c is None:
        audit.append("land_gate_refused", id=pid, why="no such card", attempted_by=decided_by)
        return f"No land card <code>{e(pid)}</code>."
    if c["status"] != "pending":
        audit.append("land_gate_refused", id=pid, property=c["property"], why=f"card is {c['status']}", attempted_by=decided_by)
        return f"<code>{pid}</code> was already {e(c['status'])}" + (f" ({e(c['invalidated_because'])})" if c.get("invalidated_because") else "") + "."
    if approve:
        latest = store.load_latest(c["property"])
        auth = case_authorization(c["property"])
        changed = ("the dossier changed" if latest is None or latest["version"] != c["dossier_version"] else
                   "the case maximum changed" if float(auth["max_exposure_usd"]) != c["max_usd"]
                   or (auth.get("authorized_at") if auth["source"] == "case" else None) != c["case_authorized_at"] else "")
        if changed:
            c["status"] = "stale"
            _save(items)
            audit.append("land_gate_stale", id=pid, property=c["property"], version=c["dossier_version"], card_max_usd=c["max_usd"],
                         reason="DOSSIER_CHANGED" if "dossier" in changed else "CASE_AUTHORIZATION_CHANGED", changed_by="detected at approval",
                         attempted_by=decided_by)
            return (f"⚠️ Not approved: {changed} after this card was made, so this approval would not cover what you read. "
                    f"<code>land propose {e(c['property'])}</code> makes a fresh card.")
    ts = now or time.time()
    c.update(status="approved" if approve else "rejected", decided_by=decided_by, decided_at=ts, decided_at_iso=_when(ts))
    _save(items)
    audit.append("land_gate_approved" if approve else "land_gate_rejected", id=pid, gate=c["gate"], property=c["property"],
                 version=c["dossier_version"], max_usd=c["max_usd"], action=c["action"], decided_by=decided_by)
    if not approve:
        return f"❌ Rejected <b>{e(c['label'])}</b>. Logged."
    return (f"✅ OWNER APPROVED\nproperty = {e(c['property'])}\nmaximum_exposure = ${c['max_usd']:,.0f}\naction = {e(c['action'])}\n"
            f"dossier_version = {c['dossier_version'][:12]}\ntimestamp = {c['decided_at_iso']}\n"
            f"Nothing has been bought or sent. Logged to the audit chain.")


def approve(pid: str, decided_by: str = "owner:telegram", now: Optional[float] = None) -> str:
    return _decide(pid, True, decided_by, now)


def decline(pid: str, decided_by: str = "owner:telegram", now: Optional[float] = None) -> str:
    return _decide(pid, False, decided_by, now)


def panel() -> Dict[str, Any]:
    return {"dossiers": [{"key": d["property"]["key"], "label": d["property"]["label"], "verdict": d["recommendation"]["verdict"],
                          "gate": d["recommendation"]["gate"], "unknowns": len(d["unknowns"]), "version": d["version"][:12],
                          "evaluated_on": d["evaluated_on"], "mode": d["mode"]} for d in dossiers()[:20]],
            "pending": [{k: c[k] for k in ("id", "property", "label", "max_usd", "dossier_version")} for c in pending()]}
