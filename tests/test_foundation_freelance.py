"""The freelance drafting pipeline: real, finished work for the 'freelance small automation jobs' money idea. The model (paid and
local) is faked throughout; no test touches the network. Delivering, disclosure, and getting paid are never AURIX's job here."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, buttons, commands, freelance, teacher  # noqa: E402

GOOD = json.dumps({"title": "Rename files by EXIF date", "explanation": "Run: python rename_by_date.py <folder>",
                    "filename": "rename_by_date.py", "code": "import sys\nprint(sys.argv)\n"})


def ok_post(reply):
    def post(url, headers, body, timeout):
        return 200, {"content": [{"type": "text", "text": reply}]}
    return post


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "ANTHROPIC_API_KEY": "sk-ant-test"})
        self.env.start()
        audit._heads.clear()
        self.no_local = mock.patch.object(teacher, "_local_fallback", return_value=(None, "no local model in tests"))
        self.no_local.start()

    def tearDown(self):
        self.no_local.stop()
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()


class DraftTests(_Base):
    def test_a_good_draft_is_stored_and_rendered(self):
        msg = freelance.request("write a script that renames files by their EXIF date", post=ok_post(GOOD))
        self.assertIn("Rename files by EXIF date", msg)
        self.assertIn("rename_by_date.py", msg)
        self.assertIn("f-", msg)
        job = freelance.pending()[0]
        self.assertEqual(job["status"], "review")
        self.assertEqual(job["filename"], "rename_by_date.py")

    def test_too_short_a_brief_is_refused_before_any_call(self):
        with mock.patch.object(freelance, "_call_drafter") as call:
            msg = freelance.request("fix it")
        self.assertIn("sentence", msg)
        call.assert_not_called()

    def test_a_credential_looking_brief_is_refused_not_sent(self):
        msg = freelance.request("use my .env file with the API key to build this", post=ok_post(GOOD))
        self.assertIn("will not draft", msg)
        self.assertEqual(freelance.jobs(), [])

    def test_secrets_in_an_otherwise_fine_brief_are_redacted_before_sending(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen["text"] = body["messages"][0]["content"]
            return 200, {"content": [{"type": "text", "text": GOOD}]}
        freelance.request("email me at owner@example.com when this script " + "x" * 20 + " is done", post=post)
        self.assertNotIn("owner@example.com", seen["text"])

    def test_a_malformed_reply_is_reported_not_stored(self):
        msg = freelance.request("write a script that does the thing", post=ok_post("not json at all"))
        self.assertIn("malformed", msg)
        self.assertEqual(freelance.jobs(), [])

    def test_a_reply_missing_a_required_field_is_rejected(self):
        bad = json.dumps({"title": "x", "explanation": "y", "filename": "z.py"})    # no "code"
        msg = freelance.request("write a script that does the thing", post=ok_post(bad))
        self.assertIn("malformed", msg)

    def test_invalid_python_is_kept_but_flagged(self):
        broken = json.dumps({"title": "x", "explanation": "y", "filename": "z.py", "code": "def f(:\n"})
        msg = freelance.request("write a script that does the thing", post=ok_post(broken))
        self.assertIn("does not parse", msg)
        self.assertEqual(freelance.pending()[0]["status"], "review")             # still stored - the owner decides, AURIX just warns

    def test_a_non_python_filename_skips_the_syntax_check(self):
        sh = json.dumps({"title": "x", "explanation": "y", "filename": "run.sh", "code": "echo hi ;;; ("})
        msg = freelance.request("write a shell script", post=ok_post(sh))
        self.assertNotIn("does not parse", msg)

    def test_falls_back_to_the_local_model_when_the_paid_path_is_unavailable(self):
        msg = freelance.request("write a script that does the thing",
                                 post=lambda *a: (400, {"error": {"message": "Your credit balance is too low."}}),
                                 local=lambda *a: (GOOD, ""))
        self.assertIn("Rename files by EXIF date", msg)

    def test_a_failed_call_is_reported_and_audited_not_stored(self):
        msg = freelance.request("write a script that does the thing", post=lambda *a: (500, {}))
        self.assertIn("Could not draft", msg)
        self.assertIn("freelance_draft_failed", [r["event"] for r in audit.recent(5)])
        self.assertEqual(freelance.jobs(), [])


class DecisionTests(_Base):
    def _draft(self):
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        return freelance.pending()[0]["id"]

    def test_approve_keeps_it_and_never_auto_estimates_earnings(self):
        jid = self._draft()
        self.assertIn("Kept", freelance.approve(jid))
        self.assertEqual(freelance.get(jid)["status"], "kept")
        from src.foundation import earnings
        self.assertEqual(earnings.entries(), [])                                 # scope varies too much to guess a number honestly

    def test_decline_discards_it(self):
        jid = self._draft()
        self.assertIn("Discarded", freelance.decline(jid))
        self.assertEqual(freelance.get(jid)["status"], "discarded")

    def test_deciding_twice_is_refused(self):
        jid = self._draft()
        freelance.approve(jid)
        self.assertIn("No draft waiting", freelance.approve(jid))
        self.assertIn("No draft waiting", freelance.decline(jid))

    def test_unknown_id_is_refused(self):
        self.assertIn("No draft waiting", freelance.approve("f-000000"))


class WiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("freelance: write a script that renames files")[0], "freelance")
        self.assertEqual(commands.parse("freelance"), ("freelance_status", ""))
        self.assertEqual(commands.parse("freelance drafts"), ("freelance_status", ""))
        self.assertEqual(commands.parse("yes f-abc123"), ("freelance_yes", "f-abc123"))
        self.assertEqual(commands.parse("no f-abc123"), ("freelance_no", "f-abc123"))

    def test_bare_yes_answers_a_single_waiting_draft(self):
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        jid = freelance.pending()[0]["id"]
        self.assertEqual(commands.parse("yes"), ("freelance_yes", jid))

    def test_buttons_reach_it(self):
        self.assertIn("freelance_status", buttons.ALLOWED_KINDS)
        self.assertNotIn("freelance", buttons.ALLOWED_KINDS)                     # drafting needs a typed brief, not a tap
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        jid = freelance.pending()[0]["id"]
        text = freelance.render(freelance.get(jid))
        kb = buttons.for_reply("freelance", text)
        cmds = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn(f"do:yes {jid}", cmds)
        self.assertIn(f"do:no {jid}", cmds)

    def test_panel_for_the_dashboard(self):
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        jid = freelance.pending()[0]["id"]
        row = freelance.panel()["jobs"][0]
        self.assertEqual(row["id"], jid)
        self.assertEqual(row["status"], "review")

    def test_shows_up_in_the_morning_digests_waiting_for_you(self):
        """A busy owner who missed the original message still sees it: a draft is a decision waiting, same as a repo or an upgrade."""
        from src.foundation import heartbeat
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        jid = freelance.pending()[0]["id"]
        text = heartbeat.waiting_for_you()
        self.assertIn(f"yes {jid}", text)
        self.assertIn("Rename files by EXIF date", text)

    def test_shows_up_as_a_dashboard_decision_card(self):
        from src.foundation import actions
        freelance.request("write a script that does the thing", post=ok_post(GOOD))
        jid = freelance.pending()[0]["id"]
        cards = [c for c in actions.decisions() if c["kind"] == "freelance"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], jid)
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["freelance_yes", "freelance_no"])
        self.assertIn("freelance_yes", actions.ACTION_NAMES)
        self.assertIn("Kept", actions.run_action("freelance_yes", jid)["message"])


FEED = json.dumps([
    {"legal": "https://remoteok.com/terms"},                                     # RemoteOK's real feed always leads with this, not a job
    {"id": "111", "position": "Python automation contractor", "company": "Acme Co",
     "description": "<p>We need a <b>small script</b> that renames files by their EXIF date, one-off job.</p>", "url": "https://remoteok.com/remote-jobs/111"},
    {"id": "222", "position": "Senior Staff Platform Engineer", "company": "BigCorp",
     "description": "Full-time role leading a team of 12 across three continents building our core platform.", "url": "https://remoteok.com/remote-jobs/222"},
])


class FindLeadTests(_Base):
    def _fetch(self, payload=FEED):
        return lambda: payload.encode()

    def test_a_good_lead_is_found_and_drafted(self):
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch())
        self.assertIn("Found a real, public listing", msg)
        self.assertIn("Python automation contractor", msg)
        self.assertIn("Acme Co", msg)
        self.assertIn("remoteok.com/remote-jobs/111", msg)
        self.assertIn("Rename files by EXIF date", msg)                          # the actual draft is attached
        job = freelance.pending()[0]
        self.assertEqual(job["status"], "review")

    def test_an_already_seen_lead_is_not_reoffered(self):
        one = json.dumps([json.loads(FEED)[0], json.loads(FEED)[1]])            # only the one qualifying listing, no second candidate
        freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch(one))
        first_count = len(freelance.jobs())
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch(one))
        self.assertIn("nothing new", msg)
        self.assertEqual(len(freelance.jobs()), first_count)                     # no second draft from the same listing

    def test_a_senior_full_time_role_is_skipped_not_drafted(self):
        # only the "Senior Staff Platform Engineer" listing is present - a real one that slipped through in a live smoke test
        only_senior = json.dumps([json.loads(FEED)[0], json.loads(FEED)[2]])
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch(only_senior))
        self.assertIn("nothing new", msg)
        self.assertEqual(freelance.jobs(), [])

    def test_a_listing_too_thin_to_describe_is_skipped(self):
        thin = json.dumps([{"legal": "x"}, {"id": "9", "position": "x", "company": "y", "description": "too short", "url": ""}])
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch(thin))
        self.assertIn("nothing new", msg)

    def test_a_fetch_failure_is_reported_plainly(self):
        def boom():
            raise OSError("no route to host")
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=boom)
        self.assertIn("Could not check for real leads", msg)

    def test_invalid_json_is_reported_plainly(self):
        msg = freelance.find_lead(post=ok_post(GOOD), fetch=self._fetch(b"not json".decode()))
        self.assertIn("Could not check for real leads", msg)

    def test_never_creates_an_account_bids_or_messages_anyone(self):
        # a static guarantee: nothing in this module ever POSTs to remoteok or any job board - it only ever reads its GET feed
        import inspect
        src = inspect.getsource(freelance)
        self.assertNotIn('"POST"', src)
        self.assertNotIn("method='POST'", src)


class FindLeadWiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("freelance find"), ("freelance_find", ""))
        self.assertEqual(commands.parse("find me a job"), ("freelance_find", ""))
        self.assertEqual(commands.parse("find a gig"), ("freelance_find", ""))

    def test_buttons_allow_it(self):
        self.assertIn("freelance_find", buttons.ALLOWED_KINDS)

    def test_owner_flow_through_the_command_handler(self):
        import asyncio

        async def notify(t):
            pass

        async def agent(t):
            return ""

        async def go():
            f = commands.Foundation(agent, notify)
            return await f.handle("freelance_find", "")
        with mock.patch.object(freelance, "_call_drafter", return_value=(GOOD, "")), \
             mock.patch.object(freelance, "_fetch_leads", return_value=([json.loads(FEED)[1]], "")):
            out = asyncio.run(go())
        self.assertIn("Found a real, public listing", out)


if __name__ == "__main__":
    unittest.main()
