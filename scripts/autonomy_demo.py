#!/usr/bin/env python3
"""scripts/autonomy_demo.py - one vague ask, run end to end through the real autonomy path, offline.

    python scripts/autonomy_demo.py

What is REAL here: commands.parse, autonomy.understand / render_brief, the planner and its domain packs, the mission
contract and its hash, the fast-lane decision, AutonomousRunner (working memory, self-check, rethink), the risk model and
mission gate for every command, the files written and the duplicate finder that actually runs.

What is a STAND-IN: the language model (scripted replies, marked MODEL below) and the agent loop (a small worker that does
real file and shell work, but picks its actions by step title instead of by reasoning). On the Tower, `do: <task>` in
Telegram uses the real model and agent loop through the same code.

Part 2 shows a guardrail: an ask that would send something out is planned but waits for the owner's go/no-go.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = tempfile.mkdtemp(prefix="aurix-demo-")
os.environ["AURIX_PROJECT_ROOT"] = ROOT
os.environ.setdefault("AURIX_FASTLANE", "1")

from src.foundation import autonomy, commands, mission as ms, openhands, risk, sandbox  # noqa: E402

PRESENT = dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)
ASK = "do: my research folder is full of near-duplicate notes, can you do something about it"
NOTES = Path(ROOT) / "raw" / "research"
LOG: list = []


def show(who: str, text: str) -> None:
    import html
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    LOG.append(f"**{who}:** {plain}")
    print(f"\n[{who}]\n" + textwrap.indent(plain, "  "))


def seed_notes() -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    notes = {
        "reynolds-gang-1864.md": "The Reynolds Gang were Confederate raiders in Colorado Territory in 1864.",
        "reynolds_gang_1864_v2.md": "The Reynolds Gang were Confederate raiders in the Colorado Territory, 1864.",
        "reynolds-gang-raid.md": "In 1864 the Reynolds Gang, Confederate raiders, crossed Colorado Territory.",
        "goldratt-constraints.md": "Goldratt's theory of constraints: find the bottleneck and subordinate everything to it.",
        "goldratt_toc.md": "Goldratt's theory of constraints: find the bottleneck, subordinate everything else to it.",
        "wii-modding.md": "Wii homebrew starts with a safe exploit and a NAND backup before anything else.",
        "viking-longships.md": "Viking longships were clinker-built, shallow-draft and fast under oar or sail.",
    }
    for name, body in notes.items():
        (NOTES / name).write_text(f"# {name[:-3]}\n\n{body}\n", encoding="utf-8")


# --------------------------------------------------------------------------- MODEL (scripted stand-in)
async def model(system: str, prompt: str) -> str:
    if system == autonomy.UNDERSTAND_SYSTEM and "email" in prompt:
        return json.dumps({"kind": "task", "goal": "Email SUMMARY.md from the duplicate-notes job to the owner's friend",
                           "done_when": ["the friend has the summary"], "assumptions": ["send it from the owner's usual address"],
                           "question": "What is your friend's address?", "question_changes_goal": False})
    if system == autonomy.UNDERSTAND_SYSTEM:
        return json.dumps({
            "kind": "task",
            "goal": "Write a script that finds near-duplicate notes in raw/research and lists each group with the copy to keep",
            "done_when": ["a report lists every duplicate group", "the script runs again on demand"],
            "assumptions": ["keep every file; only report the groups", "newest or longest copy is the one to keep",
                            "near-duplicate means 85% or more similar text"],
            "question": "Should I merge them too?", "question_changes_goal": False})
    if system.startswith("You are the planning module") and "duplicate" in prompt and "Email" not in prompt:
        return json.dumps({"steps": [], "success_criteria": ["dedupe_report.md lists every duplicate group"], "unknowns": []})
    if system.startswith("You are the planning module"):
        return json.dumps({"steps": [{"title": "Send the summary by email", "description": "Email SUMMARY.md to the friend",
                                      "capabilities": ["email"], "success_check": "the email is sent"}],
                           "success_criteria": ["the friend has the summary"], "unknowns": ["the friend's address"]})
    if system == autonomy.RETHINK_SYSTEM:
        return ("APPROACH: rapidfuzz is not installed in the sandbox; use Python's standard-library difflib "
                "SequenceMatcher instead, which needs nothing installed.")
    return ""


# --------------------------------------------------------------------------- AGENT LOOP (stand-in that does real work)
class Worker:
    def __init__(self, store: ms.MissionStore):
        self.store = store

    def run(self, m: ms.MissionContract, cmd: str) -> subprocess.CompletedProcess:
        """Every command goes through the real risk model and mission gate first, as the agent loop's tools do."""
        a = risk.assess_action("bash", cmd, m.workspace, sandboxed=m.sandboxed)
        d = ms.decide(self.store.load(m.id), "bash", cmd, a)
        print(f"    gate: {a.tier.name} -> {'allow' if d.allow else 'DENY'} ({d.reason}) :: {cmd[:90]}")
        if not d.allow:
            raise PermissionError(d.reason)
        self.store.record_tool_call(self.store.load(m.id))
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=m.workspace, timeout=60)

    async def __call__(self, prompt: str) -> str:
        mid = re.search(r"\[MISSION (m-[0-9a-f]{6})\]", prompt).group(1)
        m = self.store.load(mid)
        ws = Path(m.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        if "VERIFIED:" in prompt and "Check step" in prompt:                         # self-check: look, don't trust
            want = {"1": "plan.md", "2": "dedupe.py", "3": "dedupe_report.md", "4": "SUMMARY.md"}[
                re.search(r"Check step (\d)", prompt).group(1)]
            p = ws / want
            return (f"VERIFIED: {want} exists ({p.stat().st_size} bytes)" if p.exists() and p.stat().st_size
                    else f"NOT VERIFIED: {want} is missing")
        if "All steps are reported done" in prompt:
            rep = ws / "dedupe_report.md"
            groups = rep.read_text().count("## Group") if rep.exists() else 0
            return (f"MISSION COMPLETE: {groups} duplicate groups reported in dedupe_report.md; nothing was deleted"
                    if groups else "MISSION INCOMPLETE: no report")
        title = (re.search(r"^Step \d+ of \d+: (.+)$", prompt, re.M) or re.search(r"(.*)", "")).group(1) or ""
        if "Understand the task" in title:
            (ws / "inputs").mkdir(exist_ok=True)
            for p in NOTES.glob("*.md"):                                            # read_file: LOW, then a copy to work on
                assert risk.assess_action("read_file", str(p)).tier.name == "LOW"
                (ws / "inputs" / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            n = len(list(NOTES.glob("*.md")))
            (ws / "plan.md").write_text(f"Read {n} notes in {NOTES}. Compare every pair; group >= 0.85 similar; "
                                        "keep the longest; report only.\nAcceptance: report lists the Reynolds and "
                                        "Goldratt groups; no file is changed.\n", encoding="utf-8")
            return f"STEP DONE: plan.md written; {n} notes to compare"
        if "Implement" in title:
            if "difflib" not in prompt:                                             # first try: a library that isn't there
                r = self.run(m, "python3 -c 'import rapidfuzz'")
                if r.returncode:
                    return "STEP BLOCKED: " + (r.stderr.strip().splitlines() or ["import failed"])[-1]
            (ws / "dedupe.py").write_text(DEDUPE, encoding="utf-8")
            return "STEP DONE: dedupe.py written using difflib (no installs)"
        if "Verify" in title:
            r = self.run(m, "python3 dedupe.py inputs dedupe_report.md")
            if r.returncode:
                return "STEP BLOCKED: " + r.stderr.strip()[-200:]
            return "STEP DONE: " + r.stdout.strip()
        if "Review and summarise" in title:
            rep = (ws / "dedupe_report.md").read_text()
            (ws / "SUMMARY.md").write_text("What changed: added dedupe.py and dedupe_report.md. No note was edited or "
                                           "deleted.\n\n" + rep, encoding="utf-8")
            return "STEP DONE: SUMMARY.md written"
        return "STEP DONE: nothing to do for this step"


DEDUPE = '''import difflib, sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
notes = {p.name: p.read_text(encoding="utf-8").split("\\n", 2)[-1] for p in sorted(src.glob("*.md"))}
names, seen, groups = list(notes), set(), []
for i, a in enumerate(names):
    if a in seen:
        continue
    g = [a] + [b for b in names[i + 1:] if b not in seen and
               difflib.SequenceMatcher(None, notes[a].lower(), notes[b].lower()).ratio() >= 0.85]
    if len(g) > 1:
        seen.update(g)
        groups.append(sorted(g, key=lambda n: -len(notes[n])))
lines = ["# Near-duplicate notes", ""]
for k, g in enumerate(groups, 1):
    lines += [f"## Group {k}", f"- keep: {g[0]}"] + [f"- duplicate: {n}" for n in g[1:]] + [""]
out.write_text("\\n".join(lines), encoding="utf-8")
print(f"{len(groups)} groups, {sum(len(g) - 1 for g in groups)} duplicates, {len(notes)} notes checked")
'''


async def main() -> None:
    seed_notes()
    store = ms.MissionStore()

    async def notify(text, *a):
        show("AURIX", text)

    with mock.patch.object(sandbox, "available", return_value=True), \
            mock.patch.object(openhands, "ready", return_value=(False, "not installed in this demo")):
        f = commands.Foundation(Worker(store), notify, llm=model, session_id="demo", store=store, **PRESENT)
        print(f"Workspace root: {ROOT}")
        show("Rascal", ASK)
        kind, arg = commands.parse(ASK)
        show("AURIX", await f.handle(kind, arg))
        if f.runner_task:
            await f.runner_task
        m = store.all()[0]
        print(f"\nMission {m.id}: {m.status.value}; contract hash intact: {m.integrity_ok()}; "
              f"model calls {m.usage.model_calls}, tool calls {m.usage.tool_calls}")
        report = Path(m.workspace) / "dedupe_report.md"
        if report.exists():
            print("\n" + report.read_text())
        notes_after = sorted(p.name for p in NOTES.glob("*.md"))
        print(f"Notes still on disk: {len(notes_after)} of 7 (nothing deleted)")

        print("\n" + "=" * 70 + "\nPart 2 - a guardrail\n")
        ask2 = "do: email that summary to my friend"
        show("Rascal", ask2)
        reply = await f.handle(*commands.parse(ask2))
        show("AURIX", reply)
        m2 = next(x for x in store.all() if x.id != m.id)
        print(f"\nMission {m2.id}: {m2.status.value} (started: {m2.status == ms.MissionStatus.ACTIVE})")

    out = os.environ.get("AURIX_DEMO_TRANSCRIPT")
    if out:
        Path(out).write_text("\n\n".join(LOG) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
