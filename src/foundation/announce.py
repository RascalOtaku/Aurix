"""src/foundation/announce.py - things come to you: "what's new" messages with an OK button, so you can just watch AURIX evolve.

Two sources, both read from records that already exist:
  * milestones: every entry in timeline/milestones.json that has not been announced yet (a new deploy ships new entries, so a new release
    speaks up by itself the first time the app is running)
  * evolution events: a skill AURIX learned, a lesson you approved, a self-check score that went UP

Each announcement is ONE batched message (never a spam of many), sent once, and carries [OK] and [Timeline] buttons. Tapping OK is recorded in
the audit log; nothing else happens, it is only an acknowledgement. State lives in data/announce.json.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from src.foundation import audit, growth

e = html.escape
FIRST_RUN_WINDOW_DAYS = 2
MAX_LINES = 8


def _path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "announce.json"


def _load() -> Dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: Dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def milestone_id(m: dict) -> str:
    return f"{m.get('date')}|{m.get('title')}"


def tick(now: float = None) -> List[str]:
    """Messages to send right now (usually none). Cheap: reads the milestone file, a few audit records and the eval history."""
    now = now or time.time()
    state = _load()
    first = not state
    msgs: List[str] = []
    lines: List[str] = []

    ms = growth.load_milestones().get("milestones", [])
    known = set(state.get("milestones", []))
    fresh = [m for m in ms if milestone_id(m) not in known]
    if first:                                            # the very first run only sets the baseline: history is never replayed as "news"
        fresh = []
    for m in fresh[-MAX_LINES:]:
        lines.append(f"{m.get('icon', '•')} <b>{e(m['title'])}</b>" + (f"\n     <i>{e(str(m.get('detail', ''))[:160])}</i>" if m.get("detail") else ""))

    seen_seq = int(state.get("seen_seq", 0))
    recs = audit.recent(400)
    top = max([int(r.get("seq", 0)) for r in recs] + [seen_seq])
    if not first:
        for r in recs:
            if int(r.get("seq", 0)) <= seen_seq:
                continue
            ev = r.get("event")
            if ev == "skill_forged":
                lines.append(f"🛠️ <b>AURIX learned a new skill</b>: <code>{e(str(r.get('name', '')))}</code> (you approved it; it can now be used in missions)")
            elif ev == "lesson_approved":
                lines.append(f"🎓 <b>A new lesson is now guiding it</b>: <code>{e(str(r.get('id', '')))}</code>")
    from src.foundation import evals
    hist = evals.load_history(2)
    if len(hist) == 2 and hist[-1].get("ts") != state.get("eval_ts"):
        better = [f"{k} {hist[-2]['tiers'][k]['passed']}/{hist[-2]['tiers'][k]['total']} → {v['passed']}/{v['total']}" for k, v in hist[-1].get("tiers", {}).items()
                  if k in hist[-2].get("tiers", {}) and v["total"] == hist[-2]["tiers"][k]["total"] and v["passed"] > hist[-2]["tiers"][k]["passed"]]
        if better and not first:
            lines.append("📈 <b>Its self-check score went up</b>: " + e(", ".join(better)))

    new_state = {"milestones": sorted(known | {milestone_id(m) for m in ms}), "seen_seq": top, "eval_ts": (hist[-1].get("ts") if hist else state.get("eval_ts")),
                 "updated": now, "last_sent": state.get("last_sent", 0), "last_ack": state.get("last_ack", 0)}
    if lines:
        new_state["last_sent"] = now
        msgs.append("🆕 <b>What's new with AURIX</b>\n" + "\n".join(lines) + "\n\n<i>Tap OK when you have seen it. The timeline shows how far it has come.</i>")
        audit.append("announcement_sent", items=len(lines))
    _save(new_state)
    return msgs


def pending_ack() -> bool:
    """True while an announcement is waiting for the owner's OK (lets a bare 'ok' answer it without swallowing ordinary chat)."""
    st = _load()
    return float(st.get("last_sent", 0)) > float(st.get("last_ack", 0)) and time.time() - float(st.get("last_sent", 0)) < 86400


def acknowledge() -> str:
    st = _load()
    st["last_ack"] = time.time()
    if st:
        _save(st)
    audit.append("owner_ack")
    g = growth.growth_stats()
    bits = []
    if g.get("days_alive") is not None:
        bits.append(f"{g['days_alive']} days in")
    bits.append(f"{g['commands']} things it understands")
    if g.get("skills_active") is not None:
        bits.append(f"{g['skills_active']} skills learned")
    return "👍 Noted. " + " · ".join(bits) + ". I'll keep telling you when something new arrives."
