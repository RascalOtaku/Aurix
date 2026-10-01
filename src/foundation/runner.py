"""src/foundation/runner.py - executes an APPROVED mission step by step (Claude-Code style).

For each step it briefs the agent loop (`run_agent`) with the objective, the step, the
workspace, the rules and the exact way to report ("STEP DONE: <evidence>" or
"STEP BLOCKED: <reason>"), checks the reply, records progress, and moves on. It never
improvises around a refusal: if an action is denied or a step cannot finish, the mission
BLOCKS and tells the owner exactly why.

Authority is not the runner's business - every tool call the agent makes still passes the
gate (risk model + contract). The runner only enforces the contract's own ceilings
(steps, model calls, wall clock, deadline), verifies the contract's integrity hash before
every step, and stops the moment the mission is no longer ACTIVE.

Protected component (approval_gate).
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Dict, List, Optional

from src.foundation import audit
from src.foundation import capabilities as cap
from src.foundation import forge
from src.foundation import mission as ms
from src.foundation import openclaw as ocw
from src.foundation import openhands as oh
from src.foundation import sandbox
from src.foundation import skills as skills_mod
from src.foundation import teacher

RunAgent = Callable[[str], Awaitable[str]]
Notify = Callable[[str], Awaitable[None]]

MAX_ATTEMPTS_PER_STEP = 2
_DONE_RE = re.compile(r"STEP DONE\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_BLOCKED_RE = re.compile(r"STEP BLOCKED\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_COMPLETE_RE = re.compile(r"MISSION COMPLETE\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_INCOMPLETE_RE = re.compile(r"MISSION INCOMPLETE\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)


_CODE_CAPS = {"write_file", "bash", "openhands"}


def skills_for_step(m: ms.MissionContract, step: ms.Step) -> str:
    """Advisory rule sets (e.g. Ponytail) for steps that write or run code; '' otherwise."""
    if not (_CODE_CAPS & set(step.capabilities) or step.executor != "agent"):
        return ""
    names = (m.requirements or {}).get("skills") or []
    parts = [skills_mod.render(names)] if names else []
    try:                                                    # owner-approved, integrity-checked lessons from the teacher (advisory text)
        parts.append(teacher.relevant_text(f"{m.objective} {step.title} {step.description}"))
    except Exception:
        pass
    return "\n\n".join(p for p in parts if p)


def build_step_prompt(m: ms.MissionContract, index: int, extra: str = "") -> str:
    step = m.steps[index]
    caps = ", ".join(step.capabilities) or "none specific"
    ws = m.workspace.replace("\\", "/")
    lines = [
        f"[MISSION {m.id}] Objective: {m.objective}",
        f"Workspace: {ws}  (use ABSOLUTE paths under it for everything you create or edit)",
        f"Step {index + 1} of {len(m.steps)}: {step.title}",
        step.description,
        f"Capabilities for this step: {caps}",
    ]
    if step.success_check:
        lines.append(f"This step is done when: {step.success_check}")
    skill_text = skills_for_step(m, step)
    if skill_text:
        lines.append(skill_text)
    if extra:
        lines.append(extra)
    lines += [
        f"Tools you may use: {', '.join(m.allowed_tools)}. Do not do anything prohibited: "
        + " | ".join(m.prohibited[:6]),
        "For this mission step you may run as many commands as it needs (each is checked separately). "
        "If an action is denied, or you need something you do not have, do NOT retry or work around it: "
        "report BLOCKED. Do not ask 'should I proceed?' - just act.",
        "Finish with exactly one final line: `STEP DONE: <one-line evidence>` or `STEP BLOCKED: <reason>`.",
    ]
    return "\n".join(l for l in lines if l)


def parse_step_reply(text: str) -> Dict[str, str]:
    """{'state': 'done'|'blocked'|'unclear', 'text': ...} from the agent's reply."""
    text = text or ""
    blocked = _BLOCKED_RE.search(text)
    done = _DONE_RE.search(text)
    # the LAST marker wins if the agent mentions both
    if blocked and (not done or blocked.start() > done.start()):
        return {"state": "blocked", "text": blocked.group(1).strip().splitlines()[0][:300]}
    if done:
        return {"state": "done", "text": done.group(1).strip().splitlines()[0][:300]}
    return {"state": "unclear", "text": text.strip()[-300:]}


def _looks_repeated(prev: str, current: str) -> bool:
    """True when two consecutive step replies are substantially the same text - the agent repeating an unfinished
    proposal instead of making progress. Deliberately crude (normalized-text containment, not a diff/similarity
    library): it only needs to catch the obvious case, a false negative just falls through to the generic message."""
    def norm(s: str) -> str:
        return " ".join((s or "").split()).lower()
    a, b = norm(prev), norm(current)
    if len(a) < 40 or len(b) < 40:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter[:len(shorter) * 3 // 4] in longer


class MissionRunner:
    def __init__(self, mission_id: str, run_agent: RunAgent, notify: Notify,
                 store: Optional[ms.MissionStore] = None, **presence_kw):
        self.mission_id = mission_id
        self.run_agent = run_agent
        self.notify = notify
        self.store = store or ms.MissionStore()
        self.presence_kw = presence_kw
        self._announced: set = set()          # missions whose skill provisioning is already in the audit log

    def _load(self) -> Optional[ms.MissionContract]:
        m = self.store.load(self.mission_id)
        return m if m and m.status == ms.MissionStatus.ACTIVE else None

    async def _say(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:
            pass

    def preflight(self, m: ms.MissionContract) -> List[str]:
        """Things only the owner can provide that are still missing (authorizations, credentials)."""
        req = cap.resolve(m.requirements.get("capabilities", []), sandbox=m.sandboxed, **self.presence_kw)
        return req.manual

    async def run(self) -> str:
        """Returns: completed | blocked | stopped | failed | expired | idle."""
        m = self._load()
        if m is None:
            return "idle"
        try:
            if not m.integrity_ok():
                self.store.finish(m, ms.MissionStatus.FAILED, "contract failed its integrity check")
                await self._say(f"⛔ Mission [{m.id}] halted: its contract failed the integrity check.")
                return "failed"

            missing = self.preflight(m)
            if missing:
                audit.append("mission_blocked", mission=m.id, reason="preflight", missing=missing)
                await self._say(
                    f"⏸ Mission [{m.id}] cannot start yet. Only you can provide:\n- " + "\n- ".join(missing[:8])
                    + "\nFor authorizations reply `authorize <name>`; then `resume`.")
                return "blocked"

            audit.append("mission_run_started", mission=m.id, from_step=m.current_step)
            await self._say(f"▶ Mission [{m.id}] running: {len(m.steps) - m.current_step} step(s).")

            for index in range(m.current_step, len(m.steps)):
                m = self._load()
                if m is None:
                    return "stopped"
                over = ms.limits_reason(m)
                if over or index >= m.resources.max_steps:
                    reason = over or f"step ceiling reached ({m.resources.max_steps})"
                    self.store.finish(m, ms.MissionStatus.EXPIRED, reason)
                    await self._say(f"⏱ Mission [{m.id}] stopped: {reason}. Progress is saved.")
                    return "expired"
                if not m.integrity_ok():
                    self.store.finish(m, ms.MissionStatus.FAILED, "integrity check failed mid-run")
                    await self._say(f"⛔ Mission [{m.id}] halted: contract integrity check failed.")
                    return "failed"

                step = m.steps[index]
                step.status = "running"
                self.store.save(m)
                outcome = await self._run_step(m, index)
                m = self._load()
                if m is None:                                   # stopped while the agent was working
                    return "stopped"
                step = m.steps[index]
                if outcome["state"] != "done":
                    step.status, step.evidence = "blocked", outcome["text"]
                    m.current_step = index
                    self.store.save(m)
                    audit.append("mission_blocked", mission=m.id, step=step.id, reason=outcome["text"])
                    await self._say(f"⏸ Mission [{m.id}] BLOCKED at step {index + 1}/{len(m.steps)} "
                                    f"({step.title}): {outcome['text']}\nFix it, then reply `resume` "
                                    f"(or `stop`).")
                    return "blocked"
                step.status, step.evidence = "done", outcome["text"]
                m.current_step = index + 1
                self.store.save(m)
                audit.append("mission_step_done", mission=m.id, step=step.id, evidence=outcome["text"][:200])
                await self._say(f"✅ [{m.id}] step {index + 1}/{len(m.steps)}: {step.title}")

            return await self._finish(m)
        except asyncio.CancelledError:
            current = self.store.load(self.mission_id)
            if current and current.status == ms.MissionStatus.ACTIVE:
                self.store.finish(current, ms.MissionStatus.STOPPED, "cancelled")
            raise

    async def _run_openhands(self, m: ms.MissionContract, index: int) -> Optional[Dict[str, str]]:
        """Run a step with the OpenHands coding agent INSIDE the sandbox. Returns the step
        result, or None to fall back to the built-in agent (OpenHands not available)."""
        step = m.steps[index]
        ok, why = await asyncio.to_thread(oh.ready, m)
        if not ok:
            audit.append("openhands_unavailable", mission=m.id, step=step.id, reason=why)
            if oh.fallback_enabled():
                await self._say(f"ℹ [{m.id}] step {index + 1}: OpenHands is unavailable ({why}); "
                                "using the built-in agent for this step.")
                return None
            return {"state": "blocked", "text": f"OpenHands unavailable: {why}"[:300]}
        live = self._load()
        if live is None:
            return {"state": "blocked", "text": "mission is no longer active"}
        task_path = oh.write_task(m, step, skills_for_step(m, step))
        self.store.record_tool_call(live)
        self.store.record_model_call(live)
        audit.append("openhands_started", mission=m.id, step=step.id)
        await self._say(f"\U0001F6E0 [{m.id}] step {index + 1}: handing '{step.title}' to the OpenHands coding agent "
                        "(sandboxed). `stop` halts it.")
        try:
            res = await asyncio.to_thread(sandbox.run, m.id, "bash", oh.command(task_path), oh.STEP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await asyncio.to_thread(sandbox.kill, m.id)                # STOP: terminate it, don't just stop waiting
            raise
        except sandbox.SandboxUnavailable as e:
            return {"state": "blocked", "text": f"sandbox unavailable: {e}"[:300]}
        outcome = oh.interpret(oh.parse_result(res.get("output") or res.get("stdout") or res.get("error", "")),
                               res.get("exit_code"))
        audit.append("openhands_finished", mission=m.id, step=step.id, state=outcome["state"],
                     exit_code=res.get("exit_code"))
        return outcome

    async def _run_openclaw(self, m: ms.MissionContract, index: int) -> Optional[Dict[str, str]]:
        """Run a step through OpenClaw's workboard INSIDE the sandbox. Returns the step result, or
        None to fall back to the built-in agent (OpenClaw not available). Same contract as
        _run_openhands - AURIX's mission contract stays the authority either way, OpenClaw is only
        ever handed one task and asked for one result (see src/foundation/openclaw.py)."""
        step = m.steps[index]
        ok, why = await asyncio.to_thread(ocw.ready, m)
        if not ok:
            audit.append("openclaw_unavailable", mission=m.id, step=step.id, reason=why)
            if ocw.fallback_enabled():
                await self._say(f"ℹ [{m.id}] step {index + 1}: OpenClaw is unavailable ({why}); "
                                "using the built-in agent for this step.")
                return None
            return {"state": "blocked", "text": f"OpenClaw unavailable: {why}"[:300]}
        live = self._load()
        if live is None:
            return {"state": "blocked", "text": "mission is no longer active"}
        task_path = ocw.write_task(m, step, skills_for_step(m, step))
        self.store.record_tool_call(live)
        self.store.record_model_call(live)
        audit.append("openclaw_started", mission=m.id, step=step.id)
        await self._say(f"\U0001F99E [{m.id}] step {index + 1}: handing '{step.title}' to OpenClaw "
                        "(sandboxed). `stop` halts it.")
        try:
            res = await asyncio.to_thread(sandbox.run, m.id, "bash", ocw.command(task_path), ocw.STEP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await asyncio.to_thread(sandbox.kill, m.id)                # STOP: terminate it, don't just stop waiting
            raise
        except sandbox.SandboxUnavailable as e:
            return {"state": "blocked", "text": f"sandbox unavailable: {e}"[:300]}
        outcome = ocw.interpret(ocw.parse_result(res.get("output") or res.get("stdout") or res.get("error", "")),
                                res.get("exit_code"))
        audit.append("openclaw_finished", mission=m.id, step=step.id, state=outcome["state"],
                     exit_code=res.get("exit_code"))
        return outcome

    async def _forged_skills_note(self, m: ms.MissionContract) -> str:
        """Prompt text for the owner-approved skills a SANDBOXED mission may call ('' if none / not applicable).

        Skills are copied into the workspace (hash re-verified) and run as ordinary sandboxed python, so the contract and the
        gate govern them like any other code; this adds no authority. A convenience: any failure just means no skills."""
        if not (m.sandboxed and "python" in m.allowed_tools):
            return ""
        try:
            found = await asyncio.to_thread(forge.provision, m.workspace)
        except Exception:
            return ""
        if found and m.id not in self._announced:
            self._announced.add(m.id)
            audit.append("skills_provisioned", mission=m.id, names=[s["name"] for s in found])
        return forge.catalog_text(found, m.workspace)

    async def _run_step(self, m: ms.MissionContract, index: int) -> Dict[str, str]:
        if m.steps[index].executor == "openhands":
            outcome = await self._run_openhands(m, index)
            if outcome is not None:
                return outcome
        elif m.steps[index].executor == "openclaw":
            outcome = await self._run_openclaw(m, index)
            if outcome is not None:
                return outcome
        prompt = build_step_prompt(m, index, await self._forged_skills_note(m))
        last = {"state": "unclear", "text": "no reply"}
        prev_reply = ""
        for attempt in range(MAX_ATTEMPTS_PER_STEP):
            live = self._load()
            if live is None:
                return {"state": "blocked", "text": "mission is no longer active"}
            self.store.record_model_call(live)
            try:
                reply = await self.run_agent(prompt if attempt == 0 else
                                             prompt + "\n\nYour last reply did not end with STEP DONE or "
                                                      "STEP BLOCKED. Continue and end with one of them.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"state": "blocked", "text": f"agent loop error: {e!r}"[:300]}
            last = parse_step_reply(reply)
            audit.append("mission_step_reply", mission=m.id, step=m.steps[index].id,
                         attempt=attempt + 1, state=last["state"])
            if last["state"] in ("done", "blocked"):
                return last
            if attempt > 0 and _looks_repeated(prev_reply, reply):
                # Found live 2026-09-24 (the Reynolds Gang mission): the model can narrate the same unfinished
                # tool-call proposal turn after turn ("Let's proceed with this step...") without ever actually
                # completing it or admitting it is stuck - a stall, not a one-off unclear reply. Say so plainly
                # rather than the generic "no clear result", which reads like a formatting slip, not a real stall.
                return {"state": "blocked", "text": "the agent repeated the same unfinished step without completing "
                                                    f"it: {last['text'][:200]}"}
            prev_reply = reply
        return {"state": "blocked", "text": f"no clear result after {MAX_ATTEMPTS_PER_STEP} attempts: "
                                            f"{last['text'][:200]}"}

    async def _finish(self, m: ms.MissionContract) -> str:
        m = self._load()
        if m is None:
            return "stopped"
        criteria = "; ".join(m.success_criteria) or "the objective is met"
        prompt = (f"[MISSION {m.id}] All steps are reported done. Verify the result against the success "
                  f"criteria with concrete evidence (look at the files in {m.workspace.replace(chr(92), '/')}): "
                  f"{criteria}. Reply with one final line: `MISSION COMPLETE: <summary>` or "
                  f"`MISSION INCOMPLETE: <what is missing>`.")
        self.store.record_model_call(m)
        try:
            reply = await self.run_agent(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reply = f"MISSION INCOMPLETE: agent loop error {e!r}"
        m = self._load()
        if m is None:
            return "stopped"
        ok, bad = _COMPLETE_RE.search(reply or ""), _INCOMPLETE_RE.search(reply or "")
        if ok and not (bad and bad.start() > ok.start()):
            summary = ok.group(1).strip().splitlines()[0][:400]
            self.store.finish(m, ms.MissionStatus.COMPLETED, summary)
            await self._say(f"\U0001F3C1 Mission [{m.id}] COMPLETE: {summary}\nFiles: {m.workspace}\n"
                            "Say `files` to list what it made, `send files` to have it sent here.")
            return "completed"
        why = (bad.group(1).strip().splitlines()[0][:300] if bad else "no clear verification")
        audit.append("mission_blocked", mission=m.id, reason="verification", detail=why)
        await self._say(f"⏸ Mission [{m.id}] is not verified complete: {why}\nReply `resume` to retry "
                        f"verification or `stop`.")
        m.current_step = len(m.steps)
        self.store.save(m)
        return "blocked"
