"""src/foundation/projects.py - the Unfinished Projects registry and to-do list (handoff cornerstones 2 and 3).

    project: Bike rebuild - waiting on parts        add a project (optional " - note")
    projects                                        list, oldest-untouched first, with staleness
    project note|pause|resume|done <id> [text]      update (any update counts as "touched")
    todo: call the machinist due 2026-10-01 for p-abc123
    todos                                           open to-dos, due-soonest first
    done <t-id>                                     tick one off

Owner-only (parsed in commands.py after the Telegram sender is verified) and purely bookkeeping:
nothing here can act on the world, so it needs no approval. One JSON file, written atomically.
The morning digest and God's Eye View read it to nag about things going stale.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.foundation import audit

STALE_DAYS = 14
MAX_TEXT = 300                        # a to-do
MAX_NOTE = 800                        # a project's note holds what the owner actually wrote about it
NAME_MAX = 80
MAX_ITEMS = 500
e = html.escape

_DUE = re.compile(r"\s+due\s+(\d{4}-\d{2}-\d{2})\b", re.I)
_FOR = re.compile(r"\s+for\s+(p-[0-9a-f]{6})\b", re.I)


@dataclass
class Project:
    id: str
    name: str
    note: str = ""
    status: str = "active"                # active | paused | done
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)


@dataclass
class Todo:
    id: str
    text: str
    project: str = ""
    due: str = ""                         # YYYY-MM-DD
    done: bool = False
    created: float = field(default_factory=time.time)
    done_at: Optional[float] = None


def _clip(s: str, n: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _clip_marked(s: str, n: int) -> str:
    """Like _clip, but a cut is never silent: the text ends in '...' so the owner can see something was left out."""
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:max(0, n - 3)].rstrip() + "..."


def _short_name(text: str) -> str:
    """A readable project name from a long description: the first sentence if it is short enough, else the first words
    (cut at a word boundary), with '...' when it is not the whole thing."""
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]
    if len(first) <= NAME_MAX and len(first) < len(text):
        return first.rstrip(".!? ")
    if len(text) <= NAME_MAX:
        return text
    cut = text[:NAME_MAX - 3]
    cut = cut[:cut.rfind(" ")] if " " in cut[NAME_MAX // 2:] else cut
    return cut.rstrip(" ,;:-") + "..."


def registry_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "projects" / "registry.json"


class Registry:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or registry_path()

    # -- persistence -----------------------------------------------------------------------
    def load(self) -> Dict[str, list]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        projects = [Project(**{k: v for k, v in p.items() if k in Project.__dataclass_fields__})
                    for p in raw.get("projects", []) if isinstance(p, dict) and "id" in p and "name" in p]
        todos = [Todo(**{k: v for k, v in t.items() if k in Todo.__dataclass_fields__})
                 for t in raw.get("todos", []) if isinstance(t, dict) and "id" in t and "text" in t]
        return {"projects": projects, "todos": todos}

    def save(self, data: Dict[str, list]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = {"projects": [asdict(p) for p in data["projects"]], "todos": [asdict(t) for t in data["todos"]]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- projects ---------------------------------------------------------------------------
    def add_project(self, text: str) -> str:
        """`Name - note` keeps its shape. A free-form message with no ' - ' (how people really write) becomes a short readable
        NAME with the WHOLE text kept in the note: nothing the owner wrote is silently dropped. Anything that still has to be
        cut is marked with '...'. (Found 2026-09-20: three project descriptions were clipped to 80 characters with an empty note.)"""
        raw = re.sub(r"\s+", " ", text or "")                      # NOT stripped yet: a leading ' - ' must still mean "no name"
        name, sep, note = raw.partition(" - ")
        name, note = name.strip(), note.strip()
        if len(name) > NAME_MAX:                                   # a description, not a name: keep all of it
            note = name + (f" - {note}" if note else "")
            name = _short_name(name)
        note = _clip_marked(note, MAX_NOTE)
        if not name:
            return "Give the project a name, e.g. <code>project: Bike rebuild - waiting on parts</code>."
        data = self.load()
        if len(data["projects"]) >= MAX_ITEMS:
            return "The project list is full (500)."
        if any(p.name.lower() == name.lower() and p.status != "done" for p in data["projects"]):
            return f"There is already an open project called {e(name)}."
        p = Project(id="p-" + secrets.token_hex(3), name=name, note=note)
        data["projects"].append(p)
        self.save(data)
        audit.append("project_added", id=p.id, name=name)
        return f"Added project <code>{p.id}</code> <b>{e(name)}</b>."

    def update_project(self, action: str, pid: str, text: str = "") -> str:
        data = self.load()
        p = next((x for x in data["projects"] if x.id == pid), None)
        if p is None:
            return f"No project {e(pid)}."
        text = _clip_marked(text, MAX_NOTE)
        if action == "note":
            if not text:
                return "Say what to note, e.g. <code>project note p-abc123 parts arrived</code>."
            p.note = text
        elif action in ("pause", "resume", "done"):
            p.status = {"pause": "paused", "resume": "active", "done": "done"}[action]
        else:
            return f"Unknown project action {e(action)}."
        p.touched = time.time()
        self.save(data)
        audit.append("project_updated", id=pid, action=action)
        return f"Project <code>{p.id}</code> <b>{e(p.name)}</b>: {p.status}" + (f" - {e(p.note)}" if p.note else "") + "."

    # -- to-dos -----------------------------------------------------------------------------
    def add_todo(self, text: str) -> str:
        text = text or ""
        due = ""
        m = _DUE.search(text)
        if m:
            try:
                datetime.strptime(m.group(1), "%Y-%m-%d")
                due = m.group(1)
                text = text[:m.start()] + text[m.end():]
            except ValueError:
                return "That date is not valid; use YYYY-MM-DD."
        project = ""
        m = _FOR.search(text)
        data = self.load()
        if m:
            project = m.group(1).lower()
            if not any(p.id == project for p in data["projects"]):
                return f"No project {e(project)} to attach that to."
            text = text[:m.start()] + text[m.end():]
        text = _clip_marked(text, MAX_TEXT)
        if not text:
            return "What is the to-do? e.g. <code>todo: call the machinist due 2026-10-01</code>."
        if len(data["todos"]) >= MAX_ITEMS:
            return "The to-do list is full (500); mark some done."
        t = Todo(id="t-" + secrets.token_hex(3), text=text, project=project, due=due)
        data["todos"].append(t)
        for p in data["projects"]:
            if p.id == project:
                p.touched = time.time()
        self.save(data)
        audit.append("todo_added", id=t.id, project=project or None)
        return f"Added <code>{t.id}</code>: {e(text)}" + (f" (due {due})" if due else "") + "."

    def complete_todo(self, tid: str) -> str:
        data = self.load()
        t = next((x for x in data["todos"] if x.id == tid), None)
        if t is None:
            return f"No to-do {e(tid)}."
        if t.done:
            return f"<code>{t.id}</code> was already done."
        t.done, t.done_at = True, time.time()
        for p in data["projects"]:
            if p.id == t.project:
                p.touched = time.time()
        self.save(data)
        audit.append("todo_done", id=tid)
        return f"✅ Done: {e(t.text)}"

    # -- reports ----------------------------------------------------------------------------
    def render_projects(self, now: Optional[float] = None) -> str:
        now = now or time.time()
        projs = [p for p in self.load()["projects"] if p.status != "done"]
        if not projs:
            return "No open projects. Add one: <code>project: Bike rebuild - waiting on parts</code>."
        projs.sort(key=lambda p: p.touched)
        open_by = {}
        for t in self.load()["todos"]:
            if not t.done and t.project:
                open_by[t.project] = open_by.get(t.project, 0) + 1
        lines = [f"<b>Unfinished projects</b> ({len(projs)})"]
        for p in projs:
            idle = int((now - p.touched) / 86400)
            flag = " 🕸 <i>stale</i>" if idle >= STALE_DAYS and p.status == "active" else ""
            todo_n = open_by.get(p.id, 0)
            lines.append(f"• <code>{p.id}</code> <b>{e(p.name)}</b> [{p.status}] · untouched {idle}d{flag}"
                         + (f" · {todo_n} open to-do{'s' if todo_n != 1 else ''}" if todo_n else "")
                         + (f"\n    {e(p.note)}" if p.note else ""))
        return "\n".join(lines)

    def render_todos(self, now: Optional[float] = None) -> str:
        now = now or time.time()
        today = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        todos = [t for t in self.load()["todos"] if not t.done]
        if not todos:
            return "No open to-dos. Add one: <code>todo: call the machinist due 2026-10-01</code>."
        todos.sort(key=lambda t: (t.due or "9999-99-99", t.created))
        lines = [f"<b>To-dos</b> ({len(todos)})"]
        for t in todos:
            due = ""
            if t.due:
                due = f" · {'⚠️ OVERDUE ' if t.due < today else 'due '}{t.due}"
            lines.append(f"▫️ <code>{t.id}</code> {e(t.text)}{due}" + (f" · <code>{t.project}</code>" if t.project else ""))
        lines.append("<i>done &lt;id&gt; to tick one off</i>")
        return "\n".join(lines)

    def digest_line(self, now: Optional[float] = None) -> str:
        """One line for the morning digest; empty when there is nothing to say."""
        now = now or time.time()
        data = self.load()
        today = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        open_t = [t for t in data["todos"] if not t.done]
        overdue = [t for t in open_t if t.due and t.due < today]
        stale = [p for p in data["projects"] if p.status == "active" and (now - p.touched) / 86400 >= STALE_DAYS]
        if not (open_t or stale):
            return ""
        bits = [f"{len(open_t)} open to-do{'s' if len(open_t) != 1 else ''}"]
        if overdue:
            bits.append(f"{len(overdue)} overdue")
        if stale:
            bits.append(f"{len(stale)} stale project{'s' if len(stale) != 1 else ''}: " + e(", ".join(p.name for p in stale[:3])))
        return "<b>Yours:</b> " + ", ".join(bits)
