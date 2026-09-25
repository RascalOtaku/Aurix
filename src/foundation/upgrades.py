"""src/foundation/upgrades.py - the upgrade lane: your API tokens turn into reviewed, tested upgrades while you are away.

Every day (within a cap you set) AURIX picks ONE thing to improve, asks the frontier model to act as its engineer, tests the answer in the isolated
sandbox against the whole Foundation test suite, and only then bothers you - with a plain report and one tap:

    backlog (your `upgrade: <idea>`s first, then a rotating self-review of its own modules)
      -> frontier engineer proposes small search/replace edits + a NEW test          (capped calls, redacted, JSON only)
      -> code validates: allowed paths only, never a protected component, no risky calls, parses, small, has a test
      -> sandbox runs the suite before and after the edit: the change may not break anything that passed
      -> a proposal `u-xxxxxx` with the diff, the test results and a risk note        -> you tap Approve / Reject
      -> Approve = a SIGNED approval; the host agent (scripts/aurix_upgrade_agent.py) backs up the files, applies them, runs the deploy with its
         health check, and PUTS THE OLD CODE BACK BY ITSELF if the new build does not come up. `undo u-xxxxxx` rolls back later.

The model proposes; code decides. Nothing here can change the gate, the audit log, credentials, the exam, the listener or this module itself
(identity.PROTECTED_COMPONENTS), existing tests, scripts, compose files or data. Audit records carry ids, file names and counts, never code or text.
"""
from __future__ import annotations

import ast
import base64
import difflib
import hashlib
import html
import io
import json
import os
import re
import secrets
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit, gaming, identity, teacher

e = html.escape

UID = re.compile(r"u-[0-9a-f]{6}")
DEFAULT_MODEL = os.environ.get("AURIX_UPGRADE_MODEL", "") or teacher.DEFAULT_MODEL
MAX_OUTPUT_TOKENS = 12000
MAX_PROMPT_CHARS = 60000
MAX_FILES = 4
MAX_CHANGED_LINES = 320
MAX_NEW_FILE_BYTES = 40000
MAX_PENDING = 2
MIN_GAP_SECONDS = 3 * 3600
APPROVAL_TTL_HOURS = 24
SUITE_TIMEOUT = 900
ALLOW_PREFIXES = ("src/foundation/", "static/command.html")
NEW_TEST_RX = re.compile(r"^tests/test_upgrade_[a-z0-9_]{3,40}\.py$")
NEW_MODULE_RX = re.compile(r"^src/foundation/[a-z][a-z0-9_]{2,40}\.py$")
RISKY = re.compile(r"\b(?:subprocess|os\.system|os\.popen|eval|exec|__import__|ctypes|socket|pickle|marshal|shutil\.rmtree|os\.remove|os\.unlink|os\.chmod|"
                   r"urllib\.request|urlopen)\b|ANTHROPIC|api[_-]?key|\.env\b", re.I)

Post = Callable[[str, Dict[str, str], dict, float], Tuple[int, dict]]

_ENGINEER_EXAMPLE_REPLY = json.dumps({
    "title": "divide: refuse a zero divisor with a clear error",
    "why": "divide(a, b) raised an unhelpful ZeroDivisionError instead of a clear message; added a check and a regression test.",
    "risk": "low",
    "files": [
        {"path": "src/foundation/example.py",
         "edits": [{"find": "def divide(a, b):\n    return a / b", "replace": "def divide(a, b):\n    if b == 0:\n        raise ValueError(\"b must not be zero\")\n    return a / b"}]},
        {"path": "tests/test_upgrade_divide_zero.py",
         "content": "import unittest\n\nfrom src.foundation.example import divide\n\n\nclass T(unittest.TestCase):\n    def test_zero_divisor_raises(self):\n        with self.assertRaises(ValueError):\n            divide(4, 0)\n"}],
})

ENGINEER_SYSTEM = (
    "You are the staff engineer for AURIX, a self-hosted personal assistant whose core rule is: the LLM proposes, plain code decides. You are given ONE "
    "improvement task and the current source of the relevant files. Make the single most valuable SMALL change (a bug fix, a missing edge case, clearer "
    "behavior, a useful small feature) - not a rewrite. Conventions: Python 3, standard library only, unittest for tests, match the surrounding style and "
    "comment density, no new dependencies, no network or subprocess calls, no file deletion, never touch credentials or the approval/audit machinery. "
    "You MUST include a NEW test file `tests/test_upgrade_<short_name>.py` that fails without your change and passes with it (use unittest; import from "
    "`src.foundation`; sys.path is the repo root). Reply with ONLY a JSON object, no other text before or after it, in exactly this shape - here is a "
    f"complete, correctly-formatted example reply (for a DIFFERENT task, do not reuse it):\n{_ENGINEER_EXAMPLE_REPLY}\n"
    "Each `find` must match the file exactly (including indentation) and exactly once; a `content` file is only for a brand-new path. Keep edits small. "
    "COPY `find` CHARACTER-FOR-CHARACTER from the file shown below - do not retype, reformat or reindent it from memory, even a single changed space "
    "makes it fail; when in doubt, pick a SHORTER exact snippet (one line is safer than a whole block) that still identifies the spot uniquely. "
    'If nothing worth changing exists, reply {"title": "", "why": "nothing worth changing", "files": []} and nothing else.')


# ---------------------------------------------------------------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------------------------------------------------------------

def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "upgrades"


def repo_root() -> Path:
    return Path(os.environ.get("AURIX_APP_ROOT", "/app"))


def upgrades_dir() -> Path:
    return Path(os.environ.get("AURIX_UPGRADES_DIR") or (Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "upgrades"))


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------------------
# config + budget
# ---------------------------------------------------------------------------------------------------------------------------------

def config() -> Dict[str, Any]:
    raw = _read(_data() / "config.json", {})
    return {"enabled": bool(raw.get("enabled", False)), "daily_drafts": max(0, min(int(raw.get("daily_drafts", 0) or 0), 12)),
            "model": DEFAULT_MODEL, "key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())}


def set_config(enabled: bool, daily: int = 0) -> str:
    _atomic(_data() / "config.json", {"enabled": bool(enabled), "daily_drafts": max(0, min(int(daily), 12))})
    audit.append("upgrades_set", enabled=bool(enabled), daily_drafts=max(0, min(int(daily), 12)))
    return status_text()


def _usage() -> Dict[str, Any]:
    return _read(_data() / "usage.json", {}).get(time.strftime("%Y-%m-%d"), {"drafts": 0, "in_chars": 0, "out_chars": 0})


def _record_usage(in_chars: int, out_chars: int, draft: bool) -> None:
    allu = _read(_data() / "usage.json", {})
    day = allu.setdefault(time.strftime("%Y-%m-%d"), {"drafts": 0, "calls": 0, "in_chars": 0, "out_chars": 0})
    day["calls"] = day.get("calls", 0) + 1
    day["drafts"] += 1 if draft else 0
    day["in_chars"] += in_chars
    day["out_chars"] += out_chars
    for k in sorted(allu)[:-30]:
        allu.pop(k, None)
    _atomic(_data() / "usage.json", allu)


def can_draft() -> Tuple[bool, str]:
    from src.foundation import shards
    if shards.is_paused("upgrades"):
        return False, "The upgrade lane is paused (`resume upgrades`)."
    c = config()
    if not c["enabled"] or c["daily_drafts"] <= 0:
        return False, "The upgrade lane is off. `upgrades on <per day>` turns it on."
    if _usage()["drafts"] >= c["daily_drafts"]:
        return False, f"Today's upgrade budget is used ({_usage()['drafts']}/{c['daily_drafts']}). It resets tomorrow, or raise it with `upgrades on <n>`."
    return True, ""


def status_text() -> str:
    c, u = config(), _usage()
    props = all_proposals()
    pend = [p for p in props if p["status"] == "review"]
    lines = ["<b>Upgrade lane</b> · " + ("ON" if c["enabled"] and c["daily_drafts"] else "OFF"),
             f"Budget: {u['drafts']}/{c['daily_drafts']} drafts today · about {round((u['in_chars'] + u['out_chars']) / 4000, 1)}k tokens used today · model {e(c['model'])}"]
    if not c["key_present"]:
        lines.append("ℹ️ No ANTHROPIC_API_KEY in the app's environment: drafting with the free local model instead of the frontier one.")
    lines.append(f"Waiting for your yes/no: {len(pend)} · applied so far: {len([p for p in props if p['status'] == 'applied'])} · backlog: {len([b for b in backlog() if b['status'] == 'open'])} ideas of yours")
    lines += [f"• <code>{p['id']}</code> {e(p['title'])}" for p in pend[:5]]
    lines.append("<code>upgrade: &lt;idea&gt;</code> queues yours first · <code>upgrade now</code> · <code>upgrades on 3</code> / <code>upgrades off</code> · <code>yes|no u-xxxxxx</code> · <code>undo u-xxxxxx</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------------------
# backlog
# ---------------------------------------------------------------------------------------------------------------------------------

def backlog() -> List[dict]:
    return _read(_data() / "backlog.json", [])


def add_idea(text: str) -> str:
    text = " ".join((text or "").split())[:600]
    if len(text) < 8:
        return "Describe the upgrade in a sentence, e.g. <code>upgrade: the dashboard should show trades per week</code>."
    if teacher.refuse_reason(text):
        return "I will not queue that: it mentions a credential file or key."
    items = backlog()
    b = {"id": "b-" + secrets.token_hex(3), "text": text, "source": "owner", "status": "open", "added": time.time()}
    items.append(b)
    _atomic(_data() / "backlog.json", items[-60:])
    audit.append("upgrade_idea_added", id=b["id"])
    return f"📝 Queued <code>{b['id']}</code>: “{e(text[:160])}”. It goes to the front of the line for the next draft (<code>upgrade now</code> starts one immediately)."


def editable_files() -> List[str]:
    root = repo_root()
    out = []
    for p in sorted((root / "src" / "foundation").glob("*.py")):
        rel = f"src/foundation/{p.name}"
        if p.name != "__init__.py" and not identity.is_protected_path(rel) and p.stat().st_size <= 45000:
            out.append(rel)
    page = root / "static" / "command.html"
    if page.exists() and page.stat().st_size <= 45000:                          # a bigger page cannot be sent whole, so it cannot be reviewed
        out.append("static/command.html")
    return out


def next_item(now: Optional[float] = None) -> Optional[dict]:
    """Your ideas first; otherwise the module that has gone longest without a review."""
    for b in backlog():
        if b["status"] == "open":
            return {"kind": "idea", "id": b["id"], "text": b["text"], "paths": _paths_in(b["text"])}
    state = _read(_data() / "state.json", {})
    state.setdefault("reviewed", {})
    busy = {c["path"] for p in all_proposals() if p["status"] in ("review", "approved") for c in p.get("changes", [])}
    pool = [f for f in editable_files() if f not in busy]
    if not pool:
        return None
    path = sorted(pool, key=lambda f: state["reviewed"].get(f, 0))[0]
    return {"kind": "review", "id": path, "paths": [path],
            "text": (f"Review `{path}` as a careful maintainer: find the single most valuable improvement - a real bug or unhandled edge case first, otherwise a small, "
                     "clearly useful behavior improvement for the owner (a busy person who wants AURIX to just work). Implement it with a new test.")}


def _paths_in(text: str) -> List[str]:
    found = re.findall(r"(?:src/foundation/[a-z_]+\.py|static/command\.html)", text or "")
    names = re.findall(r"\b([a-z_]{3,30})\.py\b", text or "")
    ok = set(editable_files())
    return [p for p in dict.fromkeys(found + [f"src/foundation/{n}.py" for n in names]) if p in ok][:3]


# ---------------------------------------------------------------------------------------------------------------------------------
# validating what the model sent (all code, no model)
# ---------------------------------------------------------------------------------------------------------------------------------

def check_path(path: str, exists: bool) -> Optional[str]:
    p = str(path or "").replace("\\", "/")
    if not p or ".." in p.split("/") or p.startswith("/") or "\x00" in p:
        return f"bad path {path!r}"
    if exists:
        if not p.startswith(ALLOW_PREFIXES):
            return f"{p} is outside the areas I may change"
        if identity.is_protected_path(p) or identity.is_secret_path(p):
            return f"{p} is a protected component"
    else:
        if not (NEW_TEST_RX.match(p) or NEW_MODULE_RX.match(p)):
            return f"{p} is not a path I may create"
        if identity.is_protected_path(p):
            return f"{p} is a protected component"
    return None


def _changed_lines(old: str, new: str) -> int:
    return sum(1 for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0) if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))


def build_changes(obj: Any, root: Optional[Path] = None) -> Tuple[Optional[List[dict]], str]:
    """Turn the model's JSON into concrete changes [{path, old, new, diff, is_new}], or (None, why not). Nothing is written to disk."""
    root = root or repo_root()
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), list):
        return None, "the reply was not the JSON I asked for"
    if not obj["files"]:
        return None, "nothing worth changing"
    if len(obj["files"]) > MAX_FILES:
        return None, f"it touched {len(obj['files'])} files (limit {MAX_FILES})"
    changes: List[dict] = []
    seen = set()
    for f in obj["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            return None, "a file entry had no path"
        path = f["path"].replace("\\", "/")
        if path in seen:
            return None, f"{path} appears twice"
        seen.add(path)
        target = root / path
        exists = target.is_file()
        bad = check_path(path, exists)
        if bad:
            return None, bad
        if exists:
            old = target.read_text(encoding="utf-8")
            new = old
            edits = f.get("edits")
            if not isinstance(edits, list) or not edits or "content" in f:
                return None, f"{path}: existing files may only be changed with `edits`"
            for ed in edits:
                if not isinstance(ed, dict) or not isinstance(ed.get("find"), str) or not isinstance(ed.get("replace"), str) or not ed["find"]:
                    return None, f"{path}: a malformed edit"
                if new.count(ed["find"]) != 1:
                    return None, f"{path}: an edit's `find` text matched {new.count(ed['find'])} times (must be exactly once)"
                new = new.replace(ed["find"], ed["replace"])
        else:
            old, new = "", f.get("content")
            if not isinstance(new, str) or "edits" in f or not new.strip():
                return None, f"{path}: a new file needs `content`"
            if len(new.encode("utf-8")) > MAX_NEW_FILE_BYTES:
                return None, f"{path} is larger than {MAX_NEW_FILE_BYTES // 1000} KB"
        if new == old:
            return None, f"{path}: the edit changes nothing"
        if path.endswith(".py"):
            try:
                ast.parse(new)
            except SyntaxError as ex:
                return None, f"{path} does not parse ({ex.msg} at line {ex.lineno})"
        added = [ln[1:] for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0) if ln.startswith("+") and not ln.startswith("+++")]
        if not path.startswith("tests/"):
            hit = next((RISKY.search(ln).group(0) for ln in added if RISKY.search(ln)), None)
            if hit:
                return None, f"{path}: new code uses `{hit}`, which I do not allow in an automatic upgrade"
        changes.append({"path": path, "old": old, "new": new, "is_new": not exists,
                        "diff": "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=2)), "lines": _changed_lines(old, new)})
    if sum(c["lines"] for c in changes) > MAX_CHANGED_LINES:
        return None, f"the change is {sum(c['lines'] for c in changes)} lines (limit {MAX_CHANGED_LINES}); too big to review from a phone"
    if not any(NEW_TEST_RX.match(c["path"]) for c in changes):
        return None, "it did not include a new test file"
    if all(NEW_TEST_RX.match(c["path"]) for c in changes):
        return None, "it only added a test and changed nothing"
    return changes, ""


# ---------------------------------------------------------------------------------------------------------------------------------
# asking the engineer
# ---------------------------------------------------------------------------------------------------------------------------------

def _context(item: dict) -> Tuple[str, Optional[str]]:
    root = repo_root()
    parts = [f"TASK:\n{item['text']}"]
    used = len(parts[0])
    paths = list(item.get("paths") or [])
    for p in list(paths):
        stem = Path(p).stem
        for t in (f"tests/test_foundation_{stem}.py",):
            if (root / t).is_file() and t not in paths:
                paths.append(t)
    for p in paths:
        f = root / p
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        why = teacher.refuse_reason(text)
        if why:
            return "", f"{p} mentions {why.split('(')[-1].strip(')')}"
        red, _ = teacher.redact(text)
        block = f"\n\n=== FILE: {p} ({'read-only context' if p.startswith('tests/') else 'you may edit'}) ===\n{red}"
        if used + len(block) > MAX_PROMPT_CHARS:
            break
        parts.append(block)
        used += len(block)
    parts.append("\n\nFiles you may edit are only those marked 'you may edit' plus brand-new `tests/test_upgrade_*.py` / `src/foundation/*.py` files.")
    return "".join(parts), None


def _call_engineer(user_text: str, post: Post, extra_messages: Optional[List[dict]] = None,
                    local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None,
                    meta: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], str]:
    """Asks the frontier engineer; if no key is set, its credit balance is empty, it's refused, or the network is down, falls back
    to the free local model (self-hosted: the upgrade lane never has to stop for lack of paid credits, just draws weaker drafts).
    `meta`, when given, is filled with {"source": "frontier"|"local"} so the caller can log which one actually answered."""
    local = local or teacher._local_fallback
    c = config()
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        msgs = [{"role": "user", "content": user_text}] + (extra_messages or [])
        body = {"model": c["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": ENGINEER_SYSTEM, "messages": msgs}
        try:
            status, data = post(teacher.API_URL, teacher.api_headers(), body, 240)
        except Exception as ex:                                                     # noqa: BLE001
            err = f"network error: {type(ex).__name__}"
        else:
            if status == 200:
                text = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    if meta is not None:
                        meta["source"] = "frontier"
                    return text, ""
                err = "empty reply"
            else:
                err = teacher.api_error(status, data)
    prompt = user_text
    for m in (extra_messages or []):                     # the local bridge is single-turn: fold any repair history into one prompt instead of skipping it
        prompt += "\n\n---\n" + ("Your previous reply:\n" if m.get("role") == "assistant" else "") + str(m.get("content", ""))
    text, local_err = local(ENGINEER_SYSTEM, prompt, MAX_OUTPUT_TOKENS)
    if meta is not None:
        meta["source"] = "local"
    return (text, "") if text else (None, f"{err}; {local_err}")


# ---------------------------------------------------------------------------------------------------------------------------------
# testing it in the sandbox
# ---------------------------------------------------------------------------------------------------------------------------------

_RUNNER = '''import io, json, os, subprocess, sys, tarfile
here = os.getcwd()
with tarfile.open("pkg.tgz") as t:
    t.extractall("base")
patch = json.load(open("patch.json"))
SNIP = r"""
import io, json, sys, unittest
ld = unittest.TestLoader()
suite = unittest.TestSuite([ld.discover("tests", pattern="test_foundation*.py"), ld.discover("tests", pattern="test_upgrade_*.py")])
res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
bad = sorted(set(str(t[0]) for t in res.failures + res.errors))
print("@@" + json.dumps({"ran": res.testsRun, "failed": bad}))
"""
def suite(d):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "AURIX_PROJECT_ROOT": "/tmp/aurix-suite"}
    r = subprocess.run([sys.executable, "-c", SNIP], cwd=d, capture_output=True, text=True, timeout=600, env=env)
    line = [l for l in r.stdout.splitlines() if l.startswith("@@")]
    return json.loads(line[-1][2:]) if line else {"ran": 0, "failed": ["<suite did not finish>"], "tail": (r.stderr or "")[-800:]}
base = suite(os.path.join(here, "base"))
for p, content in patch.items():
    fp = os.path.join(here, "base", p)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    open(fp, "w", encoding="utf-8").write(content)
after = suite(os.path.join(here, "base"))
print("RESULT " + json.dumps({"base": base, "after": after}))
'''


def build_package(root: Optional[Path] = None) -> bytes:
    """The slice of the source tree the Foundation tests need, as a .tgz (nothing secret: source files only)."""
    root = root or repo_root()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(rel: Path) -> None:
            tar.add(str(root / rel), arcname=rel.as_posix(), filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith((".pyc", ".env")) else ti)
        for d in ("src", "services", "poc", "timeline", "evals", "constitution"):
            if (root / d).exists():
                add(Path(d))
        for f in sorted((root / "scripts").glob("*.py")) if (root / "scripts").exists() else []:
            add(Path("scripts") / f.name)
        for f in sorted((root / "static").glob("*.html")) if (root / "static").exists() else []:
            add(Path("static") / f.name)
        for f in sorted((root / "tests").glob("test_*.py")) if (root / "tests").exists() else []:
            add(Path("tests") / f.name)
    return buf.getvalue()


def sandbox_suite(changes: List[dict], run: Optional[Callable] = None) -> Dict[str, Any]:
    """Run the Foundation suite before and after the change, inside the isolated sandbox. Returns {base, after} or {error}."""
    from src.foundation import sandbox
    run = run or sandbox.run
    mid = "m-" + secrets.token_hex(3)
    ws = Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "workspace" / mid
    if not list((repo_root() / "tests").glob("test_foundation*.py")):
        return {"error": "this build has no tests inside it, so I cannot test an upgrade (the tests folder must be in the image)"}
    ws.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ws, 0o777)                                                     # the sandbox runs as another uid and must be able to unpack into it
    except OSError:
        pass
    try:
        (ws / "pkg.tgz").write_bytes(build_package())
        (ws / "patch.json").write_text(json.dumps({c["path"]: c["new"] for c in changes}), encoding="utf-8")
        (ws / "runner.py").write_text(_RUNNER, encoding="utf-8")
        res = run(mid, "python", "exec(open('runner.py').read())", SUITE_TIMEOUT)
        out = str(res.get("output") or res.get("stdout") or "")
        line = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
        if not line:
            return {"error": (res.get("error") or out or "no result")[-300:]}
        return json.loads(line[-1][7:])
    except Exception as ex:                                                     # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {ex}"[:300]}
    finally:
        try:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)
        except OSError:
            pass


def judge(res: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    if "error" in res:
        return False, f"the sandbox could not run the tests ({res['error']})", {}
    b, a = res["base"], res["after"]
    new_fail = sorted(set(a["failed"]) - set(b["failed"]))
    summary = {"base_ran": b["ran"], "after_ran": a["ran"], "added_tests": a["ran"] - b["ran"], "new_failures": new_fail[:8], "base_failing": len(b["failed"]), "after_failing": len(a["failed"])}
    if a["ran"] <= 0 or "<suite did not finish>" in a["failed"]:
        return False, "the test suite did not finish", summary
    if new_fail:
        return False, f"it broke {len(new_fail)} test(s): {', '.join(x.split(' ')[0] for x in new_fail[:3])}", summary
    if summary["added_tests"] < 1:
        return False, "its new test did not run", summary
    return True, "", summary


# ---------------------------------------------------------------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------------------------------------------------------------

def _ppath(uid: str) -> Path:
    return _data() / "proposals" / f"{uid}.json"


def load(uid: str) -> Optional[dict]:
    return _read(_ppath(uid), None) if UID.fullmatch(uid or "") else None


def save(p: dict) -> None:
    _atomic(_ppath(p["id"]), p)


def all_proposals() -> List[dict]:
    d = _data() / "proposals"
    out = []
    for f in d.glob("u-*.json") if d.exists() else []:
        v = _read(f, None)
        if isinstance(v, dict):
            out.append(v)
    return sorted(out, key=lambda p: p.get("created", 0))


def draft(item: dict, post: Post = teacher.default_post, run: Optional[Callable] = None, now: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """One full attempt: ask, validate, test (with one repair round), and file a proposal. Returns (proposal or None, why-not)."""
    now = now or time.time()
    ok, why = can_draft()
    if not ok:
        return None, why
    state = _read(_data() / "state.json", {"reviewed": {}})
    if item["kind"] == "review":                                                # rotate even when this module cannot be sent, so it never blocks the line
        state.setdefault("reviewed", {})[item["id"]] = now
        _atomic(_data() / "state.json", state)
    user_text, refused = _context(item)
    if refused:
        return None, f"not sent: {refused}"
    meta: Dict[str, str] = {}
    text, err = _call_engineer(user_text, post, meta=meta)
    _record_usage(len(user_text), len(text or ""), text is not None)            # a call that never got an answer does not use up the day's budget
    if text is None:
        audit.append("upgrade_draft_failed", item=item["id"], why=err[:80])
        return None, err
    model_used = _model_label(meta.get("source"))
    audit.append("upgrade_called", item=item["id"], model=model_used, in_chars=len(user_text), out_chars=len(text))
    from src.foundation.planner import extract_json
    obj = extract_json(text)
    changes, why = build_changes(obj)
    if changes is None:
        if why == "nothing worth changing":
            _close_item(item)
            return None, why
        return _repair_or_drop(item, user_text, text, why, post, run, now, model_used)
    return _test_and_file(item, obj, changes, run, now, user_text, text, post, repaired=False, model_used=model_used)


def _model_label(source: Optional[str]) -> str:
    """What actually answered, for the audit trail and the proposal itself - never just the configured paid model name by default."""
    return "AURIX's free local model" if source == "local" else config()["model"]


def _repair_or_drop(item, user_text, text, why, post, run, now, model_used):
    """The reply did not validate: give the engineer ONE chance, with the reason."""
    meta: Dict[str, str] = {}
    reply, err = _call_engineer(user_text, post, [{"role": "assistant", "content": text[:6000]},
                                                  {"role": "user", "content": f"That cannot be used: {why}. Reply again with ONLY the corrected JSON object."}],
                                 meta=meta)
    _record_usage(len(user_text), len(reply or ""), False)
    if reply is None:
        audit.append("upgrade_discarded", item=item["id"], why=why[:80])
        return None, why
    model_used = _model_label(meta.get("source"))                               # the repair round can answer from a different source than the first attempt
    from src.foundation.planner import extract_json
    obj = extract_json(reply)
    changes, why2 = build_changes(obj)
    if changes is None:
        audit.append("upgrade_discarded", item=item["id"], why=why2[:80])
        return None, why2
    return _test_and_file(item, obj, changes, run, now, user_text, reply, post, repaired=True, model_used=model_used)


def _test_and_file(item, obj, changes, run, now, user_text, text, post, repaired, model_used):
    ok, why, summary = judge(sandbox_suite(changes, run))
    if not ok and not repaired and summary.get("new_failures"):
        meta: Dict[str, str] = {}
        reply, err = _call_engineer(user_text, post, [{"role": "assistant", "content": text[:6000]}, {"role": "user", "content":
                                                       f"Testing in the sandbox failed: {why}. Fix your change (or the new test) and reply with ONLY the corrected JSON object."}],
                                     meta=meta)
        _record_usage(len(user_text), len(reply or ""), False)
        if reply:
            from src.foundation.planner import extract_json
            obj2 = extract_json(reply)
            changes2, why2 = build_changes(obj2)
            if changes2 is not None:
                obj, changes = obj2, changes2
                model_used = _model_label(meta.get("source"))
                ok, why, summary = judge(sandbox_suite(changes, run))
    if not ok:
        audit.append("upgrade_discarded", item=item["id"], why=why[:80])
        return None, why
    p = {"id": "u-" + secrets.token_hex(3), "title": re.sub(r"\s+", " ", str(obj.get("title") or "upgrade"))[:80], "why": re.sub(r"\s+", " ", str(obj.get("why") or ""))[:500],
         "risk": "medium" if str(obj.get("risk", "")).lower() == "medium" or len([c for c in changes if not c["is_new"]]) > 2 else "low",
         "item": {"kind": item["kind"], "id": item["id"]}, "status": "review", "created": now, "verify": summary, "model": model_used,
         "changes": [{"path": c["path"], "is_new": c["is_new"], "lines": c["lines"], "diff": c["diff"][:9000], "new": c["new"], "sha_old": _sha(c["old"]) if c["old"] else "", "sha_new": _sha(c["new"])}
                     for c in changes]}
    save(p)
    if item["kind"] == "idea":
        _close_item(item)
    audit.append("upgrade_proposed", id=p["id"], title=p["title"][:80], files=[c["path"] for c in changes], added_tests=summary["added_tests"], risk=p["risk"], model=model_used)
    return p, ""


OPENHANDS_TIMEOUT = 600
OPENHANDS_MAX_ITERATIONS = 15


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _openhands_ready() -> Tuple[bool, str]:
    from src.foundation import sandbox
    if not sandbox.available():
        return False, "the sandbox is not reachable"
    try:
        found = sandbox.probe([], ["openhands.sdk"])
    except sandbox.SandboxUnavailable as ex:
        return False, str(ex)
    if not found.get("modules", {}).get("openhands.sdk"):
        return False, "the OpenHands SDK is not installed in the sandbox image"
    return True, ""


def draft_with_openhands(item: dict, run: Optional[Callable] = None, now: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """Alternative drafting path: hands the SAME task to the OpenHands coding agent - already proven, already used for
    software_dev mission steps (src/foundation/openhands.py) - instead of AURIX's own JSON-diff engineer prompt. OpenHands
    edits a throwaway, sandboxed copy of the target file (plus a new test file, in the same workspace); the result goes
    through the IDENTICAL validation/sandbox-test/proposal pipeline as a normal draft() - nothing about the safety model
    changes, only how the edit itself gets written. Only handles one target file at a time."""
    from src.foundation import mission as ms
    from src.foundation import openhands as oh
    from src.foundation import sandbox
    from types import SimpleNamespace
    now = now or time.time()
    ok, why = can_draft()
    if not ok:
        return None, why
    ready, why = _openhands_ready()
    if not ready:
        return None, f"OpenHands unavailable: {why}"
    paths = item.get("paths") or []
    if len(paths) != 1:
        return None, "OpenHands drafting only handles one file at a time right now"
    path = paths[0]
    root = repo_root()
    src_file = root / path
    if not src_file.is_file():
        return None, f"{path} does not exist"
    old_text = src_file.read_text(encoding="utf-8")

    ws_id = "m-" + secrets.token_hex(3)
    ws_dir = Path(ms.workspace_for(ws_id))
    ws_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ws_dir, 0o777)
    except OSError:
        pass
    target = ws_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(old_text, encoding="utf-8")

    fake_mission = SimpleNamespace(id=ws_id, workspace=str(ws_dir), objective=item.get("text", ""), steps=[],
                                    prohibited=["touching any file other than " + path + " and one new tests/test_upgrade_*.py file",
                                                "adding dependencies", "network calls", "deleting files"])
    fake_step = SimpleNamespace(
        id="draft", title="Improve " + path,
        description=(item.get("text", "") + f"\n\nEdit ONLY {path} in this workspace, and add a NEW test file at "
                     "tests/test_upgrade_<short_name>.py (unittest, importing from src.foundation) that fails without "
                     "your change and passes with it. If nothing is worth changing, make no edits and say so."),
        success_check=f"{path} was improved with a matching new test in tests/test_upgrade_*.py, or you concluded nothing was worth changing")
    task_path = oh.write_task(fake_mission, fake_step)
    try:
        res = (run or sandbox.run)(ws_id, "bash", oh.command(task_path, max_iterations=OPENHANDS_MAX_ITERATIONS), OPENHANDS_TIMEOUT)
    except sandbox.SandboxUnavailable as ex:
        _rmtree(ws_dir)
        return None, f"sandbox unavailable: {ex}"
    outcome = oh.interpret(oh.parse_result(res.get("output") or res.get("stdout") or res.get("error", "")), res.get("exit_code"))
    if outcome["state"] != "done":
        audit.append("upgrade_draft_failed", item=item["id"], why=("openhands: " + outcome["text"])[:80])
        _rmtree(ws_dir)
        return None, outcome["text"]

    new_text = target.read_text(encoding="utf-8") if target.is_file() else old_text
    new_test_files = sorted(p for p in ws_dir.glob("tests/test_upgrade_*.py") if p.is_file())
    _record_usage(len(fake_step.description), len(outcome["text"]), True)
    if new_text == old_text and not new_test_files:
        if item["kind"] == "idea":
            _close_item(item)
        _rmtree(ws_dir)
        return None, "nothing worth changing"
    files_obj: List[dict] = []
    if new_text != old_text:
        files_obj.append({"path": path, "edits": [{"find": old_text, "replace": new_text}]})
    for tf in new_test_files:
        rel = "tests/" + tf.name
        files_obj.append({"path": rel, "content": tf.read_text(encoding="utf-8")})
    _rmtree(ws_dir)
    if not files_obj or not new_test_files:
        audit.append("upgrade_discarded", item=item["id"], why="openhands: did not include a new test file"[:80])
        return None, "OpenHands did not include a new test file"
    obj = {"title": re.sub(r"\s+", " ", outcome["text"])[:80] or f"Improve {path}",
           "why": re.sub(r"\s+", " ", outcome["text"])[:500], "risk": "low", "files": files_obj}
    changes, why2 = build_changes(obj, root)
    if changes is None:
        audit.append("upgrade_discarded", item=item["id"], why=("openhands: " + why2)[:80])
        return None, why2
    ok2, why3, summary = judge(sandbox_suite(changes, run))
    if not ok2:
        audit.append("upgrade_discarded", item=item["id"], why=("openhands: " + why3)[:80])
        return None, why3
    model_label = "OpenHands (sandboxed coding agent)"
    p = {"id": "u-" + secrets.token_hex(3), "title": obj["title"], "why": obj["why"],
         "risk": "medium" if len([c for c in changes if not c["is_new"]]) > 2 else "low",
         "item": {"kind": item["kind"], "id": item["id"]}, "status": "review", "created": now, "verify": summary,
         "model": model_label,
         "changes": [{"path": c["path"], "is_new": c["is_new"], "lines": c["lines"], "diff": c["diff"][:9000], "new": c["new"],
                      "sha_old": _sha(c["old"]) if c["old"] else "", "sha_new": _sha(c["new"])} for c in changes]}
    save(p)
    if item["kind"] == "idea":
        _close_item(item)
    audit.append("upgrade_proposed", id=p["id"], title=p["title"][:80], files=[c["path"] for c in changes],
                 added_tests=summary["added_tests"], risk=p["risk"], model=model_label)
    return p, ""


def _close_item(item: dict) -> None:
    if item["kind"] != "idea":
        return
    items = backlog()
    for b in items:
        if b["id"] == item["id"]:
            b["status"] = "done"
    _atomic(_data() / "backlog.json", items)


def render(p: dict) -> str:
    v = p.get("verify", {})
    files = "\n".join(f"• <code>{e(c['path'])}</code> {'(new)' if c['is_new'] else ''} {c['lines']} lines" for c in p["changes"])
    return (f"🛠️ <b>Upgrade ready:</b> {e(p['title'])}  <code>{p['id']}</code>\n<i>{e(p['why'])}</i>\n{files}\n"
            f"<b>Tested in the sandbox:</b> {v.get('after_ran', '?')} tests pass with it (+{v.get('added_tests', '?')} new), nothing that passed before fails. Risk: {e(p['risk'])}.\n"
            f"<b>If you approve:</b> I apply it to the server, rebuild with a health check, and put the old code back by myself if it does not come up. "
            f"<code>undo {p['id']}</code> rolls it back later.\n<b>Your call:</b>  <code>yes {p['id']}</code>   or   <code>no {p['id']}</code>   (<code>diff {p['id']}</code> shows the code)")


def diff_text(uid: str) -> str:
    p = load(uid)
    if p is None:
        return f"No upgrade {e(uid)}."
    return f"<b>{e(p['title'])}</b> <code>{uid}</code>\n" + "\n".join(f"<pre>{e(c['diff'][:3200])}</pre>" for c in p["changes"])[:3900]


# ---------------------------------------------------------------------------------------------------------------------------------
# deciding (signed, like every other outward change)
# ---------------------------------------------------------------------------------------------------------------------------------

def _signed(p: dict, kind: str, now: float) -> dict:
    key = gaming.load_key()
    if key is None:
        raise RuntimeError("AURIX_GAMING_HMAC_KEY is not set on the server")
    from datetime import datetime, timedelta, timezone
    item = {"id": p["id"], "kind": kind, "files": [{"path": c["path"], "sha_old": c["sha_old"], "sha_new": c["sha_new"]} for c in p["changes"]],
            "created": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "expires": (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS)).astimezone().isoformat(timespec="seconds")}
    item["sig"] = gaming.sign(key, item)
    d = upgrades_dir()
    if kind == "upgrade":
        _atomic(d / "payload" / f"{p['id']}.json", {c["path"]: c["new"] for c in p["changes"]})
    _atomic(d / "approved" / f"{'undo-' if kind == 'undo' else ''}{p['id']}.json", item)
    return item


def approve(uid: str, now: Optional[float] = None) -> str:
    p = load(uid)
    if p is None:
        return f"No upgrade {e(uid)}."
    if p["status"] != "review":
        return f"Upgrade {uid} is {p['status']}; nothing to approve."
    now = now or time.time()
    try:
        _signed(p, "upgrade", now)
    except RuntimeError as ex:
        return f"I cannot sign an approval: {e(str(ex))}."
    p["status"], p["decided"] = "approved", now
    save(p)
    audit.append("upgrade_approved", id=uid, files=[c["path"] for c in p["changes"]])
    return f"✅ Approved <code>{uid}</code>. The server applies it, rebuilds and health-checks it (a few minutes). I will tell you how it went."


def decline(uid: str, now: Optional[float] = None) -> str:
    p = load(uid)
    if p is None:
        return f"No upgrade {e(uid)}."
    if p["status"] != "review":
        return f"Upgrade {uid} is {p['status']}."
    p["status"], p["decided"] = "declined", now or time.time()
    save(p)
    audit.append("upgrade_declined", id=uid)
    return f"Rejected <code>{uid}</code> ({e(p['title'])}). I will not propose that exact change again."


def undo(uid: str, now: Optional[float] = None) -> str:
    p = load(uid)
    if p is None or p["status"] != "applied":
        return f"Upgrade {e(uid)} was not applied, so there is nothing to undo."
    try:
        _signed(p, "undo", now or time.time())
    except RuntimeError as ex:
        return f"I cannot sign an undo: {e(str(ex))}."
    audit.append("upgrade_undo_requested", id=uid)
    return f"↩️ Rolling back <code>{uid}</code>: the server restores the old files and redeploys (a few minutes)."


# ---------------------------------------------------------------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------------------------------------------------------------

_running = threading.Lock()


def run_once(post: Post = teacher.default_post, run: Optional[Callable] = None, now: Optional[float] = None, notify_no_result: bool = False) -> str:
    """One draft attempt for the next backlog item. Returns a short outcome line (for `upgrade now`)."""
    if not _running.acquire(blocking=False):
        return "A draft is already in progress."
    try:
        return _run_once(post, run, now, notify_no_result)
    except Exception as ex:                                                     # noqa: BLE001 - a background thread must never die silently
        audit.append("upgrade_draft_failed", item="?", why=f"{type(ex).__name__}: {ex}"[:80])
        return f"The draft crashed ({type(ex).__name__}); it is in the audit log."
    finally:
        _running.release()


def _run_once(post, run, now, notify_no_result):
    item = next_item(now)
    if item is None:
        return "Nothing to work on right now."
    p, why = draft(item, post, run, now)
    st = _read(_data() / "state.json", {})
    st["last_attempt"] = now or time.time()
    if not p and why.startswith(("the API", "network error")):                  # tell the owner ONCE a day when the lane cannot reach the model, with what to do
        day = time.strftime("%Y-%m-%d")
        if st.get("api_note_day") != day:
            st["api_note_day"] = day
            _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [f"note:🛠️ The upgrade lane could not reach the model: {e(why[:300])}"]))
    _atomic(_data() / "state.json", st)
    if p:
        _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [p["id"]]))
        return f"Drafted {p['id']}: {p['title']}"
    if item["kind"] == "idea" or notify_no_result:
        _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [f"note:🛠️ No upgrade came out of “{e(str(item.get('text', ''))[:80])}”: {e(why[:200])}"]))
    return f"No upgrade this time ({why})."


def run_once_openhands(run: Optional[Callable] = None, now: Optional[float] = None) -> str:
    """Same one-attempt-for-the-next-item shape as run_once(), but drafted by OpenHands (draft_with_openhands)
    instead of AURIX's own JSON-diff engineer prompt - an explicit, owner-triggered alternative, not the scheduled
    default. Shares run_once()'s lock so the two paths never race on the same backlog/state files."""
    if not _running.acquire(blocking=False):
        return "A draft is already in progress."
    try:
        item = next_item(now)
        if item is None:
            return "Nothing to work on right now."
        if len(item.get("paths") or []) != 1:
            return f"“{item.get('id')}” touches more than one file; OpenHands drafting only handles one at a time right now."
        p, why = draft_with_openhands(item, run, now)
        st = _read(_data() / "state.json", {})
        st["last_attempt"] = now or time.time()
        _atomic(_data() / "state.json", st)
        if p:
            _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [p["id"]]))
            return f"Drafted {p['id']}: {p['title']} (via OpenHands)"
        _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [f"note:🛠️ OpenHands draft of “{e(str(item.get('id', ''))[:80])}”: {e(why[:200])}"]))
        return f"No upgrade this time ({why})."
    except Exception as ex:                                                     # noqa: BLE001 - a background thread must never die silently
        audit.append("upgrade_draft_failed", item="?", why=f"openhands: {type(ex).__name__}: {ex}"[:80])
        _atomic(_data() / "outbox.json", (_read(_data() / "outbox.json", []) + [f"note:🛠️ OpenHands draft crashed: {e(str(ex)[:150])}"]))
        return f"The draft crashed ({type(ex).__name__}); it is in the audit log."
    finally:
        _running.release()


def tick(now: Optional[float] = None, background: bool = True) -> List[str]:
    """Owner messages (new proposals, outcomes) and, when enabled, kick off the next draft in a background thread. Cheap when idle."""
    now = now or time.time()
    msgs: List[str] = []
    outbox = _read(_data() / "outbox.json", [])
    for uid in outbox:
        if str(uid).startswith("note:"):
            msgs.append(str(uid)[5:])
            continue
        p = load(uid)
        if p and p["status"] == "review":
            msgs.append(render(p))
    if outbox:
        _atomic(_data() / "outbox.json", [])
    rd = upgrades_dir() / "results"
    for p in all_proposals():
        if p["status"] not in ("approved", "applied"):
            continue
        undo_res = _read(rd / f"undo-{p['id']}.json", None) if p["status"] == "applied" else None
        res = _read(rd / f"{p['id']}.json", None) if p["status"] == "approved" else undo_res
        if not res:
            continue
        if p["status"] == "approved":
            if res.get("status") == "applied":
                p["status"], p["result"] = "applied", res
                audit.append("upgrade_applied", id=p["id"], files=[c["path"] for c in p["changes"]])
                msgs.append(f"🚀 <b>Upgrade live:</b> {e(p['title'])} <code>{p['id']}</code>\n<i>{e(str(res.get('detail', ''))[:250])}</i>\nNot right? <code>undo {p['id']}</code>")
            else:
                p["status"], p["result"] = "failed", res
                audit.append("upgrade_failed", id=p["id"], status=str(res.get("status"))[:30])
                msgs.append(f"⚠️ <b>Upgrade not applied:</b> {e(p['title'])} <code>{p['id']}</code> - {e(str(res.get('status')))}: {e(str(res.get('detail', ''))[:300])}\nThe old code is untouched or was restored.")
            save(p)
        elif undo_res and p.get("undone") is None:
            p["undone"] = now
            if undo_res.get("status") == "undone":
                p["status"] = "undone"
                audit.append("upgrade_undone", id=p["id"])
                msgs.append(f"↩️ <b>Rolled back:</b> {e(p['title'])} <code>{p['id']}</code>. {e(str(undo_res.get('detail', ''))[:200])}")
            else:
                msgs.append(f"⚠️ Rollback of <code>{p['id']}</code> did not complete: {e(str(undo_res.get('detail', ''))[:300])}")
            save(p)
    if background and can_draft()[0]:
        st = _read(_data() / "state.json", {})
        pending = len([p for p in all_proposals() if p["status"] == "review"])
        if pending < MAX_PENDING and now - float(st.get("last_attempt", 0)) >= MIN_GAP_SECONDS and not _running.locked():
            st["last_attempt"] = now
            _atomic(_data() / "state.json", st)
            threading.Thread(target=run_once, kwargs={"now": now}, daemon=True, name="aurix-upgrade").start()
    return msgs


def panel() -> Dict[str, Any]:
    c, u = config(), _usage()
    props = all_proposals()[-8:][::-1]
    return {"enabled": bool(c["enabled"] and c["daily_drafts"]), "daily": c["daily_drafts"], "used": u["drafts"], "key": c["key_present"], "model": c["model"],
            "backlog": len([b for b in backlog() if b["status"] == "open"]),
            "items": [{"id": p["id"], "title": p["title"], "status": p["status"], "why": p.get("why", ""), "risk": p.get("risk", ""),
                       "files": [{"path": x["path"], "lines": x["lines"], "diff": x["diff"][:6000]} for x in p["changes"]] if p["status"] in ("review", "approved") else [],
                       "tests": (p.get("verify") or {}).get("after_ran")} for p in props]}
