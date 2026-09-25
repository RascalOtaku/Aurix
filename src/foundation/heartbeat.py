"""src/foundation/heartbeat.py - "what are you doing and why", live, on demand (§10.8).

Reads the active mission, the approval queue and the audit chain and produces one
owner-facing status message. It also reports the audit chain head: because the owner's
phone keeps a copy of that hash, truncating the tail of the audit file later can be
noticed (the file alone cannot show that).
"""
from __future__ import annotations

import html
import time
from typing import Optional

from collections import Counter
from datetime import datetime

from src.foundation import audit
from src.foundation import mission as ms
from src.foundation import standing as st

_AUDIT_TS = "%Y-%m-%dT%H:%M:%S%z"


def activity_summary(hours: int = 24, now_ts: Optional[float] = None) -> str:
    """What happened in the last `hours`, counted from the audit chain."""
    since = (now_ts or time.time()) - hours * 3600
    counts: Counter = Counter()
    for rec in audit.recent(5000):
        try:
            if datetime.strptime(rec.get("ts", ""), _AUDIT_TS).timestamp() >= since:
                counts[rec.get("event", "?")] += 1
        except ValueError:
            continue
    if not counts:
        return f"Last {hours}h: nothing recorded."
    pick = lambda *names: sum(counts[n] for n in names)  # noqa: E731
    parts = [
        f"missions: {pick('mission_completed')} completed, {pick('mission_blocked')} blocked, "
        f"{pick('mission_failed', 'mission_expired', 'mission_stopped')} failed/expired/stopped",
        f"standing runs started: {pick('standing_run_started')}",
        f"approvals: {pick('requested')} asked, {pick('approved')} approved, {pick('denied')} denied/refused",
        f"protected-component attempts refused: "
        f"{sum(1 for r in audit.recent(5000) if r.get('reason') == 'protected_component')}",
        f"sandbox/mission actions run without a ping: {pick('mission_allowed')}",
        f"read-only commands auto-allowed: {pick('auto_allowed')}",
    ]
    return f"<b>Last {hours}h</b>\n" + "\n".join("• " + p for p in parts)


def waiting_for_you(limit: int = 6) -> str:
    """One line per decision waiting on the owner, each with the exact yes/no words (the buttons under the digest are built from them)."""
    e = html.escape
    lines = []

    def add(fn, render):
        try:
            for x in fn()[:limit]:
                lines.append(render(x))
        except Exception:
            pass
    from src.foundation import content, forge, freelance, gaming, learning, memory, repos, teacher, upgrades
    add(gaming.pending, lambda p: f"🎮 {e(p['title'][:70])} - <code>yes {p['id']}</code> / <code>no {p['id']}</code>")
    add(lambda: [u for u in upgrades.all_proposals() if u["status"] == "review"], lambda u: f"🛠️ Upgrade: {e(u['title'][:60])} - <code>yes {u['id']}</code> / <code>no {u['id']}</code>")
    add(repos.pending, lambda r: f"📥 {e(r['owner'])}/{e(r['repo'])} - <code>yes {r['id']}</code> / <code>no {r['id']}</code>")
    add(memory.pending, lambda k: f"🧠 {'Forget' if k.get('kind') == 'forget' else 'Remember'}: {e(k.get('text', '')[:60])} - <code>yes {k['id']}</code> / <code>no {k['id']}</code>")
    add(lambda: [l for l in teacher.all_lessons() if l.get("status") == "pending"], lambda l: f"🎓 Lesson: {e(l['title'][:60])} - <code>approve lesson {l['id']}</code> / <code>deny lesson {l['id']}</code>")
    add(freelance.pending, lambda j: f"🧰 {e(j['title'][:60])} - <code>yes {j['id']}</code> / <code>no {j['id']}</code>")
    add(content.pending, lambda p: f"📝 {e(p['title'][:60])} - <code>yes {p['id']}</code> / <code>no {p['id']}</code>")
    add(learning.pending, lambda r: f"📚 {e(r['title'][:60])} - <code>yes {r['id']}</code> / <code>no {r['id']}</code>")
    add(forge.pending, lambda s: f"🛠️ Skill: {e(s['name'])} - <code>approve skill {s['name']}</code> / <code>deny skill {s['name']}</code>")
    try:
        from src.foundation import shards as _shards
        for r in _shards.stale():
            lines.append(f"⚠️ {e(r['name'])}: {e(r['detail'][:90])}")
        for pid in _shards.paused_ids():
            lines.append(f"⏸️ Paused by you: <code>{pid}</code> (<code>resume {pid}</code>)")
    except Exception:
        pass
    if not lines:
        return "✅ <b>Nothing is waiting on you.</b>"
    return "🙋 <b>Waiting for your yes / no</b>\n" + "\n".join(lines[:limit * 2])


def overnight_digest(store: Optional[ms.MissionStore] = None) -> str:
    from src.foundation import projects
    try:
        yours = projects.Registry().digest_line()
    except Exception:
        yours = ""
    try:
        from src.foundation import qol
        trading = qol.trades_digest_line()
    except Exception:
        trading = ""
    try:
        from src.foundation import evals as _evals
        scoreboard = _evals.digest_line()
    except Exception:
        scoreboard = ""
    try:
        from src.foundation import gaming as _gaming
        games = _gaming.digest_line()
    except Exception:
        games = ""
    yours = "\n".join(x for x in (yours, trading, scoreboard, games) if x)
    try:
        waiting = waiting_for_you()
    except Exception:
        waiting = ""
    return ("☀️ <b>AURIX morning digest</b>\n" + (waiting + "\n\n" if waiting else "") + activity_summary(24) + "\n\n" + self_report(store)
            + (("\n\n" + yours) if yours else ""))


def _bar(done: int, total: int, width: int = 10) -> str:
    filled = int(width * done / total) if total else 0
    return "■" * filled + "□" * (width - filled)


def self_report(store: Optional[ms.MissionStore] = None, events: int = 5) -> str:
    e = html.escape
    store = store or ms.MissionStore()
    lines = ["\U0001FA7A <b>AURIX status</b> " + time.strftime("%a %b %d %H:%M")]

    m = store.active()
    if m:
        total = len(m.steps)
        step = m.steps[m.current_step] if m.current_step < total else None
        lines += [f"<b>Mission [{m.id}]</b> {e(m.status.value)}",
                  f"<b>Why:</b> {e(m.objective[:300])}",
                  f"<b>Progress:</b> {_bar(m.current_step, total)} {m.current_step}/{total}"]
        if step:
            lines.append(f"<b>Now:</b> {e(step.title)} - {e(step.description[:200])}")
        r, u = m.resources, m.usage
        used_min = int((time.time() - m.approved_at) / 60) if m.approved_at else 0
        lines.append(f"<b>Budget:</b> tool calls {u.tool_calls}/{r.max_tool_calls}, model calls "
                     f"{u.model_calls}/{r.max_model_calls}, {used_min}/{r.max_wall_minutes} min, "
                     f"${u.cost_usd:.2f}/${r.max_cost_usd:.2f}")
        if m.deadline:
            lines.append(f"<b>Deadline:</b> {e(m.deadline)}")
        if not m.integrity_ok():
            lines.append("⚠ <b>Contract integrity check FAILED</b>")
    else:
        recent = sorted(store.all(), key=lambda x: x.created_at)[-1:]
        lines.append("No active mission." + (f" Last: [{recent[0].id}] {recent[0].status.value} - "
                                             f"{e(recent[0].objective[:80])}" if recent else ""))

    standing = st.StandingStore().all()
    active_n = sum(1 for s in standing if s.status == st.StandingStatus.ACTIVE)
    paused = [s.id for s in standing if s.status == st.StandingStatus.PAUSED]
    if active_n or paused:
        lines.append(f"<b>Standing missions:</b> {active_n} active"
                     + (f", paused: {', '.join(paused)}" if paused else ""))

    try:                                    # lazy: approval_gate imports this package
        from src import approval_gate as ag
        pending = ag.pending_ids()
        lines.append(f"<b>Waiting on you:</b> {', '.join(pending) if pending else 'nothing'}")
        lines.append(f"<b>Gate:</b> mode={ag.gate_mode()}" + (" ⛔ STOP engaged" if ag.stop_engaged() else ""))
    except Exception:
        pass

    v = audit.verify()
    seq, head = audit.head()
    if v.ok:
        note = f" (+{len(v.acknowledged)} owner-acknowledged fork record: #{', #'.join(str(s) for s in v.acknowledged)})" if v.acknowledged else ""
        lines.append(f"<b>Audit:</b> chain OK, {v.records} records{note}, head #{seq} <code>{head[:12]}</code>")
    else:
        lines.append(f"⚠ <b>AUDIT TAMPERING DETECTED</b> at record {v.bad_seq}: {e(v.reason)}")

    for rec in audit.recent(events):
        what = rec.get("mission") or rec.get("tool") or rec.get("id") or ""
        lines.append(f"• {e(str(rec.get('ts', ''))[11:19])} {e(str(rec.get('event')))} {e(str(what))}")
    return "\n".join(lines)
