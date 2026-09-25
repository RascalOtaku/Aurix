"""Foundation 2 memory: one store, provenance, remember/forget/undo, proposals you approve, stale-entry cleanup, and the relevance gate on the brain-tier block."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src import aurix_memory  # noqa: E402
from src.foundation import actions, audit, buttons, commands, memory  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "data").mkdir()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_MEMORY_OWNER": "admin"})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def seed(self, text, age_days=0, uses=0, pinned=False):
        mm = memory._mm()
        e = mm.add_entry(text, source="user", category="fact", owner="admin")
        e["timestamp"] = int(time.time() - age_days * 86400)
        e["uses"], e["pinned"] = uses, pinned
        allm = mm.load_all()
        allm.append(e)
        mm.save(allm)
        return memory.short(e["id"])


class RememberForgetTests(_Base):
    def test_remember_stores_with_provenance_and_audits_without_the_text(self):
        msg = memory.remember("I prefer short answers", source="owner:telegram")
        self.assertIn("Remembered", msg)
        [m] = memory.entries()
        self.assertEqual((m["text"], m["provenance"], m["owner"], m["category"]), ("I prefer short answers", "owner:telegram", "admin", "preference"))
        ev = audit.recent(2)[-1]
        self.assertEqual(ev["event"], "memory_added")
        self.assertNotIn("short answers", json.dumps(ev))

    def test_duplicates_and_secrets_and_tiny_input(self):
        memory.remember("My dog is named Biscuit")
        self.assertIn("already remember", memory.remember("my dog is named biscuit"))
        self.assertIn("secret", memory.remember("my api key is sk-abcdefghijklmnopqrstuvwx"))
        self.assertIn("secret", memory.remember("the password is hunter2"))
        self.assertIn("Tell me what", memory.remember("hi"))
        self.assertEqual(len(memory.entries()), 1)

    def test_forget_by_id_prefix_or_words_and_unforget(self):
        a = self.seed("I like dark roast coffee")
        self.seed("My birthday is in June")
        self.assertIn("Forgot", memory.forget("birthday"))
        self.assertEqual([m["text"] for m in memory.entries()], ["I like dark roast coffee"])
        self.assertIn("Brought back", memory.unforget())
        self.assertEqual(len(memory.entries()), 2)
        self.assertIn("Forgot", memory.forget(a))
        self.assertNotIn(a, [memory.short(m["id"]) for m in memory.entries()])
        self.assertIn("do not have a memory", memory.forget("zebras"))
        self.assertEqual(memory.unforget().startswith("↩️"), True)
        self.assertIn("Nothing to bring back", memory.unforget())

    def test_ambiguous_forget_asks_and_removes_nothing(self):
        self.seed("I like coffee in the morning")
        self.seed("I like coffee after dinner")
        out = memory.forget("coffee")
        self.assertIn("More than one", out)
        self.assertEqual(len(memory.entries()), 2)

    def test_only_the_owners_entries_are_touched(self):
        mm = memory._mm()
        other = mm.add_entry("someone else's memory", owner="guest")
        mm.save([other])
        self.assertEqual(memory.entries(), [])
        self.assertIn("do not have", memory.forget("someone else's memory"))
        self.assertEqual(len(mm.load_all()), 1)

    def test_pin_and_search_and_listing(self):
        a = self.seed("My favourite editor is vim")
        self.seed("The garage code reminder lives in the notebook")
        self.assertIn("Pinned", memory.pin(a))
        self.assertTrue(memory.entries()[0].get("pinned") or any(m.get("pinned") for m in memory.entries()))
        self.assertEqual([m["text"] for m in memory.search("what editor do I use")], ["My favourite editor is vim"])
        text = memory.list_text()
        self.assertIn("2 remembered, 1 pinned", text)
        self.assertLess(text.index("vim"), text.index("garage"))                          # pinned first
        self.assertIn("do not remember anything", memory.recall_text("quantum"))


class ProposalTests(_Base):
    def test_only_durable_first_person_statements_become_candidates(self):
        good = memory.candidates("I really prefer dark mode everywhere. What time is it?")
        self.assertEqual([c[0] for c in good], ["I really prefer dark mode everywhere."])
        self.assertEqual(memory.candidates("my dog is named Biscuit")[0][1], "fact")
        self.assertEqual(memory.candidates("from now on keep answers under three lines")[0][1], "rule")
        for text in ["what do I prefer?", "short", "```code I always```", "see https://x.io I prefer this", "my password is hunter2 and I prefer it", "x" * 500,
                     "I want to know the weather?", "Can you help me? I like it"]:
            self.assertEqual([c for c in memory.candidates(text) if "?" in c[0]], [], text)
        self.assertEqual(memory.candidates("my password is hunter2 and I prefer it"), [])
        self.assertEqual(memory.candidates("see https://x.io I prefer this"), [])

    def test_observe_files_proposals_not_memories_and_dedupes(self):
        self.assertEqual(memory.observe("I prefer short answers."), 1)
        self.assertEqual(memory.entries(), [])                                            # nothing is remembered without your yes
        self.assertEqual(memory.observe("I prefer short answers."), 0)
        [p] = memory.pending()
        self.assertEqual((p["kind"], p["status"]), ("add", "pending"))
        memory.approve(p["id"])
        self.assertEqual([m["text"] for m in memory.entries()], ["I prefer short answers."])
        self.assertEqual(memory.entries()[0]["provenance"], "owner:message")
        self.assertEqual(memory.observe("I prefer short answers."), 0)                     # already remembered

    def test_declined_proposals_are_not_asked_again_and_limits_hold(self):
        memory.observe("I hate loud notifications.")
        [p] = memory.pending()
        self.assertIn("will not remember", memory.decline(p["id"]))
        self.assertEqual(memory.pending(), [])
        for i in range(12):
            memory.observe(f"I always use tool number {i}{'abcdefghij'[i % 10]}{i * 7} for thing {i * 13} at home.")
        self.assertLessEqual(len(memory.pending()), memory.MAX_PENDING)

    def test_the_daily_pass_proposes_forgetting_stale_and_prompt_like_entries_only(self):
        keep = self.seed("I like tea", age_days=200, uses=3)                               # old but used
        self.seed("I like rye bread", age_days=200, uses=0, pinned=True)                    # pinned is never proposed
        stale = self.seed("I once mentioned a trip", age_days=200, uses=0)
        dump = self.seed("You are AURIX, a living organism. " + "blah " * 120)
        fresh = self.seed("I like fresh things", age_days=2)
        self.assertEqual(memory.audit_store(), 2)
        kinds = {memory.short(p["target"]): p for p in memory.pending()}
        self.assertEqual(set(kinds), {stale, dump})
        self.assertIn("pasted prompt", kinds[dump]["why"])
        self.assertEqual(memory.audit_store(), 0)                                          # once a day
        memory.approve(kinds[stale]["id"])
        self.assertNotIn(stale, [memory.short(m["id"]) for m in memory.entries()])
        memory.decline(kinds[dump]["id"])
        self.assertIn(dump, [memory.short(m["id"]) for m in memory.entries()])            # "no" keeps it
        self.assertEqual(memory.audit_store(time.time() + 3 * 86400), 0)                    # and a kept one is not asked again
        self.assertTrue({keep, fresh} <= {memory.short(m["id"]) for m in memory.entries()})

    def test_tick_announces_each_proposal_once_with_buttons(self):
        memory.observe("I prefer short answers.")
        self.seed("You are AURIX " + "x " * 300)
        msgs = memory.tick()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(memory.tick(), [])
        add = [m for m in msgs if "Remember this?" in m][0]
        fg = [m for m in msgs if "Forget this memory?" in m][0]
        labels = lambda t: [b["text"] for row in buttons.for_reply("", t)["inline_keyboard"] for b in row]
        self.assertEqual(labels(add), ["🧠 Remember", "❌ No"])
        self.assertEqual(labels(fg), ["🗑️ Forget it", "✅ Keep it"])

    def test_bad_or_repeated_answers_are_harmless(self):
        self.assertIn("No memory note", memory.approve("k-000000"))
        memory.observe("I prefer short answers.")
        [p] = memory.pending()
        memory.approve(p["id"])
        self.assertIn("nothing to do", memory.approve(p["id"]))
        self.assertIn("nothing to do", memory.decline(p["id"]))


class WiringTests(_Base):
    def test_commands(self):
        self.assertEqual(commands.parse("remember: I prefer short answers"), ("remember", "I prefer short answers"))
        self.assertEqual(commands.parse("Remember that my dog is Biscuit"), ("remember", "my dog is Biscuit"))
        self.assertIsNone(commands.parse("remember to call mom"))                          # that is a reminder, left to the assistant
        self.assertEqual(commands.parse("forget the garage thing"), ("forget", "the garage thing"))
        self.assertEqual(commands.parse("unforget"), ("unforget", ""))
        self.assertEqual(commands.parse("memory"), ("memory", ""))
        self.assertEqual(commands.parse("what do you remember about my dog?"), ("recall", "my dog"))
        self.assertEqual(commands.parse("yes k-abc123"), ("mem_yes", "k-abc123"))
        self.assertEqual(commands.parse("no k-abc123"), ("mem_no", "k-abc123"))

    def test_the_listener_hook_only_files_proposals(self):
        self.assertEqual(commands.observe_owner_text("I prefer short answers."), 1)
        self.assertEqual(memory.entries(), [])

    def test_dashboard_actions_and_cards(self):
        memory.observe("I prefer short answers.")
        [p] = memory.pending()
        cards = [d for d in actions.decisions() if d["kind"] == "memory"]
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["mem_yes", "mem_no"])
        self.assertIn("Remembered", actions.run_action("mem_yes", p["id"])["message"])
        mid = memory.short(memory.entries()[0]["id"])
        self.assertIn("Pinned", actions.run_action("mem_pin", mid)["message"])
        self.assertIn("Unpinned", actions.run_action("mem_unpin", mid)["message"])
        self.assertIn("Forgot", actions.run_action("mem_forget", mid)["message"])
        self.assertIn("Brought back", actions.run_action("mem_unforget")["message"])
        self.assertTrue(actions.run_action("mem_add", "My garage is detached")["ok"])
        self.assertFalse(actions.run_action("mem_add", "x")["ok"])
        self.assertFalse(actions.run_action("mem_forget", "../etc")["ok"])
        for n in ("mem_yes", "mem_no", "mem_forget", "mem_pin", "mem_unpin", "mem_add", "mem_unforget"):
            self.assertIn(n, actions.ACTION_NAMES)
        self.assertNotIn("<", json.dumps(actions.decisions()))

    def test_panel(self):
        self.seed("I like tea", uses=2, pinned=True)
        memory.observe("I prefer short answers.")
        p = memory.panel()
        self.assertEqual((p["total"], p["pinned"], len(p["pending"])), (1, 1, 1))
        self.assertEqual(p["rows"][0]["uses"], 2)

    def test_protected(self):
        from src.foundation import identity
        self.assertEqual(identity.protected_component_for("src/foundation/memory.py"), "approval_gate")

    def test_buttons_reach_only_memory_decisions_by_id(self):
        for k in ("mem_yes", "mem_no", "memory", "recall", "unforget"):
            self.assertIn(k, buttons.ALLOWED_KINDS)
        self.assertNotIn("forget", buttons.ALLOWED_KINDS)                                 # deleting is typed or confirmed on the page, not a stray tap
        self.assertNotIn("remember", buttons.ALLOWED_KINDS)


class RelevanceGateTests(unittest.TestCase):
    def make_brain(self, root, notes):
        d = Path(root) / "memory" / "longterm"
        d.mkdir(parents=True)
        for i, body in enumerate(notes):
            (d / f"n{i}.md").write_text(f"---\ntag: consolidated\ntier: longterm\n---\n{body}\n", encoding="utf-8")
        return Path(root)

    def test_only_notes_that_share_real_words_with_the_question_are_injected(self):
        with tempfile.TemporaryDirectory() as t:
            brain = self.make_brain(t, ["AAPL rose 18.3 percent and AMD gained on Thursday in the portfolio",
                                        "The cyberdeck build uses a Pi 5 with the Apache 2800 case and a battery pack"])
            self.assertEqual(len(aurix_memory.recent_memory(brain)), 2)                       # old behaviour: everything
            got = aurix_memory.recent_memory(brain, query="how is my cyberdeck battery build going")
            self.assertEqual(len(got), 1)
            self.assertIn("cyberdeck", got[0])
            self.assertEqual(aurix_memory.recent_memory(brain, query="what's the weather like today"), [])
            self.assertEqual(aurix_memory.live_memory_message(brain, "hello there"), "")
            self.assertTrue(aurix_memory.live_memory_message(brain, "cyberdeck battery").startswith("AURIX LIVE MEMORY:"))


if __name__ == "__main__":
    unittest.main()
