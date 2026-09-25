"""src/foundation/shards.py - the shards: every autonomous helper AURIX runs, in one place, each with its own pause switch and a health light.

A "shard" is a specialist that works on its own inside a fixed budget and under the same gate as everything else: the upgrade lane, the frontier teacher, the
fast lane, paper trading, the strategy lab, the game doctor, the repo reviewer, GamePilot, memory intake and the nightly backup. Nothing new can act on its own
that is not listed here. For each one you can see what it does, when it runs, what it can spend, what only you can approve, and whether it is healthy; and you
can pause any of them with one tap (`pause <id>`, `resume <id>`, `pause all`).

The pause flag is a plain file (paused.json in the shards folder) that BOTH the app and the host scripts read, so pausing also stops the cron jobs on the host
(each checks the file before it does anything). Pausing never deletes anything and never changes what you already approved. "stale" means it should have run
recently and did not; the morning digest says so.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit

e = html.escape


def shards_dir() -> Path:
    return Path(os.environ.get("AURIX_SHARDS_DIR") or (Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "shards"))


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# id, name, icon, what, schedule, runs on, spend, gate
REGISTRY = [
    ("upgrades", "Upgrade lane", "🛠️", "Drafts one small upgrade at a time with the frontier model and tests it in the sandbox.", "up to N drafts a day, every ~3 h", "app (server)", "Anthropic API tokens (capped per day)",
     "you approve every deploy; bad builds roll back by themselves"),
    ("upgrade_applier", "Upgrade applier", "🚀", "Applies ONLY upgrades you approved: backup, apply, health-checked deploy, auto-restore.", "every minute (does nothing unless approved)", "host cron (server)", "none", "a signed approval from you"),
    ("teacher", "Frontier teacher", "🎓", "Turns failures into lessons for the small local model.", "on demand, capped per day", "app (server)", "Anthropic API tokens (capped per day)", "you approve every lesson"),
    ("fastlane", "Fast lane", "⚡", "Starts safe, read-only or sandbox-only missions without waiting for a tap.", "whenever a safe mission is proposed", "app (server)", "local model time", "code decides what counts as safe; anything risky still asks"),
    ("paper_trading", "Paper trading agent", "📈", "Simulated trading with a paper ledger against SPY. Places no orders.", "weekdays: 08:00 scan, every 30 min monitor, 14:15 close", "host cron (server)", "none (free public prices)", "simulation only; real money is refused in code"),
    ("strategy_lab", "Strategy lab", "🧪", "Backtests and live-tracks 11 textbook strategies on paper.", "weekdays 14:30 after the close", "host cron (server)", "none (free public prices)", "simulation only"),
    ("game_doctor", "Game doctor", "🎮", "Reads your Steam games and mods, proposes signed, reversible fixes.", "every ~30 min while the PC is on", "your PC (scheduled task) + app", "none", "you approve every fix; the PC re-checks the signature"),
    ("absorb", "Repo reviewer", "📥", "Fetches a repo you send, reads it as text, reports; stores it inert only if you say yes.", "every minute (does nothing unless you sent a link)", "host cron (server)", "none", "you approve storing anything; nothing is ever run"),
    ("gamepilot", "GamePilot", "📺", "Shows a game to a paired Fire TV and, only while armed, presses keys for it.", "on demand", "app (server) + your PC agent (started by you)", "none", "pairing code from you; arming from you; expires by itself"),
    ("memory", "Memory intake", "🧠", "Notices things you say about yourself and offers to remember them.", "on every message you send", "app (server)", "none", "you approve every memory; secrets are refused"),
    ("subscriptions", "Subscriptions", "💳", "Builds a checklist and monthly total from subscriptions you list, with a to-do reminder near each renewal.", "on demand, whenever you send a list", "app (server)", "none (code only, no model)", "reviewing statements and cancelling stays yours"),
    ("content", "Content drafts", "📝", "Drafts a piece for a niche channel/site topic you give it, flagging any fact it is not sure of with [VERIFY].", "on demand, whenever you send a topic", "app (server)", "none (free, local by default)", "recording, editing, and publishing stays yours"),
    ("freelance", "Freelance drafts", "🧰", "Drafts a small script for a job brief you paste in, using the frontier or free local model.", "on demand, whenever you send a brief", "app (server)", "none (free, local by default)", "keeping, delivering, and getting paid stays yours"),
    ("learning", "Reading library", "📚", "Fetches an article or takes pasted text you send, summarizes it, and tracks video links, storing anything inert only if you say yes.", "on demand, whenever you send a link or text", "app (server)", "none (free, local by default)", "you approve every item kept; nothing fetched is ever run"),
    ("transcription", "Transcription", "🎙️", "Turns a voice note or audio file you send into a text transcript with a local Whisper model.", "on demand, whenever you send audio", "app (server)", "none (free, local, no account)", "delivering and getting paid for it stays yours"),
    ("nightshift", "Overnight shift", "🎁", "Builds a pile of finished things each night (health check, a money idea researched, quiet projects, strategy lab news) for the morning.", "02:00 nightly, opened at the morning digest", "app (server)", "none (free, local)", "read-only notes; nothing is sent, bought or changed"),
    ("backup", "Nightly backup", "💾", "Copies AURIX's state to the NAS as dated snapshots.", "03:30 every night", "host cron (server)", "none", "read-only copy; secrets excluded"),
]
PAUSABLE_NOT = {"game_doctor", "backup"}                                      # these run outside the app's reach; the pause button says so instead of pretending
IDS = [r[0] for r in REGISTRY]


def paused_ids() -> List[str]:
    try:
        v = json.loads((shards_dir() / "paused.json").read_text(encoding="utf-8"))
        return [x for x in v if x in IDS] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def is_paused(shard_id: str) -> bool:
    return shard_id in paused_ids()


def set_paused(shard_id: str, flag: bool) -> str:
    shard_id = (shard_id or "").strip().lower()
    if shard_id == "all":
        targets = [i for i in IDS if i not in PAUSABLE_NOT]
        _atomic(shards_dir() / "paused.json", sorted(targets) if flag else [])
        audit.append("shards_pause_all" if flag else "shards_resume_all")
        return ("⏸️ Paused every helper I can pause (" + str(len(targets)) + "). Nothing autonomous will run; what you already approved stays as it is. <code>resume all</code> starts them again."
                if flag else "▶️ Everything is running again.")
    row = next((r for r in REGISTRY if r[0] == shard_id), None)
    if row is None:
        return f"I do not have a helper called <code>{e(shard_id)}</code>. <code>shards</code> lists them."
    if shard_id in PAUSABLE_NOT:
        return (f"<b>{e(row[1])}</b> runs outside my reach ({e(row[5])}), so I cannot pause it from here. "
                + ("Stop it on the PC by removing its scheduled task." if shard_id == "game_doctor" else "It is a read-only copy on a cron job; remove the cron line to stop it."))
    cur = set(paused_ids())
    (cur.add if flag else cur.discard)(shard_id)
    _atomic(shards_dir() / "paused.json", sorted(cur))
    audit.append("shard_paused" if flag else "shard_resumed", id=shard_id)
    return f"{'⏸️ Paused' if flag else '▶️ Resumed'} <b>{e(row[1])}</b>."


# ---------------------------------------------------------------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------------------------------------------------------------

def _age_h(ts: Optional[float], now: float) -> Optional[float]:
    return round((now - ts) / 3600, 1) if ts else None


def _iso_ts(s: str) -> Optional[float]:
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(s)[:19]).timestamp()
    except ValueError:
        return None


def _health(shard_id: str, now: float) -> Dict[str, Any]:
    """{state: ok|stale|off|blocked|idle, detail}. Never raises: a probe that fails is reported as unknown, not as healthy."""
    try:
        if shard_id == "upgrades":
            from src.foundation import upgrades
            c = upgrades.config()
            if not c["enabled"] or not c["daily_drafts"]:
                return {"state": "off", "detail": "switched off (`upgrades on 3` starts it)"}
            st = json.loads((upgrades._data() / "state.json").read_text(encoding="utf-8")) if (upgrades._data() / "state.json").exists() else {}
            last = next((r for r in reversed(audit.recent(300)) if r.get("event") in ("upgrade_called", "upgrade_draft_failed")), None)
            if st.get("api_note_day") == time.strftime("%Y-%m-%d") or (last and last["event"] == "upgrade_draft_failed" and str(last.get("why", "")).startswith(("the API", "network"))):
                return {"state": "blocked", "detail": ("cannot use the model: " + str(last.get("why", ""))[:140]) if last and last["event"] == "upgrade_draft_failed" else "cannot reach the model (see the last note)"}
            age = _age_h(st.get("last_attempt"), now)
            ok, why = upgrades.can_draft()
            if not ok:
                return {"state": "idle", "detail": why}
            return {"state": "stale" if age is not None and age > 12 else "ok", "detail": f"last attempt {age} h ago" if age is not None else "no attempt yet",
                    "used": f"{upgrades._usage()['drafts']}/{c['daily_drafts']} drafts today"}
        if shard_id == "teacher":
            from src.foundation import teacher
            c = teacher.config()
            return {"state": "ok" if c["enabled"] and c["daily_calls"] else "off", "detail": f"{teacher.usage_today().get('calls', 0)}/{c['daily_calls']} calls today" if c["enabled"] else "off"}
        if shard_id == "fastlane":
            from src.foundation import fastlane
            return {"state": "ok" if fastlane.enabled() else "off", "detail": "on" if fastlane.enabled() else "off"}
        if shard_id == "paper_trading":
            rep = json.loads((Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime" / "trade_report.json").read_text(encoding="utf-8"))
            age = _age_h(_iso_ts(rep.get("updated", "")), now)
            return {"state": "stale" if age is not None and age > 72 else "ok", "detail": f"report {age} h old, lifeforce {rep.get('lifeforce_pct')}%"}
        if shard_id == "strategy_lab":
            from src.foundation import money
            rep = money.lab_report()
            if not rep:
                return {"state": "idle", "detail": "no report yet"}
            age = _age_h(_iso_ts(str(rep.get("generated", "")) + "T00:00:00"), now)
            return {"state": "stale" if age is not None and age > 96 else "ok", "detail": f"report from {rep.get('generated')}, {rep.get('live_days', 0)} live days"}
        if shard_id == "game_doctor":
            from src.foundation import gaming
            rep = gaming.load_report()
            age = _age_h(float(rep.get("generated_ts", 0)), now) if rep else None
            return {"state": "idle" if age is None or age > 6 else "ok", "detail": "no report yet" if age is None else (f"PC report {age} h old" + (" (PC asleep or off?)" if age > 6 else ""))}
        if shard_id == "gamepilot":
            from src.foundation import gamepilot
            p = gamepilot.panel(now)
            return {"state": "ok" if p["pc_online"] else "idle", "detail": ("PC agent online" if p["pc_online"] else "PC agent not started") + f", {p['paired']} screen(s), " + ("ARMED" if p["armed"] else "watch only")}
        if shard_id == "nightshift":
            from src.foundation import nightshift
            if not nightshift.enabled():
                return {"state": "off", "detail": "switched off"}
            ts = nightshift.state().get("last_shift_ts")
            age = _age_h(ts, now)
            return {"state": "idle" if age is None else ("stale" if age > 40 else "ok"), "detail": "has not run yet (first shift tonight)" if age is None else f"last shift {age} h ago, {len(nightshift.unopened())} unopened"}
        if shard_id == "memory":
            from src.foundation import memory
            return {"state": "ok", "detail": f"{len(memory.entries())} remembered, {len(memory.pending())} waiting"}
        if shard_id == "subscriptions":
            from src.foundation import subscriptions
            items = subscriptions.entries()
            return {"state": "ok", "detail": f"{len(items)} tracked, ~${subscriptions.total_monthly():,.2f}/mo" if items else "nothing tracked yet"}
        if shard_id == "content":
            from src.foundation import content
            items = content.pieces()
            return {"state": "ok", "detail": f"{len(items)} draft(s) so far, {len(content.pending())} waiting on you"}
        if shard_id == "freelance":
            from src.foundation import freelance
            items = freelance.jobs()
            return {"state": "ok", "detail": f"{len(items)} draft(s) so far, {len(freelance.pending())} waiting on you"}
        if shard_id == "learning":
            from src.foundation import learning
            items = learning.items()
            return {"state": "ok", "detail": f"{len(items)} item(s) so far, {len(learning.pending())} waiting on you"}
        if shard_id == "transcription":
            from src.foundation import transcription
            items = transcription.jobs()
            running = sum(1 for j in items if j["status"] == "running")
            return {"state": "ok", "detail": f"{len(items)} job(s) so far" + (f", {running} running" if running else "")}
        if shard_id == "backup":
            st = json.loads((Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "backup_status.json").read_text(encoding="utf-8"))
            age = _age_h(st.get("finished"), now) if isinstance(st.get("finished"), (int, float)) else None
            return {"state": "ok" if st.get("ok") and (age is None or age <= 30) else "stale", "detail": (st.get("message") or "")[:80] + (f" ({age} h ago)" if age is not None else "")}
    except Exception:                                                        # noqa: BLE001
        return {"state": "idle", "detail": "no data yet"}
    return {"state": "ok", "detail": "event-driven: does nothing until there is something to do"}


def rows(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = now or time.time()
    paused = set(paused_ids())
    out = []
    for sid, name, icon, what, sched, where, spend, gate in REGISTRY:
        h = {"state": "paused", "detail": "paused by you"} if sid in paused else _health(sid, now)
        out.append({"id": sid, "name": name, "icon": icon, "what": what, "schedule": sched, "where": where, "spend": spend, "gate": gate, "pausable": sid not in PAUSABLE_NOT, **h})
    return out


def stale(now: Optional[float] = None) -> List[Dict[str, Any]]:
    return [r for r in rows(now) if r["state"] in ("stale", "blocked")]


def text() -> str:
    icon = {"ok": "🟢", "stale": "🟠", "blocked": "🟠", "off": "⚪", "idle": "⚪", "paused": "⏸️"}
    lines = ["🤖 <b>Everything that works on its own</b> <i>(each one lists what it can spend and what only you can approve)</i>"]
    for r in rows():
        lines.append(f"{icon.get(r['state'], '•')} {r['icon']} <b>{e(r['name'])}</b> <code>{r['id']}</code> · {e(r['detail'])}" + (f"\n     <i>spends: {e(r['spend'])} · gate: {e(r['gate'])}</i>" if r["state"] not in ("off",) else ""))
    lines.append("<code>pause &lt;id&gt;</code> · <code>resume &lt;id&gt;</code> · <code>pause all</code> · <code>resume all</code>")
    return "\n".join(lines)


def panel(now: Optional[float] = None) -> List[Dict[str, Any]]:
    return rows(now)
