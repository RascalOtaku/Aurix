"""economy/pipeline.py - an opportunity's life, from discovery to measured results, with evidence required at every step.

    discovered -> researched -> validated -> APPROVED -> built -> LAUNCHED -> measured -> SCALED
                                                 ^                                    |
                                                 +------------- modified <------------+        closed: from anywhere, with a reason

Each move has evidence requirements (REQUIREMENTS below) checked in code. A model saying something could earn $1,000/month is not
evidence; `customer_interest`, `presale` and `payment` evidence and collected revenue in the ledger are.

The three CAPITALISED moves commit money, go public or raise risk, so they never happen on a command alone: they produce an approval
card (action, cost, expected outcome, downside, permissions, stop conditions, deadline) and only the owner's `yes econ x-...`
moves the venture. Hard constraints are re-checked at the moment of approval. Raising a running venture's budget, startup cash,
loss or hours is refused - modify it and approve again instead. Nothing in this module spends, contacts or publishes anything.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.foundation import audit
from src.foundation.economy import freedom, ledger, scorecard, store

e = html.escape
STAGES = ("discovered", "researched", "validated", "approved", "built", "launched", "measured", "scaled", "modified", "closed")
NEXT = {"discovered": "researched", "researched": "validated", "validated": "approved", "approved": "built", "built": "launched",
        "launched": "measured", "measured": "scaled", "modified": "approved"}
NEEDS_APPROVAL = ("approved", "launched", "scaled")
LANES = ("research", "automation", "products", "software", "land", "other")
ID_RX = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
APPROVAL_RX = re.compile(r"^x-[0-9a-f]{6}$")
APPROVAL_TTL = 14 * 86400
NUMBER_FIELDS = ("startup_cash", "max_loss", "months_to_revenue", "net_low", "net_high", "owner_hours", "automation", "durability",
                 "reversibility", "strategic", "budget")
TEXT_FIELDS = ("name", "lane", "problem", "customer", "next_action", "deadline", "success_criteria", "stop_conditions", "notes")
RISK_FIELDS = ("startup_cash", "max_loss", "owner_hours", "budget")
FIELD_ALIASES = {"startup": "startup_cash", "cash": "startup_cash", "loss": "max_loss", "months": "months_to_revenue",
                 "low": "net_low", "high": "net_high", "hours": "owner_hours", "success": "success_criteria",
                 "stop": "stop_conditions", "next": "next_action", "due": "deadline"}
PERMISSIONS = {
    "approved": "You may spend up to the budget on the next action. AURIX may research, draft, build and test in the sandbox; "
                "it contacts nobody, publishes nothing and pays for nothing.",
    "launched": "Going public: the listing, outreach or offer described in the next action. AURIX drafts it; you send, post or sell.",
    "scaled": "More budget and hours for a venture that has measured, positive results.",
}


def get(opp_id: str) -> Optional[dict]:
    return store.one("SELECT * FROM opportunities WHERE id = ?", ((opp_id or "").lower(),))


def create(opp_id: str, name: str, lane: str = "other", origin: str = "owner", **fields) -> dict:
    opp_id = (opp_id or "").lower()
    if not ID_RX.match(opp_id):
        raise ValueError("an id is 3-31 lowercase letters, digits or _ and starts with a letter")
    if get(opp_id):
        raise ValueError(f"{opp_id} already exists")
    lane = lane if lane in LANES else "other"
    t = store.now()
    cols = {"id": opp_id, "name": name[:120], "lane": lane, "origin": origin, "created": t, "updated": t}
    for k, v in fields.items():
        if k in NUMBER_FIELDS:
            cols[k] = float(v)
        elif k in TEXT_FIELDS:
            cols[k] = str(v)[:600]
    store.execute(f"INSERT INTO opportunities({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", tuple(cols.values()))
    store.execute("INSERT INTO stage_log(opp_id, from_stage, to_stage, by, reason, at) VALUES(?,?,?,?,?,?)",
                  (opp_id, "", "discovered", origin, "created", t))
    audit.append("economy_opportunity", id=opp_id, origin=origin, lane=lane)
    return get(opp_id)


def set_field(opp_id: str, field: str, value: str) -> str:
    opp = get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>. <code>opps</code> lists them."
    field = FIELD_ALIASES.get(field.lower(), field.lower())
    if field in NUMBER_FIELDS:
        try:
            v = float(str(value).replace("$", "").replace(",", ""))
        except ValueError:
            return f"{field} needs a number."
        if v < 0 or (field in ("automation", "durability", "reversibility", "strategic") and v > 1):
            return f"{field} must be {'0..1' if field in ('automation', 'durability', 'reversibility', 'strategic') else 'zero or more'}."
        if opp["stage"] in scorecard.RUNNING and field in RISK_FIELDS and v > opp[field]:
            return (f"<b>{e(opp['name'])}</b> is running ({opp['stage']}). Raising its {field} raises its risk, which needs your approval: "
                    f"<code>opp {opp['id']} modify: why</code>, change it, then <code>opp {opp['id']} advance</code>.")
    elif field in TEXT_FIELDS:
        v = str(value).strip()[:600]
        if field == "lane" and v not in LANES:
            return "lane is one of: " + ", ".join(LANES)
        if field == "deadline" and v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                return "deadline is YYYY-MM-DD."
    else:
        return "Fields: " + ", ".join(NUMBER_FIELDS + TEXT_FIELDS)
    store.execute(f"UPDATE opportunities SET {field} = ?, updated = ? WHERE id = ?", (v, store.now(), opp["id"]))
    audit.append("economy_field", id=opp["id"], field=field)
    return f"<b>{e(opp['name'])}</b>: {field} = {e(str(v))}."


def add_evidence(opp_id: str, kind: str, summary: str, source: str = "") -> str:
    opp = get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>."
    kind = kind.lower()
    if kind not in scorecard.EVIDENCE_KINDS:
        return "Evidence kinds: " + ", ".join(scorecard.EVIDENCE_KINDS)
    if len(summary.strip()) < 4:
        return "Say what the evidence is."
    eid = store.new_id("v")
    store.execute("INSERT INTO evidence(id, opp_id, kind, summary, source, at) VALUES(?,?,?,?,?,?)",
                  (eid, opp["id"], kind, summary.strip()[:600], source.strip()[:300], store.now()))
    store.execute("UPDATE opportunities SET updated = ? WHERE id = ?", (store.now(), opp["id"]))
    audit.append("economy_evidence", id=eid, opp=opp["id"], kind=kind)
    note = ""
    if kind in scorecard.NEEDS_SOURCE and not source.strip():
        note = "\n<i>No source link: kept as a note, but it does not count toward a stage until you add one with a link.</i>"
    elif kind == "hypothesis":
        note = "\n<i>A hypothesis: recorded so it can be tested, never counted as evidence.</i>"
    return f"🧾 Logged <b>{kind}</b> for <b>{e(opp['name'])}</b>. Demand evidence now: <b>{scorecard.demand(opp['id'])}</b>." + note


def requirements(opp: dict, to: str) -> List[str]:
    """What is still missing for opp to move to `to` (empty = ready)."""
    ev = [x["kind"] for x in scorecard.evidence(opp["id"])]
    sourced = [x["kind"] for x in scorecard.evidence(opp["id"]) if x["source"].strip()]
    miss: List[str] = []
    if to == "researched":
        if "research" not in sourced:
            miss.append("`research` evidence with a source link: what the market looks like")
        if "competitor" not in sourced:
            miss.append("`competitor` evidence with a source link: who already sells this, and for how much")
    elif to == "validated":
        if scorecard.demand(opp["id"]) == "none":
            miss.append("demand evidence: `customer_interest`, `presale` or `payment` (a model's opinion does not count)")
        if scorecard.legal(opp["id"]) != "checked":
            miss.append("`legal_ok` evidence: permitted use, licences and platform terms checked")
        if opp["net_high"] <= 0:
            miss.append("a projected monthly net (`opp ID set high 300`)")
    elif to == "approved":
        miss += [f"hard limit: {b}" for b in scorecard.constraints(opp)]
        if scorecard.legal(opp["id"]) != "checked":
            miss.append("`legal_ok` evidence")
        for f, label in (("success_criteria", "success criteria"), ("stop_conditions", "stop conditions"),
                         ("next_action", "the next action"), ("deadline", "a deadline (YYYY-MM-DD)")):
            if not opp[f].strip():
                miss.append(f"{label} (`opp {opp['id']} set {f.split('_')[0]} ...`)")
    elif to == "built":
        if "build" not in ev:
            miss.append("`build` evidence: the sample deliverable or working workflow exists")
    elif to == "launched":
        miss += [f"hard limit: {b}" for b in scorecard.constraints(opp)]
    elif to == "measured":
        launched = store.one("SELECT at FROM stage_log WHERE opp_id = ? AND to_stage = 'launched' ORDER BY at DESC", (opp["id"],))
        collected = ledger.venture_totals(opp["id"])["collected"]
        if not collected and not (launched and store.now() - launched["at"] >= 30 * 86400):
            miss.append("collected revenue in the ledger, or 30 days since launch (no sales after 30 days is a result too)")
    elif to == "scaled":
        if ledger.venture_totals(opp["id"])["net"] <= 0:
            miss.append("positive measured net (collected revenue minus costs)")
        miss += [f"hard limit: {b}" for b in scorecard.constraints(opp)]
    return miss


def _move(opp: dict, to: str, by: str, reason: str = "") -> None:
    t = store.now()
    store.execute("UPDATE opportunities SET stage = ?, updated = ? WHERE id = ?", (to, t, opp["id"]))
    store.execute("INSERT INTO stage_log(opp_id, from_stage, to_stage, by, reason, at) VALUES(?,?,?,?,?,?)",
                  (opp["id"], opp["stage"], to, by, reason[:300], t))
    if to == "approved":                                     # freeze the forecast the owner approved, for the learning loop
        store.execute("INSERT INTO forecasts(opp_id, at, net_mid, owner_hours, months_to_revenue) VALUES(?,?,?,?,?)",
                      (opp["id"], t, (opp["net_low"] + opp["net_high"]) / 2.0, opp["owner_hours"], opp["months_to_revenue"]))
    audit.append("economy_stage", id=opp["id"], frm=opp["stage"], to=to, by=by)


def card(opp: dict, to: str, aid: str) -> str:
    p = freedom.profile()
    mid = (opp["net_low"] + opp["net_high"]) / 2.0
    action = {"approved": "Start this venture", "launched": "Launch it publicly", "scaled": "Scale it up"}[to]
    return "\n".join([
        f"💼 <b>{action}: {e(opp['name'])}</b> <code>{opp['id']}</code>",
        f"<b>Action:</b> {e(opp['next_action'] or '(none written)')}",
        f"<b>Cost:</b> up to ${opp['budget']:,.2f} (startup cash at risk ${opp['startup_cash']:,.0f}; your test limit ${p['test_cash']:,.0f})",
        f"<b>Expected:</b> {e(opp['success_criteria'] or '(none written)')} · projected ${opp['net_low']:,.0f}-{opp['net_high']:,.0f}/mo "
        f"<i>(an assumption, demand evidence: {scorecard.demand(opp['id'])})</i>",
        f"<b>Downside:</b> up to ${opp['max_loss']:,.0f} · {opp['owner_hours']:g} h/week of your time",
        f"<b>Permissions:</b> {PERMISSIONS[to]}",
        f"<b>Stops when:</b> {e(opp['stop_conditions'] or '(none written)')} · deadline {e(opp['deadline'] or 'none')}",
        f"<i>Only your tap or</i> <code>yes econ {aid}</code> <i>moves it; AURIX's own judgement never does. Projected midpoint ${mid:,.0f}/mo.</i>"])


def buttons(aid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"do:yes econ {aid}"},
                                 {"text": "❌ Deny", "callback_data": f"do:no econ {aid}"}]]}


def advance(opp_id: str, by: str = "owner") -> Tuple[str, Optional[dict]]:
    """Move to the next stage if its evidence is in; for approval stages, issue the approval card. (text, buttons)."""
    opp = get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>. <code>opps</code> lists them.", None
    to = NEXT.get(opp["stage"])
    if not to:
        return f"<b>{e(opp['name'])}</b> is {opp['stage']}: nothing comes next (modify or close it).", None
    miss = requirements(opp, to)
    if miss:
        return (f"<b>{e(opp['name'])}</b> cannot move {opp['stage']} → {to} yet. Missing:\n" + "\n".join(f"• {e(m)}" for m in miss)), None
    if to in NEEDS_APPROVAL:
        open_ = store.one("SELECT id FROM approvals WHERE opp_id = ? AND to_stage = ? AND status = 'pending'", (opp["id"], to))
        aid = open_["id"] if open_ else store.new_id("x")
        text = card(opp, to, aid)
        if not open_:
            store.execute("INSERT INTO approvals(id, opp_id, action, to_stage, cost, card, status, created) VALUES(?,?,?,?,?,?,?,?)",
                          (aid, opp["id"], {"approved": "approve", "launched": "launch", "scaled": "scale"}[to], to,
                           opp["budget"], text, "pending", store.now()))
            audit.append("economy_approval_requested", id=aid, opp=opp["id"], to=to)
        return text, buttons(aid)
    _move(opp, to, by)
    return f"➡️ <b>{e(opp['name'])}</b>: {opp['stage']} → <b>{to}</b>. Next test: {e(scorecard.next_test(get(opp['id'])))}", None


def resolve(aid: str, approve: bool, by: str = "owner") -> str:
    aid = (aid or "").lower()
    ap = store.one("SELECT * FROM approvals WHERE id = ?", (aid,))
    if not ap:
        return f"No economic approval <code>{e(aid)}</code>."
    if ap["status"] != "pending":
        return f"<code>{aid}</code> was already {ap['status']}."
    if store.now() - ap["created"] > APPROVAL_TTL:
        store.execute("UPDATE approvals SET status = 'expired', resolved = ? WHERE id = ?", (store.now(), aid))
        return f"<code>{aid}</code> expired (older than 14 days). <code>opp {ap['opp_id']} advance</code> issues a fresh card."
    opp = get(ap["opp_id"])
    if not approve:
        store.execute("UPDATE approvals SET status = 'denied', resolved = ? WHERE id = ?", (store.now(), aid))
        audit.append("economy_approval", id=aid, opp=ap["opp_id"], decision="denied")
        return f"❌ Denied. <b>{e(opp['name'])}</b> stays {opp['stage']}."
    if opp["stage"] != {"approved": "validated", "launched": "built", "scaled": "measured"}[ap["to_stage"]] and not (
            ap["to_stage"] == "approved" and opp["stage"] == "modified"):
        return f"<b>{e(opp['name'])}</b> has moved on since this card ({opp['stage']}); nothing was changed."
    if opp["budget"] > ap["cost"]:
        return "The budget was raised after this card was issued; approve the fresh card instead (<code>opp " + opp["id"] + " advance</code>)."
    miss = requirements(opp, ap["to_stage"])
    if miss:
        return "Not approved - things changed since the card:\n" + "\n".join(f"• {e(m)}" for m in miss)
    store.execute("UPDATE approvals SET status = 'approved', resolved = ? WHERE id = ?", (store.now(), aid))
    audit.append("economy_approval", id=aid, opp=opp["id"], decision="approved")
    _move(opp, ap["to_stage"], by, f"approval {aid}")
    return (f"✅ <b>{e(opp['name'])}</b> is now <b>{ap['to_stage']}</b> with a budget of ${opp['budget']:,.2f}. "
            f"Log what you spend (<code>spent 12 {opp['id']} what</code>) and the hours you put in (<code>hours 2 {opp['id']}</code>).")


def modify(opp_id: str, reason: str, close: bool = False) -> str:
    opp = get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>."
    if len((reason or "").strip()) < 4:
        return "Give a reason (it is what the review learns from)."
    to = "closed" if close else "modified"
    if not close and opp["stage"] not in ("built", "launched", "measured", "scaled"):
        return "Only a built or running venture can be modified; earlier ideas just get new evidence or fields."
    if opp["stage"] == "closed":
        return "Already closed."
    store.execute("UPDATE approvals SET status = 'expired', resolved = ? WHERE opp_id = ? AND status = 'pending'", (store.now(), opp["id"]))
    _move(opp, to, "owner", reason.strip())
    if reason.strip():
        store.execute("INSERT INTO evidence(id, opp_id, kind, summary, source, at) VALUES(?,?,?,?,?,?)",
                      (store.new_id("v"), opp["id"], "lesson", reason.strip()[:600], "owner", store.now()))
    return (f"🛑 Closed <b>{e(opp['name'])}</b>. The reason is kept as a lesson. Stopping a weak idea early is a result, not a failure."
            if close else f"🔧 <b>{e(opp['name'])}</b> is now <b>modified</b>: change its plan, then <code>opp {opp['id']} advance</code> for a fresh approval.")
