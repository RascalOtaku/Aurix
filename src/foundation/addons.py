"""src/foundation/addons.py - the one place that knows every AURIX add-on and monitor, and can check them all with one push.

  probe_addons()   what each add-on is, whether it is healthy, one plain line about it, and how to use it (dashboard tiles + `dashboard`)
  probe_monitors() every watchdog-style signal in one strip (audit chain, disk, memory, backups, homelab, the game PC, paper trading, evals)
  run_all()        the ONE PUSH: re-checks everything that is safe to re-check and asks the game PC for fresh facts. Read-only apart from
                   that one request flag; it never changes a setting, approves anything or touches a mission.
  dashboard_text() the same picture as a compact Telegram message

Every probe is wrapped: one broken add-on shows as a red tile, never as a broken dashboard.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

e = html.escape
OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
ICON = {OK: "🟢", WARN: "🟠", BAD: "🔴", OFF: "⚪"}


def _age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    return f"{s}s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} d"


def _tile(key: str, title: str, icon: str, status: str, summary: str, detail: str = "", hint: str = "", how: str = "",
          actions: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    return {"key": key, "title": title, "icon": icon, "status": status, "summary": summary, "detail": detail, "hint": hint, "how": how, "actions": actions or []}


def _safe(key: str, title: str, icon: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return fn()
    except Exception as exc:                                    # one broken probe must never blank the dashboard
        return _tile(key, title, icon, BAD, "could not be checked", type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------------------------------
# add-ons
# ---------------------------------------------------------------------------------------------------------------------------------

def _gaming() -> Dict[str, Any]:
    from src.foundation import gaming
    rep = gaming.load_report()
    how = "Ask: “what's wrong with my Skyrim?”  ·  game skyrim  ·  fixes  ·  yes / no"
    if rep is None:
        return _tile("gaming", "Game doctor", "🎮", OFF, "waiting for the first report from your PC",
                     "The agent on the PC writes one every ~30 min.", "games refresh", how,
                     [{"label": "Ask the PC now", "action": "games_refresh"}])
    age = gaming.report_age_hours(rep)
    pend = len(gaming.pending())
    n = len(rep.get("games", []))
    if age is not None and age > gaming.STALE_REPORT_HOURS:
        return _tile("gaming", "Game doctor", "🎮", WARN, f"{n} games · report is {_age(age * 3600)} old",
                     "Is the PC on? The agent reports every few minutes.", "games refresh", how)
    return _tile("gaming", "Game doctor", "🎮", WARN if pend else OK, f"{n} games" + (f" · {pend} fix waiting for your yes/no" if pend else " · all healthy"),
                 f"report {_age((age or 0) * 3600)} old", "fixes" if pend else "games", how,
                 [{"label": "Refresh facts", "action": "games_refresh"}])


def _trading() -> Dict[str, Any]:
    from src.foundation import qol
    rt = Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime"
    rep = qol._trade_report(rt)
    how = "trades  ·  it always stays fully invested; dies at −35 % from its peak"
    if not rep:
        return _tile("trading", "Paper trading", "📈", OFF, "no report yet", "First scan is the next market open (08:00 Denver).", "trades", how)
    if rep.get("dead"):
        return _tile("trading", "Paper trading", "📈", BAD, "DEAD - crossed the survival line", "Revive on the server: trade_agent.py --revive", "trades", how)
    if not rep.get("last_scan"):
        return _tile("trading", "Paper trading", "📈", OFF, "waiting for the first market open", "Mon-Fri 08:00 Denver.", "trades", how)
    vs = f" · vs SPY {rep['excess_pct']:+.2f}%" if rep.get("excess_pct") is not None else ""
    return _tile("trading", "Paper trading", "📈", WARN if rep.get("killed") else OK,
                 f"lifeforce {rep['lifeforce_pct']:.1f}% ({rep.get('return_pct', 0):+.2f}%){vs}",
                 f"{len([p for p in rep.get('open_positions', []) if not p.get('core')])} picks + core · last scan {str(rep['last_scan'])[:16].replace('T', ' ')}"
                 + (" · kill switch tripped" if rep.get("killed") else ""), "trades", how)


def _evals() -> Dict[str, Any]:
    from src.foundation import evals
    hist = evals.load_history(1)
    how = "evals · evals plan · evals code · runs itself nightly at 03:15"
    if not hist:
        return _tile("evals", "Self-check (evals)", "🧪", OFF, "not run yet", "Send “evals” for the first scoreboard.", "evals", how,
                     [{"label": "Run planner check", "action": "evals_plan"}, {"label": "Run full check", "action": "evals_all"}])
    last = hist[-1]
    tiers = last.get("tiers", {})
    total_fail = sum(len(t.get("failed", [])) for t in tiers.values())
    summ = " · ".join(f"{k} {v['passed']}/{v['total']}" for k, v in tiers.items())
    return _tile("evals", "Self-check (evals)", "🧪", OK if not total_fail else WARN, summ, f"last run {str(last.get('ts', ''))[:16].replace('T', ' ')}", "evals last", how,
                 [{"label": "Run planner check", "action": "evals_plan"}, {"label": "Run full check", "action": "evals_all"}])


def _teacher() -> Dict[str, Any]:
    from src.foundation import teacher
    c, u = teacher.config(), teacher.usage_today()
    pend = sum(1 for x in teacher.all_lessons() if x.get("status") == "pending")
    act = sum(1 for x in teacher.all_lessons() if x.get("status") == "active")
    how = "teacher on 5 · teach · lessons · approve lesson <id>"
    if not c["key_present"]:
        return _tile("teacher", "Frontier teacher", "🎓", OFF, "no API key on the server", "Run scripts/set_teacher_key.sh, then “teacher on 5”.", "teacher", how)
    if not c["enabled"] or not c["daily_calls"]:
        return _tile("teacher", "Frontier teacher", "🎓", OFF, "key present · switched off", "“teacher on 5” allows 5 calls a day.", "teacher", how,
                     [{"label": "Turn on (5 calls a day)", "action": "teacher_on", "arg": "5"}])
    return _tile("teacher", "Frontier teacher", "🎓", WARN if pend else OK, f"on · {u.get('calls', 0)}/{c['daily_calls']} calls today · {act} active lessons",
                 (f"{pend} lesson waiting for your approval" if pend else "no lessons waiting"), "lessons" if pend else "teacher", how,
                 [{"label": "Learn from failures", "action": "teach"}, {"label": "Turn off", "action": "teacher_off"}])


def _fastlane() -> Dict[str, Any]:
    from src.foundation import fastlane
    on = fastlane.enabled()
    return _tile("fastlane", "Fast lane", "⚡", OK if on else OFF, "safe missions start without a tap" if on else "every mission waits for your approval",
                 "sandbox-only / read-only, nothing owed by you, no outward actions", "fast lane " + ("off" if on else "on"),
                 "Say a goal: “write a script that…”. Risky goals still ask.",
                 [{"label": "Turn off" if on else "Turn on", "action": "fastlane_off" if on else "fastlane_on"}])


def _forge() -> Dict[str, Any]:
    from src.foundation import forge
    ss = forge.all_skills()
    act = sum(1 for s in ss if s.get("status") == "active")
    pend = sum(1 for s in ss if s.get("status") == "pending")
    return _tile("forge", "Skill forge", "🛠️", WARN if pend else OK, f"{act} active skills" + (f" · {pend} waiting for approval" if pend else ""),
                 "AURIX writes and tests small tools in the sandbox; nothing runs until you approve", "skills" if pend else "forge: <tool>",
                 "forge: a tool that … · skills · approve skill <name>")


def _standing() -> Dict[str, Any]:
    from src.foundation import standing as st
    items = st.StandingStore().all()
    live = [s for s in items if s.status == st.StandingStatus.ACTIVE]
    prop = [s for s in items if s.status == st.StandingStatus.PROPOSED]
    return _tile("standing", "Standing missions", "🔁", WARN if prop else (OK if live else OFF), f"{len(live)} running on a schedule" + (f" · {len(prop)} to approve" if prop else ""),
                 "recurring jobs you approved once", "standing", "standing: <goal> every day at 03:00")


def _backup() -> Dict[str, Any]:
    from src.foundation import watchdog
    b = watchdog.read_backup_status()
    if not b:
        return _tile("backup", "Nightly backup", "💾", OFF, "no backup status yet", "Runs at 03:30 to the NAS.", "", "")
    age = time.time() - float(b.get("finished", 0) or 0)
    ok = bool(b.get("ok"))
    return _tile("backup", "Nightly backup", "💾", OK if ok and age < 36 * 3600 else BAD if not ok else WARN,
                 ("last backup OK " if ok else "LAST BACKUP FAILED ") + _age(age) + " ago", str(b.get("message", ""))[:80], "", "Runs at 03:30 to the NAS; I message you if it fails.")


ADDONS: List[tuple] = [("gaming", "Game doctor", "🎮", _gaming), ("trading", "Paper trading", "📈", _trading), ("evals", "Self-check (evals)", "🧪", _evals),
                       ("teacher", "Frontier teacher", "🎓", _teacher), ("fastlane", "Fast lane", "⚡", _fastlane), ("forge", "Skill forge", "🛠️", _forge),
                       ("standing", "Standing missions", "🔁", _standing), ("backup", "Nightly backup", "💾", _backup)]


def probe_addons() -> List[Dict[str, Any]]:
    return [_safe(k, t, i, fn) for k, t, i, fn in ADDONS]


# ---------------------------------------------------------------------------------------------------------------------------------
# monitors
# ---------------------------------------------------------------------------------------------------------------------------------

def probe_monitors(organs: Optional[List[Dict[str, Any]]] = None, homelab: Optional[List[Dict[str, Any]]] = None,
                   addons: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One strip: the system organs the dashboard already computes, the homelab roll-up, and the add-ons that watch something."""
    out: List[Dict[str, Any]] = []
    for o in organs or []:
        out.append({"key": o.get("key"), "title": o.get("title"), "status": o.get("status", OFF), "detail": o.get("detail") or o.get("sub", "")})
    if homelab is not None:
        down = [h for h in homelab if not h.get("ok")]
        out.append({"key": "homelab", "title": "Homelab", "status": BAD if down else (OK if homelab else OFF),
                    "detail": (f"{len(down)} of {len(homelab)} not answering: " + ", ".join(h.get("name", "?") for h in down[:3])) if down else f"{len(homelab)} services answering"})
    by = {a["key"]: a for a in addons or []}
    for key, title in (("backup", "Backups"), ("gaming", "Game PC agent"), ("trading", "Paper trading agent"), ("evals", "Self-checks")):
        a = by.get(key)
        if a:
            out.append({"key": key, "title": title, "status": a["status"], "detail": a["summary"]})
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# the one push
# ---------------------------------------------------------------------------------------------------------------------------------

def _step(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
    return {"name": name, "ok": ok, "detail": detail, "seconds": round(time.time() - t0, 1)}


def run_all() -> Dict[str, Any]:
    """Re-check everything that is safe to re-check. None of these steps changes a setting or decides anything for you."""
    from src import approval_gate as ag
    from src.foundation import audit, command_center, evals, fastlane, forge, gaming, planner, sandbox, teacher, watchdog

    def audit_chain():
        r = audit.verify(str(audit.audit_path()))
        return r.ok, "hash chain intact" if r.ok else "CHAIN BROKEN - see doctor"

    def homelab():
        rows = command_center.probe_homelab()
        if not rows:
            return None, "none configured"
        down = [h for h in rows if not h.get("ok")]
        return not down, (f"{len(rows)} services answering" if not down else f"{len(down)} down: " + ", ".join(h.get("name", "?") for h in down[:3]))

    def sandbox_up():
        sandbox.reset_cache()
        ok = sandbox.available()
        return ok, "isolated sandbox is up" if ok else "sandbox is down: missions cannot run code"

    def backup():
        b = watchdog.read_backup_status()
        if not b:
            return None, "no backup status yet"
        age = time.time() - float(b.get("finished", 0) or 0)
        return bool(b.get("ok")) and age < 36 * 3600, f"{'OK' if b.get('ok') else 'FAILED'} {_age(age)} ago"

    def game_pc():
        msg = gaming.request_refresh()
        rep = gaming.load_report()
        age = gaming.report_age_hours(rep)
        return (rep is not None and (age is None or age <= gaming.STALE_REPORT_HOURS)), ("asked for fresh facts; " + (f"last report {_age((age or 0) * 3600)} old" if rep else "no report yet"))

    def planner_selfcheck():
        tasks = evals.load_tasks("plan")
        res = asyncio.run(evals.run_plan_tier(tasks, None, sandbox.available(), sandbox.presence_kw()))
        bad = [r["id"] for r in res if not r["passed"]]
        return not bad, f"{len(res) - len(bad)}/{len(res)} planning checks pass" + (f" (failing: {', '.join(bad[:3])})" if bad else "")

    def trading():
        a = _safe("trading", "Paper trading", "📈", _trading)
        return (None if a["status"] == OFF else a["status"] == OK), a["summary"]

    def needs_you():
        n_app = len(getattr(ag, "_pending", {}) or {})
        n_fix = len(gaming.pending())
        n_les = sum(1 for x in teacher.all_lessons() if x.get("status") == "pending")
        n_sk = sum(1 for s in forge.all_skills() if s.get("status") == "pending")
        total = n_app + n_fix + n_les + n_sk
        bits = [f"{n} {w}" for n, w in ((n_app, "approval(s)"), (n_fix, "game fix(es)"), (n_les, "lesson(s)"), (n_sk, "skill(s)")) if n]
        return (total == 0), ("nothing waiting on you" if not total else "waiting on you: " + ", ".join(bits))

    steps = [_step("Audit chain", audit_chain), _step("Homelab services", homelab), _step("Code sandbox", sandbox_up), _step("Nightly backup", backup),
             _step("Game PC agent", game_pc), _step("Planner self-check", planner_selfcheck), _step("Paper trading", trading), _step("Waiting on you", needs_you)]
    bad = [s for s in steps if s["ok"] is False]
    return {"ts": time.time(), "steps": steps, "ok": not bad, "attention": len(bad),
            "summary": "Everything checks out." if not bad else f"{len(bad)} thing{'s' if len(bad) != 1 else ''} need attention."}


def render_run_all(r: Dict[str, Any]) -> str:
    lines = [f"{'✅' if r['ok'] else '⚠️'} <b>{e(r['summary'])}</b>"]
    for s in r["steps"]:
        icon = "✅" if s["ok"] else "⚠️" if s["ok"] is False else "➖"
        lines.append(f"{icon} <b>{e(s['name'])}</b> - {e(str(s['detail']))}")
    lines.append("<i>Nothing was changed. <code>dashboard</code> shows every monitor and add-on.</i>")
    return "\n".join(lines)


def dashboard_text(monitors: List[Dict[str, Any]], addons: List[Dict[str, Any]]) -> str:
    lines = ["📊 <b>AURIX dashboard</b>  <i>(the web version is at /command)</i>", "", "<b>Monitors</b>"]
    for m in monitors:
        lines.append(f"{ICON.get(m['status'], '⚪')} {e(str(m['title']))}" + (f" - {e(str(m['detail']))}" if m.get("detail") else ""))
    lines += ["", "<b>Add-ons</b>"]
    for a in addons:
        lines.append(f"{ICON.get(a['status'], '⚪')} {a['icon']} <b>{e(a['title'])}</b> - {e(a['summary'])}")
        if a.get("how"):
            lines.append(f"      <i>{e(a['how'])}</i>")
    lines += ["", "Send <code>all</code> to check everything now."]
    return "\n".join(lines)
