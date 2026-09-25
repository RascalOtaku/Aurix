"""src/foundation/mission.py - mission contracts (activation handoff §2).

"Every approval is a mission contract: objective, allowed actions, prohibited actions,
resource ceiling, risk ceiling, deadline, success criteria. Anything not reasonably
contemplated -> ask, don't assume."

The owner approves ONE contract; while it is ACTIVE, routine (LOW/MEDIUM) actions that
fall inside it run without a ping, are counted against its resource ceiling, and are
audited under its id. A contract can never cover HIGH risk - those always need the
owner's individual approval - and it can never unlock protected components.

Protected component (approval_gate): agent tools cannot write contracts.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from src.foundation import audit
from src.foundation.risk import RiskAssessment, RiskTier


class MissionStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    EXPIRED = "expired"
    DENIED = "denied"


TERMINAL = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.STOPPED,
            MissionStatus.EXPIRED, MissionStatus.DENIED}
DEFAULT_ALLOWED_TOOLS = ["bash", "write_file", "read_file", "web_search"]


@dataclass
class Resources:
    max_steps: int = 40
    max_tool_calls: int = 200
    max_model_calls: int = 120
    max_wall_minutes: int = 180
    max_cost_usd: float = 0.0          # 0 = no paid API spend allowed
    max_disk_mb: int = 2048


@dataclass
class Usage:
    tool_calls: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0


@dataclass
class Step:
    id: str
    title: str
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    success_check: str = ""
    status: str = "pending"            # pending | running | done | blocked
    evidence: str = ""
    executor: str = "agent"            # "agent" (chat agent loop) | "openhands" (coding agent in the sandbox)


@dataclass
class MissionContract:
    id: str
    objective: str
    allowed_tools: List[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    prohibited: List[str] = field(default_factory=list)            # plain-language, shown to owner
    prohibited_patterns: List[str] = field(default_factory=list)   # regexes enforced on content
    risk_ceiling: RiskTier = RiskTier.MEDIUM
    resources: Resources = field(default_factory=Resources)
    deadline: Optional[str] = None                                  # ISO-8601
    success_criteria: List[str] = field(default_factory=list)
    workspace: str = ""
    sandboxed: bool = False             # bash/python run in the isolated sandbox container
    session_id: Optional[str] = None
    steps: List[Step] = field(default_factory=list)
    requirements: Dict = field(default_factory=dict)                # from the planner
    status: MissionStatus = MissionStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    approved_at: Optional[float] = None
    decided_by: str = ""
    current_step: int = 0
    usage: Usage = field(default_factory=Usage)
    contract_hash: str = ""
    note: str = ""

    def __post_init__(self):
        self.risk_ceiling = RiskTier(self.risk_ceiling)
        if self.risk_ceiling > RiskTier.MEDIUM:
            raise ValueError("a mission contract can never cover HIGH or CRITICAL risk")

    def terms(self) -> dict:
        """The signed portion: everything the owner approved, none of the run state."""
        return {
            "id": self.id, "objective": self.objective, "allowed_tools": sorted(self.allowed_tools),
            "prohibited": self.prohibited, "prohibited_patterns": self.prohibited_patterns,
            "risk_ceiling": int(self.risk_ceiling), "resources": asdict(self.resources),
            "deadline": self.deadline, "success_criteria": self.success_criteria,
            "workspace": self.workspace, "session_id": self.session_id, "sandboxed": self.sandboxed,
            # executor joins the signed terms only when it is not the default, so missions created
            # before it existed keep the exact hash they were approved under.
            "steps": [[s.id, s.title, s.description] + ([s.executor] if s.executor != "agent" else [])
                      for s in self.steps],
        }

    def compute_hash(self) -> str:
        blob = json.dumps(self.terms(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def integrity_ok(self) -> bool:
        return bool(self.contract_hash) and self.contract_hash == self.compute_hash()


def new_mission_id() -> str:
    return "m-" + secrets.token_hex(3)


def data_dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data"


def workspace_for(mission_id: str) -> str:
    # Deliberately NOT under data/missions/ (protected: holds the contracts).
    return str(data_dir() / "workspace" / mission_id)


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _to_dict(m: MissionContract) -> dict:
    d = asdict(m)
    d["risk_ceiling"] = int(m.risk_ceiling)
    d["status"] = m.status.value
    return d


def _from_dict(d: dict) -> MissionContract:
    d = dict(d)
    d["resources"] = Resources(**d.get("resources", {}))
    d["usage"] = Usage(**d.get("usage", {}))
    d["steps"] = [Step(**s) for s in d.get("steps", [])]
    d["status"] = MissionStatus(d.get("status", "proposed"))
    d["risk_ceiling"] = RiskTier(d.get("risk_ceiling", int(RiskTier.MEDIUM)))
    return MissionContract(**d)


class MissionStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = root

    @property
    def dir(self) -> Path:
        return (self._root or data_dir()) / "missions"

    def save(self, m: MissionContract) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{m.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_to_dict(m), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, mission_id: str) -> Optional[MissionContract]:
        try:
            return _from_dict(json.loads((self.dir / f"{mission_id}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def all(self) -> List[MissionContract]:
        out = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("m-*.json")):
                m = self.load(p.stem)
                if m:
                    out.append(m)
        return out

    def active(self) -> Optional[MissionContract]:
        return next((m for m in self.all() if m.status == MissionStatus.ACTIVE), None)

    def propose(self, m: MissionContract) -> MissionContract:
        m.status = MissionStatus.PROPOSED
        m.workspace = m.workspace or workspace_for(m.id)
        self.save(m)
        audit.append("mission_proposed", mission=m.id, objective=m.objective,
                     risk_ceiling=m.risk_ceiling.name, steps=len(m.steps))
        return m

    def activate(self, mission_id: str, decided_by: str = "owner", now_ts: Optional[float] = None) -> str:
        """Owner approval (or a standing approval). Returns a reply for the owner."""
        m = self.load(mission_id)
        if m is None:
            return f"No mission [{mission_id}]."
        if m.status != MissionStatus.PROPOSED:
            return f"Mission [{mission_id}] is {m.status.value}, not awaiting approval."
        running = self.active()
        if running:
            return f"Mission [{running.id}] is still active. `stop` it first."
        m.status = MissionStatus.ACTIVE
        m.approved_at = now_ts or time.time()
        m.decided_by = decided_by
        m.contract_hash = m.compute_hash()
        Path(m.workspace).mkdir(parents=True, exist_ok=True)
        self.save(m)
        audit.append("mission_approved", mission=m.id, by=decided_by, contract_hash=m.contract_hash)
        return f"Mission [{m.id}] is ACTIVE."

    def deny(self, mission_id: str, decided_by: str = "owner") -> str:
        m = self.load(mission_id)
        if m is None or m.status != MissionStatus.PROPOSED:
            return f"No proposed mission [{mission_id}]."
        m.status, m.decided_by = MissionStatus.DENIED, decided_by
        self.save(m)
        audit.append("mission_denied", mission=m.id, by=decided_by)
        return f"Mission [{m.id}] denied."

    def finish(self, m: MissionContract, status: MissionStatus, note: str = "") -> None:
        m.status, m.note = status, note
        self.save(m)
        audit.append(f"mission_{status.value}", mission=m.id, note=note[:300])

    def stop_active(self, reason: str = "owner STOP") -> Optional[MissionContract]:
        m = self.active()
        if m:
            self.finish(m, MissionStatus.STOPPED, reason)
        return m

    def record_tool_call(self, m: MissionContract) -> None:
        m.usage.tool_calls += 1
        self.save(m)

    def record_model_call(self, m: MissionContract) -> None:
        m.usage.model_calls += 1
        self.save(m)


# ---------------------------------------------------------------------------
# coverage decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def limits_reason(m: MissionContract, now: Optional[float] = None) -> Optional[str]:
    """Why this mission may not continue (deadline / resource ceiling), else None."""
    now = now or time.time()
    r = m.resources
    if m.deadline:
        try:
            end = datetime.fromisoformat(m.deadline)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if now >= end.timestamp():
                return "deadline passed"
        except ValueError:
            return "unreadable deadline"
    if m.approved_at and (now - m.approved_at) > r.max_wall_minutes * 60:
        return f"time ceiling reached ({r.max_wall_minutes} min)"
    if m.usage.tool_calls >= r.max_tool_calls:
        return f"tool-call ceiling reached ({r.max_tool_calls})"
    if m.usage.model_calls >= r.max_model_calls:
        return f"model-call ceiling reached ({r.max_model_calls})"
    if m.usage.cost_usd > r.max_cost_usd and r.max_cost_usd >= 0 and m.usage.cost_usd > 0:
        return f"spend ceiling reached (${r.max_cost_usd:.2f})"
    return None


def decide(m: MissionContract, tool: str, content: str, assessment: RiskAssessment,
           now: Optional[float] = None) -> Decision:
    """May this action run under mission `m` without pinging the owner?"""
    if m.status != MissionStatus.ACTIVE:
        return Decision(False, f"mission is {m.status.value}")
    if not m.integrity_ok():
        return Decision(False, "mission contract failed its integrity check")
    over = limits_reason(m, now)
    if over:
        return Decision(False, over)
    if assessment.tier >= RiskTier.HIGH:
        return Decision(False, f"{assessment.tier.name} risk always needs individual approval")
    if assessment.tier > m.risk_ceiling:
        return Decision(False, f"{assessment.tier.name} exceeds the contract's risk ceiling")
    if tool not in m.allowed_tools:
        return Decision(False, f"tool '{tool}' is not in the contract")
    for pat in m.prohibited_patterns:
        try:
            if re.search(pat, content or "", re.IGNORECASE):
                return Decision(False, f"matches a prohibited pattern ({pat})")
        except re.error:
            return Decision(False, "contract has an invalid prohibited pattern")
    return Decision(True, f"covered by mission {m.id}")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_proposal(m: MissionContract, limit: int = 3600) -> str:
    """Owner-facing summary. The approve/deny footer ALWAYS survives truncation."""
    e = html.escape
    footer = f"\nReply <code>approve mission {m.id}</code> or <code>deny mission {m.id}</code>."
    objective = m.objective if len(m.objective) <= 600 else m.objective[:600] + "..."
    lines = [f"\U0001F4CB <b>Mission proposal</b> [{m.id}]", f"<b>Objective:</b> {e(objective)}", ""]
    if m.steps:
        lines.append("<b>Plan</b>")
        lines += [f"{i}. {e(s.title)}" for i, s in enumerate(m.steps, 1)]
        lines.append("")
    req = m.requirements or {}
    for label, key in (("AURIX will pip-install", "installable"),
                       ("Needs YOU to install (commands in the plan notes)", "owner_install"),
                       ("Needs YOU to provide (credentials / authorizations)", "manual"),
                       ("Unknown capabilities (treated as max risk)", "unknown")):
        vals = req.get(key) or []
        if key == "owner_install":
            vals = [str(v).split(":", 1)[0] for v in vals]
        if vals:
            lines.append(f"<b>{label}:</b> " + e(", ".join(str(v) for v in vals)))
    for note in (req.get("legal") or [])[:4]:
        lines.append("⚖ " + e(str(note)))
    if req.get("notes"):
        lines.append("<b>Planner's open questions:</b> " + e("; ".join(str(x) for x in req["notes"][:4])))
    where = ("\U0001F9EA <b>Runs code in:</b> the ISOLATED SANDBOX (no secrets, no network, only its own "
             "workspace) - no pings for routine work" if m.sandboxed else
             "<b>Runs code in:</b> the app container - every script run will ask you")
    lines += ["", where, f"<b>Allowed tools:</b> {e(', '.join(m.allowed_tools))}",
              f"<b>Auto-approves up to:</b> {m.risk_ceiling.name} risk inside <code>{e(m.workspace)}</code>",
              "<b>Always asks you for:</b> HIGH-risk actions (touching other missions' files, sends, "
              "credentials, anything outside the sandbox)" if m.sandboxed else
              "<b>Always asks you for:</b> HIGH-risk actions (deletes, sends, installs from URLs, "
              "arbitrary code, credentials)"]
    if m.prohibited:
        lines.append("<b>Prohibited:</b> " + e("; ".join(m.prohibited)))
    r = m.resources
    lines.append(f"<b>Ceiling:</b> {r.max_steps} steps, {r.max_tool_calls} tool calls, "
                 f"{r.max_wall_minutes} min, ${r.max_cost_usd:.2f} paid spend")
    if m.success_criteria:
        lines.append("<b>Done when:</b> " + e("; ".join(m.success_criteria)))
    body = "\n".join(lines)
    room = limit - len(footer) - 20
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0] + "\n... (truncated)"
    return body + "\n" + footer
