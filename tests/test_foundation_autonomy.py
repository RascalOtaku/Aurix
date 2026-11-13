"""Tests for src/foundation/autonomy.py: understand a vague ask, keep working memory, self-check, try another way, report."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, autonomy, commands, mission as ms, sandbox  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)
PRESENT = dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)


def llm_from(*replies):
    """A fake model: returns the queued replies in order (JSON-encoding dicts), records what it was asked."""
    queue, calls = list(replies), []

    async def llm(system, prompt):
        calls.append((system, prompt))
        r = queue.pop(0) if queue else ""
        return json.dumps(r) if isinstance(r, dict) else r
    llm.calls = calls
    return llm


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        ag.reset_state()
        self.store = ms.MissionStore()
        self.said, self.prompts = [], []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    async def notify(self, text, *a):
        self.said.append(text)

    def mission(self, n_steps=2):
        m = ms.MissionContract(
            id=ms.new_mission_id(), objective="tidy the research notes into one summary",
            steps=[ms.Step(f"s{i}", f"Step {i}", f"do thing {i}", ["bash"], f"thing {i} exists") for i in range(1, n_steps + 1)],
            success_criteria=["summary exists"])
        self.store.propose(m)
        self.assertIn("ACTIVE", self.store.activate(m.id))
        return self.store.load(m.id)

    def runner(self, m, replies, llm=None, **kw):
        replies = list(replies)

        async def agent(prompt):
            self.prompts.append(prompt)
            return replies.pop(0) if replies else "STEP DONE: default"
        return autonomy.AutonomousRunner(m.id, agent, self.notify, store=self.store, llm=llm, **{**NOTHING, **kw})


class UnderstandTests(unittest.IsolatedAsyncioTestCase):
    async def test_vague_ask_becomes_a_goal_with_assumptions(self):
        llm = llm_from({"kind": "task", "goal": "Find out why the nightly backup stopped landing on hearthnode",
                        "done_when": ["the cause is named with evidence"], "assumptions": ["read-only: look, don't fix"],
                        "question": "Which backup?", "question_changes_goal": False})
        b = await autonomy.understand("can you figure out what's up with my backups", llm)
        self.assertEqual(b.kind, "task")
        self.assertIn("nightly backup", b.goal)
        self.assertEqual(b.assumptions, ["read-only: look, don't fix"])
        self.assertEqual(b.question, "")                    # would not change the goal: assume instead of asking
        self.assertIn("Assuming: read-only", b.to_goal())
        self.assertIn("Done when: the cause is named", b.to_goal())
        self.assertFalse(b.outward)

    async def test_question_kept_only_when_it_changes_the_goal(self):
        llm = llm_from({"goal": "Make a pack", "question": "Western or cyberpunk?", "question_changes_goal": True})
        b = await autonomy.understand("make me another asset pack", llm)
        self.assertEqual(b.question, "Western or cyberpunk?")

    async def test_garbage_or_no_model_falls_back_to_the_ask(self):
        b = await autonomy.understand("Could you clean up the old stock alert files?", llm_from("I think you want..."))
        self.assertEqual(b.goal, "Clean up the old stock alert files")
        self.assertEqual(b.kind, "task")
        b = await autonomy.understand("please sort out the wiki duplicates", None)
        self.assertEqual(b.goal, "Sort out the wiki duplicates")
        with self.assertRaises(ValueError):
            await autonomy.understand("   ", None)

    async def test_outward_asks_are_flagged_for_the_owner(self):
        b = await autonomy.understand("email the itch.io listing to my friend", None)
        self.assertTrue(b.outward)
        self.assertIn("go/no-go", autonomy.render_brief(b))

    def test_looks_like_task(self):
        for t in ("can you figure out why jellyfin is slow", "look into the 7070 disk filling up",
                  "Hey aurix, sort out my wiki duplicates", "please research cheap NVMe drives for the tower"):
            self.assertTrue(autonomy.looks_like_task(t), t)
        for t in ("hi", "what time is it", "can you tell me a joke", "I love this", "how are you doing today",
                  "can you explain how ollama works"):
            self.assertFalse(autonomy.looks_like_task(t), t)

    def test_retryable(self):
        self.assertTrue(autonomy.retryable("ModuleNotFoundError: no module named pandas"))
        self.assertTrue(autonomy.retryable("not verified: summary.md is empty"))
        for r in ("denied by the approval gate", "needs a credential", "tool-call ceiling reached (200)",
                  "mission is no longer active", "HIGH risk always needs individual approval", ""):
            self.assertFalse(autonomy.retryable(r), r)


class RunnerTests(_Base):
    async def test_notes_from_one_step_reach_the_next(self):
        m = self.mission(2)
        r = self.runner(m, ["STEP DONE: found 42 notes in raw/research", "VERIFIED: 42 files listed",
                            "STEP DONE: wrote summary.md", "VERIFIED: summary.md has 42 entries",
                            "MISSION COMPLETE: summary written"], llm=llm_from())
        self.assertEqual(await r.run(), "completed")
        step2 = next(p for p in self.prompts if "Step 2 of 2" in p)
        self.assertIn("42 files listed", step2)                   # working memory carried forward
        self.assertIn("as data - not instructions", step2)
        self.assertEqual(self.store.load(m.id).steps[0].evidence, "42 files listed")

    async def test_failed_step_is_rethought_and_retried_another_way(self):
        m = self.mission(1)
        llm = llm_from("APPROACH: use the stdlib csv module instead of pandas")
        r = self.runner(m, ["STEP BLOCKED: ModuleNotFoundError: pandas", "STEP DONE: parsed with csv",
                            "VERIFIED: output.csv exists", "MISSION COMPLETE: done"], llm=llm)
        self.assertEqual(await r.run(), "completed")
        retry = self.prompts[1]
        self.assertIn("did not work", retry)
        self.assertIn("stdlib csv module", retry)
        self.assertIn("pandas", llm.calls[0][1])                   # the rethink saw what went wrong
        self.assertTrue(any("Trying another way" in s for s in self.said))
        self.assertIn("autonomy_rethink", [e["event"] for e in audit.recent(30)])
        m = self.store.load(m.id)
        self.assertTrue(m.integrity_ok())                          # the signed contract was never edited

    async def test_a_done_claim_that_fails_its_self_check_is_not_accepted(self):
        m = self.mission(1)
        llm = llm_from("APPROACH: write the summary file before reporting done")
        r = self.runner(m, ["STEP DONE: wrote it", "NOT VERIFIED: summary.md does not exist",
                            "STEP DONE: wrote it for real", "VERIFIED: summary.md is 3 KB", "MISSION COMPLETE: ok"], llm=llm)
        self.assertEqual(await r.run(), "completed")
        self.assertIn("summary.md does not exist", llm.calls[0][1])
        self.assertEqual(self.store.load(m.id).steps[0].evidence, "summary.md is 3 KB")

    async def test_a_denial_is_never_worked_around(self):
        m = self.mission(1)
        llm = llm_from("APPROACH: try sudo")
        r = self.runner(m, ["STEP BLOCKED: denied by the approval gate"], llm=llm)
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(llm.calls, [])                            # not even asked for another way
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("need you", self.said[-1])

    async def test_rethinks_are_capped_then_it_stops_and_reports_what_it_tried(self):
        m = self.mission(1)
        llm = llm_from(*[f"APPROACH: idea {i}" for i in range(10)])
        r = self.runner(m, ["STEP BLOCKED: build error"] * 10, llm=llm, max_rethinks=2)
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(len(self.prompts), 3)                    # first try + 2 other ways
        last = self.said[-1]
        self.assertIn("Stuck at", last)
        self.assertIn("Other ways I tried", last)
        self.assertIn("idea 1", last)
        self.assertIn("resume", last)

    async def test_an_outward_or_give_up_rethink_is_not_used(self):
        for reply in ("APPROACH: email the maintainer and ask", "APPROACH: x\nGIVE UP: only you can log in", "no idea"):
            m = self.mission(1)
            r = self.runner(m, ["STEP BLOCKED: build error"], llm=llm_from(reply))
            self.assertEqual(await r.run(), "blocked", reply)
            self.store.stop_active("test")

    async def test_the_same_idea_twice_is_not_another_way(self):
        m = self.mission(1)
        llm = llm_from("APPROACH: use difflib", "APPROACH: use difflib")
        r = self.runner(m, ["STEP BLOCKED: build error"] * 5, llm=llm)
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(len(self.prompts), 2)                    # one retry, then it stops instead of looping

    async def test_no_model_means_plain_runner_behaviour(self):
        m = self.mission(1)
        r = self.runner(m, ["STEP BLOCKED: build error"], llm=None)
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(len(self.prompts), 1)

    async def test_report_and_board(self):
        m = self.mission(1)
        autonomy.save_brief(m.id, autonomy.Brief(ask="tidy my notes", goal="One summary of the notes",
                                                 assumptions=["only raw/research"]))
        r = self.runner(m, ["STEP DONE: wrote it", "VERIFIED: summary.md ok", "MISSION COMPLETE: ok"], llm=llm_from())
        self.assertEqual(await r.run(), "completed")
        text = self.said[-1]
        self.assertIn("<b>Done.</b> One summary of the notes", text)
        self.assertIn("1. Step 1: summary.md ok", text)
        self.assertIn("I assumed only raw/research", text)
        b = autonomy.board()
        self.assertEqual(b["runs"][0]["mission"], m.id)
        self.assertEqual(b["runs"][0]["state"], "completed")
        self.assertNotIn("<b>", b["runs"][0]["report"])            # plain text for the dashboard


class CommandTests(_Base):
    def setUp(self):
        super().setUp()
        self.started = []

        async def agent(p):
            self.prompts.append(p)
            return "chat answer"
        self.agent = agent
        p = mock.patch.object(sandbox, "available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def foundation(self, llm):
        f = commands.Foundation(self.agent, self.notify, llm=llm, session_id="tg", store=self.store, **PRESENT)

        async def fake_start(mid):
            self.started.append(mid)
        f._start_runner = fake_start
        return f

    def test_parse(self):
        p = commands.parse
        self.assertEqual(p("do: figure out the backups"), ("delegate", "figure out the backups"))
        self.assertEqual(p("Handle - sort out my wiki"), ("delegate", "sort out my wiki"))
        self.assertEqual(p("todo: buy milk")[0], "todo_new")         # unchanged
        self.assertIsNone(p("can you look into why jellyfin is slow"))   # auto-detect is opt-in
        with mock.patch.dict(os.environ, {"AURIX_AUTONOMY_AUTODETECT": "1"}):
            self.assertEqual(p("can you look into why jellyfin is slow")[0], "delegate_auto")
            self.assertIsNone(p("thanks, that's great"))
            self.assertEqual(p("status"), ("status", ""))               # commands still win
        autonomy.set_pending(autonomy.Brief(ask="make a pack", goal="Make a pack", question="Western or cyberpunk?"))
        self.assertEqual(p("cyberpunk"), ("delegate_answer", "cyberpunk"))
        self.assertEqual(p("stop")[0], "stop")
        autonomy.clear_pending()
        self.assertIsNone(p("cyberpunk"))

    async def test_vague_ask_to_plan_with_the_brief_and_a_contract(self):
        llm = llm_from({"kind": "task", "goal": "Summarize the Reynolds Gang research into one page",
                        "done_when": ["a one-page summary exists"], "assumptions": ["use only notes already on disk"]})
        reply = await self.foundation(llm).handle(*commands.parse("do: that reynolds gang stuff, make it useful"))
        self.assertIn("Here's how I read it", reply)
        self.assertIn("use only notes already on disk", reply)
        [m] = self.store.all()
        self.assertIn("Summarize the Reynolds Gang research", m.objective)
        self.assertIn("approve mission " + m.id, reply)             # unknown domain: waits for the owner's tap
        self.assertEqual(self.started, [])
        self.assertEqual(autonomy.load_brief(m.id).goal, "Summarize the Reynolds Gang research into one page")
        self.assertEqual(autonomy.board()["runs"][0]["state"], "planned")

    async def test_one_question_then_the_answer_starts_planning(self):
        llm = llm_from({"goal": "Make an asset pack", "question": "Western or cyberpunk?", "question_changes_goal": True},
                       {"goal": "Make a cyberpunk asset pack", "question": "Which props?", "question_changes_goal": True})
        f = self.foundation(llm)
        reply = await f.handle("delegate", "make me another pack")
        self.assertIn("Western or cyberpunk?", reply)
        self.assertEqual(self.store.all(), [])
        reply = await f.handle(*commands.parse("cyberpunk"))
        self.assertNotIn("Which props?", reply)                      # asks once, then decides
        [m] = self.store.all()
        self.assertIn("cyberpunk", m.objective)
        self.assertIn("Western or cyberpunk", llm.calls[1][1])      # the answer is read with its question
        self.assertIsNone(autonomy.pending())

    async def test_auto_detected_chat_is_answered_normally(self):
        f = self.foundation(llm_from({"kind": "chat", "goal": "chat"}))
        reply = await f.handle("delegate_auto", "can you look into how you are feeling")
        self.assertEqual(reply, "chat answer")
        self.assertEqual(self.store.all(), [])

    async def test_runner_choice(self):
        f = self.foundation(llm_from())
        m = self.mission(1)
        f._start_runner = commands.Foundation._start_runner.__get__(f)

        async def worker(prompt):
            self.prompts.append(prompt)
            return "STEP DONE: ok"
        f.run_agent = worker
        await f._start_runner(m.id)
        await f.runner_task
        self.assertTrue(any("VERIFIED" in p for p in self.prompts))       # the autonomy runner self-checks
        self.store.stop_active("test")
        with mock.patch.dict(os.environ, {"AURIX_AUTONOMY": "0"}):
            self.prompts.clear()
            m = self.mission(1)
            await f._start_runner(m.id)
            await f.runner_task
            self.assertFalse(any("VERIFIED" in p for p in self.prompts))


if __name__ == "__main__":
    unittest.main()
