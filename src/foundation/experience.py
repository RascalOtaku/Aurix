"""src/foundation/experience.py - the learning flywheel: every failure, refusal, "no", undo and command you use becomes fuel for evolution.

    real events (audit log)  ->  harvest()  ->  experiences (a plain ledger)  ->  learned self-check tasks  ->  the nightly eval catches a
    repeat forever  ->  a failing check asks the teacher for a lesson  ->  you approve  ->  it guides future work  ->  you are told (announce.py)

So nothing is wasted: the blocked mission of 2026-09-20 ("needs alpaca-credentials for a primes script") would have become a permanent check
the moment it happened. Nothing here acts on the world; it reads records that already exist and writes to data/ only.

  harvest()         turn new audit records into experiences (incremental, cheap, idempotent) and learned eval tasks
  learned_tasks()   plan-tier eval tasks created from real failures (evals.load_tasks merges them in)
  count_usage(kind) which commands/buttons you actually use (kinds only; never message text)
  stats()           the ledger in numbers, for the dashboard
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from src.foundation import audit

MAX_EXPERIENCES = 500
MAX_LEARNED = 40


def _dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "experience"


def _read(name: str, default: Any) -> Any:
    try:
        return json.loads((_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name: str, obj: Any) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / name)


def _ts(rec: dict) -> float:
    from src.foundation.growth import _iso_ts
    return _iso_ts(rec.get("ts", "")) or time.time()


def _capability_ids(missing: List[str]) -> List[str]:
    """`alpaca-credentials (Alpaca paper-trading API key + secret)` -> `alpaca-credentials`."""
    return [re.split(r"\s+\(", str(m), maxsplit=1)[0].strip() for m in missing if str(m).strip()]


def classify(rec: dict) -> Dict[str, Any]:
    """The lesson-bearing meaning of an audit record, or {} if it teaches nothing."""
    ev, mid = rec.get("event", ""), rec.get("mission", "")
    if ev == "mission_blocked":
        return {"kind": "failure", "source": "mission", "summary": f"mission {mid} was blocked at {rec.get('reason', 'a step')}"
                + (f" (missing: {', '.join(_capability_ids(rec.get('missing', [])))})" if rec.get("missing") else ""), "signal": "planner or requirements were wrong"}
    if ev in ("mission_failed", "mission_expired"):
        return {"kind": "failure", "source": "mission", "summary": f"mission {mid} {ev.split('_')[1]}: {str(rec.get('note', ''))[:100]}", "signal": "execution failed"}
    if ev == "gaming_fix_result" and rec.get("status") in ("failed", "refused"):
        return {"kind": "failure", "source": "game doctor", "summary": f"game fix {rec.get('id')} was {rec.get('status')}", "signal": "the PC could not apply an approved fix"}
    if ev == "gaming_fix_undo_requested":
        return {"kind": "correction", "source": "game doctor", "summary": f"you undid game fix {rec.get('id')}", "signal": "a fix was not wanted: prefer information over auto-fix for this kind"}
    if ev == "gaming_fix_declined":
        return {"kind": "preference", "source": "game doctor", "summary": f"you declined game fix {rec.get('id')}", "signal": "do not re-raise this soon"}
    if ev in ("denied", "mission_denied") or (ev == "deny_all"):
        return {"kind": "preference", "source": "approval gate", "summary": f"you denied {rec.get('tool') or rec.get('mission') or 'a request'}", "signal": "the request was not wanted"}
    if rec.get("reason") == "protected_component":
        return {"kind": "guard", "source": "protection", "summary": "the protection blocked an attempt to touch a protected component", "signal": "the guard rail held"}
    if ev == "watchdog_alert" and "Cleared" not in str(rec.get("preview", "")):
        return {"kind": "incident", "source": "watchdog", "summary": re.sub(r"<[^>]+>", "", str(rec.get("preview", "")))[:100], "signal": "something needed attention"}
    if ev == "mission_completed":
        return {"kind": "success", "source": "mission", "summary": f"mission {mid} finished", "signal": "worked"}
    if ev == "skill_forged":
        return {"kind": "success", "source": "forge", "summary": f"learned skill {rec.get('name', '')}", "signal": "a new capability"}
    return {}


def harvest() -> Dict[str, int]:
    """Fold new audit records into the ledger and mint learned eval tasks. Safe to call every minute."""
    state = _read("state.json", {"seen_seq": 0})
    seen = int(state.get("seen_seq", 0))
    recs = [r for r in audit.recent(500) if int(r.get("seq", 0)) > seen]
    if not recs:
        return {"new": 0, "learned": 0}
    ledger: List[dict] = _read("ledger.json", [])
    learned: List[dict] = _read("learned_tasks.json", [])
    known = {t["id"] for t in learned}
    new = mint = 0
    for r in recs:
        c = classify(r)
        if not c:
            continue
        ledger.append({"seq": r.get("seq"), "ts": r.get("ts"), **c})
        new += 1
        if r.get("event") == "mission_blocked" and r.get("reason") == "preflight" and r.get("missing"):
            forbid = _capability_ids(r.get("missing", []))
            objective = _objective_of(r.get("mission", ""))
            tid = "learned-" + str(r.get("mission", ""))
            if objective and tid not in known and len(learned) < MAX_LEARNED:
                learned.append({"id": tid, "tier": "plan", "prompt": objective, "learned_from": f"mission {r.get('mission')} blocked at preflight ({', '.join(forbid)})",
                                "expect": {"forbid_capabilities": forbid, "no_duplicate_steps": True}, "created": time.time()})
                known.add(tid)
                mint += 1
    _write("ledger.json", ledger[-MAX_EXPERIENCES:])
    if mint:
        _write("learned_tasks.json", learned)
        audit.append("learned_check_created", count=mint)
    _write("state.json", {"seen_seq": max(int(r.get("seq", 0)) for r in recs)})
    return {"new": new, "learned": mint}


def _objective_of(mission_id: str) -> str:
    try:
        from src.foundation import mission as ms
        m = ms.MissionStore().load(mission_id)
        return (m.objective if m else "") or ""
    except Exception:
        return ""


def learned_tasks() -> List[dict]:
    return [t for t in _read("learned_tasks.json", []) if isinstance(t, dict) and t.get("id") and t.get("prompt")]


def count_usage(kind: str) -> None:
    """Which kinds of command are used (kinds only, never the text of a message)."""
    if not kind:
        return
    u = _read("usage.json", {})
    row = u.setdefault(kind, {"n": 0})
    row["n"] += 1
    row["last"] = time.time()
    _write("usage.json", u)


def stats() -> Dict[str, Any]:
    ledger = _read("ledger.json", [])
    by: Dict[str, int] = {}
    for x in ledger:
        by[x.get("kind", "?")] = by.get(x.get("kind", "?"), 0) + 1
    usage = _read("usage.json", {})
    top = sorted(usage.items(), key=lambda kv: -kv[1].get("n", 0))[:4]
    return {"experiences": len(ledger), "by_kind": by, "learned_checks": len(learned_tasks()), "top_used": [(k, v.get("n", 0)) for k, v in top],
            "recent": [{"ts": x.get("ts"), "kind": x.get("kind"), "summary": x.get("summary")} for x in ledger[-6:]][::-1]}
