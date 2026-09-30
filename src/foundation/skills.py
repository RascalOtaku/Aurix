"""src/foundation/skills.py - loadable prompt "skills" (rule sets) for coding steps.

Built in: ponytail (coding), archify (diagrams, used by the `diagrams` domain pack), adhd (answer-first
output style). Each reads the upstream file from an absorbed checkout when present, else an embedded summary.

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

# Archify (github.com/tt-a1i/archify, MIT): diagrams as a typed model first, then a verified render.
_ARCHIFY_FALLBACK = """\
Archify, diagrams you can trust. Pick the type that fits the question: architecture (components and who talks
to whom), workflow (steps, decisions, owners), sequence (calls over time), data flow (sources, transforms, sinks)
or lifecycle (states and transitions).
1. Write the model first: a JSON list of nodes (id, label, kind) and edges (from, to, label). Only facts you read
   in the code or notes; mark guesses as such.
2. Render ONE self-contained HTML file with inline SVG: no CDN, no external fonts, readable in light and dark.
3. Verify before delivering: every node and edge in the model is drawn, no label overlaps a line or another label,
   arrows point the right way, nothing is clipped. Fix and re-check instead of describing the problem.
4. Keep the model next to the diagram so the next edit starts from it, not from the picture."""

# i-have-adhd (github.com/ayghri/i-have-adhd, MIT): answer-first output for the owner.
_ADHD_FALLBACK = """\
Answer-first output. Open with the answer or the next concrete action, not background.
Multi-step instructions are a numbered list, one action per line. Keep lists short; cut tangents,
caveats that don't change the action, and closing pleasantries.
End with exactly one specific next step or question, if one is needed."""

SKILLS: Dict[str, dict] = {
    "ponytail": {
        "title": "Ponytail (lazy senior dev)",
        "paths": ("ponytail/.agents/rules/ponytail.md", "plugins/ponytail/.agents/rules/ponytail.md"),
        "fallback": _PONYTAIL_FALLBACK,
    },
    "archify": {
        "title": "Archify (verified diagrams)",
        "paths": ("archify/archify/SKILL.md", "plugins/archify/archify/SKILL.md"),
        "fallback": _ARCHIFY_FALLBACK,
    },
    "adhd": {
        "title": "Answer-first output (i-have-adhd)",
        "paths": ("i-have-adhd/skills/i-have-adhd/SKILL.md", "plugins/i-have-adhd/skills/i-have-adhd/SKILL.md"),
        "fallback": _ADHD_FALLBACK,
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
