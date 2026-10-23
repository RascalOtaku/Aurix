"""economy/hub.py - the Economic Engine's owner-facing side: the Freedom Dashboard, the opportunity desk, the weekly review and the
monthly close, as Telegram text. Every command here is owner-only (commands.py parses it after the sender is verified).

    freedom                              the Freedom Dashboard (the governing question, answered with numbers)
    freedom set <field> <amount>         essentials · irregular (per year) · margin % · reserve (months) · saved · tax % · wage ($/h)
                                         and the hard limits: test (cash per experiment) · loss (max loss) · time (hours/week)
    opps                                 every open opportunity, ranked (blocked ones last, with the reason)
    opp <id>                             the business case, scorecard, evidence, and what the next stage needs
    opp new <id> <lane>: <name>          add one (lanes: research automation products software land other)
    opp import <money id>                bring an idea in from the money catalog
    opp <id> set <field> <value>         startup · loss · months · low · high · hours · budget · success · stop · next · due · ...
    evidence <id> <kind>: <what> [url]   research competitor customer_interest presale payment cost_quote legal_ok legal_problem build risk lesson hypothesis differentiator offer
                                         (research/competitor need a source link to count; a hypothesis never counts)
    opp <id> advance                     next stage if the evidence is in; approval stages produce a card
    yes econ <x-id> / no econ <x-id>     decide an approval card
    sample <id>: owner=95 machine=14 cost=0 findings=15 sourced=14 unresolved=4 value=medium <what it shows>
                                         a measured sample deliverable (`land buyer <key>` prints one ready to fill in)
    opp <id> experiment $<cap>: <plan>   a capped validation experiment -> an approval card
    opp <id> result: <what happened>     ends it; no demand = close it or change the offer once, then move on
    opp <id> modify: <why> / close: <why>
    booked <amount> <id> [note]          a sale agreed, not yet paid       (earned <amount> <id> = cash collected)
    refund <amount> <id> [note]          money given back to a customer
    spent <amount> <id> [note]           money actually paid out
    hours <h> <id> [category] [note]     your time: production · acquisition · revision · support · admin
    review                               the weekly portfolio review now   ·   close month [YYYY-MM]   the monthly close
    econ check                           after a deploy: version, settings, database, held cards, a live approval round-trip
"""
from __future__ import annotations

import html
from typing import Optional

from src.foundation.economy import freedom, learning, ledger, pipeline, scorecard, seed, store, validation

e = html.escape
SEP = "\t"


def _money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _bar(frac: float, width: int = 10) -> str:
    n = max(0, min(width, round(frac * width)))
    return "▰" * n + "▱" * (width - n) + f" {frac:.0%}"


def _seeded_note() -> str:
    made = seed.seed_if_empty()
    if not made:
        return ""
    if "property_dossiers" not in made:
        return (f"\n<i>Added {len(made)} variant(s) of the property offer to compare on the same scorecard: "
                + ", ".join(f"<code>{m}</code>" for m in made) + ".</i>")
    return (f"\n<i>First run: I wrote down {len(made)} candidate opportunities to rank (placeholder numbers, no money spent, "
            "nobody contacted). <code>opps</code> shows them.</i>")


def dashboard(now: Optional[float] = None) -> str:
    note = _seeded_note()
    c = freedom.compute(now)
    p = freedom.profile()
    _, months = ledger.dependable(now)
    this = ledger.month_summary(ledger.month_of(now or store.now()))
    hist = ledger.history_months(now)
    lines = ["🧭 <b>Freedom Dashboard</b>",
             "<i>What is the most reliable, lowest-risk path from what I have now to a life that no longer needs a conventional job?</i>",
             f"<b>Monthly costs:</b> {_money(c['monthly_costs'])} · <b>target income</b> {_money(c['target'])} (with a {p['margin']:g}% margin)",
             f"<b>Dependable income:</b> {_money(c['dependable'])}/mo <i>(a conservative rule, not a forecast: the worst of the last "
             "3 full months, " + ", ".join(f"{ym} {_money(v)}" for ym, v in months) + f"; {hist} full month(s) of records)</i>",
             f"<b>Gap:</b> {_money(c['gap'])}/mo",
             f"Breathing room   {_bar(c['breathing_room'])}  ({_money(c['saved'])} of {_money(c['emergency_fund'])} reserve)",
             f"Work optionality {_bar(c['optionality'])}  (dependable income vs half your costs)",
             f"Independence     {_bar(c['independence'])}",
             f"<b>Stage:</b> {freedom.milestone(c)}"]
    if c["job_hours_replaced"]:
        lines.append(f"<b>Time bought back:</b> ~{c['job_hours_replaced']:g} job hours a month replaced by dependable income.")
    lines.append(progress_line(now))
    lines.append(f"<b>This month so far:</b> collected {_money(this['collected'])} · booked {_money(this['booked'])} · costs "
                 f"{_money(this['costs'])} · available {_money(this['available'])} · {this['hours']:g} h of your time")
    ranked = scorecard.ranked()
    top = [r for r in ranked if not r["blocked"]][:3]
    focus = next((r for r in ranked if r["stage"] in scorecard.RUNNING), None) or (top[0] if top else None)
    if top:
        lines.append("<b>Best next bets:</b> " + " · ".join(f"<code>{r['id']}</code> {r['score']:g}" for r in top))
    if focus:
        lines.append(f"<b>Do next:</b> {e(scorecard.next_test(focus))} (<code>opp {focus['id']}</code>)")
    pend = store.rows("SELECT id FROM approvals WHERE status = 'pending'")
    if pend:
        lines.append(f"⏳ {len(pend)} approval card(s) waiting: " + " ".join(f"<code>yes econ {a['id']}</code>" for a in pend[:3]))
    ph = freedom.placeholders()
    if ph:
        lines.append(f"<i>Still placeholders: {', '.join(ph)}. Set your real numbers, e.g. <code>freedom set essentials 1800</code>.</i>")
    lines.append("<i>Only cash collected, minus costs and a tax reserve, counts. Forecasts never do.</i>")
    return "\n".join(lines) + note


def progress_line(now: Optional[float] = None) -> str:
    """Three different kinds of progress, never blurred together."""
    built = store.one("SELECT COUNT(*) AS n FROM opportunities WHERE stage IN ('built', 'launched', 'measured', 'scaled', 'modified')")["n"]
    t = ledger.totals(now)
    rate = f" · {_money((t['collected'] - t['costs']) / t['hours'])} per hour of your time" if t["hours"] else ""
    return (f"<b>Progress</b> · engineering: {built} venture(s) built · commercial: {t['sales']} sale(s) collected, "
            f"{_money(t['collected'])} in, {_money(t['costs'])} out{rate} · freedom: {_money(freedom.compute(now)['dependable'])}/mo dependable")


def opps_text() -> str:
    note = _seeded_note()
    rows = scorecard.ranked()
    if not rows:
        return "No open opportunities. <code>opp new &lt;id&gt; &lt;lane&gt;: &lt;name&gt;</code> adds one." + note
    cal = learning.calibration()
    lines = ["💼 <b>Opportunity desk</b> (same scorecard for all; hard limits first)"]
    if cal < 1:
        lines.append(f"<i>Projections discounted to {cal:.0%}: that is how past forecasts compared with reality.</i>")
    for i, r in enumerate(rows, 1):
        flag = "⛔ " if r["blocked"] else ""
        lines.append(f"{i}. {flag}<b>{e(r['name'])}</b> <code>{r['id']}</code> · {r['stage']} · score <b>{r['score']:g}</b> · "
                     f"demand {r['demand']} · ${r['startup_cash']:,.0f} at risk · {r['owner_hours']:g} h/wk")
        if r["blocked"]:
            lines.append("   <i>blocked: " + e("; ".join(r["blocked"])) + "</i>")
    lines.append("<code>opp &lt;id&gt;</code> for the case · <code>freedom</code> for the big picture")
    return "\n".join(lines) + note


def opp_text(opp_id: str) -> str:
    opp = pipeline.get(opp_id)
    if not opp:
        return f"No opportunity <code>{e(opp_id)}</code>. <code>opps</code> lists them."
    total, parts = scorecard.score(opp)
    blocked = scorecard.constraints(opp)
    tot = ledger.venture_totals(opp["id"])
    ev = scorecard.evidence(opp["id"])
    lines = [f"💼 <b>{e(opp['name'])}</b> <code>{opp['id']}</code> · lane {opp['lane']} · stage <b>{opp['stage']}</b>",
             f"<b>Problem:</b> {e(opp['problem'] or '(not written)')}" + (f" · <b>for</b> {e(opp['customer'])}" if opp["customer"] else ""),
             f"<b>Business case</b> <i>(assumptions until measured)</i>: startup cash ${opp['startup_cash']:,.0f} · first revenue in "
             f"~{opp['months_to_revenue']:g} mo · net ${opp['net_low']:,.0f}-{opp['net_high']:,.0f}/mo · {opp['owner_hours']:g} h/wk · "
             f"worst case -${opp['max_loss']:,.0f}",
             f"<b>Score {total:g}/100</b>: " + ", ".join(f"{k} {parts[k]:.2f}×{w}" for k, w in scorecard.WEIGHTS.items())
             + f" · counted net ${parts['effective_net']:,.0f}/mo",
             f"<b>Demand evidence:</b> {scorecard.demand(opp['id'])} · <b>legal:</b> {scorecard.legal(opp['id'])}"]
    if blocked:
        lines.append("⛔ <b>Hard limits:</b> " + e("; ".join(blocked)))
    if tot["collected"] or tot["costs"] or tot["hours"]:
        rate = f" · <b>{_money(tot['per_hour'])} per hour of your time</b>" if tot["per_hour"] is not None else ""
        lines.append(f"<b>Actual:</b> {tot['sales']} sale(s) · collected {_money(tot['collected'])} · booked {_money(tot['booked'])} · "
                     f"costs {_money(tot['costs'])} · net {_money(tot['net'])} · {tot['hours']:g} h{rate}")
    for label, f in (("Next action", "next_action"), ("Success looks like", "success_criteria"), ("Stop when", "stop_conditions"),
                     ("Deadline", "deadline"), ("Notes", "notes")):
        if opp[f]:
            lines.append(f"<b>{label}:</b> {e(opp[f])}")
    if opp["budget"]:
        lines.append(f"<b>Approved budget:</b> {_money(opp['budget'])}")
    if ev:
        lines.append("<b>Evidence:</b>")
        lines += [f"• {x['kind']}: {e(x['summary'][:160])}" + (f" <i>({e(x['source'][:60])})</i>" if x["source"] else "") for x in ev[-6:]]
    cats = ledger.hours_by_category(opp["id"])
    if cats:
        lines.append("<b>Your time:</b> " + " · ".join(f"{k} {v:g} h" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
                     + "  <i>(contribution per hour = (revenue − costs) ÷ ALL of these hours)</i>")
    for smp in validation.samples(opp["id"])[-2:]:
        lines.append(f"<b>Sample</b> <code>{smp['id']}</code>: you {smp['owner_min']:g} min · AURIX {smp['machine_min']:g} min · "
                     f"${smp['cost']:,.2f} · {smp['sourced']}/{smp['findings']} sourced · {smp['unresolved']} open · value {smp['value']}")
    for x in validation.experiments(opp["id"])[-2:]:
        lines.append(f"<b>Experiment</b> <code>{x['id']}</code> ({x['status']}, cap ${x['cap']:,.2f}): {e(x['plan'][:140])}"
                     + (f" → <i>{e(x['result'][:160])}</i>" if x["result"] else ""))
    if opp["stage"] == "researched":
        lines.append(validation.checklist_text(opp))
    nxt = pipeline.NEXT.get(opp["stage"])
    if nxt:
        miss = pipeline.requirements(opp, nxt)
        lines.append(f"<b>To reach {nxt}:</b> " + ("ready - <code>opp " + opp["id"] + " advance</code>" if not miss
                                                   else e("; ".join(miss))))
    lines.append(f"<b>Cheapest next test:</b> {e(scorecard.next_test(opp))}")
    return "\n".join(lines)


def _stop_flags(now: float) -> list:
    flags = []
    today = ledger._local(now).strftime("%Y-%m-%d")
    for o in store.rows("SELECT * FROM opportunities WHERE stage NOT IN ('closed', 'discovered')"):
        tot = ledger.venture_totals(o["id"])
        if o["budget"] and tot["costs"] > o["budget"]:
            flags.append(f"<b>{e(o['name'])}</b> spent {_money(tot['costs'])}, over its {_money(o['budget'])} budget: pause or re-approve.")
        if o["deadline"] and o["deadline"] < today and o["stage"] not in ("measured", "scaled"):
            flags.append(f"<b>{e(o['name'])}</b> passed its deadline ({o['deadline']}) at stage {o['stage']}: "
                         f"check its stop conditions ({e(o['stop_conditions'] or 'none written')}) - modify or close?")
    for x in store.rows("SELECT * FROM experiments WHERE status = 'running'"):
        if now - (x["started"] or now) > 30 * 86400:
            flags.append(f"Experiment <code>{x['id']}</code> on <code>{x['opp_id']}</code> has run 30 days: record the result "
                         f"(<code>opp {x['opp_id']} result: ...</code>).")
    for x in store.rows("SELECT * FROM experiments WHERE status = 'done' AND ended >= ?", (now - 7 * 86400,)):
        o = pipeline.get(x["opp_id"])
        if o and o["stage"] != "closed" and scorecard.demand(o["id"]) == "none":
            flags.append(f"<b>{e(o['name'])}</b>'s experiment found no demand: close it or change the offer once - "
                         "then compare the next opportunity rather than polishing this one.")
    return flags


def review_text(now: Optional[float] = None) -> str:
    note = _seeded_note()
    now = now or store.now()
    week = now - 7 * 86400
    txns = [r for r in store.rows("SELECT * FROM transactions WHERE at >= ?", (week,))]
    hrs = store.rows("SELECT * FROM hours WHERE at >= ?", (week,))
    names = {o["id"]: o["name"] for o in store.rows("SELECT id, name FROM opportunities")}
    lines = ["📒 <b>Weekly portfolio review</b>"]
    earned = {}
    for r in txns:
        if r["kind"] == "revenue" and r["status"] == "collected":
            earned[r["opp_id"]] = earned.get(r["opp_id"], 0) + r["amount"]
    lines.append("<b>What earned money:</b> " + (", ".join(f"{e(names.get(k, k or 'other'))} {_money(v)}" for k, v in earned.items())
                                                 or "nothing collected this week"))
    spent = sum(r["amount"] for r in txns if r["kind"] == "cost")
    used = {}
    for r in hrs:
        used[r["opp_id"]] = used.get(r["opp_id"], 0) + r["hours"]
    lines.append(f"<b>What it cost:</b> {_money(spent)} · <b>your time:</b> "
                 + (", ".join(f"{e(names.get(k, k or 'other'))} {v:g} h" for k, v in used.items()) or "no hours logged"))
    comps = learning.comparisons(now)
    if comps:
        lines.append("<b>Forecast vs real</b> (per month):")
        lines += [f"• {e(names.get(c['opp_id'], c['opp_id']))}: forecast {_money(c['forecast_net'])} → real {_money(c['actual_net'])}; "
                  f"hours {c['forecast_hours']:g} → {c['actual_hours']:g}/wk" for c in comps]
        lines.append(f"<i>Calibration applied to new projections: {learning.calibration(now):.0%}.</i>")
    flags = _stop_flags(now)
    if flags:
        lines.append("<b>Needs a decision:</b>")
        lines += [f"• {f}" for f in flags]
    ranked = scorecard.ranked()
    running = [r for r in ranked if r["stage"] in scorecard.RUNNING]
    ideas = [r for r in ranked if r["stage"] not in scorecard.RUNNING and not r["blocked"]]
    if running:
        lines.append("<b>Running:</b> " + "; ".join(f"{e(r['name'])} ({r['stage']})" for r in running))
        lines.append(f"<b>Focus:</b> {e(running[0]['name'])} - {e(scorecard.next_test(running[0]))}")
    elif ideas:
        lines.append(f"<b>Fund or test next:</b> {e(ideas[0]['name'])} <code>{ideas[0]['id']}</code> - {e(scorecard.next_test(ideas[0]))}")
    c = freedom.compute(now)
    lines.append(f"<b>Freedom gap:</b> {_money(c['gap'])}/mo · stage: {freedom.milestone(c)}")
    if not running:
        lines.append("<i>One venture at a time: a second lane starts only when the first has a working, measured process.</i>")
    return "\n".join(lines) + note


def close_text(ym: Optional[str] = None, now: Optional[float] = None) -> str:
    ym = ym or ledger.previous_months(1, now)[0]
    s = ledger.month_summary(ym)
    lines = [f"🧾 <b>Monthly close {ym}</b>",
             f"Projected (not money): {_money(s['projected'])} · booked (owed): {_money(s['booked'])}",
             f"Collected: {_money(s['collected'])} · costs: {_money(s['costs'])} · <b>operating profit {_money(s['operating_profit'])}</b>",
             f"Tax reserve ({freedom.profile()['tax']:g}%): {_money(s['tax_reserve'])} · <b>available to you {_money(s['available'])}</b>",
             f"Paid out to you: {_money(s['distributions'])} · your hours: {s['hours']:g}"]
    per = []
    for o in store.rows("SELECT id, name FROM opportunities"):
        v = ledger.month_summary(ym, o["id"])
        if v["collected"] or v["costs"] or v["hours"]:
            rate = f" · {_money(v['operating_profit'] / v['hours'])}/h" if v["hours"] else ""
            per.append(f"• {e(o['name'])}: profit {_money(v['operating_profit'])}{rate}")
    lines += per
    lines.append("<i>A planning record, not tax advice: self-employment income may need estimated tax payments.</i>")
    return "\n".join(lines)


def mirror_earned(amount: float, source: str, note: str = "") -> str:
    """`earned <amount> <id>` also lands in the engine's ledger as COLLECTED revenue when <id> is an opportunity."""
    if not pipeline.get(source):
        return ""
    ledger.add("revenue", amount, source, "collected", note)
    return f"\n🧭 Also recorded as collected revenue for <code>{source}</code> in the Economic Engine."


def handle(kind: str, arg: str) -> str:
    a = arg.split(SEP) if arg else []
    try:
        if kind == "econ_help":
            return "🧭 <b>Economic Engine commands</b>\n<pre>" + e(HELP) + "</pre>"
        if kind == "econ_freedom":
            return dashboard()
        if kind == "econ_set":
            return freedom.set_value(a[0], float(a[1].replace(",", ""))) + "\n" + dashboard()
        if kind == "econ_opps":
            return opps_text()
        if kind == "econ_opp":
            seed.seed_if_empty()
            return opp_text(a[0])
        if kind == "econ_new":
            opp = pipeline.create(a[0], a[2].strip(), a[1], origin="owner")
            return f"➕ Added <b>{e(opp['name'])}</b> <code>{opp['id']}</code>. Fill in the case: <code>opp {opp['id']} set high 300</code> ..."
        if kind == "econ_import":
            return seed.import_money_idea(a[0])
        if kind == "econ_opp_set":
            return pipeline.set_field(a[0], a[1], a[2])
        if kind == "econ_advance":
            return pipeline.advance(a[0])[0]
        if kind == "econ_modify":
            return pipeline.modify(a[0], a[2], close=a[1] == "close")
        if kind == "econ_evidence":
            return pipeline.add_evidence(a[0], a[1], a[2], a[3] if len(a) > 3 else "")
        if kind == "econ_refund":
            amount, oid, note = float(a[0]), a[1], (a[2] if len(a) > 2 else "")
            if not pipeline.get(oid):
                return f"No opportunity <code>{e(oid)}</code>."
            ledger.add("refund", amount, oid, "collected", note)
            return f"↩️ Logged a {_money(amount)} refund on <code>{oid}</code>; it comes off this month's collected revenue."
        if kind == "econ_sample":
            return validation.record_sample(a[0], a[1])
        if kind == "econ_experiment":
            return validation.propose(a[0], float(a[1].replace(",", "")), a[2])
        if kind == "econ_result":
            return validation.result(a[0], a[1])
        if kind == "econ_check":
            from src.foundation.economy import check
            return check.run()
        if kind in ("econ_spent", "econ_booked"):
            amount, oid, note = float(a[0]), a[1], (a[2] if len(a) > 2 else "")
            if not pipeline.get(oid):
                return f"No opportunity <code>{e(oid)}</code>."
            if kind == "econ_spent":
                ledger.add("cost", amount, oid, "collected", note)
                tot, opp = ledger.venture_totals(oid), pipeline.get(oid)
                x = validation.running(oid)
                if x:
                    used = sum(r["amount"] for r in store.rows("SELECT amount FROM transactions WHERE opp_id = ? AND kind = 'cost' "
                                                                 "AND at >= ?", (oid, x["started"])))
                    warn = f"\n⚠️ Over the experiment's ${x['cap']:,.2f} cap: stop and record the result." if used > x["cap"] else ""
                    return f"💸 Logged {_money(amount)} on <code>{oid}</code>: {_money(used)} of the experiment's {_money(x['cap'])} cap." + warn
                over = (f"\n⚠️ That is over its approved budget of {_money(opp['budget'])}." if opp["budget"] and tot["costs"] > opp["budget"]
                        else "\n⚠️ It has no approved budget: spending before approval breaks your own rule." if opp["stage"] in
                        ("discovered", "researched", "validated") else "")
                return f"💸 Logged {_money(amount)} spent on <code>{oid}</code> (total {_money(tot['costs'])})." + over
            ledger.add("revenue", amount, oid, "booked", note)
            return f"📝 Booked {_money(amount)} for <code>{oid}</code>. It counts once it is collected (<code>earned {a[0]} {oid}</code>)."
        if kind == "econ_hours":
            oid = a[1]
            if not pipeline.get(oid):
                return f"No opportunity <code>{e(oid)}</code>."
            rest = (a[2] if len(a) > 2 else "").split(None, 1)
            cat = rest[0].lower() if rest and rest[0].lower() in ledger.HOUR_CATEGORIES else "production"
            note = (rest[1] if len(rest) > 1 else "") if rest and rest[0].lower() in ledger.HOUR_CATEGORIES else (a[2] if len(a) > 2 else "")
            ledger.log_hours(float(a[0]), oid, note, category=cat)
            return f"⏱️ Logged {float(a[0]):g} h of {cat} on <code>{oid}</code> ({ledger.venture_totals(oid)['hours']:g} h in total)."
        if kind == "econ_review":
            return review_text()
        if kind == "econ_close":
            return close_text(a[0] if a and a[0] else None)
        if kind == "econ_yes":
            return pipeline.resolve(a[0], True)
        if kind == "econ_no":
            return pipeline.resolve(a[0], False)
    except (ValueError, IndexError) as exc:
        return f"That did not work: {e(str(exc))}. <code>help economy</code> lists the commands."
    return "Unknown economy command."


HELP = __doc__.split("\n\n", 1)[1].strip()
