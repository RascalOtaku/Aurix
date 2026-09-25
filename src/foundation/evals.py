"""src/foundation/evals.py - the eval runner: a scoreboard for "does AURIX get things done", so improvements are measured, not guessed.

Two tiers, both read-only with respect to the real system (nothing is stored in the mission store or the audit chain except one
summary event per run):

  plan   what the planner + fast-lane policy DO with a request: which domain packs match, whether a model-invented credential sneaks in,
         duplicate steps, whether the fast lane would start it (or, for outward/gated goals, correctly refuse). Uses the live planner
         and the live model, so it measures the model too. Regression guard for today's "alpaca-credentials for a primes script" bug.
  code   the model writes a small tool through the forge pipeline (draft -> static check -> its own tests in the sandbox, with repair
         attempts) and is then graded against HIDDEN cases it never saw, run in the sandbox. Pass = the forge would have offered it AND
         every hidden case is right. This is the honest capability number.

A `guidance` string can be appended to the model's system prompt; the same tasks with and without it are the A/B test that decides
whether a guidance change is kept (see docs/AURIX_CLAUDE_CODE_MODE.md). Results append to data/evals/history.jsonl.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.foundation import audit, fastlane, forge, planner, sandbox, teacher

LLM = Callable[[str, str], Awaitable[str]]
Runner = Callable[[str, str, str, int], dict]
HIDDEN_TIMEOUT = 30


def tasks_path() -> Path:
    return Path(__file__).resolve().parents[2] / "evals" / "tasks.json"


def eval_dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "evals"


def load_tasks(tier: Optional[str] = None, path: Optional[Path] = None) -> List[dict]:
    data = json.loads((path or tasks_path()).read_text(encoding="utf-8"))
    tasks = list(data.get("tasks", []))
    if path is None:                                     # checks minted from REAL failures join the exam permanently (experience.py)
        try:
            from src.foundation import experience
            tasks += experience.learned_tasks()
        except Exception:
            pass
    return [t for t in tasks if tier in (None, t.get("tier"))]


# ---------------------------------------------------------------------------------------------------------------------------------
# plan tier
# ---------------------------------------------------------------------------------------------------------------------------------

def plan_checks(task: dict, m, fast_ok: bool, fast_why: List[str]) -> List[dict]:
    """Pure: the contract `m` measured against the task's expectations."""
    exp = task.get("expect", {})
    req = m.requirements or {}
    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    packs = set(req.get("packs") or [])
    for p in exp.get("packs_include", []):
        add(f"pack {p} matched", p in packs, f"packs={sorted(packs)}")
    if "fast_lane" in exp:
        add("fast lane " + ("starts it" if exp["fast_lane"] else "refuses it"), fast_ok == exp["fast_lane"],
            "; ".join(fast_why[:2]))
    if exp.get("no_manual"):
        add("needs nothing from the owner", not req.get("manual"), f"manual={req.get('manual')}")
    for c in exp.get("forbid_capabilities", []):
        used = {x for s in m.steps for x in s.capabilities} | set(req.get("capabilities") or [])
        add(f"does not demand {c}", c not in used)
    if exp.get("no_duplicate_steps"):
        titles = [planner._tokens(s.title) for s in m.steps]
        dup = [i for i, a in enumerate(titles) for b in titles[:i] if a and b and len(a & b) >= 0.8 * min(len(a), len(b))]
        add("no restated steps", not dup, f"{len(dup)} step(s) restate an earlier one")
    if "max_steps" in exp:
        add(f"at most {exp['max_steps']} steps", len(m.steps) <= exp["max_steps"], f"{len(m.steps)} steps")
    return checks


async def run_plan_tier(tasks: List[dict], llm: Optional[LLM], sandboxed: bool, presence_kw: Optional[dict] = None,
                        draft: Callable = None) -> List[dict]:
    draft = draft or planner.draft_mission
    out: List[dict] = []
    for t in tasks:
        t0 = time.time()
        try:
            m = await draft(t["prompt"], llm=llm, session_id="eval", sandboxed=sandboxed, **(presence_kw or {}))
            ok, why = fastlane.check(m)
            checks = plan_checks(t, m, ok, why)
            error = ""
        except Exception as e:                              # a planner crash is a failed task, not a crashed eval run
            checks, error = [{"name": "planner ran", "ok": False, "detail": repr(e)[:200]}], repr(e)[:200]
        failing = "; ".join(f"{c['name']} ({c['detail']})" if c["detail"] else c["name"] for c in checks if not c["ok"])
        out.append({"id": t["id"], "tier": "plan", "passed": all(c["ok"] for c in checks), "checks": checks,
                    "seconds": round(time.time() - t0, 1), "error": error or failing})
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# code tier
# ---------------------------------------------------------------------------------------------------------------------------------

def _same(got: Any, want: Any) -> bool:
    """Equal values; 212 == 212.0 is fine, but a bool never equals a number (True != 1)."""
    if isinstance(got, bool) or isinstance(want, bool):
        return isinstance(got, bool) and isinstance(want, bool) and got == want
    return got == want


def judge_case(case: dict, out: Optional[dict]) -> Dict[str, Any]:
    """Pure: one hidden case against the forge run-driver's FORGE_OUT dict (None = no result)."""
    if out is None:
        return {"ok": False, "detail": "no result from the sandbox"}
    if case.get("raises"):
        err = str(out.get("error", ""))
        return {"ok": (not out.get("ok")) and err.startswith("ValueError"),
                "detail": "expected ValueError, got " + (f"result {out.get('result')!r}" if out.get("ok") else err[:80])}
    if not out.get("ok"):
        return {"ok": False, "detail": str(out.get("error", ""))[:120]}
    got, want = out.get("result"), case.get("expect")
    return {"ok": _same(got, want),
            "detail": f"args={json.dumps(case.get('args'))[:60]} want={json.dumps(want)[:60]} got={json.dumps(got, default=str)[:60]}"}


def _run_case(code: str, case: dict, run: Runner) -> Dict[str, Any]:
    try:
        res = run(forge.FORGE_WORKSPACE_ID, "python", forge.run_driver(code, json.dumps(case.get("args", {}))), HIDDEN_TIMEOUT)
    except sandbox.SandboxUnavailable as e:
        return {"ok": False, "detail": f"sandbox unavailable: {e}"}
    return judge_case(case, forge._marker(res.get("output") or res.get("stdout") or "", "FORGE_OUT"))


def failure_class(had_draft: bool, own_ok: bool, results: List[Dict[str, Any]], own_log: str, attempts: int) -> str:
    """Plain words for the teacher. Deliberately NOT the hidden cases' inputs or expected values: a lesson must generalise, not memorise."""
    if not had_draft:
        return "the model never produced a reply in the required format (NAME / DESCRIPTION / skill.py / test_skill.py)"
    if not own_ok:
        return f"its own unit tests were still failing after {attempts} attempt(s). Last report: {own_log}"
    bad = [r for r in results if not r["ok"]]
    wrong_exc = any("expected ValueError" in r["detail"] for r in bad)
    unexpected = any(("Error:" in r["detail"] or "no result" in r["detail"]) and "expected ValueError" not in r["detail"] for r in bad)
    bits = []
    if wrong_exc:
        bits.append("on invalid input it did not raise ValueError as the request required (wrong exception type or no exception)")
    if unexpected:
        bits.append("on some valid inputs it crashed or returned nothing")
    if len(bad) > wrong_exc + unexpected or not bits:
        bits.append("on some inputs it returned a wrong value even though its own tests passed (its tests missed a case the request stated)")
    return "it passed its own tests but a stricter check found problems: " + "; ".join(bits)


async def run_code_task(task: dict, llm: LLM, run: Runner, guidance: str = "", max_attempts: int = forge.MAX_ATTEMPTS) -> dict:
    t0 = time.time()
    system = forge.FORGE_SYSTEM + (f"\n\nExtra guidance:\n{guidance.strip()}" if guidance.strip() else "")
    prompt = f"Skill wanted: {task['request']}"
    draft: Optional[Dict[str, str]] = None
    own_ok, problems, attempts = False, [], 0
    for attempts in range(1, max_attempts + 1):
        try:
            reply = await llm(system, prompt)
        except Exception as e:
            problems = [f"model call failed: {e!r}"]
            break
        parsed = forge.parse_draft(reply)
        if parsed is None:
            problems = ["reply did not follow the required format"]
            prompt = f"Skill wanted: {task['request']}\n\n" + forge.REPAIR_HINT.format(problems=problems[0], code="(none)", tests="(none)")
            continue
        draft = parsed
        problems = forge.check_source(draft["code"], draft["tests"])
        if not problems:
            res = await asyncio.to_thread(forge.run_tests, draft["code"], draft["tests"], run)
            own_ok = bool(res["ok"])
            if own_ok:
                break
            problems = [f"tests failed ({res['failures']} failures, {res['errors']} errors):\n{res['log']}"]
        prompt = (f"Skill wanted: {task['request']}\n\n"
                  + forge.REPAIR_HINT.format(problems="\n".join(problems)[:1800], code=draft["code"], tests=draft["tests"]))
    cases = task.get("hidden", [])
    own_log = problems[0][:500] if problems else ""
    results: List[Dict[str, Any]] = []
    if draft is not None and own_ok:
        for c in cases:
            results.append(await asyncio.to_thread(_run_case, draft["code"], c, run))
    hidden_ok = sum(1 for r in results if r["ok"])
    passed = bool(draft) and own_ok and bool(cases) and hidden_ok == len(cases)
    failing = [r["detail"] for r in results if not r["ok"]][:2]
    return {"id": task["id"], "tier": "code", "passed": passed, "attempts": attempts, "own_tests_ok": own_ok,
            "failure_class": "" if passed else failure_class(draft is not None, own_ok, results, own_log, attempts),
            "draft": ({"code": draft["code"], "tests": draft["tests"]} if draft else None), "request": task["request"],
            "hidden_passed": hidden_ok, "hidden_total": len(cases), "seconds": round(time.time() - t0, 1),
            "error": "" if passed else ("; ".join(failing) or (problems[0][:160] if problems else "no usable draft")),
            "checks": [{"name": f"hidden case {i + 1}", "ok": r["ok"], "detail": r["detail"]} for i, r in enumerate(results)]}


async def run_code_tier(tasks: List[dict], llm: Optional[LLM], run: Optional[Runner] = None,
                        available: Optional[Callable[[], bool]] = None, guidance: str = "") -> List[dict]:
    if llm is None:
        return [{"id": t["id"], "tier": "code", "passed": False, "error": "the model is not connected", "checks": [],
                 "seconds": 0.0} for t in tasks]
    run = run or sandbox.run
    if not await asyncio.to_thread(available or sandbox.available):
        return [{"id": t["id"], "tier": "code", "passed": False, "error": "the sandbox is down", "checks": [],
                 "seconds": 0.0} for t in tasks]
    forge._ensure_workspace()
    return [await run_code_task(t, llm, run, guidance) for t in tasks]


# ---------------------------------------------------------------------------------------------------------------------------------
# scoreboard
# ---------------------------------------------------------------------------------------------------------------------------------

def summarize(results: List[dict]) -> Dict[str, Any]:
    tiers: Dict[str, Dict[str, Any]] = {}
    for r in results:
        t = tiers.setdefault(r["tier"], {"passed": 0, "total": 0, "failed": []})
        t["total"] += 1
        if r["passed"]:
            t["passed"] += 1
        else:
            t["failed"].append(r["id"])
    return tiers


def _atomic_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_failures() -> List[dict]:
    try:
        v = json.loads((eval_dir() / "failures.json").read_text(encoding="utf-8"))
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _history_path() -> Path:
    return eval_dir() / "history.jsonl"


def load_history(limit: int = 50) -> List[dict]:
    try:
        lines = _history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def record(results: List[dict], model: str = "", guidance: str = "") -> Dict[str, Any]:
    """Append one run to history.jsonl and write latest.json. One small audit event; the results themselves stay in data/evals/."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model or "unknown",
             "guidance_sha": hashlib.sha256(guidance.encode()).hexdigest()[:12] if guidance.strip() else "",
             "tiers": summarize(results), "results": [{k: r.get(k) for k in ("id", "tier", "passed", "seconds", "attempts", "error")}
                                                     for r in results]}
    d = eval_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(_history_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    fails = [{"id": r["id"], "request": r.get("request", ""), "failure_class": r.get("failure_class", ""),
              "attempt": ((r.get("draft") or {}).get("code", ""))[:3000]} for r in results if r["tier"] == "code" and not r["passed"]]
    if any(r["tier"] == "code" for r in results):
        _atomic_json(d / "failures.json", fails)
    tmp = d / "latest.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "latest.json")
    audit.append("evals_run", tiers={k: f"{v['passed']}/{v['total']}" for k, v in entry["tiers"].items()},
                 model=entry["model"], guidance=entry["guidance_sha"])
    return entry


def render(entry: dict, previous: Optional[dict] = None) -> str:
    e = html.escape
    lines = [f"📊 <b>Eval scoreboard</b> · {e(str(entry.get('ts', ''))[:16])} · model {e(str(entry.get('model')))}"
             + (f" · guidance {e(entry['guidance_sha'])}" if entry.get("guidance_sha") else "")]
    prev_tiers = (previous or {}).get("tiers", {})
    for name, t in entry.get("tiers", {}).items():
        was = prev_tiers.get(name)
        delta = ""
        if was and was.get("total"):
            d = t["passed"] - was["passed"]
            delta = f" (was {was['passed']}/{was['total']}" + (f", {'+' if d > 0 else ''}{d}" if d else ", same") + ")"
        lines.append(f"• <b>{e(name)}</b>: {t['passed']}/{t['total']}{delta}")
    bad = [r for r in entry.get("results", []) if not r.get("passed")]
    for r in bad[:8]:
        lines.append(f"   ✗ <code>{e(str(r.get('id')))}</code> {e(str(r.get('error') or '')[:110])}")
    if len(bad) > 8:
        lines.append(f"   … and {len(bad) - 8} more")
    if not bad:
        lines.append("   all passing")
    return "\n".join(lines)


def regressions(before: Optional[dict], after: Optional[dict]) -> List[str]:
    """Tiers whose pass count fell between two recorded runs of the same shape ('' list if nothing got worse or there is no baseline)."""
    if not before or not after:
        return []
    bt, at = before.get("tiers", {}), after.get("tiers", {})
    return [f"{k}: {bt[k]['passed']} -> {at[k]['passed']}/{at[k]['total']}" for k in at
            if k in bt and bt[k].get("total") == at[k].get("total") and at[k]["passed"] < bt[k]["passed"]]


def digest_line() -> str:
    """One line for the morning digest: the latest scoreboard and how many lessons wait for the owner ('' when there is nothing to say)."""
    hist = load_history(1)
    pending = sum(1 for x in teacher.all_lessons() if x.get("status") == "pending")
    if not hist and not pending:
        return ""
    bits = []
    if hist:
        bits.append(", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in hist[-1].get("tiers", {}).items()) + f" ({str(hist[-1].get('ts', ''))[:10]})")
    if pending:
        bits.append(f"{pending} lesson{'s' if pending != 1 else ''} awaiting your approval (<code>lessons</code>)")
    return "<b>Evals:</b> " + " · ".join(bits)


def render_latest() -> str:
    hist = load_history(2)
    if not hist:
        return "No eval runs yet. <code>evals</code> runs the planner and code checks (a few minutes); <code>evals plan</code> only the planner checks."
    return render(hist[-1], hist[-2] if len(hist) > 1 else None)


async def run_and_record(tiers, llm: Optional[LLM], sandboxed: bool, presence_kw: Optional[dict] = None,
                         run: Optional[Runner] = None, available: Optional[Callable[[], bool]] = None,
                         guidance: str = "", model: str = "") -> str:
    """Run the requested tiers, record, and return the scoreboard text (with the change since the previous run of the same shape)."""
    results: List[dict] = []
    if "plan" in tiers:
        results += await run_plan_tier(load_tasks("plan"), llm, sandboxed, presence_kw)
    if "code" in tiers:
        results += await run_code_tier(load_tasks("code"), llm, run, available, guidance)
    prev = next((h for h in reversed(load_history(20)) if set(h.get("tiers", {})) == set(summarize(results))
                 and h.get("guidance_sha", "") == (hashlib.sha256(guidance.encode()).hexdigest()[:12] if guidance.strip() else "")), None)
    return render(record(results, model, guidance), prev)


async def teach_failures(llm: Optional[LLM], run: Optional[Runner] = None, available: Optional[Callable[[], bool]] = None,
                         post=None, max_tasks: int = 3) -> str:
    """For the code tasks that failed in the latest run: ask the teacher for a lesson (budget-capped), then A/B test each lesson by
    re-running the failed task WITH it as guidance. The lessons stay pending: the owner decides."""
    fails = load_failures()[:max_tasks]
    if not fails:
        return "No failing code tasks are recorded. Run <code>evals code</code> first (or nothing is failing)."
    ok, why = teacher.can_call()
    if not ok:
        return why
    if llm is None:
        return "The model is not connected, so I cannot test a lesson."
    tasks = {t["id"]: t for t in load_tasks("code")}
    lines: List[str] = []
    for f in fails:
        ok, why = teacher.can_call()
        if not ok:
            lines.append(why)
            break
        les, msg = await asyncio.to_thread(teacher.teach, "code task", f["id"], f["request"], f["failure_class"], f.get("attempt", ""),
                                           post or teacher.default_post)
        if les is None:
            lines.append(f"<code>{html.escape(f['id'])}</code>: {html.escape(msg)}")
            continue
        task = tasks.get(f["id"])
        if task is None or not await asyncio.to_thread(available or sandbox.available):
            les["evidence"] = "not tested (sandbox down)"
        else:
            forge._ensure_workspace()
            res = await run_code_task(task, llm, run or sandbox.run, guidance=teacher.lesson_text(les))
            les["evidence"] = f"{f['id']}: " + ("passes with this lesson (failed before)" if res["passed"] else "still fails with this lesson")
        teacher.save_lesson(les)
        lines.append(f"📝 <code>{les['id']}</code> {html.escape(les['title'])} - {html.escape(les['evidence'])}")
    lines.append("<i><code>show lesson &lt;id&gt;</code> to read one, <code>approve lesson &lt;id&gt;</code> to keep it.</i>")
    return "\n".join(lines)
