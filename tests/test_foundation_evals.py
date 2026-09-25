"""Tests for src/foundation/evals.py: the task file is well-formed and FAIR (a correct reference solution passes every hidden case),
the runner scores right and wrong solutions correctly, history/scoreboard work, and the `evals` command runs in the background."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, evals, forge  # noqa: E402

PRESENT = dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)


def local_runner(mission, kind, code, timeout):
    """Stands in for sandbox.run: really executes the driver program."""
    assert kind == "python" and mission == forge.FORGE_WORKSPACE_ID
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    out, err = p.stdout.rstrip(), p.stderr.rstrip()
    return {"output": (out + ("\nSTDERR: " + err if err else "")).strip() or "(no output)", "exit_code": p.returncode}


def reply(name, desc, code, tests):
    return f"NAME: {name}\nDESCRIPTION: {desc}\n=== skill.py ===\n{code.strip()}\n=== test_skill.py ===\n{tests.strip()}\n"


HEAD = "import unittest\nfrom skill import run\n\n\nclass T(unittest.TestCase):\n"
STRICT_INT = "not isinstance(n, int) or isinstance(n, bool)"

REFERENCE = {
    "code-primes": reply("first_primes", "first n primes", f'''
def run(args):
    n = args.get("n") if isinstance(args, dict) else None
    if {STRICT_INT} or n < 0:
        raise ValueError("n must be a non-negative integer")
    out, c = [], 2
    while len(out) < n:
        if all(c % p for p in out if p * p <= c):
            out.append(c)
        c += 1
    return out
''', HEAD + '''
    def test_five(self):
        self.assertEqual(run({"n": 5}), [2, 3, 5, 7, 11])

    def test_zero(self):
        self.assertEqual(run({"n": 0}), [])

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"n": "x"})
'''),
    "code-reverse-words": reply("reverse_words", "reverse word order", '''
def run(args):
    t = args.get("text") if isinstance(args, dict) else None
    if not isinstance(t, str):
        raise ValueError("text must be a string")
    return " ".join(reversed(t.split()))
''', HEAD + '''
    def test_basic(self):
        self.assertEqual(run({"text": "a b c"}), "c b a")

    def test_empty(self):
        self.assertEqual(run({"text": "  "}), "")

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"text": 1})
'''),
    "code-celsius": reply("c_to_f", "celsius to fahrenheit", '''
def run(args):
    v = args.get("values") if isinstance(args, dict) else None
    if not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v):
        raise ValueError("values must be a list of numbers")
    return [x * 9 / 5 + 32 for x in v]
''', HEAD + '''
    def test_basic(self):
        self.assertEqual(run({"values": [0, 100]}), [32, 212])

    def test_empty(self):
        self.assertEqual(run({"values": []}), [])

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"values": "x"})
'''),
    "code-word-count": reply("word_count", "count words", '''
def run(args):
    t = args.get("text") if isinstance(args, dict) else None
    if not isinstance(t, str):
        raise ValueError("text must be a string")
    out = {}
    for w in t.split():
        w = w.lower().strip(".,!?")
        if w:
            out[w] = out.get(w, 0) + 1
    return out
''', HEAD + '''
    def test_basic(self):
        self.assertEqual(run({"text": "a A b"}), {"a": 2, "b": 1})

    def test_empty(self):
        self.assertEqual(run({"text": ""}), {})

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"text": 3})
'''),
    "code-fizzbuzz": reply("fizzbuzz", "fizzbuzz list", f'''
def run(args):
    n = args.get("n") if isinstance(args, dict) else None
    if {STRICT_INT} or n < 1:
        raise ValueError("n must be an integer >= 1")
    return ["FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i) for i in range(1, n + 1)]
''', HEAD + '''
    def test_five(self):
        self.assertEqual(run({"n": 5}), ["1", "2", "Fizz", "4", "Buzz"])

    def test_fifteen(self):
        self.assertEqual(run({"n": 15})[-1], "FizzBuzz")

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"n": 0})
'''),
    "code-unique-sorted": reply("unique_sorted", "sorted unique ints", '''
def run(args):
    v = args.get("items") if isinstance(args, dict) else None
    if not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in v):
        raise ValueError("items must be a list of integers")
    return sorted(set(v))
''', HEAD + '''
    def test_basic(self):
        self.assertEqual(run({"items": [2, 1, 2]}), [1, 2])

    def test_empty(self):
        self.assertEqual(run({"items": []}), [])

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"items": "x"})
'''),
    "code-palindrome": reply("is_palindrome", "palindrome check", '''
def run(args):
    t = args.get("text") if isinstance(args, dict) else None
    if not isinstance(t, str):
        raise ValueError("text must be a string")
    s = "".join(ch.lower() for ch in t if ch.isalnum())
    return s == s[::-1]
''', HEAD + '''
    def test_yes(self):
        self.assertTrue(run({"text": "Aba"}))

    def test_no(self):
        self.assertFalse(run({"text": "ab"}))

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"text": 1})
'''),
    "code-days-between": reply("days_between", "days between dates", '''
import datetime


def run(args):
    a = args.get("start") if isinstance(args, dict) else None
    b = args.get("end") if isinstance(args, dict) else None
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("start and end must be strings")
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except ValueError:
        raise ValueError("invalid date")
''', HEAD + '''
    def test_basic(self):
        self.assertEqual(run({"start": "2026-01-01", "end": "2026-01-02"}), 1)

    def test_negative(self):
        self.assertEqual(run({"start": "2026-01-02", "end": "2026-01-01"}), -1)

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"start": "nope", "end": "2026-01-01"})
'''),
}


class ReferenceLLM:
    """Answers every code task with its reference solution (found by the request text in the prompt)."""

    def __init__(self, tasks, overrides=None):
        self.by_request = {t["request"]: t["id"] for t in tasks}
        self.overrides = overrides or {}
        self.systems, self.calls = [], 0

    async def __call__(self, system, prompt):
        self.systems.append(system)
        self.calls += 1
        for req, tid in self.by_request.items():
            if req in prompt:
                return self.overrides.get(tid, REFERENCE[tid])
        return "no idea"


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()


class TaskFileTests(unittest.TestCase):
    def test_well_formed(self):
        tasks = evals.load_tasks()
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)), "task ids must be unique")
        for t in tasks:
            self.assertIn(t["tier"], ("plan", "code"))
            if t["tier"] == "plan":
                self.assertTrue(t["prompt"].strip())
                self.assertTrue(t["expect"])
            else:
                self.assertIn("run(args)", t["request"])
                self.assertGreaterEqual(len(t["hidden"]), 3)
                for c in t["hidden"]:
                    self.assertIn("args", c)
                    self.assertTrue(("expect" in c) != bool(c.get("raises")))
        self.assertEqual(sum(1 for t in tasks if t["tier"] == "code"), len(REFERENCE))

    def test_every_code_task_has_a_reference_solution(self):
        for t in evals.load_tasks("code"):
            self.assertIn(t["id"], REFERENCE)


class JudgeTests(unittest.TestCase):
    def test_values_and_numbers(self):
        self.assertTrue(evals.judge_case({"expect": [32, 212]}, {"ok": True, "result": [32.0, 212.0]})["ok"])
        self.assertTrue(evals.judge_case({"expect": 30}, {"ok": True, "result": 30})["ok"])
        self.assertFalse(evals.judge_case({"expect": 30}, {"ok": True, "result": 31})["ok"])

    def test_bools_are_strict(self):
        self.assertTrue(evals.judge_case({"expect": True}, {"ok": True, "result": True})["ok"])
        self.assertFalse(evals.judge_case({"expect": True}, {"ok": True, "result": 1})["ok"])
        self.assertFalse(evals.judge_case({"expect": 1}, {"ok": True, "result": True})["ok"])

    def test_raises_must_be_a_value_error(self):
        case = {"raises": True}
        self.assertTrue(evals.judge_case(case, {"ok": False, "error": "ValueError: bad"})["ok"])
        self.assertFalse(evals.judge_case(case, {"ok": False, "error": "TypeError: bad"})["ok"])
        self.assertFalse(evals.judge_case(case, {"ok": True, "result": []})["ok"])

    def test_no_result_and_unexpected_error(self):
        self.assertFalse(evals.judge_case({"expect": 1}, None)["ok"])
        self.assertFalse(evals.judge_case({"expect": 1}, {"ok": False, "error": "KeyError: 'n'"})["ok"])


class PlanTierTests(_Base):
    async def test_all_plan_tasks_pass_with_the_deterministic_planner(self):
        res = await evals.run_plan_tier(evals.load_tasks("plan"), None, True, PRESENT)
        failed = [(r["id"], [c for c in r["checks"] if not c["ok"]]) for r in res if not r["passed"]]
        self.assertEqual(failed, [])

    async def test_a_hallucinating_model_is_caught_or_screened(self):
        async def llm(system, prompt):
            return json.dumps({"steps": [{"title": "Write the script", "capabilities": ["alpaca-credentials", "alpaca-py"]}], "unknowns": []})
        no_alpaca = {**PRESENT, "find_spec": lambda m: None if "alpaca" in m else object()}     # everything installed except Alpaca
        res = await evals.run_plan_tier([t for t in evals.load_tasks("plan") if t["id"] == "plan-primes"], llm, True, no_alpaca)
        self.assertTrue(res[0]["passed"], res[0]["checks"])                       # the planner screens it, so the check passes

    async def test_the_check_really_fails_when_a_credential_gets_through(self):
        t = [x for x in evals.load_tasks("plan") if x["id"] == "plan-primes"][0]

        async def leaky(goal, llm=None, session_id=None, sandboxed=False, **kw):
            from src.foundation import planner as p
            m = await p.draft_mission(goal, llm=None, session_id=session_id, sandboxed=sandboxed, **kw)
            m.requirements["manual"] = ["alpaca-credentials (Alpaca paper-trading API key + secret)"]
            m.steps[0].capabilities.append("alpaca-credentials")
            return m
        res = await evals.run_plan_tier([t], None, True, PRESENT, draft=leaky)
        self.assertFalse(res[0]["passed"])
        self.assertTrue(any(c["name"] == "does not demand alpaca-credentials" and not c["ok"] for c in res[0]["checks"]))

    async def test_a_planner_crash_is_one_failed_task_not_a_dead_run(self):
        async def boom(*a, **k):
            raise RuntimeError("planner exploded")
        res = await evals.run_plan_tier(evals.load_tasks("plan")[:2], None, True, PRESENT, draft=boom)
        self.assertEqual([r["passed"] for r in res], [False, False])
        self.assertIn("exploded", res[0]["error"])

    async def test_drafting_does_not_touch_the_mission_store_or_the_audit_chain(self):
        before = len(audit.recent(50))
        await evals.run_plan_tier(evals.load_tasks("plan")[:3], None, True, PRESENT)
        self.assertEqual(len(audit.recent(50)), before)
        self.assertFalse((evals.eval_dir().parent / "missions").exists() and any((evals.eval_dir().parent / "missions").iterdir()))


class CodeTierTests(_Base):
    async def test_reference_solutions_pass_every_hidden_case(self):
        """This is also the fairness test: a correct solution must pass all hidden cases."""
        tasks = evals.load_tasks("code")
        llm = ReferenceLLM(tasks)
        res = await evals.run_code_tier(tasks, llm, run=local_runner, available=lambda: True)
        self.assertEqual([(r["id"], r["error"]) for r in res if not r["passed"]], [])
        self.assertTrue(all(r["hidden_passed"] == r["hidden_total"] for r in res))

    async def test_an_overfitted_solution_passes_its_own_tests_but_fails_the_hidden_ones(self):
        tasks = [t for t in evals.load_tasks("code") if t["id"] == "code-primes"]
        cheat = reply("first_primes", "primes", '''
def run(args):
    n = args.get("n") if isinstance(args, dict) else None
    if not isinstance(n, int):
        raise ValueError("bad")
    return [2, 3, 5, 7, 11][:n]
''', HEAD + '''
    def test_five(self):
        self.assertEqual(run({"n": 5}), [2, 3, 5, 7, 11])

    def test_two(self):
        self.assertEqual(run({"n": 2}), [2, 3])

    def test_bad(self):
        with self.assertRaises(ValueError):
            run({"n": "x"})
''')
        res = await evals.run_code_tier(tasks, ReferenceLLM(tasks, {"code-primes": cheat}), run=local_runner, available=lambda: True)
        self.assertFalse(res[0]["passed"])
        self.assertTrue(res[0]["own_tests_ok"])
        self.assertLess(res[0]["hidden_passed"], res[0]["hidden_total"])
        self.assertIn("want", res[0]["error"])

    async def test_a_solution_that_raises_the_wrong_exception_type_fails(self):
        tasks = [t for t in evals.load_tasks("code") if t["id"] == "code-fizzbuzz"]
        wrong = REFERENCE["code-fizzbuzz"].replace('raise ValueError("n must be an integer >= 1")', 'raise TypeError("n")') \
                                          .replace("with self.assertRaises(ValueError):", "with self.assertRaises(TypeError):")
        res = await evals.run_code_tier(tasks, ReferenceLLM(tasks, {"code-fizzbuzz": wrong}), run=local_runner, available=lambda: True)
        self.assertFalse(res[0]["passed"])
        self.assertIn("expected ValueError", res[0]["error"])

    async def test_a_model_that_never_follows_the_format_fails_after_the_attempts(self):
        tasks = evals.load_tasks("code")[:1]

        async def rambling(system, prompt):
            return "Sure, here you go!"
        res = await evals.run_code_tier(tasks, rambling, run=local_runner, available=lambda: True)
        self.assertFalse(res[0]["passed"])
        self.assertEqual(res[0]["attempts"], forge.MAX_ATTEMPTS)

    async def test_guidance_reaches_the_system_prompt(self):
        tasks = evals.load_tasks("code")[:1]
        llm = ReferenceLLM(tasks)
        await evals.run_code_tier(tasks, llm, run=local_runner, available=lambda: True, guidance="Always validate types first.")
        self.assertTrue(all("Always validate types first." in s for s in llm.systems))
        plain = ReferenceLLM(tasks)
        await evals.run_code_tier(tasks, plain, run=local_runner, available=lambda: True)
        self.assertFalse(any("Extra guidance" in s for s in plain.systems))

    async def test_sandbox_down_and_no_model_are_reported_not_faked(self):
        tasks = evals.load_tasks("code")[:2]
        down = await evals.run_code_tier(tasks, ReferenceLLM(tasks), run=local_runner, available=lambda: False)
        self.assertTrue(all((not r["passed"]) and "sandbox is down" in r["error"] for r in down))
        nomodel = await evals.run_code_tier(tasks, None, run=local_runner, available=lambda: True)
        self.assertTrue(all((not r["passed"]) and "not connected" in r["error"] for r in nomodel))


class ScoreboardTests(_Base):
    def _res(self, passed_ids, all_ids, tier="plan"):
        return [{"id": i, "tier": tier, "passed": i in passed_ids, "seconds": 1.0, "error": "" if i in passed_ids else "nope"} for i in all_ids]

    def test_summarize(self):
        s = evals.summarize(self._res({"a"}, ["a", "b"]) + self._res({"c"}, ["c"], "code"))
        self.assertEqual(s["plan"], {"passed": 1, "total": 2, "failed": ["b"]})
        self.assertEqual(s["code"]["passed"], 1)

    def test_record_history_and_delta_rendering(self):
        first = evals.record(self._res({"a"}, ["a", "b", "c"]), model="m1")
        self.assertEqual(first["tiers"]["plan"]["passed"], 1)
        second = evals.record(self._res({"a", "b"}, ["a", "b", "c"]), model="m1")
        text = evals.render(second, first)
        self.assertIn("2/3", text)
        self.assertIn("was 1/3", text)
        self.assertIn("+1", text)
        self.assertIn("<code>c</code>", text)
        self.assertEqual(len(evals.load_history()), 2)
        self.assertEqual(json.loads((evals.eval_dir() / "latest.json").read_text())["tiers"]["plan"]["passed"], 2)
        self.assertEqual([r["event"] for r in audit.recent(3)].count("evals_run"), 2)

    def test_render_escapes_and_handles_all_passing(self):
        e = evals.record(self._res({"a"}, ["a"]), model="<b>x</b>")
        t = evals.render(e)
        self.assertNotIn("<b>x</b>", t)
        self.assertIn("all passing", t)

    def test_latest_with_no_history(self):
        self.assertIn("No eval runs yet", evals.render_latest())

    def test_guidance_is_fingerprinted_not_stored(self):
        e = evals.record(self._res({"a"}, ["a"]), guidance="secret sauce")
        self.assertEqual(len(e["guidance_sha"]), 12)
        self.assertNotIn("secret sauce", json.dumps(e))

    async def test_run_and_record_compares_with_the_matching_previous_run(self):
        tasks = evals.load_tasks("plan")
        with mock.patch.object(evals, "load_tasks", return_value=tasks[:2]):
            await evals.run_and_record(("plan",), None, True, PRESENT, model="m")
            text = await evals.run_and_record(("plan",), None, True, PRESENT, model="m")
        self.assertIn("was 2/2", text)


class ParseTests(unittest.TestCase):
    def test_parse(self):
        for text, want in (("evals", ""), ("Evals plan", "plan"), ("/eval code", "code"), ("evals last", "last"), ("evals all", "all")):
            kind, arg = commands.parse(text)
            self.assertEqual((kind, arg or ""), ("evals", want), text)
        self.assertIsNone(commands.parse("evaluate my life"))


class CommandTests(_Base):
    def setUp(self):
        super().setUp()
        self.notified = []

        async def notify(t):
            self.notified.append(t)

        async def agent(p):
            return "ok"

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", **PRESENT)

    async def test_last_with_nothing_recorded(self):
        self.assertIn("No eval runs yet", await self.f.handle("evals", "last"))

    async def test_runs_in_the_background_and_sends_the_scoreboard(self):
        real = evals.load_tasks
        few = lambda tier=None, path=None: real(tier)[:2]      # noqa: E731
        with mock.patch.object(commands.sandbox, "available", return_value=True), mock.patch.object(evals, "load_tasks", few):
            reply_text = await self.f.handle("evals", "plan")
            self.assertIn("Running 2 eval task(s)", reply_text)
            again = await self.f.handle("evals", "plan")
            self.assertIn("already in progress", again)
            await self.f.eval_task
        self.assertEqual(len(self.notified), 1)
        self.assertIn("Eval scoreboard", self.notified[0])
        self.assertIn("plan", self.notified[0])
        self.assertIn("Eval scoreboard", await self.f.handle("evals", "last"))

    async def test_a_crashing_eval_run_reports_instead_of_dying(self):
        with mock.patch.object(commands.sandbox, "available", return_value=True), \
                mock.patch.object(evals, "run_and_record", side_effect=RuntimeError("kaput")):
            await self.f.handle("evals", "plan")
            await self.f.eval_task
        self.assertIn("Eval run failed", self.notified[0])


class NightlyTests(CommandTests):
    """AURIX checks itself overnight: once a day, only when idle, silent unless something got worse."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"AURIX_EVALS_AT": "03:15", "AURIX_TZ_OFFSET_MINUTES": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.ts = lambda h, m, d=21: __import__("datetime").datetime(2026, 9, d, h, m, tzinfo=__import__("datetime").timezone.utc).timestamp()

    def _fake_run(self, plan_pass, plan_total=2):
        async def run_and_record(tiers, llm, sandboxed, presence_kw=None, run=None, available=None, guidance="", model=""):
            res = [{"id": f"p{i}", "tier": "plan", "passed": i < plan_pass, "seconds": 0.1, "error": "x"} for i in range(plan_total)]
            return evals.render(evals.record(res, model="m"))
        return run_and_record

    async def _tick(self, h, m, d=21):
        await self.f._maybe_nightly_evals(self.ts(h, m, d))
        task = getattr(self.f, "eval_task", None)
        if task is not None:
            await task

    async def test_not_before_the_hour_and_only_once_a_day(self):
        with mock.patch.object(commands.sandbox, "available", return_value=True), \
                mock.patch.object(evals, "run_and_record", side_effect=self._fake_run(2)) as run:
            await self._tick(3, 0)
            self.assertEqual(run.call_count, 0)                     # too early
            await self._tick(3, 20)
            self.assertEqual(run.call_count, 1)
            await self._tick(4, 0)
            self.assertEqual(run.call_count, 1)                     # already ran today
            await self._tick(3, 20, d=22)
            self.assertEqual(run.call_count, 2)                     # a new day

    async def test_silent_when_nothing_got_worse(self):
        evals.record([{"id": "p0", "tier": "plan", "passed": True, "seconds": 0, "error": ""},
                      {"id": "p1", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        with mock.patch.object(commands.sandbox, "available", return_value=True), \
                mock.patch.object(evals, "run_and_record", side_effect=self._fake_run(2)):
            await self._tick(3, 20)
        self.assertEqual(self.notified, [])

    async def test_a_regression_is_reported(self):
        evals.record([{"id": "p0", "tier": "plan", "passed": True, "seconds": 0, "error": ""},
                      {"id": "p1", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        with mock.patch.object(commands.sandbox, "available", return_value=True), \
                mock.patch.object(evals, "run_and_record", side_effect=self._fake_run(1)):
            await self._tick(3, 20)
        self.assertEqual(len(self.notified), 1)
        self.assertIn("something got worse", self.notified[0])
        self.assertIn("plan: 2 -&gt; 1/2", self.notified[0])

    async def test_disabled_when_the_time_is_empty(self):
        with mock.patch.dict(os.environ, {"AURIX_EVALS_AT": ""}), mock.patch.object(evals, "run_and_record") as run:
            await self._tick(4, 0)
        self.assertEqual(run.call_count, 0)

    async def test_skipped_while_a_mission_is_running(self):
        with mock.patch.object(self.f, "running", return_value=True), mock.patch.object(evals, "run_and_record") as run:
            await self._tick(3, 20)
        self.assertEqual(run.call_count, 0)
        self.assertFalse((evals.eval_dir() / "nightly.json").exists())      # not marked done, so it retries later

    async def test_a_crash_is_reported_not_raised(self):
        with mock.patch.object(commands.sandbox, "available", return_value=True), \
                mock.patch.object(evals, "run_and_record", side_effect=RuntimeError("kaput")):
            await self._tick(3, 20)
        self.assertIn("Overnight eval failed", self.notified[0])


class DigestAndRegressionTests(_Base):
    def test_regressions_only_compare_like_with_like(self):
        a = {"tiers": {"plan": {"passed": 5, "total": 6}, "code": {"passed": 4, "total": 8}}}
        b = {"tiers": {"plan": {"passed": 6, "total": 6}, "code": {"passed": 3, "total": 8}}}
        self.assertEqual(evals.regressions(a, b), ["code: 4 -> 3/8"])
        self.assertEqual(evals.regressions(None, b), [])
        c = {"tiers": {"plan": {"passed": 1, "total": 3}}}                    # different task count: not comparable
        self.assertEqual(evals.regressions(a, c), [])

    def test_digest_line(self):
        self.assertEqual(evals.digest_line(), "")
        evals.record([{"id": "a", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        self.assertIn("plan 1/1", evals.digest_line())
        from src.foundation import teacher
        teacher.new_lesson({"title": "t", "diagnosis": "", "guidance": ["g"], "applies_when": ["x"]}, "src", "m")
        self.assertIn("1 lesson awaiting your approval", evals.digest_line())


if __name__ == "__main__":
    unittest.main()
