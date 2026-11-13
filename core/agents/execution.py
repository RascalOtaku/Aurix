"""Real, gated execution of agent actions.

Before this module every agent's ``_execute_action`` returned a canned dict (``"executed"`` or
``"not_implemented"``) without running anything, so a caller could believe an action happened
when it had not. Now an action goes through the same executor chat tools use
(``src.tool_execution.execute_tool_block``), inside ``approval_gate.force_gate()``: any
consequential step (shell, file writes, email, settings, ...) is held for the owner's
approve/deny on Telegram, in every session and gate mode, and fails closed. The returned status
reflects what really happened: ``executed``, ``failed``, ``denied`` or ``error``.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tools whose block content is plain text (a command, code, a path); everything else takes JSON.
_TEXT_PARAM = {"bash": "command", "python": "code", "read_file": "path"}


def action_content(action: str, params: Optional[Dict[str, Any]]) -> str:
    """The text body of the tool block for ``action`` with ``params``."""
    params = params or {}
    if "content" in params and isinstance(params["content"], str):
        return params["content"]
    key = _TEXT_PARAM.get(action)
    if key and isinstance(params.get(key), str):
        return params[key]
    return json.dumps(params, ensure_ascii=False)


def _status(description: str, result: Dict[str, Any]) -> str:
    desc = (description or "").upper()
    if desc.endswith(": DENIED") or desc.endswith(": BLOCKED"):
        return "denied"
    if result.get("error") or result.get("exit_code") not in (None, 0):
        return "failed"
    return "executed"


async def run_gated_action(action: str, params: Optional[Dict[str, Any]] = None, *,
                           session_id: Optional[str] = None, owner: Optional[str] = None,
                           executor=None) -> Dict[str, Any]:
    """Run one agent action through the owner approval gate. Never reports success it didn't see."""
    from src import approval_gate
    from src.agent_tools import ToolBlock

    if not action or not isinstance(action, str):
        return {"action": action, "status": "error", "result": {"error": "no action named"}}
    if executor is None:
        from src.tool_execution import execute_tool_block as executor

    block = ToolBlock(action, action_content(action, params))
    try:
        with approval_gate.force_gate():
            description, result = await executor(block, session_id=session_id, owner=owner)
    except Exception as e:  # the executor itself blew up: nothing is known to have run
        logger.error("agent action %s failed before a result: %r", action, e)
        return {"action": action, "status": "error", "result": {"error": str(e)}}
    result = result if isinstance(result, dict) else {"output": result}
    return {"action": action, "status": _status(description, result),
            "description": description, "result": result}
