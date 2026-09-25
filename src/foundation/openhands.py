"""src/foundation/openhands.py - OpenHands as a mission step EXECUTOR (host side).

OpenHands is a coding agent (plan, edit files, run commands, iterate). AURIX runs its Python
SDK INSIDE the isolated sandbox container - never in the app container - via
mission_sandbox/tools/openhands_run.py. A mission step with `executor="openhands"` hands the
step's task to it; the runner then reads back one structured result line.

This module holds the pure/host-side parts: build the task text, launch command, parse and
interpret the result, and the readiness check. It carries no authority: the step still runs
under the mission's contract, in the mission's sandbox, with the mission's prohibitions, and
STOP kills it (sandbox.kill).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.foundation import mission as ms
from src.foundation import sandbox

RESULT_MARK = "OPENHANDS_RESULT:"
TOOL_PATH = "/opt/tools/openhands_run.py"
DEFAULT_MAX_ITERATIONS = 40
STEP_TIMEOUT_SECONDS = 3300           # under the sandbox server's 3600 s ceiling


def build_task_text(m: ms.MissionContract, step: ms.Step, skills_text: str = "") -> str:
    ws = m.workspace.replace("\\", "/")
    prior = [f"- {s.title}: {s.evidence}" for s in m.steps if s.status == "done" and s.evidence]
    lines = [
        f"# Task: {step.title}", "",
        f"Mission {m.id}. Overall objective: {m.objective}", "",
        "## What to do", step.description or step.title, "",
    ]
    if step.success_check:
        lines += ["## Done when", step.success_check, ""]
    if prior:
        lines += ["## Already done in earlier steps", *prior, ""]
    lines += [
        "## Workspace and limits",
        f"Work ONLY inside {ws}. Do not read or change anything outside it. There is no internet.",
        "Hard limits (never do these): " + " | ".join(m.prohibited[:8]) if m.prohibited else "",
        "",
    ]
    if skills_text:
        lines += [skills_text, ""]
    lines += ["## Finish",
              "Run the checks yourself before you stop. When the work is complete, stop and make your LAST "
              "message a short summary of what changed and what you verified. If you cannot finish, say exactly "
              "what is missing."]
    return "\n".join(l for l in lines if l is not None)


def write_task(m: ms.MissionContract, step: ms.Step, skills_text: str = "") -> str:
    """Write the task file into the mission workspace; returns its (container) path."""
    path = Path(m.workspace) / ".openhands" / f"task-{step.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_task_text(m, step, skills_text), encoding="utf-8")
    return str(path).replace("\\", "/")


def command(task_path: str, max_iterations: int = DEFAULT_MAX_ITERATIONS) -> str:
    return f"python3 {TOOL_PATH} --task-file '{task_path}' --max-iters {int(max_iterations)}"


def parse_result(output: str) -> Dict:
    """The last RESULT line the tool printed, else an error dict carrying the output tail."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith(RESULT_MARK):
            try:
                data = json.loads(line[len(RESULT_MARK):])
                if isinstance(data, dict):
                    return data
            except ValueError:
                break
    return {"status": "error", "summary": (output or "").strip()[-300:] or "no output from the coding agent"}


_BLOCK_REASONS = {
    "sdk_unavailable": "the OpenHands SDK is not installed in the sandbox image "
                       "(see mission_sandbox/requirements-openhands.txt, then docker compose build sandbox)",
    "llm_unreachable": "the model server is unreachable from the sandbox (check llm-gateway and LLM_UPSTREAM)",
    "stuck": "the coding agent got stuck in a loop",
    "max_iterations": "the coding agent hit its iteration limit before finishing",
    "error": "the coding agent reported an error",
}


def interpret(result: Dict, exit_code: Optional[int] = 0) -> Dict[str, str]:
    """{'state': 'done'|'blocked', 'text': ...} in the runner's step-result shape."""
    status = str(result.get("status", "error")).lower()
    summary = " ".join(str(result.get("summary", "")).split())[:300]
    if status == "finished" and exit_code in (0, None):
        return {"state": "done", "text": summary or "coding agent finished"}
    reason = _BLOCK_REASONS.get(status, f"the coding agent ended with status '{status}'")
    return {"state": "blocked", "text": f"{reason}. {summary}".strip()[:300]}


def ready(m: ms.MissionContract) -> Tuple[bool, str]:
    """Can this mission's OpenHands step run? Needs a sandboxed mission, a live sandbox, the SDK."""
    if not m.sandboxed:
        return False, "the mission is not sandboxed (OpenHands only runs inside the sandbox)"
    if not sandbox.available():
        return False, "the sandbox is not reachable"
    try:
        found = sandbox.probe([], ["openhands.sdk"])
    except sandbox.SandboxUnavailable as e:
        return False, str(e)
    if not found.get("modules", {}).get("openhands.sdk"):
        return False, _BLOCK_REASONS["sdk_unavailable"]
    return True, ""


def fallback_enabled() -> bool:
    return os.environ.get("AURIX_OPENHANDS_FALLBACK", "1").strip() != "0"
