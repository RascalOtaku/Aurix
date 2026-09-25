"""src/foundation/planner.py - turn a goal into a mission PROPOSAL.

    goal -> identify requirements (capabilities.py) -> plan steps -> contract draft

The LLM may PROPOSE steps. CODE decides authority: the risk ceiling, allowed tools,
resource ceilings, prohibited actions and every legal gate come from the domain packs
and this module - never from model output, so a weak or manipulated model cannot widen
what it is allowed to do or forget a legal requirement. Model output is parsed
defensively, length-clamped, and its capability names are checked against the registry
(unknown ones are surfaced and treated as maximum risk).

`llm` is any `async (system, prompt) -> str`; None means "packs only" (deterministic).
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Dict, List, Optional

from src.foundation import capabilities as cap
from src.foundation import mission as ms
from src.foundation.risk import RiskTier

LLM = Callable[[str, str], Awaitable[str]]

MAX_STEPS = 12
MAX_LLM_EXTRA_STEPS = 6
BASE_PROHIBITED = [
    "Never modify protected components (approval gate, audit, contracts, credentials, identity).",
    "Never spend money, send messages to third parties, or publish anything without the owner's approval.",
    "Never read or print credentials or .env files.",
]

PLANNER_SYSTEM = (
    "You are the planning module of AURIX, a personal AI system. Break the user's goal into "
    "concrete, verifiable steps. Reply with ONLY one JSON object, no prose:\n"
    '{"steps":[{"title":"...","description":"...","capabilities":["..."],"success_check":"..."}],'
    '"success_criteria":["..."],"unknowns":["anything you need that is not in the capability list"]}\n'
    "Use at most 10 steps. Prefer capability names from the list you are given."
)


def extract_json(text: str) -> Optional[dict]:
    """First balanced JSON object in `text` (tolerates code fences and chatter)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _clip(s, n: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def parse_llm_plan(text: str) -> Dict:
    """Validated, clamped plan from raw model output. Always returns the three keys."""
    obj = extract_json(text) or {}
    steps: List[Dict] = []
    for raw in (obj.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(raw, dict) or not _clip(raw.get("title"), 1):
            continue
        caps = raw.get("capabilities") or []
        steps.append({
            "title": _clip(raw.get("title"), 120),
            "description": _clip(raw.get("description"), 400),
            "capabilities": [_clip(c, 60) for c in caps if isinstance(c, str) and c.strip()][:8]
            if isinstance(caps, list) else [],
            "success_check": _clip(raw.get("success_check"), 200),
        })
    crit = obj.get("success_criteria") or []
    unknowns = obj.get("unknowns") or []
    return {
        "steps": steps,
        "success_criteria": [_clip(c, 200) for c in crit if isinstance(c, str)][:8] if isinstance(crit, list) else [],
        "unknowns": [_clip(u, 100) for u in unknowns if isinstance(u, str)][:8] if isinstance(unknowns, list) else [],
    }


def _planner_prompt(goal: str, packs: List[cap.DomainPack]) -> str:
    known = ", ".join(sorted(cap.REGISTRY))
    lines = [f"GOAL: {goal}", f"CAPABILITIES YOU MAY USE: {known}"]
    if packs:
        lines.append("A domain expert already outlined these steps; refine or add missing detail, do not repeat them:")
        for p in packs:
            lines += [f"- {s.title}" for s in p.steps]
    return "\n".join(lines)


# What a MODEL may ask for. Credentials, authorizations and services are never added on a model's say-so: they come only from domain
# packs that code matched by regex. (2026-09-20: the local 7B model listed `alpaca-credentials` for "print the first 10 primes"; that
# became a hard requirement and the approved mission was blocked at preflight for an hour.)
_MODEL_MAY_ADD_KINDS = {"builtin", "python_pkg", "binary"}
_EMPTY_ANSWER = {"", "none", "n/a", "na", "null", "nil", "nothing", "no", "no unknowns", "-", "unknown", "tbd", "?"}
_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "with", "in", "on", "it", "is", "that", "this", "then", "up"}


def screen_model_capabilities(names: List[str], **presence_kw) -> List[str]:
    """Names a model proposed, minus anything that would create a requirement out of a guess. A model may only add tools and
    packages that are ALREADY PRESENT (anything else it invents would become an install/credential demand: a hallucinated `alpaca-py`
    for a primes script). Real needs come from code-defined domain packs. Names the registry does not know are kept: they only ever
    show up as 'unknown' in the contract (informational), never as a blocking requirement."""
    out: List[str] = []
    for n in names:
        if str(n).strip().lower() in _EMPTY_ANSWER:               # the model's "None" is an answer, not a capability
            continue
        c = cap.lookup(n)
        if c is None:
            out.append(n)
        elif c.kind in _MODEL_MAY_ADD_KINDS and (c.kind == "builtin" or cap.presence(c, **presence_kw).present):
            out.append(n)
    return out


def _tokens(title: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split() if w not in _STOP}


def restates(title: str, existing_titles: List[str]) -> bool:
    """True if a model-proposed step title just restates a step we already have (one title's words contained in the other's)."""
    a = _tokens(title)
    if not a:
        return True
    for other in existing_titles:
        b = _tokens(other)
        if b and len(a & b) >= 0.8 * min(len(a), len(b)):
            return True
    return False


async def propose_mission(goal: str, llm: Optional[LLM] = None, session_id: Optional[str] = None,
                          store: Optional[ms.MissionStore] = None, sandboxed: bool = False,
                          **presence_kw) -> ms.MissionContract:
    """Build and store (as PROPOSED) a mission contract for `goal`.

    `sandboxed=True`: the mission's bash/python run in the isolated sandbox container, so the
    contract also allows `python` and missing tools mean "add to the sandbox image"."""
    m = await draft_mission(goal, llm=llm, session_id=session_id, sandboxed=sandboxed, **presence_kw)
    return (store or ms.MissionStore()).propose(m)


async def draft_mission(goal: str, llm: Optional[LLM] = None, session_id: Optional[str] = None,
                        sandboxed: bool = False, **presence_kw) -> ms.MissionContract:
    """The contract for `goal`, NOT stored and NOT audited (the eval runner uses this: measuring the planner must not pollute the
    mission store or the audit chain). propose_mission = draft_mission + store."""
    goal = _clip(goal, 1500)
    if not goal:
        raise ValueError("empty goal")

    extra_caps: List[str] = []
    llm_plan = {"steps": [], "success_criteria": [], "unknowns": []}
    _, packs = cap.identify(goal, sandbox=sandboxed, **presence_kw)
    if llm is not None:
        try:
            llm_plan = parse_llm_plan(await llm(PLANNER_SYSTEM, _planner_prompt(goal, packs)))
        except Exception:
            llm_plan = {"steps": [], "success_criteria": [], "unknowns": []}   # packs still stand
    for s in llm_plan["steps"]:
        extra_caps.extend(s["capabilities"])
    extra_caps = screen_model_capabilities(extra_caps, **presence_kw)
    for st_ in llm_plan["steps"]:                       # a step may not carry a capability it was not allowed to add
        st_["capabilities"] = screen_model_capabilities(st_["capabilities"], **presence_kw)

    req, packs = cap.identify(goal, extra_capabilities=extra_caps, sandbox=sandboxed, **presence_kw)

    # steps: pack steps are the backbone; model steps only add detail
    steps: List[ms.Step] = []
    seen = set()

    def add(title, desc, caps, check, executor="agent"):
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if key in seen or len(steps) >= MAX_STEPS:
            return
        seen.add(key)
        steps.append(ms.Step(id=f"s{len(steps) + 1}", title=title, description=desc,
                             capabilities=list(caps), success_check=check, executor=executor))

    if req.installable:
        add("Install missing Python packages",
            "Run exactly: " + "; ".join(req.install_commands[c] for c in req.installable),
            ["bash"], "each package imports")
    for p in packs:
        for s in p.steps:
            note = f" {s.sandbox_note}" if sandboxed and s.sandbox_note else ""
            add(s.title, s.description + note, s.capabilities, s.success_check, s.executor)
    extra_budget = MAX_LLM_EXTRA_STEPS if packs else MAX_STEPS
    for s in llm_plan["steps"][:extra_budget]:
        if restates(s["title"], [x.title for x in steps]):
            continue
        add(s["title"], s["description"], s["capabilities"], s["success_check"])
    if not steps:
        add("Clarify the goal with the owner",
            "The request matched no known domain and no usable plan was produced: ask what 'done' looks like "
            "before doing anything.", [], "owner confirms the goal")

    prohibited = list(BASE_PROHIBITED)
    patterns: List[str] = []
    criteria: List[str] = []
    for p in packs:
        prohibited += [x for x in p.prohibited if x not in prohibited]
        patterns += [x for x in p.prohibited_patterns if x not in patterns]
        criteria += [x for x in p.success_criteria if x not in criteria]
    criteria += [c for c in llm_plan["success_criteria"] if c not in criteria]
    if not criteria:
        criteria = ["the owner confirms the result"]

    resources = ms.Resources(max_steps=max(len(steps) * 4, 12),
                             max_wall_minutes=max([p.max_wall_minutes for p in packs] or [180]))
    allowed = list(ms.DEFAULT_ALLOWED_TOOLS) + (["python"] if sandboxed else [])
    requirements = req.as_dict()
    # The model's own open questions are INFORMATION for the owner, never requirements: a "None" or a question mark used to become an
    # "unknown capability (max risk)" and blocked the fast lane on every mission (found by the eval runner with the live model).
    requirements["notes"] = [u for u in llm_plan["unknowns"] if str(u).strip().lower() not in _EMPTY_ANSWER][:8]
    requirements["skills"] = [s for p in packs for s in p.skills]        # advisory prompt rules (skills.py)
    m = ms.MissionContract(
        id=ms.new_mission_id(), objective=goal, prohibited=prohibited, prohibited_patterns=patterns,
        allowed_tools=allowed, risk_ceiling=RiskTier.MEDIUM, resources=resources,
        success_criteria=criteria[:10], sandboxed=sandboxed, session_id=session_id, steps=steps,
        requirements=requirements,
    )
    return m
