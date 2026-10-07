"""src/foundation/qol.py - owner quality-of-life commands: help, history, show, approvals, log,
files, tools, authorizations, doctor.

Pure read-only renderers (plus `collect_files`, which only lists). Output is Telegram HTML, so
every piece of stored/model-influenced text goes through html.escape. Called from
commands.Foundation, i.e. only ever after the sender was verified as the owner.
"""
from __future__ import annotations

import html
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.foundation import audit
from src.foundation import capabilities as cap
from src.foundation import forge
from src.foundation import identity
from src.foundation import mission as ms
from src.foundation import standing as st

e = html.escape
MAX_FILES = 10
MAX_FILE_BYTES = 45 * 1024 * 1024
MAX_SCAN = 400
_STATUS_ICON = {"active": "▶️", "completed": "✅", "failed": "❌", "stopped": "⛔", "expired": "⌛",
                "denied": "🚫", "proposed": "📝"}

HELP = """<b>AURIX - what you can say</b>
<b>Missions</b>
• <code>mission: &lt;goal&gt;</code> - plan it; I show the contract first
• <code>approve mission &lt;id&gt;</code> / <code>deny mission &lt;id&gt;</code>
• <code>status</code> - live self-report · <code>resume</code> · <code>stop</code> (absolute)
• <code>history</code> - recent missions · <code>show &lt;id&gt;</code> - one mission or standing mission
• <code>files [id]</code> - what a mission produced · <code>send files [id]</code> - deliver them here
<b>Standing (scheduled)</b>
• <code>standing: &lt;goal&gt; every day at 03:00</code> · <code>standing</code> (list)
• <code>approve|deny|pause|resume|retire standing &lt;id&gt;</code>
<b>Skill forge</b>
• <code>forge: &lt;small tool to build&gt;</code> - I write it and test it in the sandbox; nothing runs until you approve
• <code>skills</code> · <code>show skill &lt;name&gt;</code> · <code>approve|deny|retire skill &lt;name&gt;</code> · <code>run skill &lt;name&gt; {json}</code>
<b>Projects &amp; to-dos</b>
• <code>projects</code> · <code>project: &lt;name&gt; - &lt;note&gt;</code> · <code>project done &lt;id&gt;</code>
• <code>todo: &lt;text&gt;</code> · <code>todos</code> · <code>done &lt;id&gt;</code>
<b>Health &amp; safety</b>
• <code>approvals</code> - what is waiting on you · <code>log [n]</code> - audit trail
• <code>doctor</code> - check every subsystem · <code>tools</code> - what I can use · <code>ping</code>
• <code>disk</code> - how full the server's disk is (no model, no approval) · <code>homelab</code> - is each homelab service answering
• <code>trades</code> - the paper-trading agent: lifeforce, vs SPY, holdings (read-only; paper money only)
• <code>menu</code> - buttons for everything · <code>timeline</code> - how far AURIX has come · <code>trust</code> - why you can rely on it
• <code>all</code> - <b>one push</b>: re-check everything (audit, homelab, sandbox, backup, game PC, planner, trading) and ask the PC for fresh facts · <code>dashboard</code> - every monitor and add-on at a glance (web: /command)
• <code>remember: ...</code> · <code>forget &lt;id or words&gt;</code> · <code>unforget</code> · <code>memory</code> · <code>what do you remember about &lt;topic&gt;</code> - what I know about you: I offer to remember things you say about yourself, you tap yes or no
• <code>shards</code> · <code>pause &lt;id&gt;</code> · <code>resume &lt;id&gt;</code> · <code>pause all</code> - every helper that works on its own, what it can spend, and a pause switch for each (it also stops the cron jobs)
• <code>gamepilot</code> · <code>pair 123456</code> · <code>arm [min]</code> · <code>disarm</code> · <code>unpair</code> - see and control your gaming PC from a Fire TV: watch by default, control only while armed
• <code>money</code> · <code>money &lt;id&gt;</code> · <code>money &lt;id&gt; start|pause|reject</code> · <code>lab</code> - an honest ranked list of ways to make or save money, and the paper strategy leaderboard (simulation only; real-money trading stays refused)
• <code>land</code> · <code>land build &lt;name&gt;</code> · <code>land show &lt;property&gt;</code> · <code>land cap &lt;property&gt; &lt;max&gt; [&lt;target&gt;]</code> (your maximum TRUE exposure for one property) · <code>land promote m-xxxxxx &lt;name&gt;</code> (a mission's draft into the evidence store; its sources stay leads) · <code>land propose &lt;property&gt;</code> · <code>yes|no land a-xxxxxx</code> - LandPilot: evidence-graded land dossiers (research only). An acquisition card is approved only with its id and only for the dossier version you saw; approving records your decision, AURIX never buys, signs or contacts anyone
• <code>cad: &lt;describe a part&gt;</code> · <code>parts</code> · <code>print|no d-xxxxxx</code> · <code>printer</code> - I design it in build123d, build and price it in the sandbox, and show you size and cost; <code>print</code> slices it with your profile and starts it only if the printer is idle (no printer set up: you get the STL)
• <code>lights</code> · <code>turn on|off &lt;name&gt;</code> · <code>toggle &lt;name&gt;</code> - lights, plugs and fans through Home Assistant (locks, doors, alarms and heating are read-only from chat)
• <code>look</code> · <code>look: &lt;question&gt;</code> · or just send a photo (caption = question) - a LOCAL vision model on your GPU PC answers; <code>look</code> sees the PC screen only while a game or Steam is in front
• <code>ads</code> · <code>pause ads 10m</code> · <code>resume ads</code> (Pi-hole) · <code>tv</code> · <code>pause tv</code> · <code>resume tv</code> (Jellyfin) · <code>kuma</code> (Uptime Kuma) - the homelab apps you run, from chat
• <code>gpu</code> · <code>gpu forget &lt;model&gt;</code> - the GPU PC: on or off, its Ollama models, what is loaded, how often it went offline this week; I message you if its models go missing
• Camera alerts from Frigate arrive on their own when <code>AURIX_FRIGATE_URL</code> is set (person at the door, with a one-line description if you want)
• <code>version</code> · <code>reviewed vX.Y.Z</code> - AURIX's version history; a big shift in the code opens a review ticket for Claude Code
• <code>integrations</code> · <code>adopt|skip i-xxxxxx</code> - after a repo is absorbed, AURIX's own models plan what to take from it; adopting queues the idea into the upgrade lane or skill forge, which draft, test and ask you again
• <code>models</code> (or <code>router</code>) - every model/agent AURIX can use, which are on, and where each kind of work goes (private data stays on your own machines)
• <code>workspace &lt;id&gt;</code> - if your Anthropic key needs a workspace id (the teacher and upgrade lane say so when it does)
• <code>upgrade: &lt;idea&gt;</code> · <code>upgrades</code> · <code>upgrades on 3|off</code> · <code>upgrade now</code> · <code>upgrade openhands</code> · <code>yes|no u-xxxxxx</code> · <code>undo u-xxxxxx</code> - your API tokens become tested upgrades while you are away: I draft, test in the sandbox, you tap approve; a bad build rolls itself back. <code>upgrade openhands</code> drafts the next one with the sandboxed OpenHands coding agent instead of my own prompt
• Send a GitHub link (or <code>repo &lt;link&gt;</code>) - I look inside it (nothing runs), then you answer <code>yes|no r-xxxxxx</code>; yes stores it inert in a library · <code>repos</code>
• <code>games</code> · <code>game skyrim</code> · <code>fixes</code> · <code>yes|no g-xxxxxx</code> · <code>undo g-xxxxxx</code> - your Steam games and mods: ask anything, I send clear fix proposals, you answer yes or no (a bare yes/no works when one is waiting)
• <code>fast lane [on|off]</code> - safe sandbox-only / read-only missions start without waiting for your approval
• <code>evals [plan|code|last]</code> - scoreboard: does AURIX still plan safely and write correct code? (runs in the background)
• <code>teacher [on N|off]</code> · <code>teach</code> · <code>lessons</code> · <code>approve|deny|retire lesson &lt;id&gt;</code> - a frontier model writes guidance for what the local model failed at; tested first, used only after you approve
• <code>level</code> - my XP, rank and streak (earned from finished work only; approving earns nothing)
• <code>authorizations</code> · <code>authorize &lt;name&gt; [YYYY-MM-DD]</code> · <code>revoke &lt;name&gt;</code>
I also message you on my own if the audit chain breaks, the disk fills, the sandbox dies or a mission stalls.
Web UI: <code>/command</code> (Command Center), <code>/systems</code> (every subsystem + STOP), God's Eye globe (rail button)."""


def help_text() -> str:
    return HELP


def _age(ts: Optional[float], now: float) -> str:
    if not ts:
        return "?"
    s = max(0, int(now - ts))
    return (f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d")


def missions_history(store: ms.MissionStore, n: int = 8, now: Optional[float] = None) -> str:
    now = now or time.time()
    items = sorted(store.all(), key=lambda m: m.created_at, reverse=True)[:n]
    if not items:
        return "No missions yet. Try <code>mission: convert my CT scan into an STL</code>."
    lines = [f"<b>Recent missions</b> ({len(items)})"]
    for m in items:
        done = sum(1 for s in m.steps if s.status == "done")
        lines.append(f"{_STATUS_ICON.get(m.status.value, '•')} <code>{m.id}</code> {e(m.status.value)} · "
                     f"{done}/{len(m.steps)} steps · {_age(m.created_at, now)} ago\n    {e(m.objective[:90])}")
    lines.append("<i>show &lt;id&gt; for detail</i>")
    return "\n".join(lines)


def show(store: ms.MissionStore, standing: st.StandingStore, ident: str, now: Optional[float] = None) -> str:
    now = now or time.time()
    if ident.startswith("sm-"):
        sm = standing.load(ident)
        if sm is None:
            return f"No standing mission {e(ident)}."
        lines = [f"<b>Standing [{sm.id}]</b> {e(sm.status.value)}", f"<b>Goal:</b> {e(sm.goal[:300])}",
                 f"<b>When:</b> {e(sm.schedule.describe())} (next: {e(st.next_run_text(sm, now))})",
                 f"<b>Runs:</b> {sm.runs}" + (f", {sm.consecutive_failures} failing in a row" if sm.consecutive_failures else "")]
        for r in sm.run_log[-3:]:
            lines.append("• " + e(str(r)[:140]))
        return "\n".join(lines)
    m = store.load(ident)
    if m is None:
        return f"No mission {e(ident)}."
    lines = [f"{_STATUS_ICON.get(m.status.value, '•')} <b>Mission [{m.id}]</b> {e(m.status.value)}"
             + (" · sandboxed" if m.sandboxed else ""),
             f"<b>Goal:</b> {e(m.objective[:400])}"]
    for i, s in enumerate(m.steps):
        mark = {"done": "✅", "running": "▶️", "blocked": "⏸"}.get(s.status, "▫️")
        lines.append(f"{mark} {i + 1}. {e(s.title)}" + (f" - <i>{e(s.evidence[:100])}</i>" if s.evidence else ""))
    u = m.usage
    lines.append(f"<b>Used:</b> {u.tool_calls} tool calls, {u.model_calls} model calls, "
                 f"created {_age(m.created_at, now)} ago")
    if m.note:
        lines.append(f"<b>Note:</b> {e(m.note[:200])}")
    return "\n".join(lines)


def approvals_text(now: Optional[float] = None) -> str:
    from src import approval_gate as ag
    now = now or time.time()
    if not ag._pending:
        return "Nothing is waiting on you."
    lines = [f"<b>Waiting on you</b> ({len(ag._pending)})"]
    for p in ag._pending.values():
        lines.append(f"• <code>{e(p.id)}</code> {e(p.tool)} · {_age(p.created, now)} - "
                     f"{e(' '.join(p.preview.split())[:100])}")
    lines.append("<i>approve &lt;id&gt; or deny &lt;id&gt; (or deny all)</i>")
    return "\n".join(lines)


def log_text(n: int = 12) -> str:
    n = max(1, min(int(n or 12), 40))
    recs = audit.recent(n)
    if not recs:
        return "The audit log is empty."
    lines = [f"<b>Last {len(recs)} audit events</b>"]
    for r in recs:
        subject = r.get("mission") or r.get("standing") or r.get("tool") or r.get("name") or r.get("id") or ""
        detail = " ".join(str(r.get("preview") or r.get("reason") or r.get("objective") or "").split())[:60]
        lines.append(f"#{r.get('seq')} {e(str(r.get('ts', ''))[11:19])} <b>{e(str(r.get('event')))}</b> "
                     f"{e(str(subject)[:24])} {e(detail)}")
    return "\n".join(lines)


def authorizations_text(now: Optional[float] = None) -> str:
    from src.foundation.commands import NOT_YET_AUTHORIZATIONS
    granted = cap.load_authorizations()
    known = sorted(c.id for c in cap.REGISTRY.values() if c.kind == "authorization" and c.id not in NOT_YET_AUTHORIZATIONS)
    lines = ["<b>Authorizations</b>"]
    for k in known:
        p = cap.presence(cap.REGISTRY[k], authorizations=granted, now=now)
        extra = f" until {e(str(granted[k].get('expires')))}" if p.present and granted.get(k, {}).get("expires") else ""
        lines.append(f"{'✅' if p.present else '▫️'} <code>{e(k)}</code> - {e(p.detail)}{extra}")
    for k in sorted(NOT_YET_AUTHORIZATIONS):
        lines.append(f"🔒 <code>{e(k)}</code> - not available yet (activation handoff §11)")
    lines.append("<i>authorize &lt;name&gt; [YYYY-MM-DD] · revoke &lt;name&gt;</i>")
    return "\n".join(lines)


def tools_text(**presence_kw) -> str:
    """What AURIX can use right now, grouped; `presence_kw` points at the sandbox when there is one."""
    groups: Dict[str, List[str]] = {}
    for c in sorted(cap.REGISTRY.values(), key=lambda x: x.id):
        if c.kind == "authorization":
            continue
        p = cap.presence(c, **presence_kw)
        groups.setdefault(c.kind, []).append(("✅ " if p.present else "▫️ ") + c.id)
    names = {"builtin": "Built-in tools", "python_pkg": "Python packages", "binary": "Programs",
             "credential": "Credentials (set/unset only)", "service": "Services"}
    lines = ["<b>What I can use</b> (✅ ready · ▫️ missing)"]
    for kind, items in groups.items():
        lines.append(f"<b>{names.get(kind, kind)}</b>: " + e(", ".join(items)))
    return "\n".join(lines)


def disk_text(warn_pct: float = 85.0, critical_pct: Optional[float] = None) -> str:
    """`disk`: how full the SERVER's disk is (the 7070 - this app runs in a Linux container there, not on the Windows PC).

    Deterministic: no model, no shell, no approval. Uses the same df-style percentage as the watchdog, so what this says
    and when the watchdog pings you can never disagree."""
    from src.foundation import sysview, watchdog
    critical_pct = watchdog.DISK_PCT_LIMIT if critical_pct is None else critical_pct
    data = ms.data_dir()
    try:
        d = sysview.disk_usage(data if data.exists() else "/")
    except OSError as err:
        return f"I couldn't read the disk: {e(str(err))}"
    filled = min(10, int(round(d["pct"] / 10)))
    bar = "▰" * filled + "▱" * (10 - filled)
    lines = [f"💾 <b>Server disk</b> (the 7070 - not your PC)",
             f"{bar} <b>{d['pct']:.0f}% used</b> · {d['free_gb']:.1f} GB free of {d['total_gb']:.0f} GB"]
    foot = []
    for label, path in (("mission workspaces", data / "workspace"), ("forged skills", data / "forge")):
        try:
            mb = sysview.dir_size_mb(path)
        except OSError:
            continue
        foot.append(f"{label} {mb / 1000:.1f} GB" if mb >= 1000 else f"{label} {mb:.0f} MB")
    try:
        foot.append(f"audit log {audit.audit_path().stat().st_size / 1e6:.1f} MB")
    except OSError:
        pass
    if foot:
        lines.append("AURIX's own footprint: " + " · ".join(foot))
    if d["pct"] >= critical_pct:
        lines.append(f"🚨 <b>Critically full</b> (alert level is {critical_pct:.0f}%). Builds, backups and downloads can fail now - free space first.")
    elif d["pct"] >= warn_pct:
        lines.append(f"⚠️ Getting full - I'll message you on my own at {critical_pct:.0f}%.")
    else:
        lines.append("Plenty of room.")
    lines.append("<i>This is my container's view of the disk. A per-folder breakdown of the whole box is not something I can read from inside it yet; on the server, <code>du -h --max-depth=1 ~ | sort -h</code> shows it.</i>")
    return "\n".join(lines)


def homelab_text(rows: Optional[List[dict]] = None) -> str:
    """`homelab`: is each homelab service answering right now (the same links the dashboard shows)."""
    if rows is None:
        from src.foundation import command_center
        rows = command_center.probe_homelab()
    if not rows:
        return "No homelab links are configured (<code>AURIX_HOMELAB_LINKS</code>)."
    down = [r for r in rows if not r["ok"]]
    lines = [f"🏠 <b>Homelab</b> - " + ("everything answering" if not down else f"{len(down)} of {len(rows)} not answering")]
    for r in rows:
        lines.append(f"{'✅' if r['ok'] else '❌'} {e(r['name'])} - <code>{e(r['url'])}</code>" + ("" if r["ok"] else f" <i>({e(r['detail'])})</i>"))
    lines.append("<i>I message you on my own if one of these goes down after having been up.</i>")
    return "\n".join(lines)


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _trade_report(runtime: Path) -> Optional[dict]:
    import json
    try:
        rep = json.loads((runtime / "trade_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rep if isinstance(rep, dict) and "lifeforce_pct" in rep else None


def _money(x) -> str:
    n = _num(x)
    return "n/a" if n is None else f"${n:,.2f}"


def _signed(x, suffix: str = "%") -> str:
    n = _num(x)
    return "n/a" if n is None else f"{n:+.2f}{suffix}"


def trades_digest_line(runtime: Optional[Path] = None) -> str:
    """One line for the morning digest; empty when there is no report yet."""
    rep = _trade_report(Path(runtime) if runtime else Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime")
    if not rep or not rep.get("last_scan"):
        return ""
    if rep.get("dead"):
        return "<b>Paper trading:</b> 💀 DEAD - capital crossed the survival line; it stays down until you revive it on the server."
    vs = f" vs SPY {_signed(rep.get('spy_return_pct'))}" if rep.get("spy_return_pct") is not None else ""
    return (f"<b>Paper trading:</b> lifeforce {rep['lifeforce_pct']:.1f}% ({_signed(rep.get('return_pct'))}{vs}), "
            f"{len([p for p in rep.get('open_positions', []) if not p.get('core')])} picks + core"
            + (" · ⛔ kill switch tripped" if rep.get("killed") else ""))


def _trades_from_report(rep: dict, now: float) -> str:
    from datetime import datetime
    lines = ["🧾 <b>Paper trading</b> <i>(a simulation: no orders are ever placed; capital is the lifeforce)</i>"]
    if rep.get("dead"):
        lines.append(f"💀 <b>DEAD</b> since {e(str(rep.get('died_at') or '?'))} - capital fell past the survival line "
                     f"({rep.get('dies_at_drawdown_pct')}% below its peak). It flattened and stands down until revived on the server.")
    lines.append(f"• Lifeforce: <b>{_money(rep.get('equity'))}</b> = {rep['lifeforce_pct']:.1f}% of its {_money(rep.get('bankroll_start'))} start"
                 f" · peak {_money(rep.get('peak_equity'))} · {rep.get('drawdown_from_peak_pct', 0):.2f}% below it (dies at {rep.get('dies_at_drawdown_pct')}%)")
    if rep.get("spy_return_pct") is not None:
        lines.append(f"• Return {_signed(rep.get('return_pct'))} vs SPY {_signed(rep.get('spy_return_pct'))} "
                     f"→ <b>excess {_signed(rep.get('excess_pct'))}</b> over {rep.get('days_tracked', 0)} day(s) since {e(str(rep.get('inception') or '?'))}")
    else:
        lines.append(f"• Return {_signed(rep.get('return_pct'))} (the SPY comparison starts with the first market session)")
    if rep.get("trades_closed"):
        wr = rep.get("win_rate_pct")
        lines.append(f"• Closed picks: {rep['trades_closed']} ({rep.get('wins', 0)} won, {rep.get('losses', 0)} lost"
                     + (f", {wr:.0f}% win rate" if wr is not None else "") + f") · realised {_money(rep.get('realized_pnl'))}")
    picks = [p for p in rep.get("open_positions", []) if not p.get("core")]
    core = next((p for p in rep.get("open_positions", []) if p.get("core")), None)
    if picks or core:
        shown = ", ".join(f"{e(p['symbol'])} {_signed(p.get('pnl_pct'), '%') if p.get('pnl_pct') is not None else ''}".strip() for p in picks[:8])
        lines.append(f"• Holding {len(picks)} pick(s)" + (f": {shown}" if shown else "") + (f" + {core['qty']} SPY core" if core else ""))
    if rep.get("killed") and not rep.get("dead"):
        lines.append("• ⛔ kill switch tripped (daily loss limit) - waiting on <code>trade_agent.py --resume</code> on the server")
    scan = rep.get("last_scan") or ""
    if not scan:
        lines.append("• No scan yet - the first one runs at the next market open (08:00 Denver, Mon-Fri).")
    else:
        try:
            age = int((now - datetime.fromisoformat(scan[:19]).timestamp()) / 86400)
        except ValueError:
            age = 0
        lines.append(f"• Last scan {e(scan.replace('T', ' '))} ({e(rep.get('data_source') or 'no data source yet')})"
                     + (f" ⚠️ <i>{age} days ago - is it still scheduled?</i>" if age >= 5 else ""))
    lines.append("<i>Paper money only. Real-money trading stays off.</i>")
    return "\n".join(lines)


def trades_text(runtime: Optional[Path] = None, now: Optional[float] = None) -> str:
    """`trades`: a read-only look at the paper-trading agent's own records (state + trade history/journal). It never places,
    changes or approves anything, and it says plainly when the agent has gone quiet."""
    import json
    from datetime import datetime
    runtime = Path(runtime) if runtime else Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime"
    now = now or time.time()
    report = _trade_report(runtime)
    if report:
        return _trades_from_report(report, now)

    def load(name):
        try:
            return json.loads((runtime / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    state = load("trade_state.json")
    hist = load("trade_history.json")
    journal = load("trade_journal.json")
    trades = hist if isinstance(hist, list) else (journal.get("trades") if isinstance(journal, dict) else None)
    trades = [t for t in (trades or []) if isinstance(t, dict)]
    if state is None and not trades:
        return "No paper-trading records found (<code>runtime/trade_state.json</code>, <code>trade_history.json</code>)."

    def pnl(t):
        return _num(t.get("pnl") if t.get("pnl") is not None else t.get("pnl_pct"))
    with_result = [t for t in trades if pnl(t) is not None]
    wins = [t for t in with_result if pnl(t) > 0]
    stamps = []
    for t in trades:
        raw = str(t.get("time") or t.get("closed_at") or t.get("date") or "")
        try:
            stamps.append(datetime.fromisoformat(raw[:19]).timestamp())
        except ValueError:
            pass
    if isinstance(state, dict) and state.get("daily_pnl_date"):
        try:
            stamps.append(datetime.fromisoformat(str(state["daily_pnl_date"])[:10]).timestamp())
        except ValueError:
            pass
    lines = ["🧾 <b>Paper trading</b> <i>(read-only view; I do not place or change trades here)</i>"]
    lines.append(f"• Trades on record: <b>{len(trades)}</b>"
                 + (f" · {len(with_result)} with a recorded result, {len(wins)} winning" if trades else ""))
    if isinstance(state, dict):
        eq, day = _num(state.get("portfolio_open")), _num(state.get("daily_pnl"))
        bits = []
        if eq is not None:
            bits.append(f"paper equity ${eq:,.2f}")
        if day is not None:
            bits.append(f"day P&amp;L {'+' if day >= 0 else '-'}${abs(day):,.2f}" + (f" on {e(str(state['daily_pnl_date']))}" if state.get("daily_pnl_date") else ""))
        if state.get("killed") or state.get("killed_today"):
            bits.append("⛔ kill switch tripped")
        if bits:
            lines.append("• Last state: " + ", ".join(bits))
    if stamps:
        last = max(stamps)
        idle = int((now - last) / 86400)
        lines.append(f"• Last activity: {datetime.fromtimestamp(last).strftime('%Y-%m-%d')} ({idle} day{'s' if idle != 1 else ''} ago)"
                     + (" ⚠️ <i>quiet - it may not be scheduled to scan</i>" if idle >= 7 else ""))
    lines.append("<i>Paper money only. Real-money trading stays off.</i>")
    return "\n".join(lines)


def doctor_text(snapshot: dict) -> str:
    """One line per organ from a sysview snapshot, worst problems first."""
    order = {"bad": 0, "warn": 1, "ok": 2, "idle": 3}
    icon = {"ok": "✅", "warn": "⚠️", "bad": "❌", "idle": "💤"}
    organs = sorted(snapshot["organs"].values(), key=lambda o: order.get(o["status"], 9))
    bad = [o for o in organs if o["status"] in ("bad", "warn")]
    lines = ["<b>AURIX doctor</b> - " + ("all systems normal" if not bad else f"{len(bad)} thing(s) need a look")]
    for o in organs:
        headline = "; ".join(f"{k}: {v}" for k, v in list(o["metrics"].items())[:3])
        lines.append(f"{icon[o['status']]} <b>{e(o['title'])}</b> - {e(headline[:150])}")
    if snapshot.get("stop"):
        lines.append("⛔ STOP is engaged (clears itself, or on your next approve/resume).")
    lines.append("<i>Full picture: /systems in the web UI.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# mission deliverables
# ---------------------------------------------------------------------------

def collect_files(store: ms.MissionStore, mission_id: Optional[str] = None
                  ) -> Tuple[Optional[ms.MissionContract], List[Path], List[str]]:
    """Files a mission produced, safe to hand to the owner: regular files inside the mission
    workspace only (no symlinks, nothing secret-looking, nothing huge), newest first, capped."""
    m = store.load(mission_id) if mission_id else (
        store.active() or next(iter(sorted(store.all(), key=lambda x: x.created_at, reverse=True)), None))
    if m is None:
        return None, [], ["no such mission"]
    root = Path(m.workspace or ms.workspace_for(m.id))
    notes: List[str] = []
    if not root.is_dir():
        return m, [], ["the mission has no workspace yet"]
    root_real = root.resolve()
    found: List[Path] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != forge.PROVISION_DIR]   # skill copies are not output
        for name in filenames:
            scanned += 1
            if scanned > MAX_SCAN:
                notes.append(f"stopped scanning after {MAX_SCAN} files")
                break
            p = Path(dirpath) / name
            try:
                if name.startswith(".") or p.is_symlink() or not p.is_file():
                    continue
                if identity.is_secret_path(str(p)) or identity.is_protected_path(str(p)):
                    notes.append(f"skipped {name} (protected/secret)")
                    continue
                size = p.stat().st_size
                if size == 0:
                    continue
                if size > MAX_FILE_BYTES:
                    notes.append(f"skipped {name} ({size // (1024 * 1024)} MB is too large for Telegram)")
                    continue
                if root_real not in p.resolve().parents:
                    continue
                found.append(p)
            except OSError:
                continue
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if len(found) > MAX_FILES:
        notes.append(f"{len(found) - MAX_FILES} older file(s) not listed")
    return m, found[:MAX_FILES], notes


def files_text(store: ms.MissionStore, mission_id: Optional[str] = None) -> str:
    m, files, notes = collect_files(store, mission_id)
    if m is None:
        return "No missions yet."
    lines = [f"<b>Files from [{m.id}]</b> " + (f"({len(files)})" if files else "")]
    root = Path(m.workspace or ms.workspace_for(m.id)).resolve()
    for p in files:
        kb = p.stat().st_size / 1024
        lines.append(f"• <code>{e(str(p.relative_to(root)))}</code> {kb:,.0f} KB")
    if not files:
        lines.append("Nothing produced yet.")
    lines += [f"<i>{e(n)}</i>" for n in notes]
    if files:
        lines.append(f"<i>send files {m.id} - delivers them to this chat</i>")
    return "\n".join(lines)


async def deliver_files(store: ms.MissionStore, send_file: Optional[Callable], mission_id: Optional[str] = None) -> str:
    if send_file is None:
        return "File delivery is not available in this channel."
    m, files, notes = collect_files(store, mission_id)
    if m is None:
        return "No missions yet."
    if not files:
        return f"[{m.id}] has no deliverable files yet." + (" " + "; ".join(notes) if notes else "")
    root = Path(m.workspace or ms.workspace_for(m.id)).resolve()
    sent, failed = [], []
    for p in files:
        rel = str(p.relative_to(root))
        try:
            ok = await send_file(str(p), f"[{m.id}] {rel}")
        except Exception:
            ok = False
        (sent if ok else failed).append(rel)
    audit.append("files_delivered", mission=m.id, sent=len(sent), failed=len(failed))
    out = f"Sent {len(sent)} file(s) from [{m.id}]."
    if failed:
        out += " Could not send: " + e(", ".join(failed)) + "."
    return out
