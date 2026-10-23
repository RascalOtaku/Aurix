"""economy/validation.py - earn the right to pursue an opportunity: a measured sample, a real offer, one capped experiment.

The researched -> validated gate is a checklist, every item backed by a record (not a feeling):

    1. a complete, sourced sample         `sample <id>: ...` with >= 90% of findings traceable to an authoritative source
    2. its real cost measured             the same record: owner minutes, machine minutes, direct cost
    3. a differentiator                   `evidence <id> differentiator: ...` - why this beats free tools and cheap competitors
    4. a specific offer                   `evidence <id> offer: ...` - price, scope, and what is NOT included
    5. a capped validation experiment     `opp <id> experiment $cap: plan` -> an approval card -> `yes econ x-...`
    6. its result, and demand reviewed    `opp <id> result: ...` and real demand evidence (customer_interest / presale / payment)
    7. the rules checked                  `legal_ok` evidence

An experiment can only be PROPOSED once 1-4 and 7 are in: you cannot test demand for an offer you have not defined, or contact people
before the rules are checked. Its cap must fit the owner's test limit. Approving it permits exactly the plan on the card - the
outreach or listing described, spending up to the cap - and nothing else; AURIX drafts, the owner sends.

If the result shows no demand, the review says so and points at the next-best opportunity instead of polishing a weak one.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

from src.foundation import audit
from src.foundation.economy import freedom, scorecard, store

e = html.escape
VALUES = ("none", "low", "medium", "high")
SOURCED_SHARE = 0.9
SAMPLE_KEYS = {"owner": "owner_min", "owner_min": "owner_min", "machine": "machine_min", "machine_min": "machine_min", "cost": "cost",
               "findings": "findings", "sourced": "sourced", "unresolved": "unresolved", "value": "value"}


def samples(opp_id: str) -> List[dict]:
    return store.rows("SELECT * FROM samples WHERE opp_id = ? ORDER BY at", (opp_id,))


def experiments(opp_id: str) -> List[dict]:
    return store.rows("SELECT * FROM experiments WHERE opp_id = ? ORDER BY created", (opp_id,))


def parse_sample(text: str) -> Tuple[Dict[str, object], str]:
    """'owner=95 machine=14 cost=0 findings=15 sourced=14 unresolved=4 value=high what it showed' -> (fields, note)."""
    fields: Dict[str, object] = {}
    rest = []
    for tok in text.split():
        m = re.fullmatch(r"([a-z_]+)=(\S+)", tok, re.I)
        if m and m.group(1).lower() in SAMPLE_KEYS:
            fields[SAMPLE_KEYS[m.group(1).lower()]] = m.group(2)
        else:
            rest.append(tok)
    return fields, " ".join(rest)


def record_sample(opp_id: str, text: str) -> str:
    from src.foundation.economy import pipeline
    opp = pipeline.get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>."
    f, note = parse_sample(text)
    try:
        owner, machine = float(f["owner_min"]), float(f.get("machine_min", 0))
        cost = float(str(f.get("cost", 0)).replace("$", ""))
        findings, sourced, unresolved = int(f.get("findings", 0)), int(f.get("sourced", 0)), int(f.get("unresolved", 0))
    except (KeyError, ValueError):
        return ("Say it like <code>sample property_dossiers: owner=95 machine=14 cost=0 findings=15 sourced=14 unresolved=4 "
                "value=medium what a buyer learns that free tools would not show</code> (owner= is required).")
    value = str(f.get("value", "")).lower()
    if value not in VALUES:
        return "value= is your honest judgement of decision value against free tools: none, low, medium or high."
    if min(owner, machine, cost, findings, sourced, unresolved) < 0 or sourced > findings:
        return "Numbers must be zero or more, and sourced cannot exceed findings."
    if len(note) < 8:
        return "Add a sentence on what a buyer learns from it that free tools would not show - that is the point of the sample."
    sid = store.new_id("s")
    store.execute("INSERT INTO samples(id, opp_id, owner_min, machine_min, cost, findings, sourced, unresolved, value, note, at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (sid, opp["id"], owner, machine, cost, findings, sourced, unresolved, value,
                                                     note[:600], store.now()))
    store.execute("INSERT INTO evidence(id, opp_id, kind, summary, source, at) VALUES(?,?,?,?,?,?)",
                  (store.new_id("v"), opp["id"], "build", f"sample {sid}: {note[:300]}", f"sample:{sid}", store.now()))
    if owner:
        from src.foundation.economy import ledger
        ledger.log_hours(round(owner / 60.0, 2), opp["id"], f"sample {sid}", category="production")
    audit.append("economy_sample", id=sid, opp=opp["id"], owner_min=owner, machine_min=machine, value=value)
    share = (sourced / findings) if findings else 0.0
    verdict = []
    if share < SOURCED_SHARE:
        verdict.append(f"only {share:.0%} of findings are sourced (the gate needs {SOURCED_SHARE:.0%})")
    if value in ("none", "low"):
        verdict.append("decision value is low - a report that repeats what free tools show will not justify a fee")
    return (f"🧪 Sample <code>{sid}</code> recorded for <b>{e(opp['name'])}</b>: you {owner:g} min · AURIX {machine:g} min · "
            f"${cost:,.2f} · {sourced}/{findings} sourced · {unresolved} questions for a professional · value <b>{value}</b>."
            + ("\n⚠️ " + "; ".join(verdict) + "." if verdict else ""))


def checklist(opp: dict) -> List[Tuple[str, bool]]:
    ev = {x["kind"] for x in scorecard.evidence(opp["id"])}
    ss = samples(opp["id"])
    good = [s for s in ss if s["findings"] and s["sourced"] / s["findings"] >= SOURCED_SHARE]
    exps = experiments(opp["id"])
    approved = [x for x in exps if x["status"] in ("running", "done")]
    done = [x for x in exps if x["status"] == "done" and x["result"].strip()]
    return [("a complete, sourced sample (≥90% of findings traceable)", bool(good)),
            ("its owner time, machine time and direct cost recorded", bool(ss)),
            ("the differentiator from free tools and cheap competitors", "differentiator" in ev),
            ("a specific offer: price, scope, exclusions", "offer" in ev),
            ("the rules checked (`legal_ok`)", scorecard.legal(opp["id"]) == "checked"),
            ("a validation experiment with a spending cap, approved", bool(approved)),
            ("its result recorded and real demand evidence reviewed", bool(done) and scorecard.demand(opp["id"]) != "none")]


def checklist_text(opp: dict) -> str:
    items = checklist(opp)
    return (f"<b>Validation gate</b> {sum(d for _, d in items)}/{len(items)}:\n"
            + "\n".join(f"{'✅' if d else '⬜'} {e(label)}" for label, d in items))


def running(opp_id: str) -> Optional[dict]:
    return store.one("SELECT * FROM experiments WHERE opp_id = ? AND status = 'running' ORDER BY started DESC", (opp_id,))


def propose(opp_id: str, cap: float, plan: str) -> str:
    from src.foundation.economy import pipeline
    opp = pipeline.get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>."
    if opp["stage"] in ("discovered", "closed"):
        return "Research it first (stage researched), then test it."
    if running(opp["id"]):
        return "An experiment is already running for this one: record its result first (<code>opp ID result: ...</code>)."
    before = [label for label, d in checklist(opp)[:5] if not d]
    if before:
        return "Not ready to test demand yet - missing:\n" + "\n".join(f"• {e(x)}" for x in before)
    p = freedom.profile()
    if cap > p["test_cash"]:
        return f"A ${cap:,.2f} cap is over your test limit of ${p['test_cash']:,.2f}. Lower it, or raise the limit yourself first."
    if len(plan.strip()) < 15:
        return "Describe the plan: exactly what is listed or who is contacted, where, how many, and for how long."
    xid, aid, t = store.new_id("e"), store.new_id("x"), store.now()
    text = "\n".join([
        f"🧪 <b>Validation experiment: {e(opp['name'])}</b> <code>{opp['id']}</code>",
        f"<b>Plan:</b> {e(plan.strip())}",
        f"<b>Spending cap:</b> ${cap:,.2f} (your test limit ${p['test_cash']:,.2f})",
        "<b>Expected:</b> evidence of demand for THIS offer - replies, presales or a paid order - or a clear no.",
        f"<b>Downside:</b> at most ${cap:,.2f} plus your time (log it: <code>hours 1 {opp['id']} acquisition</code>).",
        "<b>Permissions:</b> exactly the listing or outreach above, and spending up to the cap. AURIX drafts; you send, post or pay.",
        "<b>Stops when:</b> the cap is spent, 30 days pass, or the answer is clear - then <code>opp " + opp["id"] + " result: ...</code>",
        f"<i>Only your tap or</i> <code>yes econ {aid}</code> <i>starts it.</i>"])
    store.execute("INSERT INTO experiments(id, opp_id, plan, cap, status, approval, created) VALUES(?,?,?,?,?,?,?)",
                  (xid, opp["id"], plan.strip()[:600], cap, "proposed", aid, t))
    store.execute("INSERT INTO approvals(id, opp_id, action, to_stage, cost, card, status, created) VALUES(?,?,?,?,?,?,?,?)",
                  (aid, opp["id"], "experiment", opp["stage"], cap, text, "pending", t))
    audit.append("economy_experiment_proposed", id=xid, opp=opp["id"], cap=cap)
    return text


def resolve(ap: dict, approve: bool) -> str:
    x = store.one("SELECT * FROM experiments WHERE approval = ?", (ap["id"],))
    if not x:
        return "That experiment no longer exists."
    t = store.now()
    if not approve:
        store.execute("UPDATE approvals SET status = 'denied', resolved = ? WHERE id = ?", (t, ap["id"]))
        store.execute("UPDATE experiments SET status = 'denied', ended = ? WHERE id = ?", (t, x["id"]))
        return "❌ Denied. Nothing is contacted, listed or spent."
    if x["cap"] > freedom.profile()["test_cash"]:
        return "Not approved: the cap is now over your test limit."
    store.execute("UPDATE approvals SET status = 'approved', resolved = ? WHERE id = ?", (t, ap["id"]))
    store.execute("UPDATE experiments SET status = 'running', started = ? WHERE id = ?", (t, x["id"]))
    audit.append("economy_experiment_started", id=x["id"], opp=x["opp_id"], cap=x["cap"])
    return (f"✅ Experiment <code>{x['id']}</code> is running with a ${x['cap']:,.2f} cap. Log spend (<code>spent 5 {x['opp_id']} ...</code>), "
            f"your time (<code>hours 1 {x['opp_id']} acquisition</code>) and every reply as evidence; finish with "
            f"<code>opp {x['opp_id']} result: ...</code>.")


def result(opp_id: str, text: str) -> str:
    from src.foundation.economy import pipeline
    opp = pipeline.get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>."
    x = running(opp["id"])
    if not x:
        return "No experiment is running for this one."
    if len(text.strip()) < 8:
        return "Say what happened - replies, orders, silence - in a sentence."
    store.execute("UPDATE experiments SET status = 'done', result = ?, ended = ? WHERE id = ?", (text.strip()[:600], store.now(), x["id"]))
    store.execute("INSERT INTO evidence(id, opp_id, kind, summary, source, at) VALUES(?,?,?,?,?,?)",
                  (store.new_id("v"), opp["id"], "lesson", f"experiment {x['id']}: {text.strip()[:500]}", f"experiment:{x['id']}", store.now()))
    audit.append("economy_experiment_done", id=x["id"], opp=opp["id"])
    d = scorecard.demand(opp["id"])
    if d == "none":
        nxt = next((r for r in scorecard.ranked() if r["id"] != opp["id"] and not r["blocked"]), None)
        alt = f" The next-best candidate is <b>{e(nxt['name'])}</b> (<code>opp {nxt['id']}</code>)." if nxt else ""
        return (f"📉 Recorded. No demand evidence for <b>{e(opp['name'])}</b>. That is a result: close it "
                f"(<code>opp {opp['id']} close: why</code>), or change the offer and test once more - not endlessly.{alt}")
    return f"📈 Recorded. Demand evidence: <b>{d}</b>. <code>opp {opp['id']} advance</code> checks the validation gate."
