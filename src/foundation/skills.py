"""src/foundation/skills.py - loadable prompt "skills" (rule sets) for coding steps.

A skill is a short block of rules appended to a step's prompt. Ponytail ("lazy senior dev":
write only what the task needs, never cut validation/error handling/security) lives in the
absorbed repo at ponytail/.agents/rules/ponytail.md; it is read from disk when present and
falls back to an embedded summary, so a missing checkout never breaks a mission.

Skills are ADVISORY prompt text. They carry no authority: risk, contracts and the gate are
unaffected, and a skill can never widen what a mission may do.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

MAX_SKILL_CHARS = 3500

_PONYTAIL_FALLBACK = """\
Ponytail, lazy senior dev mode. Lazy means efficient, not careless: the best code is code never written.
Before writing code, stop at the first rung that holds:
1. Does this need to be built at all? 2. Does it already exist in this codebase? Reuse it.
3. Does the standard library do it? 4. Does a native platform feature cover it?
5. Does an already-installed dependency solve it? 6. Can it be one line? 7. Only then write the minimum.
Understand the problem and trace the real flow first. Bug fix = root cause: grep every caller and fix the shared function once.
No abstractions or dependencies nobody asked for. Deletion over addition; boring over clever.
Never skimp on: input validation at trust boundaries, error handling that prevents data loss, security,
accessibility, anything explicitly requested. Non-trivial logic leaves ONE runnable check behind."""

SKILLS: Dict[str, dict] = {
    "ponytail": {
        "title": "Ponytail (lazy senior dev)",
        "paths": ("ponytail/.agents/rules/ponytail.md", "plugins/ponytail/.agents/rules/ponytail.md"),
        "fallback": _PONYTAIL_FALLBACK,
    },
}


def _roots() -> List[Path]:
    roots = [Path(__file__).resolve().parents[2]]                       # the app directory
    for env in ("AURIX_PROJECT_ROOT", "VAULT_PATH"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    roots.append(Path.cwd())
    return roots


def load(name: str, roots: Optional[Iterable[Path]] = None) -> str:
    """Skill text (from the checkout if present, else the embedded fallback); '' if unknown."""
    spec = SKILLS.get((name or "").strip().lower())
    if spec is None:
        return ""
    for root in (roots if roots is not None else _roots()):
        for rel in spec["paths"]:
            try:
                text = (Path(root) / rel).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text[:MAX_SKILL_CHARS]
    return spec["fallback"]


def render(names: Iterable[str], roots: Optional[Iterable[Path]] = None) -> str:
    """Prompt block for several skills, or '' if none apply."""
    blocks = []
    for n in names or ():
        text = load(n, roots)
        if text:
            blocks.append(f"### Coding rules: {SKILLS[n.strip().lower()]['title']}\n{text}")
    return "\n\n".join(blocks)
