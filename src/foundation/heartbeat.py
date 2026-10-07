"""src/foundation/heartbeat.py - "what are you doing and why", live, on demand (§10.8).

Reads the active mission, the approval queue and the audit chain and produces one
owner-facing status message. It also reports the audit chain head: because the owner's
phone keeps a copy of that hash, truncating the tail of the audit file later can be
noticed (the file alone cannot show that).
"""
from __future__ import annotations

import html
import logging
import time
from typing import Optional

from collections import Counter
from datetime import datetime

from src.foundation import audit
from src.foundation import mission as ms
from src.foundation import standing as st

from src.failure_log import record

_AUDIT_TS = "%Y-%m-%dT%H:%M:%S%z"

logger = logging.getLogger(__name__)


def activity_summary(hours: int = 24, now_ts: Optional[float] = None) -> str:
    """What happened in the last `hours`, counted from the audit chain."""
    since = (now_ts or time.time()) - hours * 3600
    counts: Counter = Counter()
    for rec in audit.recent(5000):
        try:
            if datetime.strptime(rec.get("ts", ""), _AUDIT_TS).timestamp() >= since:
                counts[rec.get("event", "?")] += 1
        except ValueError as exc:
            record(logger, exc, context="heartbeat activity summary: skipping bad timestamp",
                   level=logging.DEBUG)
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

    def add(fn, render, label):
        try:
            for x in fn()[:limit]:
                lines.append(render(x))
        except Exception as exc:
            record(logger, exc, context=f"heartbeat waiting-for-you: section {label} failed",
                   level=logging.WARNING)
    from src.foundation import content, forge, freelance, gaming, learning, memory, repos, teacher, upgrades
    from src.foundation.land import hub as land
    from src.foundation import evolve
    add(gaming.pending, lambda p: f"🎮 {e(p['title'][:70])} - <code>yes {p['id']}</code> / <code>no {p['id']}</code>", 'gaming')
    add(lambda: [u for u in upgrades.all_proposals() if u["status"] == "review"], lambda u: f"🛠️ Upgrade: {e(u['title'][:60])} - <code>yes {u['id']}</code> / <code>no {u['id']}</code>", 'upgrades')
    add(repos.pending, lambda r: f"📥 {e(r['owner'])}/{e(r['repo'])} - <code>yes {r['id']}</code> / <code>no {r['id']}</code>", 'repos')
    add(memory.pending, lambda k: f"🧠 {'Forget' if k.get('kind') == 'forget' else 'Remember'}: {e(k.get('text', '')[:60])} - <code>yes {k['id']}</code> / <code>no {k['id']}</code>", 'memory')
    add(lambda: [l for l in teacher.all_lessons() if l.get("status") == "pending"], lambda l: f"🎓 Lesson: {e(l['title'][:60])} - <code>approve lesson {l['id']}</code> / <code>deny lesson {l['id']}</code>", 'teacher')
    add(freelance.pending, lambda j: f"🧰 {e(j['title'][:60])} - <code>yes {j['id']}</code> / <code>no {j['id']}</code>", 'freelance')
    add(content.pending, lambda p: f"📝 {e(p['title'][:60])} - <code>yes {p['id']}</code> / <code>no {p['id']}</code>", 'content')
    add(learning.pending, lambda r: f"📚 {e(r['title'][:60])} - <code>yes {r['id']}</code> / <code>no {r['id']}</code>", 'learning')
    add(forge.pending, lambda s: f"🛠️ Skill: {e(s['name'])} - <code>approve skill {s['name']}</code> / <code>deny skill {s['name']}</code>", 'forge')
    add(land.pending, lambda c: f"🏞️ Land: {e(c.get('label', c.get('property', ''))[:60])} - <code>yes land {c['id']}</code> / <code>no land {c['id']}</code>", 'land')
    add(evolve.all_pending, lambda c: f"🧬 Evolved {e(c['domain'])} (fitness {c['fitness']:.3f}) - <code>yes evolve {c['id']}</code> / <code>no evolve {c['id']}</code>", 'evolve')
    try:
        from src.foundation import shards as _shards
        for r in _shards.stale():
            lines.append(f"⚠️ {e(r['name'])}: {e(r['detail'][:90])}")
        for pid in _shards.paused_ids():
            lines.append(f"⏸️ Paused by you: <code>{pid}</code> (<code>resume {pid}</code>)")
    except Exception as exc:
        record(logger, exc, context="heartbeat waiting-for-you: shards section failed",
               level=logging.WARNING)
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


def pulse_digest() -> str:
    """A lighter, more frequent companion to overnight_digest() (see commands.Foundation._maybe_pulse, default
    every 2 hours, never during quiet hours): self-improve/evolve progress, skill requests, LandPilot activity,
    project updates, and anything waiting on you - each section reported honestly as "unavailable" rather than
    silently dropped if its module errors, so a pulse never looks emptier than it should."""
    e = html.escape
    parts = ["🔁 <b>AURIX pulse</b> " + time.strftime("%a %H:%M")]

    try:
        from src.foundation import evolve, evolve_domains  # noqa: F401 - registers forge_guidance
        names = evolve.domains()
        parts.append(evolve.overview_text() if names else "🧬 Self-improve: no domains registered.")
    except Exception:
        parts.append("🧬 Self-improve: unavailable right now.")

    try:
        from src.foundation import forge
        skills = forge.pending()
        parts.append(f"🛠️ Skill requests: {len(skills)} waiting" + (" - " + ", ".join(s["name"] for s in skills[:3]) if skills else ""))
    except Exception:
        parts.append("🛠️ Skill requests: unavailable right now.")

    try:
        from src.foundation.land import hub as land
        lp = land.panel()
        parts.append(f"🏞️ LandPilot: {len(lp.get('dossiers', []))} dossier(s), {len(lp.get('pending', []))} pending")
    except Exception:
        parts.append("🏞️ LandPilot: unavailable right now.")

    try:
        from src.foundation import projects
        yours = projects.Registry().digest_line()
        if yours:
            parts.append(yours)
    except Exception as exc:
        record(logger, exc, context="heartbeat digest: projects section failed",
               level=logging.WARNING)

    try:
        waiting = waiting_for_you()
        if "Nothing is waiting" not in waiting:
            parts.append(waiting)
    except Exception as exc:
        record(logger, exc, context="heartbeat digest: waiting-for-you section failed",
               level=logging.WARNING)

    return "\n\n".join(parts)


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
    except Exception as exc:
        record(logger, exc, context="heartbeat self-report: approval-gate section failed",
               level=logging.WARNING)

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
