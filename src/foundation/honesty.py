"""src/foundation/honesty.py - the owner's rule, 2026-09-28: "Never have it lie or half work. Either it works or doesn't."

A model's text is not evidence that anything happened. The only proof that a tool ran is the agent loop's own `tool_output`
event for this turn. check() compares the model's final reply with the tools that REALLY ran and rewrites the reply when they
disagree. Code decides, not the model:

  * nothing came back               -> "No answer", said plainly, never a vague placeholder
  * a tool call written as text     -> "Not done: the model wrote the command instead of running it"
  * "done / reverted / here is the output" with no tool run this turn -> withheld, and the owner is told nothing changed
  * tools did run                   -> a receipt line listing each one, with its exit code, and any that failed or were denied

Pure and stdlib only, so it is unit-testable anywhere. Used by services/telegram/listener.py for every agent reply.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

# A whole reply that is just `name {json}` (optionally fenced) is a tool call the model PRINTED instead of making.
_TEXT_TOOL_CALL = re.compile(r"^\s*(?:```(?:json)?\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*(\{[\s\S]*\})\s*(?:```)?\s*$")
# Phrases that assert something was done or produced.
_CLAIM = re.compile(
    r"\b(?:has|have|had)\s+been\s+(?:reverted|undone|deleted|removed|moved|copied|created|made|executed|run|updated|changed|saved|"
    r"sent|installed|downloaded|pulled|restarted|deployed|fixed|applied|completed)\b"
    r"|\bI(?:'ve|\s+have)?\s+(?:ran|run|executed|deleted|removed|moved|copied|created|updated|changed|saved|sent|installed|downloaded|"
    r"pulled|restarted|deployed|reverted|undid|fixed|applied)\b"
    r"|\bhere(?:'s|\s+is|\s+are)\s+the\s+(?:output|result|results)\b"
    r"|\bwas\s+(?:executed|run|deleted|created|moved|installed|reverted)\b"
    r"|\bSTEP\s+DONE\b|\bcommand\s+(?:ran|completed|succeeded)\b",
    re.I,
)
_EMPTY_PLACEHOLDERS = ("(agent ran but returned no clear final answer)",)


def _receipt(tools: List[Dict]) -> str:
    parts = []
    for t in tools:
        name = str(t.get("tool") or "tool")
        out = str(t.get("output") or "")
        code = t.get("exit_code")
        if out.lstrip().startswith("Denied"):
            parts.append(f"{name} (denied)")
        elif code not in (None, 0):
            parts.append(f"{name} (FAILED, exit {code})")
        else:
            parts.append(f"{name} (ok)" if code == 0 else name)
    return "🧾 Ran: " + ", ".join(parts)


def check(text: str, tools: List[Dict]) -> str:
    """The reply the owner should see, given the model's text and the tool_output events of this turn."""
    t = (text or "").strip()
    tools = [x for x in (tools or []) if isinstance(x, dict)]
    ran = {str(x.get("tool") or "") for x in tools}
    receipt = ("\n\n" + _receipt(tools)) if tools else ""

    if not t or t in _EMPTY_PLACEHOLDERS:
        return "❌ No answer: the model returned nothing (it failed or ran out of time). Nothing else was done." + receipt

    m = _TEXT_TOOL_CALL.match(t)
    if m:
        try:
            json.loads(m.group(2))
            is_call = True
        except ValueError:
            is_call = False
        if is_call and m.group(1) not in ran:
            return (f"❌ Not done: the model wrote a `{m.group(1)}` command as text instead of running it. "
                    f"Nothing was executed and nothing changed." + receipt)

    if not tools and _CLAIM.search(t):
        return ("❌ Not done: the model's reply said it had done something or showed output, but no tool ran for this message, "
                "so that did not happen. Nothing changed. (Its reply was withheld.)")

    return t + receipt
