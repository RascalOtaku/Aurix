"""src/foundation/standing.py - STANDING missions: approve once, runs on a schedule.

A standing mission is a mission TEMPLATE plus a schedule. The owner approves the template
once (they see the same proposal a one-off mission shows). Each run then spawns an ordinary
mission from that template, auto-activated under the standing approval, with the template's
ceilings, sandbox and prohibitions - nothing widens.

Rails (all enforced in code):
* Only low-risk domain packs may run unattended (`DomainPack.standing_ok`); anything else
  (bug bounty, social posting, real estate, self-improve, one-off CT jobs) is refused.
* The template is fingerprinted at approval; a run whose template no longer matches is refused.
* One mission at a time; a blocked/stuck run cannot hold the schedule forever (wall-clock
  ceiling is enforced even when the runner is not running).
* Missing owner authorizations/credentials -> the run is skipped and the owner told once a day.
* 3 consecutive runs that do not complete -> the standing mission pauses itself.
* Per-day and lifetime run caps; optional expiry. `stop` pauses every standing mission.

Protected component (approval_gate): agent tools cannot write here.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from src.foundation import audit
from src.foundation import capabilities as cap
from src.foundation import mission as ms

MAX_CONSECUTIVE_FAILURES = 3
MIN_INTERVAL_MINUTES = 60
DEFAULT_DAILY_AT = "03:00"


class StandingStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    DENIED = "denied"


@dataclass
class Schedule:
    kind: str = "daily"              # daily | every
    at: str = DEFAULT_DAILY_AT       # HH:MM local (daily)
    minutes: int = 0                 # interval (every)

    def describe(self) -> str:
        return (f"every day at {self.at}" if self.kind == "daily"
                else f"every {self.minutes // 60}h" + (f"{self.minutes % 60}m" if self.minutes % 60 else ""))


_SCHEDULE_RES = [                       # (^|\s+): a text that is ONLY a schedule leaves an empty goal
    re.compile(r"(?:^|\s+)(?:every\s+day|daily|each\s+day|nightly)\s+at\s+(\d{1,2}):(\d{2})\s*$", re.I),
    re.compile(r"(?:^|\s+)(?:every\s+night|nightly|daily)\s*$", re.I),
    re.compile(r"(?:^|\s+)every\s+(\d{1,3})\s*(hours?|hrs?|h|minutes?|mins?|m)\s*$", re.I),
]


def split_schedule(text: str):
    """(goal_without_schedule, Schedule). Default: daily at 03:00 local."""
    text = (text or "").strip()
    m = _SCHEDULE_RES[0].search(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("time must be HH:MM (24h)")
        return text[:m.start()].strip(), Schedule("daily", f"{hh:02d}:{mm:02d}")
    m = _SCHEDULE_RES[1].search(text)
    if m:
        return text[:m.start()].strip(), Schedule("daily", DEFAULT_DAILY_AT)
    m = _SCHEDULE_RES[2].search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        minutes = n * 60 if unit.startswith("h") else n
        if minutes < MIN_INTERVAL_MINUTES:
            raise ValueError(f"minimum interval is {MIN_INTERVAL_MINUTES} minutes")
        return text[:m.start()].strip(), Schedule("every", minutes=minutes)
    return text, Schedule("daily", DEFAULT_DAILY_AT)


def tz_offset(now_ts: Optional[float] = None) -> timedelta:
    """Local UTC offset at `now_ts`. Order: AURIX_TZ_OFFSET_MINUTES (fixed override, tests), then
    AURIX_TIMEZONE (IANA, DST-correct: the same setting scheduled tasks use), else MDT (-6h)."""
    fixed = os.environ.get("AURIX_TZ_OFFSET_MINUTES")
    if fixed not in (None, ""):
        try:
            return timedelta(minutes=int(fixed))
        except ValueError:
            pass
    name = (os.environ.get("AURIX_TIMEZONE") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            at = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz=timezone.utc)
            return ZoneInfo(name).utcoffset(at.astimezone(ZoneInfo(name))) or timedelta(0)
        except Exception:
            pass                                    # no tz database: fall through to the default
    return timedelta(minutes=-360)


def local_now(now_ts: float) -> datetime:
    return datetime.fromtimestamp(now_ts, tz=timezone.utc) + tz_offset(now_ts)


@dataclass
class StandingMission:
    id: str
    title: str
    goal: str
    schedule: Schedule
    template: dict                               # mission contract dict (ms._to_dict) at proposal
    template_hash: str = ""
    status: StandingStatus = StandingStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    approved_at: Optional[float] = None
    decided_by: str = ""
    max_runs_per_day: int = 1
    max_total_runs: int = 0                      # 0 = unlimited
    expires: Optional[str] = None                # YYYY-MM-DD
    last_run_at: Optional[float] = None
    last_run_local_date: str = ""
    runs: int = 0
    runs_today: int = 0
    runs_today_date: str = ""
    consecutive_failures: int = 0
    current_child: str = ""
    run_log: List[dict] = field(default_factory=list)
    last_block_notice_date: str = ""
    note: str = ""


def new_standing_id() -> str:
    return "sm-" + secrets.token_hex(3)


def fingerprint(template: dict) -> str:
    """Hash of the approved terms, ignoring per-run identity (id, workspace, run state)."""
    m = ms._from_dict(template)
    terms = m.terms()
    terms.pop("id", None)
    terms.pop("workspace", None)
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# schedule maths
# ---------------------------------------------------------------------------

def is_due(sm: StandingMission, now_ts: float) -> bool:
    if sm.status != StandingStatus.ACTIVE:
        return False
    if sm.expires:
        try:
            if local_now(now_ts).date() > datetime.strptime(sm.expires, "%Y-%m-%d").date():
                return False
        except ValueError:
            return False
    if sm.max_total_runs and sm.runs >= sm.max_total_runs:
        return False
    today = local_now(now_ts).strftime("%Y-%m-%d")
    runs_today = sm.runs_today if sm.runs_today_date == today else 0
    if runs_today >= sm.max_runs_per_day:
        return False
    if sm.schedule.kind == "every":
        return sm.last_run_at is None or now_ts - sm.last_run_at >= sm.schedule.minutes * 60
    hh, mm = (int(x) for x in sm.schedule.at.split(":"))
    ln = local_now(now_ts)
    if ln.strftime("%Y-%m-%d") == sm.last_run_local_date:
        return False
    return (ln.hour, ln.minute) >= (hh, mm)


def next_run_text(sm: StandingMission, now_ts: float) -> str:
    if sm.schedule.kind == "every":
        base = (sm.last_run_at or now_ts) + sm.schedule.minutes * 60
        return (datetime.fromtimestamp(base, tz=timezone.utc) + tz_offset(base)).strftime("%a %H:%M local")
    ln = local_now(now_ts)
    hh, mm = (int(x) for x in sm.schedule.at.split(":"))
    target = ln.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if ln.strftime("%Y-%m-%d") == sm.last_run_local_date or target <= ln:
        target += timedelta(days=1)
    return target.strftime("%a %H:%M local")


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _to_json(sm: StandingMission) -> dict:
    d = asdict(sm)
    d["status"] = sm.status.value
    return d


def _from_json(d: dict) -> StandingMission:
    d = dict(d)
    d["schedule"] = Schedule(**d.get("schedule", {}))
    d["status"] = StandingStatus(d.get("status", "proposed"))
    return StandingMission(**d)


class StandingStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = root

    @property
    def dir(self) -> Path:
        return (self._root or ms.data_dir()) / "standing"

    def save(self, sm: StandingMission) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{sm.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_to_json(sm), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, sid: str) -> Optional[StandingMission]:
        try:
            return _from_json(json.loads((self.dir / f"{sid}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def all(self) -> List[StandingMission]:
        out = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("sm-*.json")):
                sm = self.load(p.stem)
                if sm:
                    out.append(sm)
        return out

    # -- owner actions --------------------------------------------------------
    def create(self, goal: str, schedule: Schedule, contract: ms.MissionContract,
               max_runs_per_day: int = 1, max_total_runs: int = 0, expires: Optional[str] = None) -> StandingMission:
        template = ms._to_dict(contract)
        sm = StandingMission(id=new_standing_id(), title=goal[:80], goal=goal, schedule=schedule,
                             template=template, template_hash=fingerprint(template),
                             max_runs_per_day=max(1, max_runs_per_day), max_total_runs=max_total_runs,
                             expires=expires)
        self.save(sm)
        audit.append("standing_proposed", standing=sm.id, goal=goal[:200], schedule=schedule.describe())
        return sm

    def approve(self, sid: str, decided_by: str = "owner", now_ts: Optional[float] = None) -> str:
        sm = self.load(sid)
        if sm is None or sm.status != StandingStatus.PROPOSED:
            return f"No proposed standing mission [{sid}]."
        now_ts = now_ts or time.time()
        sm.status, sm.approved_at, sm.decided_by = StandingStatus.ACTIVE, now_ts, decided_by
        sm.template_hash = fingerprint(sm.template)
        sm.last_run_at = now_ts                      # first run is the NEXT slot, never immediately
        ln = local_now(now_ts)
        hh, mm = (int(x) for x in sm.schedule.at.split(":")) if sm.schedule.kind == "daily" else (0, 0)
        if sm.schedule.kind == "daily" and (ln.hour, ln.minute) >= (hh, mm):
            sm.last_run_local_date = ln.strftime("%Y-%m-%d")
        self.save(sm)
        audit.append("standing_approved", standing=sm.id, by=decided_by, template_hash=sm.template_hash,
                     schedule=sm.schedule.describe())
        return f"Standing mission [{sm.id}] is ACTIVE ({sm.schedule.describe()}); next run {next_run_text(sm, now_ts)}."

    def _set(self, sid: str, allowed_from: set, to: StandingStatus, verb: str, by: str) -> str:
        sm = self.load(sid)
        if sm is None or sm.status not in allowed_from:
            return f"No standing mission [{sid}] that can be {verb}."
        sm.status = to
        if to == StandingStatus.ACTIVE:
            sm.consecutive_failures = 0
        self.save(sm)
        audit.append(f"standing_{to.value}", standing=sm.id, by=by)
        return f"Standing mission [{sid}] {verb}."

    def deny(self, sid, by="owner"):
        return self._set(sid, {StandingStatus.PROPOSED}, StandingStatus.DENIED, "denied", by)

    def pause(self, sid, by="owner"):
        return self._set(sid, {StandingStatus.ACTIVE}, StandingStatus.PAUSED, "paused", by)

    def resume(self, sid, by="owner"):
        return self._set(sid, {StandingStatus.PAUSED}, StandingStatus.ACTIVE, "resumed", by)

    def retire(self, sid, by="owner"):
        return self._set(sid, {StandingStatus.ACTIVE, StandingStatus.PAUSED, StandingStatus.PROPOSED},
                         StandingStatus.RETIRED, "retired", by)

    def pause_all(self, by: str = "owner STOP") -> List[str]:
        paused = []
        for sm in self.all():
            if sm.status == StandingStatus.ACTIVE:
                self.pause(sm.id, by=by)
                paused.append(sm.id)
        return paused


# ---------------------------------------------------------------------------
# eligibility + rendering
# ---------------------------------------------------------------------------

def eligibility(goal: str) -> Optional[str]:
    """None if this goal may run unattended, else the reason it may not."""
    packs = cap.match_packs(goal)
    if not packs:
        return ("Standing missions need a known domain (so its legal gates and limits are known). "
                "Allowed: " + ", ".join(p.id for p in cap.PACKS if p.standing_ok))
    blocked = [p.id for p in packs if not p.standing_ok]
    if blocked:
        return (f"'{', '.join(blocked)}' cannot run unattended (it needs your judgement each time). "
                "Allowed: " + ", ".join(p.id for p in cap.PACKS if p.standing_ok))
    return None


def render_standing(sm: StandingMission, now_ts: Optional[float] = None) -> str:
    e = html.escape
    contract = ms._from_dict(sm.template)
    body = ms.render_proposal(contract, limit=3000)
    body = body.rsplit("\nReply", 1)[0]
    head = (f"\U0001F501 <b>Standing mission proposal</b> [{sm.id}]\n"
            f"<b>Runs:</b> {e(sm.schedule.describe())} (max {sm.max_runs_per_day}/day"
            + (f", {sm.max_total_runs} total" if sm.max_total_runs else "") + ")\n"
            "<b>Unattended:</b> each run starts on its own under THIS approval, with the limits below. "
            "It pauses itself after 3 runs that don't complete, and `stop` pauses it.\n\n")
    footer = (f"\nReply <code>approve standing {sm.id}</code> or <code>deny standing {sm.id}</code>.")
    text = head + body.split("\n", 1)[1] if "\n" in body else head + body
    return text[:3600 - len(footer)] + footer


def describe_all(store: StandingStore, now_ts: Optional[float] = None) -> str:
    now_ts = now_ts or time.time()
    items = [s for s in store.all() if s.status != StandingStatus.RETIRED]
    if not items:
        return "No standing missions. Create one: `standing: <goal> every day at 03:00`."
    lines = ["<b>Standing missions</b>"]
    for s in items:
        extra = f" next {next_run_text(s, now_ts)}" if s.status == StandingStatus.ACTIVE else ""
        lines.append(f"[{s.id}] {html.escape(s.status.value)} - {html.escape(s.title[:60])} "
                     f"({html.escape(s.schedule.describe())}){extra}; runs {s.runs}, "
                     f"failing streak {s.consecutive_failures}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------

Notify = Callable[[str], Awaitable[None]]
StartRunner = Callable[[str], Awaitable[None]]


class StandingScheduler:
    def __init__(self, standing: StandingStore, missions: ms.MissionStore, start_runner: StartRunner,
                 notify: Notify, now: Callable[[], float] = time.time, **presence_kw):
        self.standing, self.missions = standing, missions
        self.start_runner, self.notify, self.now = start_runner, notify, now
        self.presence_kw = presence_kw

    async def _say(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:
            pass

    async def reconcile(self, sm: StandingMission) -> None:
        """Account for the child mission of the previous run (finished, or stuck past its ceiling)."""
        if not sm.current_child:
            return
        child = self.missions.load(sm.current_child)
        if child is None:
            outcome = "missing"
        elif child.status == ms.MissionStatus.ACTIVE:
            over = ms.limits_reason(child, self.now())
            if not over:
                return                                        # still legitimately in flight or waiting
            self.missions.finish(child, ms.MissionStatus.EXPIRED, f"{over} (stuck; released by scheduler)")
            outcome = "expired"
        else:
            outcome = child.status.value
        ok = outcome == ms.MissionStatus.COMPLETED.value
        sm.run_log = (sm.run_log + [{"mission": sm.current_child, "at": self.now(), "outcome": outcome}])[-20:]
        sm.consecutive_failures = 0 if ok else sm.consecutive_failures + 1
        sm.current_child = ""
        if sm.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            sm.status = StandingStatus.PAUSED
            audit.append("standing_auto_paused", standing=sm.id, failures=sm.consecutive_failures)
            await self._say(f"⏸ Standing mission [{sm.id}] paused itself after {sm.consecutive_failures} runs "
                            f"that did not complete (last: {outcome}). `resume standing {sm.id}` to re-enable.")
        self.standing.save(sm)

    async def tick(self) -> List[str]:
        """Called about once a minute. Returns the actions taken (for logs and tests)."""
        actions: List[str] = []
        now = self.now()
        today = local_now(now).strftime("%Y-%m-%d")
        for sm in self.standing.all():
            if sm.status not in (StandingStatus.ACTIVE, StandingStatus.PAUSED):
                continue
            await self.reconcile(sm)
            sm = self.standing.load(sm.id)
            if sm is None or not is_due(sm, now):
                continue
            if self.missions.active() is not None:
                actions.append(f"{sm.id}: deferred (another mission is active)")
                continue
            if fingerprint(sm.template) != sm.template_hash:
                self.standing.pause(sm.id, by="scheduler: template fingerprint mismatch")
                actions.append(f"{sm.id}: paused (template tampered)")
                await self._say(f"⛔ Standing mission [{sm.id}] paused: its approved template no longer matches.")
                continue
            child = self._child_from(sm)
            req = cap.resolve(child.requirements.get("capabilities", []), sandbox=child.sandboxed, **self.presence_kw)
            if req.manual:
                actions.append(f"{sm.id}: skipped (needs owner: {', '.join(req.manual)[:80]})")
                if sm.last_block_notice_date != today:
                    sm.last_block_notice_date = today
                    self.standing.save(sm)
                    await self._say(f"⏸ Standing mission [{sm.id}] could not run today. Only you can provide:\n- "
                                    + "\n- ".join(req.manual[:6]) + "\nFor authorizations reply `authorize <name>`.")
                continue
            self.missions.propose(child)
            reply = self.missions.activate(child.id, decided_by=f"standing:{sm.id}", now_ts=now)
            if "ACTIVE" not in reply:
                actions.append(f"{sm.id}: could not start ({reply})")
                continue
            sm.runs += 1
            sm.runs_today = (sm.runs_today if sm.runs_today_date == today else 0) + 1
            sm.runs_today_date = today
            sm.last_run_at, sm.last_run_local_date = now, today
            sm.current_child = child.id
            self.standing.save(sm)
            audit.append("standing_run_started", standing=sm.id, mission=child.id, run=sm.runs)
            await self.start_runner(child.id)
            actions.append(f"{sm.id}: started {child.id}")
            await self._say(f"\U0001F501 Standing mission [{sm.id}] started run #{sm.runs} as [{child.id}]. `status` to watch, `stop` to halt.")
        return actions

    def _child_from(self, sm: StandingMission) -> ms.MissionContract:
        d = dict(sm.template)
        d.update(id=ms.new_mission_id(), status="proposed", approved_at=None, decided_by="", current_step=0,
                 usage={}, contract_hash="", note="", workspace="", created_at=time.time())
        d["steps"] = [dict(s, status="pending", evidence="") for s in d.get("steps", [])]
        child = ms._from_dict(d)
        child.workspace = ms.workspace_for(child.id)
        return child


# ---------------------------------------------------------------------------
# helpers used by commands.py
# ---------------------------------------------------------------------------

async def propose_standing(goal_text: str, llm, session_id: Optional[str], standing: StandingStore,
                           sandboxed: bool, **presence_kw) -> StandingMission:
    """Plan `goal_text` (which may end with a schedule) and store it as a PROPOSED standing mission."""
    from src.foundation import planner
    goal, schedule = split_schedule(goal_text)
    if not goal:
        raise ValueError("give me a goal")
    why_not = eligibility(goal)
    if why_not:
        raise PermissionError(why_not)
    with tempfile.TemporaryDirectory() as tmp:               # the template is not a live mission
        contract = await planner.propose_mission(goal, llm=llm, session_id=session_id,
                                                 store=ms.MissionStore(root=Path(tmp)), sandboxed=sandboxed,
                                                 **presence_kw)
    return standing.create(goal, schedule, contract)
