"""src/foundation/xp.py - AURIX's XP, levels (1-50) and ranks, derived from the hash-chained audit log.

The idea comes from Ada-SI's gamified "level up" chat. The difference that matters: there the XP is a number in the
browser's localStorage that anyone can edit. Here it is a PURE FUNCTION of data/audit.jsonl, which is tamper-evident,
so the number can only be earned, never typed in - and nothing is stored that could drift.

Design rules (deliberate):
  * XP rewards OUTCOMES (steps done, missions completed, skills forged, work delivered), never CONSENT. There is no XP
    for approving or denying an action - that would reward rubber-stamping the approval gate.
  * Failure costs nothing (no negative XP), so it never pays to hide problems.
  * Per-day caps on the repeatable, cheap events stop a loop from farming levels.
  * Level is a scoreboard. It NEVER changes what AURIX may do: authority stays code-decided and owner-granted
    (contracts, the gate, protected components). `UNLOCK_HINTS` describe what the owner might now reasonably trust.

`compute()` and `level_for()` are pure (unit-tested); `summary()` reads the audit file (cached by size+mtime);
`check_level_up()` returns a one-time announcement when the level rose since the last one it announced.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.foundation import audit
from src.foundation import mission as ms

MAX_LEVEL = 50

# event -> (category, xp). Anything not listed earns nothing.
XP_TABLE: Dict[str, tuple] = {
    "mission_step_done": ("steps", 5),
    "mission_completed": ("missions", 50),
    "standing_approved": ("standing", 10),
    "standing_run_started": ("standing", 3),
    "project_added": ("projects", 5),
    "project_updated": ("projects", 2),
    "todo_done": ("todos", 3),
    "files_delivered": ("delivery", 2),
    "skill_forged": ("skills", 100),          # emitted by the skill forge once a forged skill passes its tests + approval
    "skill_used": ("skills", 2),
    "graph_ingested": ("memory", 10),
}
# max XP per calendar day per category (categories not listed are uncapped, e.g. completed missions)
DAILY_CAP: Dict[str, int] = {"steps": 100, "standing": 30, "projects": 30, "todos": 30, "delivery": 10, "skills": 300, "memory": 20}

RANKS = [(1, "Initiate"), (5, "Apprentice"), (10, "Operator"), (15, "Specialist"), (20, "Engineer"),
         (30, "Architect"), (40, "Sentinel"), (50, "Sovereign")]
UNLOCK_HINTS = {
    5: "Reliable enough to propose a first standing (scheduled) mission - you still approve it.",
    10: "Track record of finished missions: consider a longer mission budget (your call, in the contract).",
    20: "Consider letting it forge and keep its own skills (each still needs your approval).",
    30: "A long clean history: review whether any protected-component rules should ever change (only you can).",
    MAX_LEVEL: "Top of the ladder. Nothing past here changes what AURIX may do - it just means a very long clean history.",
}


def xp_for_level(level: int) -> int:
    """Total XP needed to REACH `level` (level 1 = 0)."""
    level = max(1, min(int(level), MAX_LEVEL))
    return int(40 * (level - 1) ** 1.8)


def level_for(xp: int) -> int:
    level = 1
    while level < MAX_LEVEL and xp >= xp_for_level(level + 1):
        level += 1
    return level


def rank_for(level: int) -> str:
    title = RANKS[0][1]
    for start, name in RANKS:
        if level >= start:
            title = name
    return title


@dataclass
class XPState:
    total: int = 0
    level: int = 1
    rank: str = "Initiate"
    into_level: int = 0
    level_span: int = 0               # XP between this level and the next (0 at max level)
    to_next: int = 0
    by_category: Dict[str, int] = field(default_factory=dict)
    today: int = 0
    streak_days: int = 0
    events_counted: int = 0
    next_hint: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _day(rec: Dict[str, Any]) -> str:
    return str(rec.get("ts", ""))[:10]


def compute(records: Iterable[Dict[str, Any]], today: Optional[str] = None) -> XPState:
    """XP state from audit records (any iterable of dicts with `event` and `ts`)."""
    per_day_cat: Dict[tuple, int] = defaultdict(int)
    by_cat: Dict[str, int] = defaultdict(int)
    days_active = set()
    total = counted = 0
    day_totals: Dict[str, int] = defaultdict(int)
    for rec in records:
        entry = XP_TABLE.get(rec.get("event"))
        if not entry:
            continue
        cat, xp = entry
        day = _day(rec)
        cap = DAILY_CAP.get(cat)
        if cap is not None:
            room = cap - per_day_cat[(day, cat)]
            if room <= 0:
                continue
            xp = min(xp, room)
        per_day_cat[(day, cat)] += xp
        by_cat[cat] += xp
        day_totals[day] += xp
        days_active.add(day)
        total += xp
        counted += 1
    level = level_for(total)
    base = xp_for_level(level)
    span = 0 if level >= MAX_LEVEL else xp_for_level(level + 1) - base
    streak = 0
    if days_active:
        from datetime import date, timedelta
        cursor = date.fromisoformat(today) if today else date.fromisoformat(max(days_active))
        if cursor.isoformat() not in days_active:           # a streak survives until the day is over
            cursor -= timedelta(days=1)
        while cursor.isoformat() in days_active:
            streak += 1
            cursor -= timedelta(days=1)
    hint = next((UNLOCK_HINTS[l] for l in sorted(UNLOCK_HINTS) if l > level), "")
    return XPState(total=total, level=level, rank=rank_for(level), into_level=total - base, level_span=span,
                   to_next=0 if span == 0 else xp_for_level(level + 1) - total, by_category=dict(by_cat),
                   today=day_totals.get(today or "", 0) if today else 0, streak_days=streak, events_counted=counted,
                   next_hint=hint)


# ---------------------------------------------------------------------------
# reading the audit file (cached) + level-up announcements
# ---------------------------------------------------------------------------

_cache: Dict[str, Any] = {"key": None, "records": []}


def _all_records() -> List[Dict[str, Any]]:
    path = audit.audit_path()
    try:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return []
    if _cache["key"] == key:
        return _cache["records"]
    recs: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") in XP_TABLE:            # keep only what can earn XP: small and cheap to cache
                    recs.append({"event": rec["event"], "ts": rec.get("ts", "")})
    except OSError:
        return []
    _cache.update(key=key, records=recs)
    return recs


def summary(today: Optional[str] = None) -> XPState:
    import time
    return compute(_all_records(), today or time.strftime("%Y-%m-%d"))


def _state_path() -> Path:
    return ms.data_dir() / "xp_state.json"


def check_level_up(state: Optional[XPState] = None) -> Optional[str]:
    """One-time announcement text when the level is higher than the last announced one (else None).

    The very first call just records the current level silently (an existing history is not a surprise party)."""
    state = state or summary()
    path = _state_path()
    try:
        announced = int(json.loads(path.read_text(encoding="utf-8")).get("level", 0))
    except (OSError, ValueError, TypeError):
        announced = 0
    if state.level == announced:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"level": state.level, "total": state.total}), encoding="utf-8")
    os.replace(tmp, path)
    if announced == 0 or state.level < announced:
        return None
    nxt = f" Next level in {state.to_next} XP." if state.to_next else " Max level."
    hint = f"\n<i>{state.next_hint}</i>" if state.next_hint else ""
    return (f"🎉 <b>LEVEL UP - Level {state.level}: {state.rank}</b> ({state.total} XP).{nxt}{hint}\n"
            "<i>Level is a scoreboard; it never changes what AURIX is allowed to do.</i>")


def render(state: Optional[XPState] = None) -> str:
    """Telegram text for the `level` command."""
    s = state or summary()
    bar_n = 12
    filled = 0 if not s.level_span else int(bar_n * s.into_level / s.level_span)
    if s.into_level > 0 and filled == 0:
        filled = 1                                   # any progress must be visible
    bar = "■" * filled + "□" * (bar_n - filled)
    lines = [f"<b>Level {s.level} - {s.rank}</b>   {s.total} XP",
             f"{bar} " + (f"{s.into_level}/{s.level_span} to level {s.level + 1}" if s.level_span else "max level")]
    if s.by_category:
        lines.append("Earned from: " + ", ".join(f"{k} {v}" for k, v in sorted(s.by_category.items(), key=lambda kv: -kv[1])))
    lines.append(f"Today: {s.today} XP · streak: {s.streak_days} day{'s' if s.streak_days != 1 else ''}")
    if s.next_hint:
        lines.append(f"<i>{s.next_hint}</i>")
    lines.append("<i>XP comes only from finished work in the tamper-evident audit log; approving things earns nothing.</i>")
    return "\n".join(lines)
