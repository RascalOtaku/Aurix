"""src/foundation/growth.py - what AURIX is doing right now, what it has been thinking, and how it has grown.

  probe_running()   the parts that run by themselves (scheduler, paper trading, the self-improvement loop, the game PC agent, backups, the mission
                    runner, the model): each with a plain "what it does", when it last/next runs, and its latest THOUGHT
  activity_feed()   a live, human-readable stream: audit events turned into sentences, plus the paper-trading agent's own reasoning lines
  growth_stats()    then-vs-now numbers (how long alive, audit records, missions, skills, lessons, fixes, eval scores)
  timeline()        curated milestones merged with notable live events, newest first

Everything here is read-only and defensive: a missing file or a broken probe makes one row say so, never the dashboard fail.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit

MILESTONES_PATH = Path(__file__).resolve().parents[2] / "timeline" / "milestones.json"


def _brain() -> Path:
    return Path(os.environ.get("AURIX_BRAIN", "/aurix"))


def _clip(x: Any, n: int = 90) -> str:
    s = " ".join(str(x or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds or 0))
    return f"{s}s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} d"


def _iso_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("AURIX_TIMEZONE", "America/Denver"))
    except Exception:
        return None


def next_daily(hh: int, mm: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(_tz())
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return ("today " if t.date() == now.date() else "tomorrow ") + t.strftime("%H:%M")


def next_market_scan(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(_tz())
    t = now.replace(hour=8, minute=0, second=0, microsecond=0)
    while t <= now or t.weekday() >= 5:
        t += timedelta(days=1)
        t = t.replace(hour=8, minute=0)
    return t.strftime("%a %H:%M")


# ---------------------------------------------------------------------------------------------------------------------------------
# the paper-trading agent's own reasoning (its log is the closest thing to a running thought)
# ---------------------------------------------------------------------------------------------------------------------------------

_TRADE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[TRADE\] (.+)$")


def trade_thoughts(n: int = 6, log: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = log or (_brain() / "logs" / "trade_agent.log")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for ln in lines:
        m = _TRADE_LINE.match(ln)
        if not m:
            continue
        stamp, msg = m.groups()
        ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
        if msg.startswith("PAPER BUY"):
            text, icon = "Bought (paper): " + _clip(msg[len("PAPER BUY"):], 110), "🟢"
        elif msg.startswith("PAPER EXIT"):
            text, icon = "Sold (paper): " + _clip(msg[len("PAPER EXIT"):], 110), "🔴"
        elif msg.startswith("PAPER CORE"):
            text, icon = "Kept the rest of the money working in SPY: " + _clip(msg, 90), "⚖️"
        elif re.match(r"^[A-Z.]{1,6}: (BUY|SELL|HOLD) ", msg):
            text, icon = "Considered " + _clip(msg, 130), "🤔"
        elif msg.startswith("skip "):
            text, icon = "Passed on " + _clip(msg[5:], 100), "⏭️"
        elif msg.startswith("scan complete"):
            text, icon = _clip(msg, 120), "🧭"
        elif msg.startswith(("market closed", "DEAD", "daily loss", "agent is dead", "kill switch")):
            text, icon = _clip(msg, 120), "💤" if msg.startswith("market closed") else "⛔"
        else:
            continue
        out.append({"ts": ts, "icon": icon, "text": text, "source": "paper trading"})
    return out[-n:]


# ---------------------------------------------------------------------------------------------------------------------------------
# audit events as sentences
# ---------------------------------------------------------------------------------------------------------------------------------

def humanize(rec: dict) -> Optional[Dict[str, Any]]:
    ev = str(rec.get("event", ""))
    mid = rec.get("mission") or rec.get("id") or ""
    table = {
        "mission_proposed": ("📋", lambda: f"Planned mission {mid}: {_clip(rec.get('objective'), 70)}"),
        "mission_approved": ("▶️", lambda: f"Started mission {mid}" + (" on its own (fast lane)" if str(rec.get("by", "")).startswith("policy:") else " (you approved)")),
        "mission_completed": ("✅", lambda: f"Finished mission {mid}"),
        "mission_failed": ("⚠️", lambda: f"Mission {mid} failed: {_clip(rec.get('note'), 60)}"),
        "mission_blocked": ("⏸️", lambda: f"Mission {mid} is blocked: {_clip(rec.get('reason'), 50)}"),
        "mission_stopped": ("⛔", lambda: f"Mission {mid} stopped"),
        "fast_lane_started": ("⚡", lambda: f"Fast lane started {mid} without waiting for you"),
        "skill_forged": ("🛠️", lambda: f"Learned a new skill: {rec.get('name', '')}"),
        "skill_proposed": ("🛠️", lambda: f"Wrote a new skill for your approval: {rec.get('name', '')}"),
        "lesson_proposed": ("🎓", lambda: f"The teacher proposed lesson {rec.get('id', '')}"),
        "lesson_approved": ("🎓", lambda: f"You approved lesson {rec.get('id', '')}: it will guide future work"),
        "teacher_called": ("🎓", lambda: "Asked the frontier teacher for help with a failure"),
        "evals_run": ("🧪", lambda: "Ran a self-check: " + ", ".join(f"{k} {v}" for k, v in (rec.get("tiers") or {}).items())),
        "gaming_proposal": ("🎮", lambda: f"Noticed something in {rec.get('game', 'a game')}: {rec.get('kind', '')}"),
        "gaming_fix_approved": ("🔧", lambda: f"You approved game fix {rec.get('id', '')}"),
        "gaming_fix_result": ("✅" if rec.get("status") == "applied" else "⚠️", lambda: f"Game fix {rec.get('id', '')}: {rec.get('status', '')}"),
        "shard_paused": ("⏸️", lambda: f"You paused helper {rec.get('id', '')}"),
        "shard_resumed": ("▶️", lambda: f"You resumed helper {rec.get('id', '')}"),
        "shards_pause_all": ("⏸️", lambda: "You paused every helper"),
        "shards_resume_all": ("▶️", lambda: "You resumed every helper"),
        "gamepilot_paired": ("📺", lambda: "A screen was paired to GamePilot"),
        "gamepilot_armed": ("🎮", lambda: f"GamePilot control armed for {rec.get('minutes', '?')} min"),
        "gamepilot_disarmed": ("🛑", lambda: "GamePilot control disarmed"),
        "gamepilot_unpaired": ("🔌", lambda: "GamePilot screens unpaired"),
        "money_status": ("💰", lambda: f"Money idea {rec.get('id', '')} marked {rec.get('status', '')}"),
        "upgrade_proposed": ("🛠️", lambda: f"Drafted an upgrade: {_clip(rec.get('title'), 60)}"),
        "upgrade_approved": ("✅", lambda: f"You approved upgrade {rec.get('id', '')}"),
        "upgrade_applied": ("🚀", lambda: f"Upgrade {rec.get('id', '')} is live"),
        "upgrade_failed": ("⚠️", lambda: f"Upgrade {rec.get('id', '')} was not applied (old code kept)"),
        "upgrade_undone": ("↩️", lambda: f"Upgrade {rec.get('id', '')} rolled back"),
        "upgrade_discarded": ("🗑️", lambda: "A drafted upgrade was thrown away: " + _clip(rec.get('why'), 60)),
        "memory_added": ("🧠", lambda: f"Remembered something new ({rec.get('source', '')})"),
        "memory_forgotten": ("🗑️", lambda: "Forgot a memory"),
        "memory_proposed": ("🧠", lambda: "Noticed something worth " + ("forgetting" if rec.get("kind") == "forget" else "remembering")),
        "repo_requested": ("📥", lambda: f"You sent a repo link: {rec.get('repo', '')}"),
        "repo_reviewed": ("🔎", lambda: f"Went through {rec.get('repo', '')}: verdict {rec.get('level', '')}"),
        "repo_approved": ("✅", lambda: f"You approved storing {rec.get('repo', '')}"),
        "repo_absorbed": ("📦", lambda: f"Stored {rec.get('repo', '')} in the library"),
        "repo_declined": ("🚫", lambda: f"You skipped {rec.get('repo', '')}"),
        "project_added": ("📁", lambda: f"Added a project: {_clip(rec.get('name'), 60)}"),
        "todo_added": ("📝", lambda: "Added a to-do"),
        "watchdog_alert": ("🔔", lambda: _clip(rec.get("preview"), 90)),
        "standing_run_started": ("🔁", lambda: f"Standing mission {rec.get('standing', '')} started a run"),
        "owner_stop": ("⛔", lambda: "You pressed STOP"),
        "digest_sent": ("☀️", lambda: "Sent your morning digest"),
    }
    if ev not in table:
        return None
    icon, fn = table[ev]
    try:
        return {"ts": _iso_ts(rec.get("ts", "")), "icon": icon, "text": re.sub(r"<[^>]+>", "", fn()), "source": "audit", "event": ev}
    except Exception:
        return None


def activity_feed(n: int = 14) -> List[Dict[str, Any]]:
    items = [h for h in (humanize(r) for r in audit.recent(400)) if h]
    items += trade_thoughts(8)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:n]


# ---------------------------------------------------------------------------------------------------------------------------------
# the parts that run by themselves
# ---------------------------------------------------------------------------------------------------------------------------------

def _row(key: str, title: str, icon: str, state: str, what: str, when: str, thought: str) -> Dict[str, Any]:
    return {"key": key, "title": title, "icon": icon, "state": state, "what": what, "when": when, "thought": thought}


def _scheduler(organs: dict) -> Dict[str, Any]:
    from src.foundation import mission as ms, watchdog
    n = (organs or {}).get("nervous", {}).get("metrics", {})
    alive = n.get("standing scheduler") == "alive"
    try:
        last = time.time() - watchdog.state_path().stat().st_mtime
    except OSError:
        last = None
    return _row("scheduler", "Heartbeat & watchdog", "💓", "running" if alive else "bad", "Every minute: runs your standing missions, checks disk, memory, backups, homelab and the audit chain, and messages you if something breaks.",
                f"last check {_age(last)} ago" if last is not None else "runs every minute",
                "Watching quietly - nothing has needed your attention." if alive else "The scheduler is not running - see Systems.")


def _trading() -> Dict[str, Any]:
    from src.foundation import qol
    rep = qol._trade_report(_brain() / "runtime")
    th = trade_thoughts(1)
    what = "Weekdays 08:00 Denver: scans 20+ stocks, keeps all its money working (picks + SPY core), watches exits every 30 min, reports at the close."
    if not rep:
        return _row("trading", "Paper trading agent", "📈", "idle", what, f"next scan {next_market_scan()}", "Waiting for the first market open.")
    if rep.get("dead"):
        return _row("trading", "Paper trading agent", "📈", "bad", what, "stood down", "It crossed the survival line and flattened. It stays down until you revive it on the server.")
    last = rep.get("last_scan")
    thought = th[-1]["text"] if th else (f"Lifeforce {rep.get('lifeforce_pct', 0):.1f}% - holding {len([p for p in rep.get('open_positions', []) if not p.get('core')])} picks plus the SPY core.")
    return _row("trading", "Paper trading agent", "📈", "running" if last else "idle", what,
                (f"last scan {str(last)[:16].replace('T', ' ')} · " if last else "") + f"next {next_market_scan()}", thought)


def _selfimprove() -> Dict[str, Any]:
    from src.foundation import evals, teacher
    hist = evals.load_history(1)
    at = os.environ.get("AURIX_EVALS_AT", "03:15").strip()
    pend = [x for x in teacher.all_lessons() if x.get("status") == "pending"]
    fails = evals.load_failures()
    what = "The self-improvement loop: every night it re-runs 19 checks on itself (planning + code), finds what it got wrong, asks the frontier teacher how to do better, tests the advice, and waits for your yes."
    last = hist[-1] if hist else None
    when = ((f"last run {str(last.get('ts', ''))[:16].replace('T', ' ')}" if last else "not run yet") + (f" · next {next_daily(int(at[:2]), int(at[3:5]))}" if at[:2].isdigit() else " · nightly run is off"))
    if pend:
        thought = "Learning: " + _clip(pend[-1].get("diagnosis") or pend[-1].get("title"), 120) + f" ({len(pend)} lesson waiting for you)"
    elif fails:
        thought = f"Working on: {fails[0].get('id')} - {_clip(fails[0].get('failure_class'), 100)}"
    elif last:
        thought = "All checks pass. Waiting for tonight's run."
    else:
        thought = "Send `evals` for the first scoreboard."
    return _row("selfimprove", "Self-improvement loop", "🧠", "running" if at else "idle", what, when, thought)


def _old_selfimprove() -> Dict[str, Any]:
    p = _brain() / "runtime" / "improvement_proposals.json"
    what = "The original patch-writing agent (reads error logs, drafts code fixes, emailed for approval)."
    try:
        age = time.time() - p.stat().st_mtime
        data = json.loads(p.read_text(encoding="utf-8"))
        last = (data[-1] if isinstance(data, list) and data else {}) or {}
        idea = _clip(last.get("title") or last.get("description") or last.get("file"), 80)
    except (OSError, ValueError, AttributeError):
        return _row("oldimprove", "Old patch agent", "💤", "asleep", what, "not scheduled", "Retired in favour of the loop above.")
    return _row("oldimprove", "Old patch agent", "💤", "asleep", what, f"last active {_age(age)} ago", ("Asleep since then (its mail delivery failed). Its last idea: " + idea) if idea else "Asleep; replaced by the loop above.")


def _gamepc() -> Dict[str, Any]:
    from src.foundation import gaming
    rep = gaming.load_report()
    what = "On your PC every 5 minutes: reads your Steam games, mods and crash logs (read-only) and applies only fixes you approved."
    if rep is None:
        return _row("gamepc", "Game PC agent", "🎮", "idle", what, "no report yet", "Waiting for the first report from your PC.")
    age = gaming.report_age_hours(rep)
    issues = gaming.analyze(rep)
    bad = [i for i in issues if i["kind"] == "missing_master"]
    crashes = [i for i in issues if i["kind"] == "recent_crashes"]
    state = "bad" if age is not None and age > gaming.STALE_REPORT_HOURS * 2 else "running"
    thought = (f"Checked {len(rep.get('games', []))} games: " + (f"{len(bad)} load-order problem(s) to fix, " if bad else "every load order is healthy, ")
               + (f"{len(crashes)} game(s) with recent crashes to look into." if crashes else "no recent crashes."))
    return _row("gamepc", "Game PC agent", "🎮", state, what, f"report {_age((age or 0) * 3600)} old", thought)


def _runner(running: dict) -> Dict[str, Any]:
    m = (running or {}).get("mission")
    what = "Carries out missions you approve (or the fast lane starts) inside the isolated sandbox, one step at a time."
    if not m:
        return _row("runner", "Mission runner", "🏃", "idle", what, "idle", "No mission running. Ready for your next request.")
    return _row("runner", "Mission runner", "🏃", "running", what, f"mission {m.get('id')}", f"Working on: {_clip(m.get('objective'), 110)}")


def _model(running: dict) -> Dict[str, Any]:
    models = (running or {}).get("models") or []
    inflight = (running or {}).get("in_flight", 0)
    what = "The local model that plans, writes code and answers questions. A frontier model is used only as a teacher, capped, when you allow it."
    if models:
        return _row("model", "Local model", "🧠", "running", what, ", ".join(f"{m.get('model')}" for m in models[:2]), f"{len(models)} model loaded in memory" + (f", {inflight} request in flight" if inflight else ", waiting for work"))
    return _row("model", "Local model", "🧠", "idle", what, "not loaded", "Loads on demand when there is something to think about.")


def _backup() -> Dict[str, Any]:
    from src.foundation import watchdog
    b = watchdog.read_backup_status()
    what = "Every night at 03:30: copies AURIX's memory, databases and settings to the NAS as a dated snapshot (secrets excluded)."
    if not b:
        return _row("backup", "Nightly backup", "💾", "idle", what, "next tonight 03:30", "No backup recorded yet.")
    age = time.time() - float(b.get("finished", 0) or 0)
    return _row("backup", "Nightly backup", "💾", "running" if b.get("ok") else "bad", what, f"last {_age(age)} ago · next {next_daily(3, 30)}", _clip(b.get("message"), 100))


def probe_running(running: Optional[dict] = None, organs: Optional[dict] = None) -> List[Dict[str, Any]]:
    fns = [("scheduler", lambda: _scheduler(organs or {})), ("trading", _trading), ("selfimprove", _selfimprove), ("gamepc", _gamepc), ("runner", lambda: _runner(running or {})),
           ("model", lambda: _model(running or {})), ("backup", _backup), ("oldimprove", _old_selfimprove)]
    out = []
    for key, fn in fns:
        try:
            out.append(fn())
        except Exception as e:
            out.append(_row(key, key, "❔", "bad", "could not be checked", "", type(e).__name__))
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# growth
# ---------------------------------------------------------------------------------------------------------------------------------

def load_milestones() -> dict:
    try:
        return json.loads(MILESTONES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"now": {}, "milestones": []}


def _first_record_ts() -> Optional[float]:
    try:
        with open(audit.audit_path(), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    return _iso_ts(json.loads(line).get("ts", "")) or None
    except (OSError, ValueError):
        pass
    return None


def growth_stats() -> Dict[str, Any]:
    from src.foundation import commands, evals, forge, gaming, teacher, xp
    recs = audit.recent(5000)
    count = lambda ev: sum(1 for r in recs if r.get("event") == ev)          # noqa: E731
    first = _first_record_ts()
    hist = evals.load_history(200)
    ev_first = hist[0].get("tiers", {}) if hist else {}
    ev_last = hist[-1].get("tiers", {}) if hist else {}
    try:
        st = xp.summary()
        level, xpv = st.level, st.total
    except Exception:
        level = xpv = None
    now_tests = load_milestones().get("now", {}).get("foundation_tests")
    return {
        "days_alive": round((time.time() - first) / 86400, 1) if first else None, "since": datetime.fromtimestamp(first).strftime("%b %d") if first else "",
        "audit_records": len(recs), "level": level, "xp": xpv,
        "missions_done": count("mission_completed"), "skills_active": sum(1 for s in forge.all_skills() if s.get("status") == "active"),
        "lessons_active": sum(1 for x in teacher.all_lessons() if x.get("status") == "active"), "fixes_applied": sum(1 for r in recs if r.get("event") == "gaming_fix_result" and r.get("status") == "applied"),
        "commands": len(commands._PATTERNS), "games_watched": len((gaming.load_report() or {}).get("games", [])), "foundation_tests": now_tests,
        "evals": [{"ts": h.get("ts"), "tiers": {k: [v["passed"], v["total"]] for k, v in h.get("tiers", {}).items()}} for h in hist[-30:]],
        "evals_first": {k: [v["passed"], v["total"]] for k, v in ev_first.items()}, "evals_now": {k: [v["passed"], v["total"]] for k, v in ev_last.items()},
    }


NOTABLE = {"mission_completed", "skill_forged", "lesson_approved", "gaming_fix_result", "evals_run", "fast_lane_started", "project_added", "owner_stop"}


def timeline(limit: int = 60) -> List[Dict[str, Any]]:
    """Curated milestones + notable live events, newest first: [{date, icon, title, detail, live}]."""
    items: List[Dict[str, Any]] = [{"date": m["date"], "icon": m.get("icon", "•"), "title": m["title"], "detail": m.get("detail", ""), "live": False,
                                    "tests": m.get("tests"), "sort": _iso_ts(m["date"] + "T12:00:00")} for m in load_milestones().get("milestones", [])]
    for r in audit.recent(800):
        if r.get("event") in NOTABLE:
            h = humanize(r)
            if h and h["ts"]:
                items.append({"date": datetime.fromtimestamp(h["ts"]).strftime("%Y-%m-%d"), "icon": h["icon"], "title": h["text"], "detail": "", "live": True,
                              "tests": None, "sort": h["ts"], "time": datetime.fromtimestamp(h["ts"]).strftime("%H:%M")})
    items.sort(key=lambda x: x["sort"], reverse=True)
    return [{k: v for k, v in it.items() if k != "sort"} for it in items[:limit]]


def timeline_text(limit: int = 9) -> str:
    import html
    e = html.escape
    g = growth_stats()
    lines = ["🕰️ <b>AURIX growth timeline</b>"]
    bits = []
    if g.get("days_alive") is not None:
        bits.append(f"{g['days_alive']} days of recorded history")
    bits.append(f"{g['commands']} things you can say")
    if g.get("foundation_tests"):
        bits.append(f"{g['foundation_tests']} tests")
    if g.get("evals_first") and g.get("evals_now"):
        f, n = g["evals_first"], g["evals_now"]
        bits.append("self-check " + " · ".join(f"{k} {f.get(k, [0, 0])[0]}/{f.get(k, [0, 0])[1]} → {n[k][0]}/{n[k][1]}" for k in n if k in f))
    lines.append("<i>" + e(" · ".join(bits)) + "</i>")
    day = ""
    for it in timeline(limit):
        if it["date"] != day:
            day = it["date"]
            lines.append(f"\n<b>{e(day)}</b>")
        lines.append(f"{it['icon']} {e(it['title'])}" + (f"\n    <i>{e(_clip(it['detail'], 170))}</i>" if it.get("detail") else ""))
    lines.append("\n<i>The full picture with charts is on the dashboard.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------------------
# why it can be trusted: evidence, not promises
# ---------------------------------------------------------------------------------------------------------------------------------

def trust() -> Dict[str, Any]:
    """Facts a nervous owner can check: is the record intact, which guard rails are in force, what it did alone vs what it asked, its track record."""
    from src.foundation import fastlane, identity, teacher
    from src.foundation.commands import NOT_YET_AUTHORIZATIONS
    recs = audit.recent(5000)
    n = lambda pred: sum(1 for r in recs if pred(r))                        # noqa: E731
    week = time.time() - 7 * 86400
    ok = True
    try:
        ok = audit.verify(str(audit.audit_path())).ok
    except Exception:
        ok = False
    week_recs = [r for r in recs if _iso_ts(r.get("ts", "")) >= week]
    w = lambda ev: sum(1 for r in week_recs if r.get("event") == ev)        # noqa: E731
    c = teacher.config()
    rails = [
        f"Every action is written to a hash-chained log ({len(recs)} records) - {'verified intact just now' if ok else 'CHAIN BROKEN'}",
        f"{len(identity.PROTECTED_COMPONENTS)} protected components (the gate, the log, the listener, credentials, and now its own exam and approval policy): the agent cannot edit them, with or without approval",
        "Real-money trading is refused in code (" + ", ".join(NOT_YET_AUTHORIZATIONS) + "); paper trading only",
        "Code only ever runs in an isolated sandbox: no secrets, no network",
        "Your game PC accepts only fixes signed with a key that lives on just two machines, applies them with a backup, and can undo them",
        "The frontier teacher is " + ("ON, capped at " + str(c["daily_calls"]) + " calls a day, secrets redacted, hidden answers never sent" if c["enabled"] else "OFF") + "; its advice is used only after you approve it",
        "Fast lane is " + ("ON: only sandbox-only or read-only work with no outward action starts without you" if fastlane.enabled() else "OFF: everything asks first"),
        "Secrets never leave the server and are never printed",
    ]
    auto = w("auto_allowed") + w("mission_allowed") + w("fast_lane_started")
    asked = w("approved") + w("mission_approved") + w("gaming_fix_approved") + w("lesson_approved") + w("skill_forged") + w("freelance_kept") + w("content_kept") + w("learning_kept") + w("land_gate_approved") + w("evolve_approved")
    return {"integrity_ok": ok, "records": len(recs), "rails": rails,
            "week": {"did_on_its_own": auto, "asked_and_you_approved": asked,
                     "you_denied": w("denied") + w("mission_denied") + w("gaming_fix_declined") + w("freelance_discarded") + w("content_discarded") + w("learning_discarded") + w("skill_denied") + w("land_gate_rejected") + w("evolve_declined"),
                     "refused_by_protection": sum(1 for r in week_recs if r.get("reason") == "protected_component"), "missions_completed": w("mission_completed"),
                     "missions_failed": w("mission_failed") + w("mission_expired"), "fixes_undone": w("gaming_fix_undo_requested"), "stops_pressed": w("owner_stop")}}


def trust_text() -> str:
    import html
    e = html.escape
    t = trust()
    wk = t["week"]
    lines = ["🛡️ <b>Why you can trust it</b>", ("✅ " if t["integrity_ok"] else "🔴 ") + e(t["rails"][0])] + ["✅ " + e(x) for x in t["rails"][1:]]
    lines += ["", "<b>Last 7 days</b>",
              f"• Did on its own (read-only or sandboxed): {wk['did_on_its_own']}", f"• Asked first and you approved: {wk['asked_and_you_approved']}",
              f"• You said no: {wk['you_denied']} · blocked by its own protection: {wk['refused_by_protection']}",
              f"• Missions finished: {wk['missions_completed']} · failed: {wk['missions_failed']} · fixes you undid: {wk['fixes_undone']}"]
    return "\n".join(lines)
