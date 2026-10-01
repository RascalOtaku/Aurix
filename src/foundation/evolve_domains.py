"""src/foundation/evolve_domains.py - the first domain plugged into evolve.py's generic engine: evolving the guidance text
appended to forge.FORGE_SYSTEM (the self-improve-prompts track the owner asked for first). Importing this module registers
it; nothing else needs to call anything here directly except commands.py (to trigger the import) and forge.py (to read the
live-applied guidance - see live_guidance() below, mirrored into forge.propose() the same way teacher.relevant_text() is).

The genome is plain text, not a vector, so mutation/crossover are LLM-driven (EvoPrompt/Promptbreeder-style: ask a model to
vary or combine guidance text) rather than numeric operators - see tasks/strategy_evolve.py for the numeric-genome sibling
of this same engine (the Alpaca paper-trading strategy), which mutates with jitter instead because ITS genome is numbers.

Fitness is never self-reported: evaluate() runs the exact same pipeline forge.propose() uses (evals.run_code_tier), through
the real sandbox, against HIDDEN test cases the model never sees, and returns the pass fraction. A genome that merely reads
well is worth nothing here; only one that measurably ships more working code is.

Only upgrades.ENGINEER_SYSTEM was considered as a second track in this same pass and deliberately left out: unlike forge's
code tier, there is no fixed hidden-task eval harness for upgrade proposals (they're drafted against the owner's own,
ever-changing backlog) - evolving it honestly would mean building that eval harness first, which is its own real project,
not something to fake with this one's threshold.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

from src.foundation import evals, evolve, forge

LLM = evolve.LLM
_SEEDS = (
    "",
    "Double-check indentation and that every function you reference actually exists before replying.",
    "Prefer the simplest correct implementation; avoid extra parameters or files the task did not ask for.",
    "Re-read the task's exact wording for edge cases (empty input, zero, negative numbers) before writing the test.",
)

MUTATE_SYSTEM = (
    "You improve short guidance text that gets appended to a code-generation system prompt for a small, less capable "
    "coding model. Given existing guidance (it may be empty), produce a slightly revised version: keep whatever is useful, "
    "and sharpen or add exactly ONE concrete, specific tip (a common mistake to avoid, a formatting reminder, a worked-"
    "example style hint) - never vague advice like 'write good code'. Keep the whole thing under 400 characters. Reply with "
    "ONLY the new guidance text: no quotes, no preamble, no explanation.")
CROSSOVER_SYSTEM = (
    "You combine two short pieces of guidance text (each appended to a code-generation system prompt for a small, less "
    "capable coding model) into ONE new guidance string that keeps whichever ideas look most concrete and useful from "
    "each, dropping anything vague or redundant. Keep it under 400 characters. Reply with ONLY the combined text.")


def _live_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "evolve" / "forge_guidance" / "live.txt"


def live_guidance() -> str:
    """What forge.propose() should append to FORGE_SYSTEM right now - "" until the owner has approved a candidate."""
    try:
        return _live_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _apply(payload: str) -> None:
    path = _live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload or "", encoding="utf-8")


def _random_genome() -> str:
    import random
    return random.choice(_SEEDS)


async def _mutate(payload: str, llm: Optional[LLM]) -> str:
    if llm is None:
        return payload
    try:
        reply = await llm(MUTATE_SYSTEM, f"Existing guidance:\n{payload or '(empty)'}")
        return reply.strip()[:400]
    except Exception:
        return payload


async def _crossover(a: str, b: str, llm: Optional[LLM]) -> str:
    if llm is None or a == b:
        return a
    try:
        reply = await llm(CROSSOVER_SYSTEM, f"Guidance A:\n{a or '(empty)'}\n\nGuidance B:\n{b or '(empty)'}")
        return reply.strip()[:400]
    except Exception:
        return a


async def _evaluate(payload: str, llm: Optional[LLM]) -> Tuple[float, dict]:
    tasks = evals.load_tasks("code")
    if not tasks:
        return 0.0, {"error": "no code-tier eval tasks loaded"}
    results = await evals.run_code_tier(tasks, llm, guidance=payload)
    passed = sum(1 for r in results if r.get("passed"))
    fitness = passed / len(results) if results else 0.0
    return fitness, {"passed": passed, "total": len(results),
                     "failure_classes": [r.get("failure_class") for r in results if not r.get("passed")]}


def _describe(payload: str) -> str:
    return payload.strip() or "(no extra guidance - the plain FORGE_SYSTEM baseline)"


def _worth_promoting(fitness: float) -> bool:
    """A placeholder absolute bar (half the hidden cases passing) until there is enough real history to compare against a
    measured baseline instead of a guessed number - the owner can tune this once the first few generations report in."""
    return fitness >= 0.5


evolve.register(evolve.Domain(
    name="forge_guidance", random_genome=_random_genome, mutate=_mutate, crossover=_crossover, evaluate=_evaluate,
    describe=_describe, worth_promoting=_worth_promoting, apply=_apply, pop_size=4, keep_top=2))
