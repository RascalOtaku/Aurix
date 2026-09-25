"""The content drafting pipeline: real, finished work for the "niche channel or site" money idea. The model (paid and local)
is faked throughout; no test touches the network. Recording, editing, publishing and disclosure are never AURIX's job here."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, buttons, commands, content, teacher  # noqa: E402

GOOD = json.dumps({"title": "Setting Up Home Assistant on a Raspberry Pi",
                    "outline": ["Why self-host", "Flashing the SD card", "First boot and setup"],
                    "body": "Home Assistant turns a spare Pi into a real smart-home hub.\n\nFirst, flash the image.",
                    "platform_note": "Disclose AI assistance per your platform's policy."})


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
        msg = content.request("a piece on setting up Home Assistant on a Raspberry Pi for beginners", post=ok_post(GOOD))
        self.assertIn("Setting Up Home Assistant", msg)
        self.assertIn("Why self-host", msg)
        self.assertIn("c-", msg)
        piece = content.pending()[0]
        self.assertEqual(piece["status"], "review")
        self.assertEqual(piece["words"], len(json.loads(GOOD)["body"].split()))

    def test_too_short_a_topic_is_refused_before_any_call(self):
        with mock.patch.object(content, "_call_drafter") as call:
            msg = content.request("hi")
        self.assertIn("sentence", msg)
        call.assert_not_called()

    def test_a_credential_looking_topic_is_refused_not_sent(self):
        msg = content.request("use my .env file with the API key to build this piece", post=ok_post(GOOD))
        self.assertIn("will not draft", msg)
        self.assertEqual(content.pieces(), [])

    def test_secrets_in_an_otherwise_fine_topic_are_redacted_before_sending(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen["text"] = body["messages"][0]["content"]
            return 200, {"content": [{"type": "text", "text": GOOD}]}
        content.request("email me at owner@example.com about this " + "x" * 20 + " piece", post=post)
        self.assertNotIn("owner@example.com", seen["text"])

    def test_a_malformed_reply_is_reported_not_stored(self):
        msg = content.request("write a piece about ghost towns", post=ok_post("not json at all"))
        self.assertIn("malformed", msg)
        self.assertEqual(content.pieces(), [])

    def test_a_reply_missing_a_required_field_is_rejected(self):
        bad = json.dumps({"title": "x", "outline": []})                       # no "body"
        msg = content.request("write a piece about ghost towns", post=ok_post(bad))
        self.assertIn("malformed", msg)

    def test_uncertain_claims_are_flagged_and_counted(self):
        flagged = json.dumps({"title": "Old Mine History", "outline": ["Origins"],
                               "body": "The mine opened in [VERIFY: exact year] and closed after [VERIFY: how many years] of operation.",
                               "platform_note": ""})
        msg = content.request("write a piece about a local mine's history", post=ok_post(flagged))
        self.assertIn("2 claim(s) marked", msg)
        self.assertEqual(content.pending()[0]["verify_flags"], 2)

    def test_falls_back_to_the_local_model_when_the_paid_path_is_unavailable(self):
        msg = content.request("write a piece about ghost towns",
                               post=lambda *a: (400, {"error": {"message": "Your credit balance is too low."}}),
                               local=lambda *a: (GOOD, ""))
        self.assertIn("Home Assistant", msg)

    def test_a_failed_call_is_reported_and_audited_not_stored(self):
        msg = content.request("write a piece about ghost towns", post=lambda *a: (500, {}))
        self.assertIn("Could not draft", msg)
        self.assertIn("content_draft_failed", [r["event"] for r in audit.recent(5)])
        self.assertEqual(content.pieces(), [])


class DecisionTests(_Base):
    def _draft(self):
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        return content.pending()[0]["id"]

    def test_approve_keeps_it(self):
        cid = self._draft()
        self.assertIn("Kept", content.approve(cid))
        self.assertEqual(content.get(cid)["status"], "kept")

    def test_decline_discards_it(self):
        cid = self._draft()
        self.assertIn("Discarded", content.decline(cid))
        self.assertEqual(content.get(cid)["status"], "discarded")

    def test_deciding_twice_is_refused(self):
        cid = self._draft()
        content.approve(cid)
        self.assertIn("No draft waiting", content.approve(cid))
        self.assertIn("No draft waiting", content.decline(cid))

    def test_unknown_id_is_refused(self):
        self.assertIn("No draft waiting", content.approve("c-000000"))


class WiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("content: a piece on Home Assistant")[0], "content")
        self.assertEqual(commands.parse("content"), ("content_status", ""))
        self.assertEqual(commands.parse("content drafts"), ("content_status", ""))
        self.assertEqual(commands.parse("yes c-abc123"), ("content_yes", "c-abc123"))
        self.assertEqual(commands.parse("no c-abc123"), ("content_no", "c-abc123"))

    def test_bare_yes_answers_a_single_waiting_draft(self):
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        cid = content.pending()[0]["id"]
        self.assertEqual(commands.parse("yes"), ("content_yes", cid))

    def test_buttons_reach_it(self):
        self.assertIn("content_status", buttons.ALLOWED_KINDS)
        self.assertNotIn("content", buttons.ALLOWED_KINDS)                     # drafting needs a typed topic, not a tap
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        cid = content.pending()[0]["id"]
        text = content.render(content.get(cid))
        kb = buttons.for_reply("content", text)
        cmds = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn(f"do:yes {cid}", cmds)
        self.assertIn(f"do:no {cid}", cmds)

    def test_panel_for_the_dashboard(self):
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        cid = content.pending()[0]["id"]
        row = content.panel()["pieces"][0]
        self.assertEqual(row["id"], cid)
        self.assertEqual(row["status"], "review")

    def test_shows_up_in_the_morning_digests_waiting_for_you(self):
        from src.foundation import heartbeat
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        cid = content.pending()[0]["id"]
        text = heartbeat.waiting_for_you()
        self.assertIn(f"yes {cid}", text)
        self.assertIn("Setting Up Home Assistant", text)

    def test_shows_up_as_a_dashboard_decision_card(self):
        from src.foundation import actions
        content.request("write a piece about ghost towns", post=ok_post(GOOD))
        cid = content.pending()[0]["id"]
        cards = [c for c in actions.decisions() if c["kind"] == "content"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], cid)
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["content_yes", "content_no"])
        self.assertIn("content_yes", actions.ACTION_NAMES)
        self.assertIn("Kept", actions.run_action("content_yes", cid)["message"])


if __name__ == "__main__":
    unittest.main()
