"""src/foundation/nightshift.py - the overnight shift: AURIX works while you are away and leaves you a small pile of finished things to open in the morning.

Everything here is free and local: no API tokens, no accounts, nothing sent anywhere, nothing that changes a file outside its own notes. Each "gift" is a small piece of
finished work: a health check of the whole machine, a money idea researched down to its first step (with tap buttons to explore or drop it), a nudge on the project
that has gone quiet, what the paper strategy lab found. Once a night (AURIX_NIGHTSHIFT_AT, default 02:00; empty = off) `run_shift` builds the gifts and stores them.
At the morning time `unwrap` turns them into one message (with the decisions waiting on you), and the dashboard shows the same list under "While you were away".

If the frontier model is unavailable (no API credits) the shift says so plainly instead of pretending: the gifts here never needed it.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.foundation import audit

e = html.escape
KEEP_SHIFTS = 30
SPOTLIGHT_GAP_DAYS = 10


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "nightshift"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load(name: str, default: Any) -> Any:
    try:
        return json.loads((_data() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def state() -> Dict[str, Any]:
    v = _load("state.json", {})
    return v if isinstance(v, dict) else {}


def shifts() -> List[dict]:
    v = _load("shifts.json", [])
    return [s for s in v if isinstance(s, dict) and isinstance(s.get("gifts"), list)] if isinstance(v, list) else []


def enabled() -> bool:
    return bool(os.environ.get("AURIX_NIGHTSHIFT_AT", "02:00").strip())


# ---------------------------------------------------------------------------------------------------------------------------------
# the gifts (each returns {id, icon, title, body} or None; a gift that fails is skipped, never fatal)
# ---------------------------------------------------------------------------------------------------------------------------------

def _gift_health(now: float, st: dict) -> Optional[dict]:
    lines = []
    try:
        d = shutil.disk_usage(str(Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data"))
        lines.append(f"disk {round(100 * d.used / d.total)}% used, {round(d.free / 1e9)} GB free")
    except OSError:
        pass
    v = audit.verify()
    lines.append(f"audit chain {'intact' if v.ok else 'BROKEN at #' + str(v.bad_seq)} ({v.records} records)")
    try:
        from src.foundation import evals
        hist = evals.load_history(1)
        if hist:
            lines.append("self-check " + ", ".join(f"{k} {x['passed']}/{x['total']}" for k, x in hist[-1].get("tiers", {}).items()))
    except Exception:
        pass
    try:
        from src.foundation import shards
        bad = shards.stale(now)
        lines.append("all helpers healthy" if not bad else "needs attention: " + "; ".join(f"{r['name']} ({r['detail'][:60]})" for r in bad[:3]))
    except Exception:
        pass
    return {"id": "health", "icon": "🩺", "title": "Overnight health check", "body": " · ".join(lines)}


def _spotlight_pick(now: float, st: dict) -> Optional[dict]:
    from src.foundation import money
    seen = st.get("spotlights", {})
    for r in money.ranked():
        if r["status"] != "idea" or r["id"] == "crypto_yield" or r["est_high"] <= 0:
            continue
        if now - float(seen.get(r["id"], 0)) < SPOTLIGHT_GAP_DAYS * 86400:
            continue
        return r
    return None


def _gift_spotlight(now: float, st: dict) -> Optional[dict]:
    from src.foundation import money
    r = _spotlight_pick(now, st)
    if r is None:
        return None
    st.setdefault("spotlights", {})[r["id"]] = now
    first_step = e(r["first_step"])
    if r["id"] == "gpu_rental":
        real = money._gpu_estimate()
        if real:
            first_step = real
    body = (f"{e(r['summary'])}\n<b>Estimate:</b> ${r['est_low']:,.0f}-{r['est_high']:,.0f}/mo at {r['hours_per_week']:g} h/wk (a rough guess, check it). <b>Legal risk:</b> {e(r['legal_risk'])}."
            f"\n<b>I would do:</b> {e(r['aurix_does'])}\n<b>Only you:</b> {e(r['you_do'])}\n<b>First step:</b> {first_step}"
            f"\nWant me to work on it? <code>money {r['id']} explore</code> or <code>money {r['id']} reject</code>")
    return {"id": "spotlight", "icon": "💡", "title": f"Money idea of the night: {r['name']}", "body": body, "money_id": r["id"]}


def _gift_projects(now: float, st: dict) -> Optional[dict]:
    from src.foundation import projects
    data = projects.Registry().load()
    stale = sorted((p for p in data["projects"] if p.status == "active" and (now - p.touched) / 86400 >= projects.STALE_DAYS), key=lambda p: p.touched)
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    overdue = [t for t in data["todos"] if not t.done and t.due and t.due < today]
    if not stale and not overdue:
        return None
    bits = []
    if stale:
        p = stale[0]
        bits.append(f"<b>{e(p.name)}</b> has been quiet for {int((now - p.touched) / 86400)} days" + (f" (last note: {e(p.note[:80])})" if p.note else "") + f". Still worth it? <code>project keep {p.id}</code>-style updates are in <code>projects</code>.")
    if overdue:
        bits.append(f"{len(overdue)} overdue to-do{'s' if len(overdue) != 1 else ''}: " + e("; ".join(t.text[:40] for t in overdue[:3])))
    return {"id": "projects", "icon": "📌", "title": "Something has gone quiet", "body": "\n".join(bits)}


def _gift_lab(now: float, st: dict) -> Optional[dict]:
    from src.foundation import money
    rep = money.lab_report()
    if not rep or str(rep.get("generated")) == st.get("lab_seen"):
        return None
    st["lab_seen"] = str(rep.get("generated"))
    rows = [s for s in rep["strategies"] if s.get("live")]
    if not rows:
        return {"id": "lab", "icon": "🧪", "title": "Strategy lab update", "body": f"{rep['strategies_tried']} strategies tracked; live scoring has {rep.get('live_days', 0)} trading days so far. Send <code>lab</code> for the table."}
    best = max(rows, key=lambda s: s["live"]["total"])
    return {"id": "lab", "icon": "🧪", "title": "Strategy lab update",
            "body": f"After {rep.get('live_days', 0)} live paper days the best is <b>{e(best['name'])}</b> at {best['live']['total']:+.1f}%. Paper money only, and a few days prove nothing. <code>lab</code> shows all of them."}


def _gift_funding(now: float, st: dict) -> Optional[dict]:
    from src.foundation import shards
    row = next((r for r in shards.rows(now) if r["id"] == "upgrades"), None)
    if row and row["state"] == "blocked":
        return {"id": "funding", "icon": "🪙", "title": "Running free for now", "body": "The frontier model can't be used (API credits are empty), so upgrades and lessons are waiting. Everything else on this list runs on local models and free data. When you top up a small amount the upgrade lane restarts by itself, capped per day."}
    return None


def _gift_rotation(now: float, st: dict) -> Optional[dict]:
    """Closes out the Telegram chat session for the day and starts a clean one - see session_rotation.py. Found
    live 2026-09-24: an ever-growing single session degrades badly on a small local model; this gives it a real
    daily boundary instead of relying only on opportunistic mid-conversation compaction."""
    from src.foundation import session_rotation
    entry = session_rotation.daily_rotate(now)
    if not entry:
        return None
    body = f"Closed out yesterday's chat ({entry['message_count']} messages) and started a fresh one."
    if entry.get("summary"):
        body += f" Yesterday's log: {entry['summary'][:280]}"
    return {"id": "rotation", "icon": "\U0001F4D3", "title": "Daily chat reset", "body": body}


GIFTS: List[Callable[[float, dict], Optional[dict]]] = [_gift_rotation, _gift_health, _gift_spotlight, _gift_projects, _gift_lab, _gift_funding]


def run_shift(now: Optional[float] = None, force: bool = False) -> Dict[str, Any]:
    now = now or time.time()
    try:
        from src.foundation import shards
        if shards.is_paused("nightshift"):
            return {"skipped": "paused"}
    except Exception:
        pass
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    st = state()
    if st.get("last_shift_day") == day and not force:
        return {"skipped": "already ran today"}
    gifts: List[dict] = []
    for fn in GIFTS:
        try:
            g = fn(now, st)
        except Exception:                                                       # noqa: BLE001 - one bad gift must not spoil the rest
            g = None
        if g:
            gifts.append(g)
    st["last_shift_day"], st["last_shift_ts"] = day, now
    log = shifts()[-(KEEP_SHIFTS - 1):]
    log.append({"ts": now, "day": day, "gifts": gifts, "opened": False})
    _atomic(_data() / "shifts.json", log)
    _atomic(_data() / "state.json", st)
    audit.append("nightshift_ran", day=day, gifts=[g["id"] for g in gifts])
    return {"ran": True, "gifts": len(gifts)}


def unopened() -> List[dict]:
    return [s for s in shifts() if not s.get("opened")]


def unwrap(now: Optional[float] = None, mark_opened: bool = True, waiting: bool = True) -> str:
    """The morning message: every gift left since you last looked, then what is waiting on you."""
    pile = unopened()
    if not pile:
        latest = shifts()[-1:]
        if not latest:
            return "🎁 No overnight shift has run yet. <code>night now</code> runs one right away."
        pile = latest
        header = "🎁 <b>Last night's shift</b> (already opened)"
    else:
        header = "🎁 <b>Good morning. Your overnight shift is ready</b>"
    gifts = [g for s in pile for g in s["gifts"]]
    seen, uniq = set(), []
    for g in reversed(gifts):                                                   # newest first; one of each kind
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    lines = [header, f"<i>{len(uniq)} thing{'s' if len(uniq) != 1 else ''} done while you were away. All free and local; nothing was sent, bought or changed.</i>"]
    for i, g in enumerate(reversed(uniq), 1):
        lines.append(f"\n{g['icon']} <b>{i}. {e(g['title'])}</b>\n{g['body']}")
    if waiting:
        try:
            from src.foundation import heartbeat
            lines.append("\n" + heartbeat.waiting_for_you(4))
        except Exception:
            pass
    if mark_opened:
        log = shifts()
        for s in log:
            s["opened"] = True
        _atomic(_data() / "shifts.json", log)
    return "\n".join(lines)


def panel(now: Optional[float] = None) -> Dict[str, Any]:
    log = shifts()
    st = state()
    latest = log[-1] if log else None
    return {"enabled": enabled(), "at": os.environ.get("AURIX_NIGHTSHIFT_AT", "02:00").strip(), "last": st.get("last_shift_ts"), "unopened": len(unopened()),
            "gifts": [{"id": g["id"], "icon": g["icon"], "title": g["title"], "body": html.unescape(__import__("re").sub(r"<[^>]+>", "", g["body"]))} for g in (latest["gifts"] if latest else [])],
            "history": [{"day": s["day"], "n": len(s["gifts"])} for s in log[-7:]]}
