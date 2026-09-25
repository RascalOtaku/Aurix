"""The reading-library pipeline: send AURIX a link or pasted text, it fetches (read-only, SSRF-guarded) or takes the text as-is,
summarizes it (paid-then-local fallback, faked throughout - no test touches the real network), and stores it inert only on a yes.
Video links are tracked by title/URL only, no fetch or summarize call at all."""
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, buttons, commands, learning, teacher  # noqa: E402

GOOD = json.dumps({"summary": "A guide to setting up Home Assistant on a Raspberry Pi.",
                    "takeaway": "Flash the SD card with the official installer image first.", "category": "tech"})


def ok_post(reply):
    def post(url, headers, body, timeout):
        return 200, {"content": [{"type": "text", "text": reply}]}
    return post


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def read(self, n=-1):
        return self.body[:n] if n and n > 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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


class TextRequestTests(_Base):
    def test_a_good_piece_of_pasted_text_is_summarized_and_stored(self):
        msg = learning.request("Home Assistant turns a spare Raspberry Pi into a real smart-home hub, and setup is simple.",
                                post=ok_post(GOOD))
        self.assertIn("n-", msg)
        self.assertIn("Home Assistant", msg)
        self.assertIn("Flash the SD card", msg)
        row = learning.pending()[0]
        self.assertEqual(row["status"], "review")
        self.assertEqual(row["category"], "tech")
        self.assertEqual(row["kind"], "text")

    def test_too_short_input_is_refused_before_any_call(self):
        with mock.patch.object(learning, "_call_summarizer") as call:
            msg = learning.request("hi")
        self.assertIn("Send a link", msg)
        call.assert_not_called()
        self.assertEqual(learning.items(), [])

    def test_a_credential_looking_text_is_refused_not_sent(self):
        msg = learning.request("use my .env file with the API key, here is the content " + "x" * 20, post=ok_post(GOOD))
        self.assertIn("will not absorb", msg)
        self.assertEqual(learning.items(), [])

    def test_secrets_are_redacted_before_being_sent_to_the_model(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen["text"] = body["messages"][0]["content"]
            return 200, {"content": [{"type": "text", "text": GOOD}]}
        learning.request("email me at owner@example.com about this " + "x" * 20 + " article", post=post)
        self.assertNotIn("owner@example.com", seen["text"])

    def test_a_malformed_summary_reply_still_stores_the_raw_text(self):
        msg = learning.request("A long enough piece of pasted text about ghost towns and old mining camps.",
                                post=ok_post("not json at all"))
        self.assertIn("n-", msg)
        row = learning.pending()[0]
        self.assertIn("ghost towns", row["summary"])                        # fell back to the raw text, not rejected

    def test_falls_back_to_the_local_model_when_the_paid_path_is_unavailable(self):
        msg = learning.request("A long enough piece of pasted text about ghost towns and old mining camps.",
                                post=lambda *a: (400, {"error": {"message": "Your credit balance is too low."}}),
                                local=lambda *a: (GOOD, ""))
        self.assertIn("Home Assistant", msg)

    def test_total_summarizer_failure_still_stores_the_item_with_a_note(self):
        msg = learning.request("A long enough piece of pasted text about ghost towns and old mining camps.",
                                post=lambda *a: (500, {}))
        self.assertIn("n-", msg)
        row = learning.pending()[0]
        self.assertIn("Could not summarize", row["summary"])

    def test_a_tag_overrides_the_models_own_category_guess(self):
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.",
                          tag="story", post=ok_post(GOOD))
        self.assertEqual(learning.pending()[0]["category"], "story")


class VideoLinkTests(_Base):
    def test_a_video_link_is_tracked_by_title_only_with_no_fetch_or_summarize_call(self):
        with mock.patch.object(learning, "_fetch") as fetch, mock.patch.object(learning, "_call_summarizer") as summ:
            msg = learning.request("https://www.youtube.com/watch?v=abc123")
        fetch.assert_not_called()
        summ.assert_not_called()
        self.assertIn("n-", msg)
        row = learning.pending()[0]
        self.assertEqual(row["kind"], "video")
        self.assertEqual(row["category"], "video")
        self.assertIn("does not transcribe video", row["summary"])


class ArticleFetchTests(_Base):
    def test_fetching_an_article_extracts_the_title_and_strips_tags(self):
        html = b"<html><head><title>Old Mine History</title></head><body><p>The mine opened in 1890.</p></body></html>"
        with mock.patch.object(learning.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(learning.urllib.request, "urlopen", return_value=_FakeResponse(html)):
            msg = learning.request("https://example.com/old-mine", post=ok_post(GOOD))
        self.assertIn("n-", msg)
        row = learning.pending()[0]
        self.assertEqual(row["kind"], "article")
        self.assertEqual(row["title"], "Old Mine History")

    def test_an_unsafe_host_is_refused_without_calling_the_summarizer(self):
        with mock.patch.object(learning.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]), \
             mock.patch.object(learning, "_call_summarizer") as summ:
            msg = learning.request("https://internal.example.com/secret", post=ok_post(GOOD))
        summ.assert_not_called()
        self.assertIn("does not resolve to a public address", msg)
        self.assertEqual(learning.items(), [])

    def test_a_non_article_content_type_is_refused(self):
        with mock.patch.object(learning.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(learning.urllib.request, "urlopen", return_value=_FakeResponse(b"%PDF-1.4", "application/pdf")):
            msg = learning.request("https://example.com/file.pdf", post=ok_post(GOOD))
        self.assertIn("content type", msg)
        self.assertEqual(learning.items(), [])

    def test_a_response_over_the_size_cap_is_refused(self):
        big = b"x" * (learning.MAX_FETCH_BYTES + 1)
        with mock.patch.object(learning.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(learning.urllib.request, "urlopen", return_value=_FakeResponse(big)):
            msg = learning.request("https://example.com/huge", post=ok_post(GOOD))
        self.assertIn("MB limit", msg)
        self.assertEqual(learning.items(), [])

    def test_an_http_error_is_reported_and_audited(self):
        with mock.patch.object(learning.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(learning.urllib.request, "urlopen",
                                side_effect=urllib.error.HTTPError("https://example.com/x", 404, "Not Found", {}, None)):
            msg = learning.request("https://example.com/missing", post=ok_post(GOOD))
        self.assertIn("HTTP 404", msg)
        self.assertIn("learning_fetch_failed", [r["event"] for r in audit.recent(5)])


class DecisionTests(_Base):
    def _add(self):
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        return learning.pending()[0]["id"]

    def test_approve_keeps_it(self):
        nid = self._add()
        self.assertIn("Kept", learning.approve(nid))
        self.assertEqual(learning.get(nid)["status"], "kept")

    def test_decline_discards_it(self):
        nid = self._add()
        self.assertIn("Discarded", learning.decline(nid))
        self.assertEqual(learning.get(nid)["status"], "discarded")

    def test_deciding_twice_is_refused(self):
        nid = self._add()
        learning.approve(nid)
        self.assertIn("No item waiting", learning.approve(nid))
        self.assertIn("No item waiting", learning.decline(nid))

    def test_unknown_id_is_refused(self):
        self.assertIn("No item waiting", learning.approve("n-000000"))


class WiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("learn: a long enough piece of pasted text")[0], "learn")
        self.assertEqual(commands.parse("learn"), ("learn_status", ""))
        self.assertEqual(commands.parse("library"), ("learn_status", ""))
        self.assertEqual(commands.parse("yes n-abc123"), ("learn_yes", "n-abc123"))
        self.assertEqual(commands.parse("no n-abc123"), ("learn_no", "n-abc123"))

    def test_bare_yes_answers_a_single_waiting_item(self):
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        nid = learning.pending()[0]["id"]
        self.assertEqual(commands.parse("yes"), ("learn_yes", nid))

    def test_buttons_reach_it(self):
        self.assertIn("learn_status", buttons.ALLOWED_KINDS)
        self.assertNotIn("learn", buttons.ALLOWED_KINDS)                     # sending something needs typed text, not a tap
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        nid = learning.pending()[0]["id"]
        text = learning.render(learning.get(nid))
        kb = buttons.for_reply("learn", text)
        cmds = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn(f"do:yes {nid}", cmds)
        self.assertIn(f"do:no {nid}", cmds)

    def test_panel_for_the_dashboard(self):
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        nid = learning.pending()[0]["id"]
        row = learning.panel()["items"][0]
        self.assertEqual(row["id"], nid)
        self.assertEqual(row["status"], "review")

    def test_shows_up_in_the_morning_digests_waiting_for_you(self):
        from src.foundation import heartbeat
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        nid = learning.pending()[0]["id"]
        text = heartbeat.waiting_for_you()
        self.assertIn(f"yes {nid}", text)

    def test_shows_up_as_a_dashboard_decision_card(self):
        from src.foundation import actions
        learning.request("A long enough piece of pasted text about ghost towns and old mining camps.", post=ok_post(GOOD))
        nid = learning.pending()[0]["id"]
        cards = [c for c in actions.decisions() if c["kind"] == "learning"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], nid)
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["learn_yes", "learn_no"])
        self.assertIn("learn_yes", actions.ACTION_NAMES)
        self.assertIn("Kept", actions.run_action("learn_yes", nid)["message"])


if __name__ == "__main__":
    unittest.main()
