"""src/foundation/openclaw.py - OpenClaw as a mission step EXECUTOR (host side), exactly parallel
to openhands.py.

OpenClaw (github.com/openclaw/openclaw) is a full personal-assistant agent runtime with its own
Gateway daemon, model routing, and a SQLite-backed "workboard" task queue with a `--json` CLI
(`openclaw workboard create/dispatch/show/move`). AURIX does NOT become OpenClaw and does not let
OpenClaw grow its own authority: this module is the substrate, not the brain. A mission step with
`executor="openclaw"` hands ONE task to it and reads back ONE structured result, the same contract
`_run_openhands` already uses - the mission's contract, sandbox, prohibitions, and STOP still govern
it, and it carries zero authority of its own (see runner.py::_run_openclaw).

OpenClaw's Gateway process itself is not sandboxed by OpenClaw's own design ("the Gateway process
always stays on the host; only tool execution moves into the sandbox when enabled" - its docs).
AURIX closes that gap the same way it already does for OpenHands: the ENTIRE Gateway runs INSIDE
mission_sandbox (never the app container), reachable only through llm_proxy.py's allowlisted
inference endpoints (POST /api/chat, GET /api/tags already pass through unchanged - no proxy change
needed), on the sandbox's no-internet, no-LAN internal network.

Researched, not guessed (2026-09-25, docs.openclaw.ai + github.com/openclaw/openclaw):
  - `openclaw gateway --allow-unconfigured --port 18789 --ambient-channels` starts the Gateway
    headless, with zero messaging channels configured.
  - The Ollama provider MUST use the native API (no `/v1`) or tool-calling breaks - `baseUrl` bare,
    `api: "ollama"`.
  - `openclaw workboard create "<text>" --json` -> a card id; `dispatch --json` starts workers;
    `show <id> --json` returns status + result; statuses run triage->...->ready->running->review/
    blocked->done.

UPDATE 2026-09-30, live-tested against a real rebuilt sandbox: four real bugs found and fixed
(see mission_sandbox/tools/openclaw_run.py's own docstring for the full list - bind mode, a
disabled-by-default plugin, response nesting, and the create->ready->dispatch ordering). One
issue remains OPEN: `workboard dispatch` refuses every card with "target tool policy blocks
required tool workboard_heartbeat" and no documented fix was found. `executor="openclaw"` will
currently return `{"state": "blocked", "text": "OpenClaw reported an error. ..."}` for every real
task until that is resolved - same honest-gap treatment as the still-unpatched OpenHands
cache_creation_tokens bug. The mission runner's default fallback to the built-in agent means
nothing depends on this working yet; no domain pack assigns this executor in production.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.foundation import mission as ms
from src.foundation import sandbox

RESULT_MARK = "OPENCLAW_RESULT:"
TOOL_PATH = "/opt/tools/openclaw_run.py"
STEP_TIMEOUT_SECONDS = 3300            # same ceiling as OpenHands - under the sandbox server's 3600s cap


def build_task_text(m: ms.MissionContract, step: ms.Step, skills_text: str = "") -> str:
    """Same shape as openhands.build_task_text - one plain-language task, not a jailbreak of the
    mission's own limits. Kept as its own function (rather than importing openhands's) so the two
    executors can diverge later without coupling them."""
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
              "When the work is complete, your LAST message must be a short summary of what changed "
              "and what you verified. If you cannot finish, say exactly what is missing."]
    return "\n".join(l for l in lines if l is not None)


def write_task(m: ms.MissionContract, step: ms.Step, skills_text: str = "") -> str:
    """Write the task file into the mission workspace; returns its (container) path."""
    path = Path(m.workspace) / ".openclaw" / f"task-{step.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_task_text(m, step, skills_text), encoding="utf-8")
    return str(path).replace("\\", "/")


def command(task_path: str, timeout: int = STEP_TIMEOUT_SECONDS) -> str:
    return f"python3 {TOOL_PATH} --task-file '{task_path}' --timeout-seconds {int(timeout)}"


def parse_result(output: str) -> Dict:
    """The last RESULT line the tool printed, else an error dict carrying the output tail - same
    convention as openhands.parse_result so runner.py needs no extra branching to read it."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith(RESULT_MARK):
            try:
                data = json.loads(line[len(RESULT_MARK):])
                if isinstance(data, dict):
                    return data
            except ValueError:
                break
    return {"status": "error", "summary": (output or "").strip()[-300:] or "no output from OpenClaw"}


_BLOCK_REASONS = {
    "gateway_unreachable": "the OpenClaw Gateway did not come up in time inside the sandbox",
    "not_installed": "OpenClaw is not installed in the sandbox image "
                     "(see mission_sandbox/Dockerfile's optional OpenClaw layer, then docker compose build sandbox)",
    "timeout": "the workboard card never left 'running' before the step's timeout",
    "blocked": "the OpenClaw worker itself reported the card blocked",
    "error": "OpenClaw reported an error",
}


def interpret(result: Dict, exit_code: Optional[int] = 0) -> Dict[str, str]:
    """{'state': 'done'|'blocked', 'text': ...} in the runner's step-result shape - identical
    contract to openhands.interpret."""
    status = str(result.get("status", "error")).lower()
    summary = " ".join(str(result.get("summary", "")).split())[:300]
    if status == "done" and exit_code in (0, None):
        return {"state": "done", "text": summary or "OpenClaw finished the card"}
    reason = _BLOCK_REASONS.get(status, f"OpenClaw ended with status '{status}'")
    return {"state": "blocked", "text": f"{reason}. {summary}".strip()[:300]}


def ready(m: ms.MissionContract) -> Tuple[bool, str]:
    """Can this mission's OpenClaw step run? Needs a sandboxed mission, a live sandbox, the binary."""
    if not m.sandboxed:
        return False, "the mission is not sandboxed (OpenClaw only runs inside the sandbox)"
    if not sandbox.available():
        return False, "the sandbox is not reachable"
    try:
        found = sandbox.probe(["openclaw"], [])
    except sandbox.SandboxUnavailable as e:
        return False, str(e)
    if not found.get("bins", {}).get("openclaw"):
        return False, _BLOCK_REASONS["not_installed"]
    return True, ""


def fallback_enabled() -> bool:
    return os.environ.get("AURIX_OPENCLAW_FALLBACK", "1").strip() != "0"
