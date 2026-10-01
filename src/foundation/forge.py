"""src/foundation/forge.py - the skill forge: AURIX writes a small Python tool, proves it, and keeps it - if you say so.

The idea comes from Ada-SI's runtime "skill forging". The differences that matter here:
  * The LLM only PROPOSES source text. Code decides everything else: a static check, a real test run INSIDE THE SANDBOX
    (no secrets, no network), and then the owner's explicit `approve skill <name>`.
  * Approval pins the sha256 of skill.py + test_skill.py. A skill whose files change afterwards is refused and reported
    (`skill_tamper`), not run - so a forged skill cannot be quietly rewritten after you approved it.
  * Skills execute ONLY in the sandbox, via the same fail-closed client as missions. If the sandbox is down nothing runs
    and nothing is verified; there is no local fallback.
  * A skill is a pure function `run(args: dict)` on JSON in / JSON out. No files, no network, no shell.
  * Everything is audited. The forge and data/forge/ are protected components: the agent cannot write an "approved" skill.

Layout (matches command_center.probe_skills): data/forge/<name>/{skill.json, skill.py, test_skill.py}
Status: draft (failed checks/tests, cannot be approved) -> pending (passed, awaiting you) -> active | denied | retired.
XP: `skill_forged` on approval and `skill_used` on a successful run (see xp.py) - outcomes, not consent.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.foundation import audit
from src.foundation import mission as ms
from src.foundation import sandbox
from src.foundation import teacher

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
MAX_FILE_CHARS = 16000
MAX_ARGS_CHARS = 8000
MAX_RESULT_CHARS = 6000
MAX_ATTEMPTS = 3                             # first draft + two repairs
TEST_TIMEOUT = 60
RUN_TIMEOUT = 30
# A fixed directory the sandbox already understands (`m-` + 6 hex). It is NOT a mission and has no contract.
FORGE_WORKSPACE_ID = "m-0f0f0f"

LLM = Callable[[str, str], Awaitable[str]]
Runner = Callable[[str, str, str, int], dict]

BANNED_IMPORTS = {
    "subprocess", "socket", "ctypes", "multiprocessing", "pty", "signal", "importlib", "asyncio", "threading",
    "urllib", "http", "ftplib", "smtplib", "telnetlib", "ssl", "requests", "httpx", "shutil", "webbrowser",
    "pickle", "marshal", "builtins", "sys",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals",
                "setattr", "delattr", "getattr", "vars"}
BANNED_OS_ATTRS = {"system", "popen", "fork", "kill", "remove", "unlink", "rmdir", "rename", "environ", "getenv",
                   "putenv", "chmod", "chown", "walk", "listdir", "scandir", "execv", "execl", "spawnl", "startfile"}
SECRET_MARKERS = (".env", "/proc/", "SANDBOX_TOKEN", "/app/", "id_ed25519", "id_rsa", ".ssh", "auth.json", "TELEGRAM")

FORGE_SYSTEM = """You write ONE small, self-contained Python tool for an assistant, plus its unit tests.
Rules (a checker rejects violations):
- Python 3 standard library only. Pure function: `def run(args: dict):` takes JSON-like input and RETURNS a JSON-serializable value.
- No files, no network, no shell, no environment, no eval/exec/open/getattr/sys/os. Validate `args`; raise ValueError on bad input.
- Validate the TYPE of every field before using it, not just its format: a wrong type entirely (a number where text is required,
  or vice versa) must raise ValueError just as reliably as a right-type-wrong-format value (e.g. text that isn't a valid date).
  isinstance() checks up front catch both; a bare try/except around one parsing call usually only catches the format case.
- Tests may ONLY check behaviour stated in the skill request / DESCRIPTION. Never invent limits, formats or extra rules.
  The bad-input test uses a wrong TYPE only (e.g. a number where text is required).
- Tests use `unittest` in a module that does `from skill import run`. At least 3 tests including an edge case and a bad-input case.
- Deterministic: no randomness or clocks in results.
- NEVER return NaN or Infinity (not valid JSON, and nan != nan breaks tests): for empty or degenerate input raise ValueError instead.
- Keep it short. Tests compare plain values (ints, strings, lists, dicts of those); avoid float equality, use assertAlmostEqual.

Worked example (follow this exact shape - a real, recurring failure is tests that run zero cases because they were not
methods on a unittest.TestCase subclass, or a bad-input test that only exercises a format error, not a type error):
NAME: add_two
DESCRIPTION: args {"a": number, "b": number} -> their sum
=== skill.py ===
def run(args: dict):
    a, b = args.get("a"), args.get("b")
    if not isinstance(a, (int, float)) or isinstance(a, bool) or not isinstance(b, (int, float)) or isinstance(b, bool):
        raise ValueError("a and b must both be numbers")
    return a + b
=== test_skill.py ===
import unittest
from skill import run


class T(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(run({"a": 2, "b": 3}), 5)

    def test_negative(self):
        self.assertEqual(run({"a": -1, "b": 1}), 0)

    def test_bad_input_wrong_type(self):
        with self.assertRaises(ValueError):
            run({"a": "2", "b": 3})

Reply in EXACTLY this format and nothing else:
NAME: <snake_case name, 3-31 chars>
DESCRIPTION: <one line: what it does and what `args` it takes>
=== skill.py ===
<python source>
=== test_skill.py ===
<python unittest source>"""

REPAIR_HINT = ("The previous attempt failed. The failing TEST may be the wrong part: if it asserts something the request "
               "never asked for, delete or correct that test rather than bending the skill. "
               "Fix ONLY what is wrong and reply in the same format. "
               "Problems reported:\n{problems}\n\nPrevious skill.py:\n{code}\n\nPrevious test_skill.py:\n{tests}")


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def forge_dir() -> Path:
    return ms.data_dir() / "forge"


def skill_dir(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError("bad skill name")
    return forge_dir() / name


def digest(code: str, tests: str) -> str:
    return hashlib.sha256((code + "\n\x00\n" + tests).encode("utf-8")).hexdigest()


def _strip_fences(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()   # one newline style, so the pinned hash survives a file round trip
    text = re.sub(r"^```[a-zA-Z0-9]*\n", "", text)
    return re.sub(r"\n```\s*$", "", text).strip("\n")


def parse_draft(text: str) -> Optional[Dict[str, str]]:
    """{name, description, code, tests} from the model's delimited reply, else None."""
    text = text or ""
    name = re.search(r"^\s*NAME:\s*([A-Za-z0-9_\- ]{1,64})\s*$", text, re.M)
    desc = re.search(r"^\s*DESCRIPTION:\s*(.+?)\s*$", text, re.M)
    code = re.search(r"^=== skill\.py ===\s*\n(.*?)(?=^=== test_skill\.py ===)", text, re.M | re.S)
    tests = re.search(r"^=== test_skill\.py ===\s*\n(.*)\Z", text, re.M | re.S)
    if not (name and code and tests):
        return None
    slug = re.sub(r"[^a-z0-9_]+", "_", name.group(1).strip().lower()).strip("_")
    return {"name": slug, "description": (desc.group(1) if desc else "")[:200],
            "code": _strip_fences(code.group(1)), "tests": _strip_fences(tests.group(1))}


def check_source(code: str, tests: str) -> List[str]:
    """Static problems with a draft (empty list = passes). Defense in depth: the sandbox is the real boundary."""
    problems: List[str] = []
    for label, src in (("skill.py", code), ("test_skill.py", tests)):
        if not src.strip():
            problems.append(f"{label} is empty")
        elif len(src) > MAX_FILE_CHARS:
            problems.append(f"{label} is over {MAX_FILE_CHARS} characters")
    if problems:
        return problems
    for label, src in (("skill.py", code), ("test_skill.py", tests)):
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            problems.append(f"{label}: syntax error line {e.lineno}: {e.msg}")
            continue
        problems += _scan(label, tree)
    if not problems:
        tree = ast.parse(code)
        if not any(isinstance(n, ast.FunctionDef) and n.name == "run" and len(n.args.args) == 1 for n in tree.body):
            problems.append("skill.py must define a top-level `def run(args):`")
        if not re.search(r"^\s*def test_", tests, re.M):
            problems.append("test_skill.py has no `def test_...` methods")
        if not re.search(r"from\s+skill\s+import|import\s+skill\b", tests):
            problems.append("test_skill.py must import the skill (`from skill import run`)")
    return problems


def _scan(label: str, tree: ast.AST) -> List[str]:
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_IMPORTS or a.name.split(".")[0] == "os":
                    out.append(f"{label}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS or root == "os":
                out.append(f"{label}: import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            out.append(f"{label}: call to '{node.func.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_OS_ATTRS and isinstance(node.value, ast.Name) and node.value.id == "os":
                out.append(f"{label}: os.{node.attr} is not allowed")
            if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in ("__name__", "__init__", "__str__", "__repr__", "__eq__", "__len__", "__iter__"):
                out.append(f"{label}: dunder access '{node.attr}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = next((m for m in SECRET_MARKERS if m in node.value), None)
            if hit:
                out.append(f"{label}: string mentions '{hit}' (secrets and system paths are off limits)")
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:12]


# The driver programs run INSIDE the sandbox as `python -c`. The skill source is embedded as a string literal
# (repr), so the sandbox needs no shared files. Results come back on one marker line; no marker = failure.
_TEST_DRIVER = '''
import io, json, sys, types, unittest
def _load(name, src):
    mod = types.ModuleType(name)
    exec(compile(src, name + ".py", "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod
res = {"ran": 0, "failures": 0, "errors": 0, "ok": False, "log": ""}
try:
    _load("skill", CODE)
    tmod = _load("test_skill", TESTS)
    stream = io.StringIO()
    r = unittest.TextTestRunner(stream=stream, verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(tmod))
    res.update(ran=r.testsRun, failures=len(r.failures), errors=len(r.errors),
               ok=bool(r.wasSuccessful() and r.testsRun > 0), log=stream.getvalue()[-1500:])
except BaseException as e:
    res["log"] = "load error: %r" % (e,)
print("FORGE_RESULT " + json.dumps(res))
'''

_RUN_DRIVER = '''
import json, sys, types
try:
    mod = types.ModuleType("skill")
    exec(compile(CODE, "skill.py", "exec"), mod.__dict__)
    out = {"ok": True, "result": mod.run(json.loads(ARGS))}
    json.dumps(out, allow_nan=False)
except BaseException as e:
    out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
print("FORGE_OUT " + json.dumps(out, default=str, allow_nan=False))
'''


def test_driver(code: str, tests: str) -> str:
    return f"CODE = {code!r}\nTESTS = {tests!r}\n{_TEST_DRIVER}"


def run_driver(code: str, args_json: str) -> str:
    return f"CODE = {code!r}\nARGS = {args_json!r}\n{_RUN_DRIVER}"


def _marker(output: str, marker: str) -> Optional[dict]:
    for line in reversed((output or "").splitlines()):
        if line.startswith(marker + " "):
            try:
                val = json.loads(line[len(marker) + 1:])
                return val if isinstance(val, dict) else None
            except ValueError:
                return None
    return None


def run_tests(code: str, tests: str, run: Runner) -> dict:
    """Real test run in the sandbox. Returns {ok, ran, failures, errors, log}; ok is False on ANY doubt."""
    try:
        res = run(FORGE_WORKSPACE_ID, "python", test_driver(code, tests), TEST_TIMEOUT)
    except sandbox.SandboxUnavailable as e:
        return {"ok": False, "ran": 0, "failures": 0, "errors": 0, "log": f"sandbox unavailable: {e}"}
    parsed = _marker(res.get("output") or res.get("stdout") or "", "FORGE_RESULT")
    if parsed is None:
        why = res.get("error") or (res.get("output") or "no result marker")
        return {"ok": False, "ran": 0, "failures": 0, "errors": 0, "log": str(why)[-600:]}
    return {"ok": bool(parsed.get("ok")), "ran": int(parsed.get("ran", 0)), "failures": int(parsed.get("failures", 0)),
            "errors": int(parsed.get("errors", 0)), "log": str(parsed.get("log", ""))[-1200:]}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load(name: str) -> Optional[Dict[str, Any]]:
    """skill.json plus the source texts, or None."""
    try:
        d = skill_dir(name)
        meta = json.loads((d / "skill.json").read_text(encoding="utf-8"))
        meta["code"] = (d / "skill.py").read_text(encoding="utf-8")
        meta["tests"] = (d / "test_skill.py").read_text(encoding="utf-8")
        return meta
    except (OSError, ValueError):
        return None


def _save(meta: Dict[str, Any], code: str, tests: str) -> None:
    d = skill_dir(meta["name"])
    _write_atomic(d / "skill.py", code)
    _write_atomic(d / "test_skill.py", tests)
    _write_atomic(d / "skill.json", json.dumps({k: v for k, v in meta.items() if k not in ("code", "tests")}, indent=2))


def _update_meta(name: str, **changes) -> Dict[str, Any]:
    s = load(name) or {}
    meta = {k: v for k, v in s.items() if k not in ("code", "tests")}
    meta.update(changes)
    _write_atomic(skill_dir(name) / "skill.json", json.dumps(meta, indent=2))
    return meta


def all_skills() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for p in sorted(forge_dir().glob("*/skill.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def pending() -> List[Dict[str, Any]]:
    return [s for s in all_skills() if s.get("status") == "pending"]


def integrity_ok(s: Dict[str, Any]) -> bool:
    return bool(s.get("sha256")) and s["sha256"] == digest(s.get("code", ""), s.get("tests", ""))


def _unique_name(base: str) -> str:
    base = base if NAME_RE.match(base) else "skill_" + hashlib.sha256(base.encode()).hexdigest()[:6]
    taken = {s.get("name") for s in all_skills()}                     # denied/retired names stay reserved: their record is evidence
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[:31 - len(suffix)] + suffix, n + 1
    return name


# ---------------------------------------------------------------------------
# forging
# ---------------------------------------------------------------------------

def _ensure_workspace() -> None:
    (ms.data_dir() / "workspace" / FORGE_WORKSPACE_ID).mkdir(parents=True, exist_ok=True)


async def propose(description: str, llm: Optional[LLM], run: Optional[Runner] = None,
                  available: Optional[Callable[[], bool]] = None) -> str:
    """Draft -> check -> test in the sandbox (repairing up to MAX_ATTEMPTS) -> store as `pending`. Returns owner text."""
    description = (description or "").strip()[:600]
    if len(description) < 8:
        return "Tell me what the skill should do, e.g. `forge: a tool that turns a list of dates into weekday names`."
    if llm is None:
        return "The forge needs the model, and it isn't connected right now."
    run = run or sandbox.run
    if not await asyncio.to_thread(available or sandbox.available):
        return ("The sandbox is down, so I can't test a new skill safely. I don't run untested code anywhere else - "
                "try again when `doctor` shows the sandbox healthy.")
    _ensure_workspace()

    prompt = f"Skill wanted: {description}"
    draft: Optional[Dict[str, str]] = None
    problems: List[str] = []
    result: Dict[str, Any] = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            system = FORGE_SYSTEM
            try:                                            # owner-approved lessons from the teacher (advisory text)
                lessons = teacher.relevant_text(description)
                if lessons:
                    system = system + "\n\nLessons from a stronger model (advisory):\n" + lessons
            except Exception:
                pass
            try:                                            # owner-approved guidance evolved by evolve.py (see evolve_domains.py)
                from src.foundation import evolve_domains
                guidance = evolve_domains.live_guidance()
                if guidance:
                    system = system + "\n\nExtra guidance:\n" + guidance
            except Exception:
                pass
            reply = await llm(system, prompt)
        except Exception as e:                                        # a model outage must not crash the command loop
            problems = [f"model call failed: {e!r}"]
            break
        parsed = parse_draft(reply)
        if parsed is None:
            problems = ["reply did not follow the required format (NAME / DESCRIPTION / === skill.py === / === test_skill.py ===)"]
            prompt = f"Skill wanted: {description}\n\n" + REPAIR_HINT.format(problems="\n".join(problems), code="(none)", tests="(none)")
            continue
        draft = parsed
        problems = check_source(draft["code"], draft["tests"])
        result = {}
        if not problems:
            result = await asyncio.to_thread(run_tests, draft["code"], draft["tests"], run)
            if result["ok"]:
                break
            problems = [f"tests failed ({result['failures']} failures, {result['errors']} errors, {result['ran']} ran):\n{result['log']}"]
        prompt = f"Skill wanted: {description}\n\n" + REPAIR_HINT.format(problems="\n".join(problems)[:1800], code=draft["code"], tests=draft["tests"])

    if draft is None:
        audit.append("skill_draft_failed", description=description[:100], reason=problems[:1])
        return "I couldn't get a usable draft from the model: " + html.escape(problems[0] if problems else "no reply") + "."

    passed = not problems and bool(result.get("ok"))
    name = _unique_name(draft["name"])
    meta = {"name": name, "description": draft["description"] or description[:120],
            "wanted": description, "status": "pending" if passed else "draft", "uses": 0, "attempts": attempt,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sha256": digest(draft["code"], draft["tests"]),
            "test_result": {k: result.get(k) for k in ("ran", "failures", "errors")} if result else {}}
    _save(meta, draft["code"], draft["tests"])
    audit.append("skill_proposed", name=name, status=meta["status"], sha256=meta["sha256"][:16], attempts=attempt,
                 tests_ran=result.get("ran", 0))
    return render_proposal(load(name) or {**meta, **draft}, problems if not passed else [])


# ---------------------------------------------------------------------------
# autonomous trigger: notice a repeated gap, draft on its own (still only ever reaches "pending" -
# approval stays the owner's, exactly like an on-demand `forge: <idea>` draft)
# ---------------------------------------------------------------------------

AUTONOMY_MIN_REPEATS = 2                    # the SAME kind of mission must have genuinely stalled at least this often
AUTONOMY_COOLDOWN_DAYS = 14                  # don't re-propose the same noticed gap more than once per this many days
AUTONOMY_LOOKBACK_EVENTS = 500


def _autonomy_state_path() -> Path:
    return forge_dir() / "autonomy_state.json"


def _load_autonomy_state() -> Dict[str, Any]:
    try:
        v = json.loads(_autonomy_state_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_autonomy_state(state: Dict[str, Any]) -> None:
    for k in sorted(state, key=lambda k: state[k].get("ts", 0))[:-40]:               # keep the state file bounded
        state.pop(k, None)
    _write_atomic(_autonomy_state_path(), json.dumps(state, indent=2, ensure_ascii=False))


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _similar(a: str, b: str) -> bool:
    """Deliberately crude (same style as runner._looks_repeated): normalized-text containment, not a
    diff/similarity library - a false negative just means the same gap gets noticed one cycle later."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if len(a) < 12 or len(b) < 12:
        return a == b
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter[:len(shorter) * 3 // 4] in longer


def find_repeated_gap(min_repeats: int = AUTONOMY_MIN_REPEATS, lookback: int = AUTONOMY_LOOKBACK_EVENTS,
                      store: Optional[ms.MissionStore] = None) -> Optional[Dict[str, Any]]:
    """Scans the audit log for missions whose STEPS got blocked - never the "only you can provide this"
    preflight kind (credentials/authorizations), since no skill can substitute for those - and groups
    them by their mission's objective. Returns the most-repeated group once the SAME kind of task has
    genuinely stalled more than once (across separate missions, not just retries within one), or None."""
    store = store or ms.MissionStore()
    groups: List[Dict[str, Any]] = []                          # [{"objective", "missions": set(), "reasons": [...]}]
    for rec in audit.recent(lookback):
        if rec.get("event") != "mission_blocked" or "step" not in rec:
            continue
        mission_id = rec.get("mission")
        if not mission_id:
            continue
        m = store.load(mission_id)
        if m is None or not m.objective:
            continue
        idx = next((i for i, g in enumerate(groups) if _similar(g["objective"], m.objective)), None)
        if idx is None:
            idx = len(groups)
            groups.append({"objective": m.objective, "missions": set(), "reasons": []})
        groups[idx]["missions"].add(mission_id)
        groups[idx]["reasons"].append(str(rec.get("reason", ""))[:200])

    candidates = [g for g in groups if len(g["missions"]) >= min_repeats]
    if not candidates:
        return None
    best = max(candidates, key=lambda g: len(g["missions"]))
    return {"objective": best["objective"], "count": len(best["missions"]),
            "reasons": [r for r in best["reasons"] if r][-3:]}


async def find_backlog_gap(llm: Optional[LLM]) -> Optional[Dict[str, Any]]:
    """The second autonomous source (2026-10-01, owner's finding that LandPilot/skills sat idle between real
    mission failures): the open to-do list almost always has at least a few items that are purely the owner's
    own action (approve a mission, run a script on the 7070, review a bill) - this never proposes for those.
    It only returns a to-do a cheap classification call judges a small sandboxed tool could plausibly help
    with, and even then forge's own real draft+test is still the only thing that can reach `pending` - the
    classification is advisory, never a substitute for the real sandbox proof. Tried at most once per to-do,
    ever (no cooldown re-try the way a repeated mission gap gets one, since a to-do does not repeat)."""
    if llm is None:
        return None
    from src.foundation import projects
    reg = projects.Registry()
    todos = [t for t in reg.load()["todos"] if not t.done]
    state = _load_autonomy_state()
    for t in todos:
        if not t.text or _already_covered(t.text):
            continue
        state_key = f"todo:{_norm(t.text)}"
        if state_key in state:
            continue
        try:
            verdict = await llm(
                "Reply with exactly one word, YES or NO, then a colon and a one-sentence reason. Nothing else.",
                "Could a small, self-contained Python command-line tool meaningfully help someone complete this "
                "to-do, WITHOUT needing an account/credentials only a human has, a real-world physical action, "
                f"or a subjective personal judgment call?\n\nTo-do: {t.text}")
        except Exception:
            continue
        state[state_key] = {"ts": time.time(), "verdict": verdict[:200]}
        _save_autonomy_state(state)
        if verdict.strip().upper().startswith("YES"):
            return {"id": t.id, "text": t.text}
    return None


def _already_covered(objective: str) -> bool:
    """True when a skill - in ANY status, including denied/retired - was already drafted for
    essentially this same gap, so autonomy should not keep re-proposing something already tried."""
    return any(_similar(s.get("wanted", ""), objective) for s in all_skills())


async def check_autonomous_trigger(llm: Optional[LLM], run: Optional[Runner] = None,
                                   now: Optional[float] = None,
                                   available: Optional[Callable[[], bool]] = None) -> Optional[str]:
    """The autonomous entry points into the forge - at most ONE draft per call, repeated-mission-gap checked
    first since it is grounded in a REAL failure, the open to-do backlog only as a fallback when that finds
    nothing: notices a REAL repeated gap (the same kind of mission objective genuinely blocked more than
    once), or a to-do a cheap classification judged forge-shaped (see find_backlog_gap), and drafts a
    candidate skill for it exactly as if the owner had typed `forge: <idea>`. Returns the forge result text
    to tell the owner about, or None when there is nothing new to say. Forging itself never became
    autonomous here, only the NOTICING did: the result still only ever reaches `pending`, awaiting
    `approve skill <name>` like any other draft."""
    now = now or time.time()
    gap = find_repeated_gap()
    if gap is not None and not _already_covered(gap["objective"]):
        key = _norm(gap["objective"])
        state = _load_autonomy_state()
        last = state.get(key)
        if not last or (now - float(last.get("ts", 0))) >= AUTONOMY_COOLDOWN_DAYS * 86400:
            state[key] = {"ts": now, "objective": gap["objective"], "count": gap["count"]}
            _save_autonomy_state(state)
            description = (f"AURIX has tried and failed to complete this kind of mission {gap['count']} separate times: "
                           f"\"{gap['objective'][:300]}\". Recent blockers: {' | '.join(gap['reasons'])[:300]}. "
                           "Draft a small tool that would help get past this specific kind of blocker.")
            audit.append("forge_autonomy_triggered", objective=gap["objective"][:200], count=gap["count"], source="mission_gap")
            result = await propose(description, llm, run, available=available)
            return "🔧 <b>Noticed a repeated gap on its own</b>\n" + result

    todo = await find_backlog_gap(llm)
    if todo is not None:
        description = f"From the open to-do list: \"{todo['text'][:400]}\". Draft a small tool that would meaningfully help with this."
        audit.append("forge_autonomy_triggered", todo=todo["id"], source="backlog")
        result = await propose(description, llm, run, available=available)
        return f"🔧 <b>Picked up an open to-do on its own (<code>{todo['id']}</code>)</b>\n" + result
    return None


def approve(name: str) -> str:
    s = load(name)
    if s is None:
        return f"No skill named '{html.escape(name)}'. `skills` lists them."
    if s["status"] == "active":
        return f"Skill '{name}' is already active."
    if s["status"] != "pending":
        return f"Skill '{name}' is {s['status']}, not pending - only a draft that passed its tests can be approved."
    if not integrity_ok(s):
        audit.append("skill_tamper", name=name, phase="approve")
        return f"⚠️ Skill '{name}' changed after its tests passed. Refusing to approve - forge it again."
    _update_meta(name, status="active", approved=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    audit.append("skill_forged", name=name, sha256=s["sha256"][:16])
    return (f"✅ Skill <b>{html.escape(name)}</b> is active (code pinned <code>{s['sha256'][:10]}</code>). "
            f"Run it with <code>run skill {name} {{\"...\": ...}}</code>. It only ever executes in the sandbox.")


def deny(name: str) -> str:
    s = load(name)
    if s is None or s["status"] not in ("pending", "draft"):
        return f"No pending skill named '{html.escape(name)}'."
    _update_meta(name, status="denied")
    audit.append("skill_denied", name=name)
    return f"Skill '{name}' denied and kept for the record; it can never run."


def retire(name: str) -> str:
    s = load(name)
    if s is None or s["status"] in ("retired", "denied"):
        return f"No live skill named '{html.escape(name)}'."
    _update_meta(name, status="retired")
    audit.append("skill_retired", name=name)
    return f"Skill '{name}' retired. It can no longer run."


async def run_skill(name: str, args_json: str = "", run: Optional[Runner] = None,
                    available: Optional[Callable[[], bool]] = None) -> str:
    args_json = (args_json or "").strip() or "{}"
    if len(args_json) > MAX_ARGS_CHARS:
        return f"Arguments are too long (max {MAX_ARGS_CHARS} characters)."
    try:
        args = json.loads(args_json)
    except ValueError:
        return "Arguments must be a JSON object, e.g. `run skill my_skill {\"text\": \"hello\"}`."
    if not isinstance(args, dict):
        return "Arguments must be a JSON object (curly braces)."
    s = load(name)
    if s is None or s["status"] != "active":
        return f"No active skill named '{html.escape(name)}'. `skills` shows status."
    if not integrity_ok(s):
        audit.append("skill_tamper", name=name, phase="run")
        _update_meta(name, status="retired")
        return f"⚠️ Skill '{name}' no longer matches the code you approved. I retired it and did not run it."
    run = run or sandbox.run
    if not await asyncio.to_thread(available or sandbox.available):
        return "The sandbox is down, so I won't run the skill anywhere else."
    _ensure_workspace()
    try:
        res = await asyncio.to_thread(run, FORGE_WORKSPACE_ID, "python", run_driver(s["code"], json.dumps(args)), RUN_TIMEOUT)
    except sandbox.SandboxUnavailable as e:
        return f"Sandbox error: {html.escape(str(e))}"
    out = _marker(res.get("output") or res.get("stdout") or "", "FORGE_OUT")
    if out is None:
        audit.append("skill_run_failed", name=name, reason=str(res.get("error", "no result"))[:120])
        return f"Skill '{name}' produced no result: {html.escape(str(res.get('error') or res.get('output') or 'unknown')[:400])}"
    if not out.get("ok"):
        audit.append("skill_run_failed", name=name, reason=str(out.get("error", ""))[:120])
        return f"Skill '{name}' failed: {html.escape(str(out.get('error', ''))[:400])}"
    _update_meta(name, uses=int(s.get("uses", 0)) + 1)
    audit.append("skill_used", name=name)
    text = json.dumps(out.get("result"), ensure_ascii=False, indent=2, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n... [truncated]"
    return f"<b>{html.escape(name)}</b> →\n<pre>{html.escape(text)}</pre>"


# ---------------------------------------------------------------------------
# mission provisioning: how a SANDBOXED mission gets to use approved skills
# ---------------------------------------------------------------------------
# No new tool and no new authority. Before a sandboxed step, every ACTIVE skill whose hash still matches is copied into
# <mission workspace>/forged_skills/<name>.py. The agent imports and calls it from an ordinary sandboxed python step, so
# the mission contract (allowed tools, risk ceiling), the gate and STOP all apply exactly as for any other sandbox code.
# The copy is the mission's own scratch file; editing it changes nothing that is approved (data/forge/ is protected and
# is re-verified at every provisioning).

PROVISION_DIR = "forged_skills"
MAX_PROVISIONED = 12


def _one_line(text: str, limit: int = 160) -> str:
    """Skill descriptions are model-written (owner-reviewed) text that lands in a prompt: flatten it to a plain line."""
    return re.sub(r"\s+", " ", str(text or "").replace("`", "")).strip()[:limit]


def provision(workspace: str) -> List[Dict[str, str]]:
    """Copy every ACTIVE, hash-verified skill into `<workspace>/forged_skills/` and return [{name, description}].

    A skill that no longer matches its approved hash is retired and skipped (`skill_tamper`); copies of skills that are
    no longer active are removed. Never raises: a filesystem problem just means fewer skills."""
    provisioned: List[Dict[str, str]] = []
    if not workspace:
        return provisioned
    dest = Path(workspace) / PROVISION_DIR
    try:
        active = [s for s in all_skills() if s.get("status") == "active"][:MAX_PROVISIONED]
        wanted = set()
        for meta in active:
            name = str(meta.get("name", ""))
            s = load(name)
            if s is None or s["status"] != "active":
                continue
            if not integrity_ok(s):
                audit.append("skill_tamper", name=name, phase="provision")
                _update_meta(name, status="retired")
                continue
            _write_atomic(dest / f"{name}.py", s["code"])
            wanted.add(f"{name}.py")
            provisioned.append({"name": name, "description": _one_line(s.get("description", ""))})
        if provisioned:
            _write_atomic(dest / "__init__.py", "")
        if dest.is_dir():
            for stale in dest.glob("*.py"):
                if stale.name != "__init__.py" and stale.name not in wanted:
                    stale.unlink(missing_ok=True)
    except OSError:
        pass
    return provisioned


def catalog_text(skills: List[Dict[str, str]], workspace: str) -> str:
    """Prompt block telling a sandboxed step which skills exist and exactly how to call them ('' if none)."""
    if not skills:
        return ""
    ws = str(workspace).replace("\\", "/")
    lines = [f"Forged skills available (owner-approved, tested, pure functions: JSON dict in, JSON value out; no files, no network). "
             f"Call them from a python step:",
             f"  import sys; sys.path.insert(0, \"{ws}\"); from {PROVISION_DIR}.<name> import run; print(run({{...}}))"]
    lines += [f"  - {s['name']}: {s['description']}" for s in skills]
    lines.append("Prefer a skill over re-writing the same logic. If a skill raises ValueError, your input was wrong - fix the input, don't edit the skill.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_proposal(s: Dict[str, Any], problems: List[str]) -> str:
    e = html.escape
    t = s.get("test_result") if isinstance(s.get("test_result"), dict) else {}
    lines = [f"🛠 <b>Skill forged: {e(s['name'])}</b> ({e(s.get('description', ''))})"]
    if s["status"] == "pending":
        lines.append(f"✅ Passed {t.get('ran', '?')} tests in the sandbox after {s.get('attempts', 1)} attempt(s). Code pinned <code>{s['sha256'][:10]}</code>.")
        lines.append(f"Review with <code>show skill {s['name']}</code>, then <code>approve skill {s['name']}</code> or <code>deny skill {s['name']}</code>.")
    else:
        lines.append("❌ Not usable yet - it can't be approved:")
        lines += [f"• {e(p[:400])}" for p in problems[:4]]
        lines.append("Try `forge:` again with a clearer description.")
    code = s.get("code", "")
    lines.append(f"<pre>{e(code[:1400])}{'...' if len(code) > 1400 else ''}</pre>")
    return "\n".join(lines)


def render_show(name: str) -> str:
    s = load(name)
    if s is None:
        return f"No skill named '{html.escape(name)}'."
    e = html.escape
    ok = "code matches its approved hash" if integrity_ok(s) else "⚠️ code does NOT match its pinned hash"
    return (f"<b>{e(name)}</b> · {s['status']} · used {s.get('uses', 0)}x · {ok}\n{e(s.get('description', ''))}\n"
            f"<pre>{e(s['code'])}</pre>\n<b>tests</b>\n<pre>{e(s['tests'])}</pre>")


def render_list() -> str:
    skills = all_skills()
    if not skills:
        return "No skills yet. Ask for one: `forge: a tool that <does something small and pure>`."
    icon = {"active": "🟢", "pending": "🟡", "draft": "⚪", "denied": "🚫", "retired": "⌛"}
    lines = ["<b>Forged skills</b>"]
    for s in skills:
        lines.append(f"{icon.get(s.get('status'), '•')} <code>{html.escape(str(s.get('name')))}</code> - {s.get('status')} · "
                     f"{s.get('uses', 0)} uses - {html.escape(str(s.get('description', ''))[:80])}")
    lines.append("<i>Only 'active' skills can run; each was tested in the sandbox and approved by you.</i>")
    return "\n".join(lines)
